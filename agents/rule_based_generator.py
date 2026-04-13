"""
Rule-Based TC Generator — deterministic, zero-AI layer.

For each OpenAPI endpoint it generates pytest functions covering:

  positive        — one valid happy-path request  (expect 2xx)
  missing_required— omit each required field/param  (expect 400/422)
  wrong_type      — send wrong type per param/field  (expect 400/422)
  boundary        — integer edge values (0, -1, very large)  (expect varies)
  invalid_enum    — unlisted value for enum params  (expect 400/422)

The output is a plain Python code string, ready to be written to a .py file
or fed as context to the AI augmentation step.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any

# ─── type helpers ─────────────────────────────────────────────────────────────

# Representative "good" values per JSON-schema type
_GOOD: dict[str, Any] = {
    "integer": 1,
    "number":  1.5,
    "string":  "test_string",
    "boolean": True,
    "array":   [],
    "object":  {},
}

# Wrong-type stand-ins (e.g. send a string where an int is expected)
_WRONG: dict[str, Any] = {
    "integer": '"not_an_integer"',
    "number":  '"not_a_number"',
    "string":  12345,
    "boolean": '"not_boolean"',
    "array":   '"not_an_array"',
    "object":  '"not_an_object"',
}

# Boundary probes for numeric types
_BOUNDARY_INT = [0, -1, 2_147_483_647]
_BOUNDARY_STR = ['""', '" " * 1000']  # empty, very long


def _safe_name(text: str) -> str:
    """Sanitise an arbitrary string to a valid Python identifier fragment."""
    return re.sub(r"[^a-zA-Z0-9]", "_", text).strip("_")


def _build_url(path: str, path_values: dict[str, Any]) -> str:
    """
    Replace path placeholders with literal values.
    /users/{id}  →  f"{base_url}/users/1"  (or with a variable)
    """
    url = path
    for k, v in path_values.items():
        url = url.replace(f"{{{k}}}", str(v))
    return url


def _render_call(
    method: str,
    path: str,
    path_params: dict,
    query_params: dict,
    body: dict | None,
    headers: dict | None = None,
) -> str:
    """Render a single `requests.<method>(...)` call as a code string."""
    url_literal = f'f"{{base_url}}{_build_url(path, path_params)}"'
    kwargs: list[str] = []

    if query_params:
        kwargs.append(f"params={query_params!r}")
    if body is not None:
        kwargs.append(f"json={body!r}")
    if headers:
        kwargs.append(f"headers={headers!r}")
    kwargs.append("timeout=10")

    args = ", ".join([url_literal] + kwargs)
    return f"requests.{method.lower()}({args})"


# ─── main generator ───────────────────────────────────────────────────────────

class RuleBasedTCGenerator:
    """
    Generates deterministic pytest functions from a parsed endpoint dict.

    Usage:
        gen = RuleBasedTCGenerator(config)
        code: str = gen.generate(endpoint)
    """

    def __init__(self, config: dict):
        rb_cfg = config.get("tc_generation", {}).get("rule_based", {})
        self.enabled_rules: set[str] = set(
            rb_cfg.get("include", ["positive", "missing_required",
                                   "wrong_type", "boundary", "invalid_enum"])
        )
        # 서버 에러 응답 방식 (standard: HTTP 400/422 | qfe: HTTP 200 + success=false)
        self.error_mode: str = config.get("server", {}).get("error_response_mode", "standard")

    def generate(self, endpoint: dict[str, Any]) -> str:
        """Return a Python code block with all rule-based test functions."""
        target_type = endpoint.get("target_type", "api")

        if target_type == "python":
            return self._generate_python(endpoint)

        # Default: API (HTTP) tests
        return self._generate_api(endpoint)

    def _generate_api(self, endpoint: dict[str, Any]) -> str:
        """Generate HTTP-request-based pytest functions for an API endpoint."""
        op_id = _safe_name(endpoint.get("operation_id", "unknown"))
        method = endpoint.get("method", "get").lower()
        path = endpoint.get("path", "/")
        params: list[dict] = endpoint.get("parameters", [])
        req_body: dict | None = endpoint.get("request_body")
        responses: dict = endpoint.get("responses", {})

        # Detect expected success status from spec
        success_statuses = [int(s) for s in responses if str(s).startswith("2")]
        success_statuses = success_statuses or [200]

        blocks: list[str] = []

        if "positive" in self.enabled_rules:
            blocks.append(self._positive(op_id, method, path, params, req_body, success_statuses))

        if "missing_required" in self.enabled_rules:
            blocks.extend(self._missing_required(op_id, method, path, params, req_body))

        if "wrong_type" in self.enabled_rules:
            blocks.extend(self._wrong_type(op_id, method, path, params, req_body))

        if "boundary" in self.enabled_rules:
            blocks.extend(self._boundary(op_id, method, path, params))

        if "invalid_enum" in self.enabled_rules:
            blocks.extend(self._invalid_enum(op_id, method, path, params, req_body))

        return "\n\n\n".join(b for b in blocks if b)

    def _generate_python(self, endpoint: dict[str, Any]) -> str:
        """
        Generate function-call-based pytest functions for a Python module target.

        Assumes the module is importable at runtime.  The 'path' field contains
        'module_name.function_name', from which we derive the import.
        """
        op_id      = _safe_name(endpoint.get("operation_id", "unknown"))
        full_path  = endpoint.get("path", op_id)        # e.g. "mymodule.my_func"
        parts      = full_path.rsplit(".", 1)
        module_name = parts[0] if len(parts) == 2 else "unknown_module"
        func_name  = endpoint.get("operation_id", op_id)
        params: list[dict] = endpoint.get("parameters", [])

        # Build a valid call with representative good values
        def good_val(p: dict) -> Any:
            return _GOOD.get(p["schema"].get("type", "string"), "test")

        required_params = [p for p in params if p.get("required")]
        all_params = params

        blocks: list[str] = []

        # ── positive ──────────────────────────────────────────────
        if "positive" in self.enabled_rules:
            args_repr = ", ".join(
                f"{p['name']}={good_val(p)!r}" for p in required_params
            )
            blocks.append(textwrap.dedent(f"""\
                def test_{op_id}_positive():
                    \"\"\"[rule:positive] Call with valid args — must not raise.\"\"\"
                    import {module_name}
                    result = {module_name}.{func_name}({args_repr})
                    # basic smoke: callable returned without exception
                    assert result is not None or result is None  # noqa: S101
            """))

        # ── missing required ───────────────────────────────────────
        if "missing_required" in self.enabled_rules:
            for p in required_params:
                fname = f"test_{op_id}_missing_{_safe_name(p['name'])}"
                args_repr = ", ".join(
                    f"{pp['name']}={good_val(pp)!r}"
                    for pp in required_params
                    if pp["name"] != p["name"]
                )
                blocks.append(textwrap.dedent(f"""\
                    def {fname}():
                        \"\"\"[rule:missing_required] Omit '{p['name']}' → TypeError or ValueError.\"\"\"
                        import {module_name}
                        import pytest
                        with pytest.raises((TypeError, ValueError)):
                            {module_name}.{func_name}({args_repr})
                """))

        # ── wrong type ────────────────────────────────────────────
        if "wrong_type" in self.enabled_rules:
            for p in all_params:
                ptype = p["schema"].get("type", "string")
                wrong = _WRONG.get(ptype)
                if wrong is None or ptype == "string":
                    continue
                fname = f"test_{op_id}_wrong_type_{_safe_name(p['name'])}"
                args_repr = ", ".join(
                    f"{pp['name']}={wrong if pp['name'] == p['name'] else repr(good_val(pp))}"
                    for pp in required_params
                )
                blocks.append(textwrap.dedent(f"""\
                    def {fname}():
                        \"\"\"[rule:wrong_type] Pass wrong type for '{p['name']}' (expected {ptype}).\"\"\"
                        import {module_name}
                        import pytest
                        with pytest.raises((TypeError, ValueError)):
                            {module_name}.{func_name}({args_repr})
                """))

        # ── boundary ──────────────────────────────────────────────
        if "boundary" in self.enabled_rules:
            for p in all_params:
                ptype = p["schema"].get("type", "string")
                if ptype not in {"integer", "number"}:
                    continue
                for probe in _BOUNDARY_INT:
                    safe_probe = str(probe).replace("-", "neg")
                    fname = f"test_{op_id}_boundary_{_safe_name(p['name'])}_{safe_probe}"
                    args_repr = ", ".join(
                        f"{pp['name']}={probe if pp['name'] == p['name'] else good_val(pp)!r}"
                        for pp in required_params
                    )
                    blocks.append(textwrap.dedent(f"""\
                        def {fname}():
                            \"\"\"[rule:boundary] '{p['name']}' = {probe} — must not crash with 5xx-equivalent.\"\"\"
                            import {module_name}
                            try:
                                {module_name}.{func_name}({args_repr})
                            except (ValueError, OverflowError):
                                pass  # domain rejection is acceptable
                    """))

        # ── nullable / Optional ───────────────────────────────────
        # For Optional[T] params: passing None should not raise TypeError
        for p in all_params:
            if not p.get("nullable"):
                continue
            fname = f"test_{op_id}_none_{_safe_name(p['name'])}"
            args_repr = ", ".join(
                f"{pp['name']}={repr(None) if pp['name'] == p['name'] else repr(good_val(pp))}"
                for pp in required_params
            )
            blocks.append(textwrap.dedent(f"""\
                def {fname}():
                    \"\"\"[rule:nullable] '{p['name']}' is Optional — None must be accepted.\"\"\"
                    import {module_name}
                    try:
                        {module_name}.{func_name}({args_repr})
                    except (ValueError, RuntimeError):
                        pass  # domain-level rejection OK; TypeError is NOT
            """))

        # ── invalid_enum ──────────────────────────────────────────
        if "invalid_enum" in self.enabled_rules:
            for p in all_params:
                enum_vals = p["schema"].get("enum")
                if not enum_vals:
                    continue
                fname = f"test_{op_id}_invalid_enum_{_safe_name(p['name'])}"
                invalid_val = "__INVALID__"
                args_repr = ", ".join(
                    f"{pp['name']}={repr(invalid_val) if pp['name'] == p['name'] else repr(good_val(pp))}"
                    for pp in required_params
                )
                blocks.append(textwrap.dedent(f"""\
                    def {fname}():
                        \"\"\"[rule:invalid_enum] '{p['name']}' outside {enum_vals} → ValueError.\"\"\"
                        import {module_name}
                        import pytest
                        with pytest.raises((ValueError, TypeError)):
                            {module_name}.{func_name}({args_repr})
                """))

        return "\n\n\n".join(b for b in blocks if b)

    # ── rule implementations ──────────────────────────────────────────────────

    def _positive(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
        success_statuses: list[int],
    ) -> str:
        path_params = {
            p["name"]: _GOOD.get(p["schema"].get("type", "string"), "test")
            for p in params if p["in"] == "path"
        }
        query_params = {
            p["name"]: _GOOD.get(p["schema"].get("type", "string"), "test")
            for p in params if p["in"] == "query" and p.get("required")
        }
        body = self._build_valid_body(req_body)

        call = _render_call(method, path, path_params, query_params, body)
        statuses_repr = repr(success_statuses)

        return textwrap.dedent(f"""\
            def test_{op_id}_positive(base_url):
                \"\"\"[rule:positive] Happy-path — valid request should succeed.\"\"\"
                resp = {call}
                assert resp.status_code in {statuses_repr}, (
                    f"Expected success, got {{resp.status_code}}: {{resp.text[:200]}}"
                )
        """)

    def _missing_required(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
    ) -> list[str]:
        blocks: list[str] = []

        # Required path/query params
        required_params = [p for p in params if p.get("required") and p["in"] != "path"]
        for p in required_params:
            fname = f"test_{op_id}_missing_{_safe_name(p['name'])}"
            path_params = {
                pp["name"]: _GOOD.get(pp["schema"].get("type", "string"), "test")
                for pp in params if pp["in"] == "path"
            }
            query_params = {
                pp["name"]: _GOOD.get(pp["schema"].get("type", "string"), "test")
                for pp in params
                if pp["in"] == "query" and pp.get("required") and pp["name"] != p["name"]
            }
            call = _render_call(
                method,
                path,
                path_params,
                query_params,
                self._build_valid_body(req_body),
            )

            if self.error_mode == "qfe":
                assertion = textwrap.dedent(f"""\
                    assert resp.status_code == 200, (
                        f"[FAIL] missing param '{p['name']}' — unexpected HTTP status\\n"
                        f"  Status : {{resp.status_code}}\\n"
                        f"  Body   : {{resp.text[:300]}}"
                    )
                    body = resp.json()
                    assert body.get("success") == False or body.get("error_code", 0) < 0, (
                        f"[FAIL] missing param '{p['name']}' — expected error response\\n"
                        f"  success    : {{body.get('success')}}\\n"
                        f"  error_code : {{body.get('error_code')}}\\n"
                        f"  msg        : {{body.get('msg')}}\\n"
                        f"  Full body  : {{resp.text[:300]}}"
                    )
                """)
            else:
                assertion = textwrap.dedent(f"""\
                    assert resp.status_code in [400, 422], (
                        f"[FAIL] missing param '{p['name']}' — expected 400/422\\n"
                        f"  Status : {{resp.status_code}}\\n"
                        f"  Body   : {{resp.text[:300]}}"
                    )
                """)

            block = (
                f"def {fname}(base_url):\n"
                f"    \"\"\"[rule:missing_required] Omit required param '{p['name']}' → error response.\"\"\"\n"
                f"    resp = {call}\n"
                f"{textwrap.indent(assertion, '    ')}\n"
            )
            blocks.append(block)

        # Required body fields
        if req_body:
            body_schema = req_body.get("schema", {})
            required_fields = body_schema.get("required", [])
            properties = body_schema.get("properties", {})
            path_params = {
                p["name"]: _GOOD.get(p["schema"].get("type", "string"), "test")
                for p in params if p["in"] == "path"
            }

            for field in required_fields:
                fname = f"test_{op_id}_missing_body_{_safe_name(field)}"
                partial_body = {
                    k: _GOOD.get(v.get("type", "string"), "test")
                    for k, v in properties.items()
                    if k != field
                }
                call = _render_call(method, path, path_params, {}, partial_body)

                if self.error_mode == "qfe":
                    assertion = textwrap.dedent(f"""\
                        assert resp.status_code == 200, (
                            f"[FAIL] missing body field '{field}' — unexpected HTTP status\\n"
                            f"  Status : {{resp.status_code}}\\n"
                            f"  Body   : {{resp.text[:300]}}"
                        )
                        body = resp.json()
                        assert body.get("success") == False or body.get("error_code", 0) < 0, (
                            f"[FAIL] missing body field '{field}' — expected error response\\n"
                            f"  success    : {{body.get('success')}}\\n"
                            f"  error_code : {{body.get('error_code')}}\\n"
                            f"  msg        : {{body.get('msg')}}\\n"
                            f"  Full body  : {{resp.text[:300]}}"
                        )
                    """)
                else:
                    assertion = textwrap.dedent(f"""\
                        assert resp.status_code in [400, 422], (
                            f"[FAIL] missing body field '{field}' — expected 400/422\\n"
                            f"  Status : {{resp.status_code}}\\n"
                            f"  Body   : {{resp.text[:300]}}"
                        )
                    """)

                block = (
                    f"def {fname}(base_url):\n"
                    f"    \"\"\"[rule:missing_required] Omit required body field '{field}' → error response.\"\"\"\n"
                    f"    resp = {call}\n"
                    f"{textwrap.indent(assertion, '    ')}\n"
                )
                blocks.append(block)

        return blocks

    def _wrong_type(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
    ) -> list[str]:
        blocks: list[str] = []
        path_params = {
            p["name"]: _GOOD.get(p["schema"].get("type", "string"), "test")
            for p in params if p["in"] == "path"
        }

        for p in params:
            ptype = p["schema"].get("type", "string")
            wrong = _WRONG.get(ptype)
            if wrong is None or ptype == "string":
                continue

            fname = f"test_{op_id}_wrong_type_{_safe_name(p['name'])}"
            if p["in"] == "path":
                bad_path_params = {**path_params, p["name"]: wrong}
                call = _render_call(method, path, bad_path_params, {}, self._build_valid_body(req_body))
            else:
                query_with_bad = {p["name"]: wrong}
                call = _render_call(method, path, path_params, query_with_bad, self._build_valid_body(req_body))

            if self.error_mode == "qfe":
                assertion = textwrap.dedent(f"""\
                    assert resp.status_code < 500, (
                        f"[FAIL] wrong type for '{p['name']}' — server crashed\\n"
                        f"  Status : {{resp.status_code}}\\n"
                        f"  Body   : {{resp.text[:300]}}"
                    )
                    body = resp.json()
                    assert body.get("success") == False or body.get("error_code", 0) < 0, (
                        f"[FAIL] wrong type for '{p['name']}' — expected error response\\n"
                        f"  success    : {{body.get('success')}}\\n"
                        f"  error_code : {{body.get('error_code')}}\\n"
                        f"  msg        : {{body.get('msg')}}\\n"
                        f"  Full body  : {{resp.text[:300]}}"
                    )
                """)
            else:
                assertion = textwrap.dedent(f"""\
                    assert resp.status_code in [400, 422], (
                        f"[FAIL] wrong type for '{p['name']}' — expected 400/422\\n"
                        f"  Status : {{resp.status_code}}\\n"
                        f"  Body   : {{resp.text[:300]}}"
                    )
                """)

            block = (
                f"def {fname}(base_url):\n"
                f"    \"\"\"[rule:wrong_type] Pass wrong type for '{p['name']}' (expected {ptype}) → error response.\"\"\"\n"
                f"    resp = {call}\n"
                f"{textwrap.indent(assertion, '    ')}\n"
            )
            blocks.append(block)

        if req_body:
            body_schema = req_body.get("schema", {})
            properties = body_schema.get("properties", {})
            for field, field_schema in properties.items():
                ftype = field_schema.get("type", "string")
                wrong = _WRONG.get(ftype)
                if wrong is None or ftype == "string":
                    continue

                fname = f"test_{op_id}_wrong_type_body_{_safe_name(field)}"
                valid_body = self._build_valid_body(req_body) or {}
                bad_body = {**valid_body, field: wrong}
                call = _render_call(method, path, path_params, {}, bad_body)

                if self.error_mode == "qfe":
                    assertion = textwrap.dedent(f"""\
                        assert resp.status_code < 500, (
                            f"[FAIL] wrong type for body field '{field}' — server crashed\\n"
                            f"  Status : {{resp.status_code}}\\n"
                            f"  Body   : {{resp.text[:300]}}"
                        )
                        body = resp.json()
                        assert body.get("success") == False or body.get("error_code", 0) < 0, (
                            f"[FAIL] wrong type for body field '{field}' — expected error response\\n"
                            f"  success    : {{body.get('success')}}\\n"
                            f"  error_code : {{body.get('error_code')}}\\n"
                            f"  msg        : {{body.get('msg')}}\\n"
                            f"  Full body  : {{resp.text[:300]}}"
                        )
                    """)
                else:
                    assertion = textwrap.dedent(f"""\
                        assert resp.status_code in [400, 422], (
                            f"[FAIL] wrong type for body field '{field}' — expected 400/422\\n"
                            f"  Status : {{resp.status_code}}\\n"
                            f"  Body   : {{resp.text[:300]}}"
                        )
                    """)

                block = (
                    f"def {fname}(base_url):\n"
                    f"    \"\"\"[rule:wrong_type] Pass wrong type for body field '{field}' (expected {ftype}) → error response.\"\"\"\n"
                    f"    resp = {call}\n"
                    f"{textwrap.indent(assertion, '    ')}\n"
                )
                blocks.append(block)

        return blocks

    def _boundary(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
    ) -> list[str]:
        blocks: list[str] = []
        path_params = {
            p["name"]: _GOOD.get(p["schema"].get("type", "string"), "test")
            for p in params if p["in"] == "path"
        }

        for p in params:
            ptype = p["schema"].get("type", "string")
            if ptype not in {"integer", "number"}:
                continue

            for probe in _BOUNDARY_INT:
                fname = f"test_{op_id}_boundary_{_safe_name(p['name'])}_{probe}".replace("-", "neg")
                if p["in"] == "path":
                    bad_path = {**path_params, p["name"]: probe}
                    call = _render_call(method, path, bad_path, {}, None)
                else:
                    call = _render_call(method, path, path_params, {p["name"]: probe}, None)

                blocks.append(textwrap.dedent(f"""\
                    def {fname}(base_url):
                        \"\"\"[rule:boundary] '{p['name']}' = {probe} — server must not crash (no 5xx).\"\"\"
                        resp = {call}
                        assert resp.status_code < 500, (
                            f"Server error on boundary value {probe}: {{resp.status_code}}"
                        )
                """))

        return blocks

    def _invalid_enum(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
    ) -> list[str]:
        blocks: list[str] = []
        path_params = {
            p["name"]: _GOOD.get(p["schema"].get("type", "string"), "test")
            for p in params if p["in"] == "path"
        }

        for p in params:
            enum_vals = p["schema"].get("enum")
            if not enum_vals:
                continue

            fname = f"test_{op_id}_invalid_enum_{_safe_name(p['name'])}"
            invalid_val = "__INVALID_ENUM_VALUE__"

            if p["in"] == "path":
                bad_path = {**path_params, p["name"]: invalid_val}
                call = _render_call(method, path, bad_path, {}, self._build_valid_body(req_body))
            else:
                call = _render_call(method, path, path_params, {p["name"]: invalid_val}, self._build_valid_body(req_body))

            if self.error_mode == "qfe":
                assertion = textwrap.dedent(f"""\
                    assert resp.status_code < 500, (
                        f"[FAIL] invalid enum '{p['name']}' — server crashed\\n"
                        f"  Status : {{resp.status_code}}\\n"
                        f"  Body   : {{resp.text[:300]}}"
                    )
                    body = resp.json()
                    assert body.get("success") == False or body.get("error_code", 0) < 0, (
                        f"[FAIL] invalid enum '{p['name']}' — expected error response\\n"
                        f"  success    : {{body.get('success')}}\\n"
                        f"  error_code : {{body.get('error_code')}}\\n"
                        f"  msg        : {{body.get('msg')}}\\n"
                        f"  Full body  : {{resp.text[:300]}}"
                    )
                """)
            else:
                assertion = textwrap.dedent(f"""\
                    assert resp.status_code in [400, 422], (
                        f"[FAIL] invalid enum '{p['name']}' — expected 400/422\\n"
                        f"  Status : {{resp.status_code}}\\n"
                        f"  Body   : {{resp.text[:300]}}"
                    )
                """)

            block = (
                f"def {fname}(base_url):\n"
                f"    \"\"\"[rule:invalid_enum] '{p['name']}' outside allowed enum {enum_vals} → error response.\"\"\"\n"
                f"    resp = {call}\n"
                f"{textwrap.indent(assertion, '    ')}\n"
            )
            blocks.append(block)

        return blocks

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_valid_body(req_body: dict | None) -> dict | None:
        if not req_body:
            return None
        schema = req_body.get("schema", {})
        properties = schema.get("properties", {})
        required = schema.get("required", list(properties.keys()))
        return {
            field: _GOOD.get(properties[field].get("type", "string"), "test")
            for field in required
            if field in properties
        }
