"""
TC Generator — orchestrates the two-layer generation strategy:

  Layer 1 · Rule-based  (RuleBasedTCGenerator)
    → deterministic, no AI, covers: positive / missing_required /
      wrong_type / boundary / invalid_enum

  Layer 2 · AI augmentation  (LLM via llm_client factory)
    → edge-case only; receives the already-generated rule tests as context
      so it does NOT duplicate them.

Output per endpoint:  tests/generated/test_<operation_id>.py
  ├── header comment
  ├── imports
  ├── [Layer-1 functions]   ← always present when rule_based.enabled = true
  └── [Layer-2 functions]   ← appended block when ai_augment.enabled = true
"""

from __future__ import annotations

import ast
import hashlib
import re
import textwrap
from pathlib import Path
from typing import Any

from .llm_client import BaseLLMClient, create_llm_client
from .rule_based_generator import RuleBasedTCGenerator


# ─── AI prompt ────────────────────────────────────────────────────────────────

_AI_SYSTEM = textwrap.dedent("""
You are a senior QA engineer specialising in API testing.
You will be given:
  1. An OpenAPI endpoint description (JSON).
  2. Rule-based pytest tests that have already been generated for it.

Your task: generate ONLY additional edge-case pytest test functions
that are NOT already covered by the rule-based tests.

Focus on:
  - Non-obvious / domain-specific negative inputs
  - Combinatorial negative cases (multiple bad fields at once)
  - Atypical but plausible inputs (unicode, very long strings, SQL-injection probes, whitespace-only)
  - Business-logic edge cases you can infer from the endpoint name / schema

Output rules:
  - Valid Python only — no markdown fences, no prose outside code.
  - Every function must start with `test_` and accept `base_url` as the first arg.
  - Use `requests.<method>(f"{base_url}<path>", ...)` for HTTP calls.
  - Do NOT repeat any test function name from the already-generated tests.
  - Do NOT add import statements (they are added by the file header).
""").strip()

_AI_USER_TEMPLATE = textwrap.dedent("""
=== Endpoint (JSON) ===
{endpoint_json}

=== Already generated rule-based tests ===
{rule_code}

Generate at most {max_extra} additional edge-case test functions.
""").strip()


# ─── orchestrator ─────────────────────────────────────────────────────────────

