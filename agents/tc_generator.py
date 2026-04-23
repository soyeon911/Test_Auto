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
import os

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
from .schema_enricher import SchemaEnricher


# ─── AI prompts ───────────────────────────────────────────────────────────────

_AI_SYSTEM_API = textwrap.dedent("""
You are a QA engineer for a biometric face-recognition API.

CONTRACT:
- HTTP status is ALWAYS 200
- success/failure is in JSON:
  success: {"success": true, "data": {...}}
  error  : {"success": false, "error_code": <neg>, "msg": "..."}
- ALWAYS assert:
  body = resp.json()
  assert body.get("success")==False or body.get("error_code",0)<0

RULE_BASED_ALREADY:
- positive
- missing_required
- wrong_type
- boundary
- input_validation  (base64 invalid/empty, threshold range/type, numeric_id negative/zero, boolean wrong_type)
→ DO NOT DUPLICATE

TASK:
Generate ONLY additional edge-case tests:
- combinatorial negatives (multi invalid fields)
- domain logic (duplicate enroll, unknown user_id, sequence cases)
- semantic edges (huge/truncated/corrupt base64)
- injection/fuzz (SQL, null byte, >10KB values)
- optional-field combos

RULES:
- Python only (no markdown, no prose)
- max 2 functions
- def test_*(base_url)
- must include: path = "<endpoint>"
- NEVER hardcode host
- call: requests.<method>(f"{base_url}{path}", json=..., timeout=10)
- JSON-serializable only (no bytes)
- no top-level imports
- no duplicate names
- use body-level assertion only
- @pytest.mark.xfail allowed
""").strip()

_AI_SYSTEM_PYTHON = textwrap.dedent("""
You are a QA engineer for Python unit tests.

TASK:
Generate ONLY additional edge-case pytest tests NOT covered by rule-based tests.

FOCUS:
- None / null
- empty values ([], {}, "")
- boundary edges
- invalid combinations
- side effects / mutation / exception messages

RULES:
- Python only (no markdown, no prose)
- def test_*()
- no fixtures
- import module inside function
- no top-level imports
- no duplicate test names
- @pytest.mark.xfail allowed
""").strip()

_AI_USER_TEMPLATE = textwrap.dedent("""
EP:
{endpoint_summary}

RULE_COVERAGE:
{rule_summary}

Generate <= {max_extra} extra edge-case tests.
Focus: combinatorial / domain / injection.
Skip single-field invalids already covered.

Return Python only.
""").strip()

# ─── helpers ─────────────────────────────────────────────────────────────────

def _build_semantic_tag_summary(endpoint: dict[str, Any]) -> str:
    lines: list[str] = []

    def fmt_constraints(schema: dict) -> str:
        cons = schema.get("x_constraints", {}) or {}
        parts = []
        if schema.get("semantic_tag"):
            parts.append(f"tag={schema['semantic_tag']}")
        if cons.get("minimum") is not None:
            parts.append(f"min={cons['minimum']}")
        if cons.get("maximum") is not None:
            parts.append(f"max={cons['maximum']}")
        if cons.get("format_hint"):
            parts.append(f"format={cons['format_hint']}")
        if cons.get("encoding_hint"):
            parts.append(f"encoding={cons['encoding_hint']}")
        return ", ".join(parts) if parts else "none"

    for p in endpoint.get("parameters", []):
        schema = p.get("schema") or {}
        lines.append(f"  {p['name']:20s}: {fmt_constraints(schema)}  (param {p.get('in', '')})")

    rb = endpoint.get("request_body")
    if rb:
        for fname, fschema in ((rb.get("schema") or {}).get("properties", {}) or {}).items():
            lines.append(f"  {fname:20s}: {fmt_constraints(fschema)}  (body)")

    return "\n".join(lines) if lines else "  (none)"


def _build_compact_endpoint_summary(endpoint: dict[str, Any]) -> str:
    """
    Strip the endpoint dict down to only what AI needs for test generation.
    x_constraints is flattened: only type/required/semantic_tag/min/max/enum/format_hint/encoding_hint.
    """
    req_body = endpoint.get("request_body") or {}
    schema   = req_body.get("schema") or {}

    def _compact_field(prop: dict, required: bool) -> dict:
        cons = prop.get("x_constraints") or {}
        entry: dict[str, Any] = {"type": prop.get("type", "string")}
        if required:
            entry["req"] = True
        tag = prop.get("semantic_tag", "")
        if tag:
            entry["tag"] = tag
        if cons.get("minimum") is not None:
            entry["min"] = cons["minimum"]
        if cons.get("maximum") is not None:
            entry["max"] = cons["maximum"]
        if prop.get("enum"):
            entry["enum"] = prop["enum"]
        if cons.get("format_hint"):
            entry["fmt"] = cons["format_hint"]
        if cons.get("encoding_hint"):
            entry["enc"] = cons["encoding_hint"]
        return entry

    params = {}
    for p in endpoint.get("parameters", []):
        ps = p.get("schema") or {}
        entry = _compact_field(ps, bool(p.get("required")))
        entry["in"] = p.get("in", "")
        params[p.get("name", "")] = entry

    req_list = schema.get("required") or []
    props = {
        name: _compact_field(prop, name in req_list)
        for name, prop in (schema.get("properties") or {}).items()
    }

    compact: dict[str, Any] = {
        "op":     endpoint.get("operation_id", ""),
        "method": endpoint.get("method", "").upper(),
        "path":   endpoint.get("path", ""),
    }
    if params:
        compact["params"] = params
    if props:
        compact["body"] = props

    return json.dumps(compact, separators=(",", ":"), ensure_ascii=False)


