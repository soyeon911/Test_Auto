"""
TC Generator — orchestrates the two-layer generation strategy:

  Layer 1 · Rule-based  (RuleBasedTCGenerator)
    → deterministic, no AI, covers: positive / missing_required /
      wrong_type / boundary / invalid_enum

  Layer 2 · AI augmentation  (LLM via llm_client factory)
    → edge-case only; receives the already-generated rule tests as context
      so it does NOT duplicate them.

Output per endpoint:  tests/generated/test_<operation_id>.py
  ├── header comment  (spec_hash for dedup)
  ├── imports
  ├── [Layer-1 functions]   ← always present when rule_based.enabled = true
  └── [Layer-2 functions]   ← appended block when ai_augment.enabled = true

── Fixes applied (todo.md 1차) ──────────────────────────────────────────────
  [1] dedup hash  → spec fingerprint (operation_id+method+path+params),
                    NOT generated-code hash. Stable across re-runs.
  [2] collect-only → ast.parse() + pytest --collect-only on a temp file
                    before accepting AI output.
  [4] decorator  → AST-based extraction of new functions; decorators (@mark,
                   @parametrize …) are preserved correctly.
"""

from __future__ import annotations

import ast
import hashlib
import json

import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

from .llm_client import BaseLLMClient, create_llm_client
from .rule_based_generator import RuleBasedTCGenerator


# ─── AI prompts ───────────────────────────────────────────────────────────────

_AI_SYSTEM_API = textwrap.dedent("""
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
  - Decorators like @pytest.mark.xfail are allowed and encouraged.
""").strip()

_AI_SYSTEM_PYTHON = textwrap.dedent("""
You are a senior QA engineer specialising in Python unit testing.
You will be given:
  1. A Python function signature and docstring (JSON).
  2. Rule-based pytest tests that have already been generated for it.

Your task: generate ONLY additional edge-case pytest test functions
that are NOT already covered by the rule-based tests.

Focus on:
  - None / null inputs for optional args
  - Empty containers ([], {}, "")
  - Boundary values near documented limits
  - Combinations of invalid arguments
  - Side effects, mutability, exception message content

Output rules:
  - Valid Python only — no markdown fences, no prose outside code.
  - Every function must start with `test_` and take no arguments (no fixtures).
  - Import the module inside each test function body.
  - Do NOT repeat any test function name from the already-generated tests.
  - Do NOT add top-level import statements.
  - Decorators like @pytest.mark.xfail are allowed and encouraged.
""").strip()

_AI_USER_TEMPLATE = textwrap.dedent("""
=== Endpoint / Function (JSON) ===
{endpoint_json}

=== Already generated rule-based tests ===
{rule_code}

Generate at most {max_extra} additional edge-case test functions.
""").strip()


# ─── helpers ─────────────────────────────────────────────────────────────────

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


def _endpoint_fingerprint(endpoint: dict[str, Any]) -> str:
    """
    [TODO-1] Stable hash of the endpoint *spec*, not the generated code.
    Re-running with the same spec → same fingerprint → skip generation.
    """
    key = {
        "operation_id": endpoint.get("operation_id", ""),
        "method":       endpoint.get("method", ""),
        "path":         endpoint.get("path", ""),
        "params":       sorted(
            f"{p.get('name','')}:{p.get('in','')}:{p.get('required', False)}"
            for p in endpoint.get("parameters", [])
        ),
        "body_required": (
            (endpoint.get("request_body") or {}).get("required", False)
        ),
    }
    return hashlib.sha256(
        json.dumps(key, sort_keys=True).encode()
    ).hexdigest()