class TCGeneratorAgent:
    def __init__(self, config: dict):
        self.config = config
        tc_cfg = config.get("tc_generation", {})
        self.output_dir = Path(tc_cfg.get("output_dir", "./tests/generated"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dedup_check: bool = tc_cfg.get("dedup_check", True)

        rb_cfg = tc_cfg.get("rule_based", {})
        ai_cfg = tc_cfg.get("ai_augment", {})
        self.rule_enabled: bool = rb_cfg.get("enabled", True)
        self.ai_enabled: bool = ai_cfg.get("enabled", True)
        self.max_extra: int = int(ai_cfg.get("max_extra_tc", 3))

        self._rule_gen = RuleBasedTCGenerator(config)
        self._llm: BaseLLMClient | None = None   # lazy-init to avoid API key check on import
        self._seen_hashes: set[str] = self._load_existing_hashes()

    # ─── public ──────────────────────────────────────────────────────────────

    def generate_for_endpoints(self, endpoints: list[dict[str, Any]]) -> list[Path]:
        """Generate (or append to) one TC file per endpoint. Returns written paths."""
        written: list[Path] = []
        for ep in endpoints:
            path = self._generate_one(ep)
            if path:
                written.append(path)
        return written

    # ─── internal ─────────────────────────────────────────────────────────────

    def _generate_one(self, endpoint: dict[str, Any]) -> Path | None:
        import json

        op_id = _safe_name(endpoint.get("operation_id", "unknown"))

        # ── Layer 1: Rule-based ─────────────────────────────────
        rule_code = ""
        if self.rule_enabled:
            rule_code = self._rule_gen.generate(endpoint)

        # ── Layer 2: AI edge cases ──────────────────────────────
        ai_code = ""
        if self.ai_enabled:
            ai_code = self._ai_generate(endpoint, rule_code)

        # Combine
        combined = "\n\n\n".join(block for block in [rule_code, ai_code] if block.strip())
        if not combined.strip():
            print(f"[TCAgent] No TC generated for {op_id}.")
            return None

        # Dedup by hash of combined block
        code_hash = hashlib.sha256(combined.encode()).hexdigest()
        if self.dedup_check and code_hash in self._seen_hashes:
            print(f"[TCAgent] Duplicate — skipping {op_id}.")
            return None

        path = self._save(endpoint, rule_code, ai_code, code_hash)
        self._seen_hashes.add(code_hash)
        return path

    def _ai_generate(self, endpoint: dict[str, Any], rule_code: str) -> str:
        import json

        if self._llm is None:
            try:
                self._llm = create_llm_client(self.config)
            except (EnvironmentError, ImportError) as e:
                print(f"[TCAgent] AI disabled: {e}")
                return ""

        user_prompt = _AI_USER_TEMPLATE.format(
            endpoint_json=json.dumps(endpoint, indent=2, ensure_ascii=False),
            rule_code=rule_code or "(none)",
            max_extra=self.max_extra,
        )

        for attempt in range(1, 4):
            try:
                raw = self._llm.generate(_AI_SYSTEM, user_prompt)
                code = _strip_fences(raw)
                if _is_valid_python(code):
                    return code
                print(f"[TCAgent] AI attempt {attempt}: syntax error, retrying…")
                user_prompt += "\n\nFix the syntax errors in your previous output."
            except Exception as e:
                print(f"[TCAgent] AI attempt {attempt} failed: {e}")

        return ""

    def _save(
        self,
        endpoint: dict,
        rule_code: str,
        ai_code: str,
        code_hash: str,
    ) -> Path:
        op_id = _safe_name(endpoint.get("operation_id", "unknown"))
        file_path = self.output_dir / f"test_{op_id}.py"

        header = textwrap.dedent(f"""\
            # Auto-generated by TCGeneratorAgent
            # {endpoint.get('method', '').upper()} {endpoint.get('path', '')}
            # operation : {endpoint.get('operation_id', '')}
            # hash      : {code_hash[:12]}
            # ───────────────────────────────────────────────────────
            import pytest
            import requests

        """)

        if file_path.exists():
            # Append only new functions to avoid overwriting manual edits
            existing = file_path.read_text(encoding="utf-8")
            new_rule = _only_new_functions(existing, rule_code)
            new_ai   = _only_new_functions(existing, ai_code)

            additions: list[str] = []
            if new_rule.strip():
                additions.append("# --- rule-based (appended) ---\n" + new_rule)
            if new_ai.strip():
                additions.append("# --- AI edge cases (appended) ---\n" + new_ai)

            if not additions:
                print(f"[TCAgent] No new functions for {file_path.name}.")
                return file_path

            with file_path.open("a", encoding="utf-8") as f:
                f.write("\n\n" + "\n\n".join(additions))
        else:
            parts: list[str] = []
            if rule_code.strip():
                parts.append(f"# ── Layer 1: Rule-based ──────────────────────────────\n\n{rule_code}")
            if ai_code.strip():
                parts.append(f"# ── Layer 2: AI edge cases ──────────────────────────\n\n{ai_code}")
            file_path.write_text(header + "\n\n".join(parts), encoding="utf-8")

        print(f"[TCAgent] ✓ {file_path}")
        return file_path

    def _load_existing_hashes(self) -> set[str]:
        hashes: set[str] = set()
        for f in self.output_dir.glob("test_*.py"):
            for line in f.read_text(encoding="utf-8").splitlines():
                m = re.search(r"# hash\s*:\s*([a-f0-9]+)", line)
                if m:
                    hashes.add(m.group(1))
        return hashes


# ─── helpers ──────────────────────────────────────────────────────────────────

def _safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text).strip("_")


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```python\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def _is_valid_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _only_new_functions(existing: str, new_code: str) -> str:
    """Return functions from new_code whose names don't appear in existing."""
    if not new_code:
        return ""
    existing_names = set(re.findall(r"^def (test_\w+)", existing, re.MULTILINE))
    lines = new_code.splitlines(keepends=True)
    result: list[str] = []
    include = False
    for line in lines:
        m = re.match(r"^def (test_\w+)", line)
        if m:
            include = m.group(1) not in existing_names
        if include:
            result.append(line)
    return "".join(result)