def _build_rule_test_summary(rule_code: str) -> str:
    """Return a coverage summary instead of the full function name list."""
    if not rule_code.strip():
        return "(none)"
    names = re.findall(r"^def\s+(test_[a-zA-Z0-9_]+)\s*\(", rule_code, re.MULTILINE)
    if not names:
        return "(could not extract)"

    cats: dict[str, list[str]] = {}
    for n in names:
        if "_input_val_" in n:
            # extract field+probe: input_val_{field}_{probe_label}
            detail = n.split("_input_val_", 1)[-1]
            cats.setdefault("input_validation", []).append(detail)
        elif "_semantic_" in n:
            # legacy name — treat as input_validation
            detail = n.split("_semantic_", 1)[-1]
            cats.setdefault("input_validation", []).append(detail)
        elif n.endswith("_positive"):
            cats.setdefault("positive", []).append("")
        elif "_missing_required_" in n or "_missing_body_" in n or re.search(r"_missing_[a-z]", n):
            cats.setdefault("missing_req", []).append("")
        elif "_wrong_type_" in n:
            cats.setdefault("wrong_type", []).append("")
        elif "_boundary_" in n:
            cats.setdefault("boundary", []).append("")
        elif "_invalid_enum_" in n:
            cats.setdefault("invalid_enum", []).append("")
        elif "_raw_image_relation" in n:
            cats.setdefault("raw_image_relation", []).append("")
        else:
            cats.setdefault("other", []).append(n.rsplit("_", 1)[-1])

    lines = []
    for cat, items in cats.items():
        if cat in ("input_validation", "other"):
            probes = ", ".join(items[:6]) + ("…" if len(items) > 6 else "")
            lines.append(f"{cat}({len(items)}): {probes}")
        else:
            lines.append(f"{cat}({len(items)})")
    return "; ".join(lines)


def _safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text).strip("_")