def _ast_extract_new_functions(existing_src: str, new_src: str) -> str:
    """
    [TODO-4] AST-based extraction that preserves decorators.

    Parses new_src with ast, finds FunctionDef nodes whose names don't
    already appear in existing_src.  Uses node.lineno/end_lineno to slice
    out the exact source lines INCLUDING any leading decorators.
    """
    if not new_src.strip():
        return ""

    # Names already in the file
    try:
        existing_tree = ast.parse(existing_src)
        existing_names: set[str] = {
            node.name
            for node in ast.walk(existing_tree)
            if isinstance(node, ast.FunctionDef)
        }
    except SyntaxError:
        existing_names = set(re.findall(r"^def (test_\w+)", existing_src, re.MULTILINE))

    try:
        new_tree = ast.parse(new_src)
    except SyntaxError:
        return ""

    new_lines = new_src.splitlines(keepends=True)
    blocks: list[str] = []

    for node in ast.walk(new_tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if node.name in existing_names:
            continue

        # Include decorators: start from first decorator line (1-indexed → 0-indexed)
        start = (node.decorator_list[0].lineno - 1) if node.decorator_list else (node.lineno - 1)
        end   = node.end_lineno          # end_lineno is 1-indexed inclusive
        block = "".join(new_lines[start:end])
        blocks.append(block)

    return "\n\n".join(blocks)


# ─── orchestrator ─────────────────────────────────────────────────────────────

class TCGeneratorAgent:
    def __init__(self, config: dict):
        self.config = config
        tc_cfg = config.get("tc_generation", {})
        self.dedup_check: bool = tc_cfg.get("dedup_check", True)

        # Separate output directories for rule-based vs AI-generated tests
        output_dirs = tc_cfg.get("output_dirs", {})
        self.rule_dir = Path(output_dirs.get("rule", "./tests/generated/rule"))
        self.ai_dir   = Path(output_dirs.get("ai",   "./tests/generated/ai"))
        self.rule_dir.mkdir(parents=True, exist_ok=True)
        self.ai_dir.mkdir(parents=True, exist_ok=True)

        rb_cfg = tc_cfg.get("rule_based", {})
        ai_cfg = tc_cfg.get("ai_augment", {})
        self.rule_enabled: bool = rb_cfg.get("enabled", True)
        self.ai_enabled:   bool = ai_cfg.get("enabled", True)
        self.max_extra:    int  = int(ai_cfg.get("max_extra_tc", 3))
        self.max_ai_endpoints: int = int(ai_cfg.get("max_endpoints_to_augment", 1))  # ⚠️ Quota 제한
        self._ai_endpoints_count: int = 0  # AI 호출 카운터

        self._rule_gen = RuleBasedTCGenerator(config)
        self._llm: BaseLLMClient | None = None   # lazy-init

        # [TODO-1] Load stored spec fingerprints from existing generated files
        self._known_fingerprints: set[str] = self._load_fingerprints()

    # ─── public ──────────────────────────────────────────────────────────────

    def generate_for_endpoints(self, endpoints: list[dict[str, Any]]) -> list[Path]:
        """Generate TC files per endpoint. Returns list of all written paths."""
        written: list[Path] = []
        for ep in endpoints:
            paths = self._generate_one(ep)
            written.extend(paths)
        return written

    # ─── internal ─────────────────────────────────────────────────────────────

    def _generate_one(self, endpoint: dict[str, Any]) -> list[Path]:
        op_id       = _safe_name(endpoint.get("operation_id", "unknown"))
        fingerprint = _endpoint_fingerprint(endpoint)

        # [TODO-1] Skip if this exact spec was already generated
        if self.dedup_check and fingerprint in self._known_fingerprints:
            print(f"[TCAgent] Spec unchanged — skip {op_id}")
            return []

        # ── Layer 1: Rule-based ─────────────────────────────────
        rule_code = ""
        if self.rule_enabled:
            rule_code = self._rule_gen.generate(endpoint)

        # ── Layer 2: AI edge cases ──────────────────────────────
        # ⚠️ Quota 제한: max_endpoints_to_augment 초과 시 AI 호출 스킵
        ai_code = ""
        if self.ai_enabled and self._ai_endpoints_count < self.max_ai_endpoints:
            ai_code = self._ai_generate(endpoint, rule_code)
            self._ai_endpoints_count += 1
            print(f"[TCAgent] AI augmentation: {self._ai_endpoints_count}/{self.max_ai_endpoints} endpoints")
        elif self.ai_enabled and self._ai_endpoints_count >= self.max_ai_endpoints:
            print(f"[TCAgent] AI quota limit reached ({self.max_ai_endpoints} endpoints). Skipping AI for {op_id}")

        # Save each layer to its own directory
        written: list[Path] = []
        rule_path = self._save_layer(endpoint, rule_code, fingerprint, self.rule_dir, "rule")
        ai_path   = self._save_layer(endpoint, ai_code,   fingerprint, self.ai_dir,   "ai")
        if rule_path:
            written.append(rule_path)
        if ai_path:
            written.append(ai_path)

        if written:
            self._known_fingerprints.add(fingerprint)
        return written

    # ── AI generation ────────────────────────────────────────────────────────

    def _ai_generate(self, endpoint: dict[str, Any], rule_code: str) -> str:
        if self._llm is None:
            try:
                self._llm = create_llm_client(self.config)
            except (EnvironmentError, ImportError) as e:
                print(f"[TCAgent] AI disabled: {e}")
                return ""

        target_type = endpoint.get("target_type", "api")
        system_prompt = _AI_SYSTEM_PYTHON if target_type == "python" else _AI_SYSTEM_API

        user_prompt = _AI_USER_TEMPLATE.format(
            endpoint_json=json.dumps(endpoint, indent=2, ensure_ascii=False),
            rule_code=rule_code or "(none)",
            max_extra=self.max_extra,
        )

        wait_time = 2  # 지수 백오프 초기값 (2s, 4s, 8s)
        for attempt in range(1, 4):
            try:
                raw  = self._llm.generate(system_prompt, user_prompt)
                code = _strip_fences(raw)

                # [TODO-2] Step 1: syntax check
                if not _is_valid_python(code):
                    print(f"[TCAgent] AI attempt {attempt}: syntax error — retrying…")
                    user_prompt += "\n\nFix all syntax errors in the previous output."
                    continue

                # [TODO-2] Step 2: pytest --collect-only validation
                ok, err = self._validate_collect(code)
                if ok:
                    return code
                print(f"[TCAgent] AI attempt {attempt}: collect failed — retrying…\n  {err[:300]}")
                user_prompt += (
                    f"\n\nThe previous output failed pytest --collect-only:\n{err[:300]}\n"
                    "Fix all issues (import errors, fixture name errors, parametrize structure errors)."
                )

            except Exception as e:
                print(f"[TCAgent] AI attempt {attempt} error: {e}")

            # 429 에러(QuotaExhausted 등)인 경우 대기 후 재시도
            if attempt < 3:  # 마지막 시도가 아닐 때만 대기
                print(f"잠시 대기 후 {wait_time}초 뒤에 다시 시도합니다...")
                time.sleep(wait_time)
                wait_time *= 2  # 지수 백오프: 대기 시간을 2배로 늘림 (2s → 4s → 8s)

        return ""

    def _validate_collect(self, code: str) -> tuple[bool, str]:
        """
        [TODO-2] Write code to a temp file and run pytest --collect-only.
        Returns (success, error_message).
        """
        # Build a minimal file with imports prepended
        full_src = "import pytest\nimport requests\n\n" + code

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                dir=self.rule_dir,   # use rule_dir for temp validation files
                prefix="_tmp_validate_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(full_src)
                tmp_path = Path(f.name)

            result = subprocess.run(
                [
                    sys.executable, "-m", "pytest",
                    "--collect-only", "-q",
                    "--no-header",
                    str(tmp_path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                return True, ""
            return False, (result.stdout + result.stderr).strip()

        except subprocess.TimeoutExpired:
            return False, "collect-only timed out"
        except Exception as e:
            return False, str(e)
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    # ── file save ────────────────────────────────────────────────────────────

    def _save_layer(
        self,
        endpoint:    dict,
        code:        str,
        fingerprint: str,
        out_dir:     Path,
        layer:       str,   # "rule" | "ai"
    ) -> Path | None:
        """
        Write a single layer's code to out_dir/test_{op_id}.py.
        - New file  → write header + code.
        - Existing  → append only genuinely new functions (AST-based, preserves decorators).
        - No code   → skip.
        """
        if not code.strip():
            return None

        op_id     = _safe_name(endpoint.get("operation_id", "unknown"))
        file_path = out_dir / f"test_{op_id}.py"

        header = textwrap.dedent(f"""\
            # Auto-generated by TCGeneratorAgent [{layer}]
            # {endpoint.get('method', '').upper()} {endpoint.get('path', '')}
            # operation : {endpoint.get('operation_id', '')}
            # spec_hash : {fingerprint}
            # ───────────────────────────────────────────────────────
            import pytest
            import requests

        """)

        if file_path.exists():
            existing = file_path.read_text(encoding="utf-8")
            new_code = _ast_extract_new_functions(existing, code)   # [TODO-4]

            if not new_code.strip():
                print(f"[TCAgent] No new functions for {file_path.name} — updating spec_hash.")
                updated = re.sub(
                    r"# spec_hash : [a-f0-9]+",
                    f"# spec_hash : {fingerprint}",
                    existing,
                )
                file_path.write_text(updated, encoding="utf-8")
                return file_path

            with file_path.open("a", encoding="utf-8") as f:
                f.write(f"\n\n# --- {layer} (appended) ---\n\n" + new_code)
        else:
            file_path.write_text(header + code, encoding="utf-8")

        print(f"[TCAgent] [OK] [{layer}] {file_path}")
        return file_path

    # ── dedup persistence ─────────────────────────────────────────────────────

    def _load_fingerprints(self) -> set[str]:
        """[TODO-1] Read stored spec_hash values from both rule and AI output dirs."""
        fps: set[str] = set()
        for search_dir in (self.rule_dir, self.ai_dir):
            for f in search_dir.glob("test_*.py"):
                for line in f.read_text(encoding="utf-8").splitlines():
                    m = re.search(r"# spec_hash\s*:\s*([a-f0-9]{64})", line)
                    if m:
                        fps.add(m.group(1))
        return fps