def _strip_fences(text: str) -> str:
    text = re.sub(r'^(import pytest\s*\n)(?:import pytest\s*\n)+', r'\1', text, flags=re.MULTILINE)
    text = re.sub(r'^(import requests\s*\n)(?:import requests\s*\n)+', r'\1', text, flags=re.MULTILINE)

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
        self.max_ai_endpoints = self._resolve_ai_endpoint_limit(config)

        # Separate output directories for rule-based vs AI-generated tests
        output_dirs = tc_cfg.get("output_dirs", {})
        self.rule_dir = Path(output_dirs.get("rule", "./tests/generated/rule"))
        self.ai_dir   = Path(output_dirs.get("ai",   "./tests/generated/ai"))
        self.rule_dir.mkdir(parents=True, exist_ok=True)
        self.ai_dir.mkdir(parents=True, exist_ok=True)
        self._schema_enricher = SchemaEnricher(config)

        rb_cfg = tc_cfg.get("rule_based", {})
        ai_cfg = tc_cfg.get("ai_augment", {})
        self.rule_enabled: bool = rb_cfg.get("enabled", True)
        self.ai_enabled:   bool = ai_cfg.get("enabled", True)
        self.max_extra:    int  = int(ai_cfg.get("max_extra_tc", 3))
        #self.max_ai_endpoints: int = int(ai_cfg.get("max_endpoints_to_augment", 1))  # ⚠️ Quota 제한
        self._ai_endpoints_count: int = 0  # AI 호출 카운터

        self._rule_gen = RuleBasedTCGenerator(config)
        self._llm: BaseLLMClient | None = None   # lazy-init

        # [TODO-1] Load stored spec fingerprints from existing generated files
        self._known_fingerprints: set[str] = self._load_fingerprints()

    def _load_fingerprints(self) -> set[str]:
        """Extract spec_hash from existing generated TC files."""
        fingerprints: set[str] = set()
        for d in [self.rule_dir, self.ai_dir]:
            if not d.exists():
                continue
            for f in d.glob("test_*.py"):
                try:
                    content = f.read_text(encoding="utf-8")
                    # Extract: # spec_hash : <hash>
                    m = re.search(r"# spec_hash\s*:\s*([a-f0-9]+)", content)
                    if m:
                        fingerprints.add(m.group(1))
                except Exception:
                    pass  # 파일 읽기 실패 무시
        return fingerprints

    def _resolve_ai_endpoint_limit(self, config: dict) -> int | None:
        """
        Resolve how many endpoints may use AI augmentation.

        Returns:
        - int  : hard cap
        - None : unlimited
        Priority:
        1. tc_generation.ai_augment.max_endpoints_to_augment
        2. agent.ai_endpoint_limit
        3. provider default
            - ollama  -> unlimited
            - others  -> AI_ENDPOINT_LIMIT env var or 1
        """
        tc_cfg = config.get("tc_generation", {})
        ai_cfg = tc_cfg.get("ai_augment", {})
        agent_cfg = config.get("agent", {})

        # 1) legacy / existing config support
        if "max_endpoints_to_augment" in ai_cfg:
            value = ai_cfg.get("max_endpoints_to_augment")
            if value is None:
                return None
            return int(value)

        # 2) provider-level explicit override
        if "ai_endpoint_limit" in agent_cfg:
            value = agent_cfg.get("ai_endpoint_limit")
            if value is None:
                return None
            return int(value)

        # 3) provider default
        provider = str(agent_cfg.get("provider", "gemini")).lower()
        if provider == "ollama":
            return None  # unlimited by default

        return int(os.getenv("AI_ENDPOINT_LIMIT", "1"))
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
        endpoint = self._schema_enricher.tag_endpoint(endpoint)
        
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
        # AI augmentation
        ai_code = ""
        can_use_ai = (
            self.ai_enabled and (
                self.max_ai_endpoints is None
                or self._ai_endpoints_count < self.max_ai_endpoints
            )
        )

        if can_use_ai:
            ai_code = self._ai_generate(endpoint, rule_code)
            self._ai_endpoints_count += 1

            if self.max_ai_endpoints is None:
                print(f"[TCAgent] AI augmentation: {self._ai_endpoints_count} endpoints (unlimited)")
            else:
                print(f"[TCAgent] AI augmentation: {self._ai_endpoints_count}/{self.max_ai_endpoints} endpoints")

        elif self.ai_enabled:
            print(f"[TCAgent] AI endpoint limit reached ({self.max_ai_endpoints}). Skipping AI for {op_id}")

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

        target_type   = endpoint.get("target_type", "api")
        system_prompt = _AI_SYSTEM_PYTHON if target_type == "python" else _AI_SYSTEM_API

        # ── compact prompts (Ollama/local-LLM friendly) ───────────────────────
        endpoint_summary = _build_compact_endpoint_summary(endpoint)
        rule_summary     = _build_rule_test_summary(rule_code)

        base_user_prompt = _AI_USER_TEMPLATE.format(
            endpoint_summary = endpoint_summary,
            rule_summary     = rule_summary,
            max_extra        = self.max_extra,
        )
        print(f"[TCAgent] prompt tokens ~= {len(base_user_prompt) // 4} "
              f"(system {len(system_prompt)//4} + user {len(base_user_prompt)//4})")

        user_prompt = base_user_prompt   # start clean; rebuilt on each retry
        wait_time   = 2                  # exponential back-off seed (2s → 4s → 8s)

        for attempt in range(1, 4):
            try:
                raw  = self._llm.generate(system_prompt, user_prompt)
                code = _strip_fences(raw)
                #code = self._postprocess_ai_code(code, endpoint)

                # Step 1: syntax check
                if not _is_valid_python(code):
                    print(f"[TCAgent] attempt {attempt}: syntax error — retrying…")
                    print("[TCAgent] --- AI raw code start ---")
                    print(code[:2000])
                    print("[TCAgent] --- AI raw code end ---")
                    user_prompt = (
                        base_user_prompt
                        + "\n\nPrevious output had syntax errors. "
                        "Return EXACTLY 1 short pytest function. "
                        "No prose. No imports. No comments. "
                        "Ensure valid Python only."
                    )
                    continue

                # Step 2: pytest --collect-only validation
                ok, err = self._validate_collect(code)
                if ok:
                    return code
                print(f"[TCAgent] attempt {attempt}: collect failed — retrying…\n  {err[:200]}")
                user_prompt = (
                    base_user_prompt
                    + f"\n\nPrevious output failed pytest --collect-only:\n{err}\n"
                      "Return fewer functions and fix all issues."
                )

            except Exception as e:
                print(f"[TCAgent] attempt {attempt} error: {e}")
                user_prompt = base_user_prompt   # reset for next attempt

            if attempt < 3:
                print(f"[TCAgent] waiting {wait_time}s before retry…")
                time.sleep(wait_time)
                wait_time *= 2

        return ""
    
    def _postprocess_ai_code(self, code: str, endpoint: dict[str, Any]) -> str:
        """
        Normalize AI-generated pytest code so it matches this project's execution model.

        Fixes:
        - enforce `base_url` fixture param
        - remove duplicate top-level imports
        - remove hardcoded localhost/base_url assignments
        - ensure `path = "<endpoint>"` exists inside each test
        - normalize requests calls to f"{base_url}{path}"
        - remove obvious non-JSON bytes literals
        """
        path = str(endpoint.get("path", ""))
        method = str(endpoint.get("method", "post")).lower()

        code = code.strip()

        # 1) remove duplicate imports and top-level imports we already inject in saved file
        lines = code.splitlines()
        filtered: list[str] = []
        seen_pytest = False
        seen_requests = False

        for line in lines:
            stripped = line.strip()

            if stripped == "import pytest":
                if seen_pytest:
                    continue
                seen_pytest = True
                continue  # remove top-level import entirely
            if stripped == "import requests":
                if seen_requests:
                    continue
                seen_requests = True
                continue  # remove top-level import entirely

            filtered.append(line)

        code = "\n".join(filtered).strip()

        # 2) remove hardcoded base_url assignments
        code = re.sub(
            r'^\s*base_url\s*=\s*["\'][^"\']+["\']\s*$',
            '',
            code,
            flags=re.MULTILINE,
        )

        # 3) ensure every test function has (base_url)
        code = re.sub(
            r'^def\s+(test_[a-zA-Z0-9_]+)\s*\(\s*\)\s*:',
            r'def \1(base_url):',
            code,
            flags=re.MULTILINE,
        )

        # 4) if function has wrong arg list, normalize to exactly (base_url)
        code = re.sub(
            r'^def\s+(test_[a-zA-Z0-9_]+)\s*\([^)]*\)\s*:',
            r'def \1(base_url):',
            code,
            flags=re.MULTILINE,
        )

        # 5) add path declaration if missing inside each test function
        def _ensure_path_block(match: re.Match) -> str:
            func_block = match.group(0)
            if re.search(r'^\s+path\s*=', func_block, flags=re.MULTILINE):
                return func_block
            first_line_end = func_block.find("\n")
            if first_line_end == -1:
                return func_block + f'\n    path = "{path}"'
            return func_block[:first_line_end + 1] + f'    path = "{path}"\n' + func_block[first_line_end + 1:]

        code = re.sub(
            r'^def\s+test_[a-zA-Z0-9_]+\s*\(\s*base_url\s*\)\s*:\n(?:^[ \t]+.*\n?)*',
            _ensure_path_block,
            code,
            flags=re.MULTILINE,
        )

        # 6) normalize localhost calls -> base_url + path
        code = re.sub(
            rf'requests\.{method}\(\s*f?["\']http://localhost(?::\d+)?[^"\']*["\']',
            f'requests.{method}(f"{{base_url}}{{path}}"',
            code,
        )

        # 7) normalize any direct f"{base_url}/something" -> f"{base_url}{path}"
        code = re.sub(
            rf'requests\.{method}\(\s*f["\']\{{base_url\}}/[^"\']*["\']',
            f'requests.{method}(f"{{base_url}}{{path}}"',
            code,
        )

        # 8) bytes literals are not JSON-serializable; convert obvious bytes literal to plain string
        code = re.sub(r'b"([^"]*)"', r'"\1"', code)
        code = re.sub(r"b'([^']*)'", r'"\1"', code)

        # 9) remove excessive blank lines
        code = re.sub(r'\n{3,}', '\n\n', code).strip()

        return code + "\n"
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

        op_id    = _safe_name(endpoint.get("operation_id", "unknown"))
        out_path = out_dir / f"test_{op_id}.py"

        method = endpoint.get("method", "").upper()
        path   = endpoint.get("path", "")
        op     = endpoint.get("operation_id", "")
        header = (
            f"# Auto-generated by TCGeneratorAgent [{layer}]\n"
            f"# {method} {path}\n"
            f"# operation : {op}\n"
            f"# spec_hash : {fingerprint}\n"
            f"# {'─' * 53}\n"
            "import json\n"
            "import pytest\n"
            "import requests\n"
            "from tests.helpers.diag import build_diag, attach_diag\n\n"
        )

        if not out_path.exists():
            out_path.write_text(header + code, encoding="utf-8")
        else:
            existing = out_path.read_text(encoding="utf-8")
            new_funcs = _ast_extract_new_functions(existing, code)
            if new_funcs.strip():
                out_path.write_text(existing.rstrip() + "\n\n" + new_funcs, encoding="utf-8")
            else:
                return None  # nothing new to add

        print(f"[TCAgent] [OK] [{layer}] {out_path}")
        return out_path
        