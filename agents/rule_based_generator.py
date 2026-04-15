"""
Rule-Based TC Generator — deterministic, zero-AI layer.

For each API endpoint it generates pytest functions covering:

  positive         — one valid happy-path request
  missing_required — omit each required field/param
  wrong_type       — send wrong type per param/field
  boundary         — schema/enriched min/max boundary values
  invalid_enum     — unlisted value for enum params/body
  semantic_probe   — semantic-tag-specific invalid / exploratory probes

The output is a plain Python code string, ready to be written to a .py file
or fed as context to the AI augmentation step.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any

# 0415 수정사항
"""
현재 semantic tag 기본값을 range/type모다 먼저 써서 TC 생성 -> matching-threshold를 깨뜨림
수정 로직
1. explicit/inferred range가 있으면 그 값을 먼저 사용
2. integer면 int, number면 float
3. 그 다음에만 tag fallback
"""


# ─── type helpers ─────────────────────────────────────────────────────────────

_GOOD: dict[str, Any] = {
    "integer": 1,
    "number": 1.5,
    "string": "test_string",
    "boolean": True,
    "array": [],
    "object": {},
}

_GOOD_BY_TAG: dict[str, Any] = {
    "plain_string": "hello",
    "identifier": "item_001",
    "base64_image": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    "base64_template": "AAEC",
    # "threshold_float": 0.7,
    "threshold_numeric": 1,
    "config_json": "{}",
    "path_user_id": 1,
    # "integer_count": 10,
    "integer_count": 1,
    "channel_count": 3,
    "boolean_flag": True,
    "datetime_string": "2024-01-01T00:00:00Z",
    "email_string": "test@example.com",
    "password_string": "Test1234!",
    "file_path": "/tmp/test.txt",
    "url_string": "https://example.com",
    "uuid_string": "00000000-0000-0000-0000-000000000001",
    "numeric_id": 1,
}

_SEMANTIC_PROBES: dict[str, list[dict[str, Any]]] = {
    "base64_image": [
        {"value": "not_base64!@#", "label": "invalid_b64", "policy": "must_fail"},
        {"value": "", "label": "empty_b64", "policy": "must_fail"},
    ],
    "base64_template": [
        {"value": "not_base64!@#", "label": "invalid_b64", "policy": "must_fail"},
        {"value": "", "label": "empty_b64", "policy": "must_fail"},
    ],
    "threshold_numeric": [
        {"value": -0.1, "label": "below_range", "policy": "probe_only"},
        {"value": 1.1, "label": "above_range", "policy": "probe_only"},
        {"value": "not_a_number", "label": "wrong_type", "policy": "must_fail"},
    ],
    "numeric_id": [
        {"value": -1, "label": "negative_id", "policy": "probe_only"},
        {"value": 0, "label": "zero_id", "policy": "probe_only"},
    ],
    "integer_count": [
        {"value": -1, "label": "negative", "policy": "probe_only"},
        {"value": 0, "label": "zero", "policy": "probe_only"},
        {"value": 10001, "label": "overflow", "policy": "probe_only"},
    ],
    "email_string": [
        {"value": "not_an_email", "label": "invalid_fmt", "policy": "must_fail"},
        {"value": "@nodomain", "label": "malformed", "policy": "must_fail"},
    ],
    "uuid_string": [
        {"value": "not-a-uuid", "label": "invalid_fmt", "policy": "must_fail"},
        {"value": "", "label": "empty", "policy": "must_fail"},
    ],
    "datetime_string": [
        {"value": "not_a_date", "label": "invalid_fmt", "policy": "must_fail"},
    ],
    "url_string": [
        {"value": "not_a_url", "label": "invalid_fmt", "policy": "must_fail"},
    ],
    "boolean_flag": [
        {"value": "not_boolean", "label": "wrong_type", "policy": "must_fail"},
    ],
}

# (semantic_tag, probe_label) → (axis, reason_code)
_SEMANTIC_PROBE_DIAG: dict[tuple[str, str], tuple[str, str]] = {
    ("base64_image",    "invalid_b64"):  ("domain", "invalid_base64"),
    ("base64_image",    "empty_b64"):    ("domain", "invalid_base64"),
    ("base64_template", "invalid_b64"):  ("domain", "invalid_base64"),
    ("base64_template", "empty_b64"):    ("domain", "invalid_base64"),
    ("threshold_float", "below_range"):  ("domain", "range_violation"),
    ("threshold_float", "above_range"):  ("domain", "range_violation"),
    ("threshold_float", "wrong_type"):   ("schema", "type_mismatch"),
    ("numeric_id",      "negative_id"):  ("domain", "range_violation"),
    ("numeric_id",      "zero_id"):      ("domain", "range_violation"),
    ("integer_count",   "negative"):     ("domain", "range_violation"),
    ("integer_count",   "zero"):         ("domain", "range_violation"),
    ("integer_count",   "overflow"):     ("domain", "range_violation"),
    ("email_string",    "invalid_fmt"):  ("schema", "query_param_invalid"),
    ("email_string",    "malformed"):    ("schema", "query_param_invalid"),
    ("uuid_string",     "invalid_fmt"):  ("schema", "query_param_invalid"),
    ("uuid_string",     "empty"):        ("schema", "query_param_invalid"),
    ("datetime_string", "invalid_fmt"):  ("schema", "query_param_invalid"),
    ("url_string",      "invalid_fmt"):  ("schema", "query_param_invalid"),
    ("boolean_flag",    "wrong_type"):   ("schema", "type_mismatch"),
}


_WRONG: dict[str, Any] = {
    "integer": "not_an_integer",
    "number": "not_a_number",
    "string": 12345,
    "boolean": "not_boolean",
    "array": "not_an_array",
    "object": "not_an_object",
}

# Python target fallback boundary probes
_BOUNDARY_INT = [0, -1, 2_147_483_647]


def _safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", text).strip("_")


def _build_url(path: str, path_values: dict[str, Any]) -> str:
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


class RuleBasedTCGenerator:
    def __init__(self, config: dict):
        rb_cfg = config.get("tc_generation", {}).get("rule_based", {})
        self.enabled_rules: set[str] = set(
            rb_cfg.get(
                "include",
                ["positive", "missing_required", "wrong_type", "boundary", "invalid_enum", "semantic_probe"],
            )
        )
        self.error_mode: str = config.get("server", {}).get("error_response_mode", "standard")

    # ──────────────────────────────────────────────────────────────
    # public
    # ──────────────────────────────────────────────────────────────

    def generate(self, endpoint: dict[str, Any]) -> str:
        target_type = endpoint.get("target_type", "api")
        if target_type == "python":
            return self._generate_python(endpoint)
        return self._generate_api(endpoint)

    # ──────────────────────────────────────────────────────────────
    # generic helpers
    # ──────────────────────────────────────────────────────────────

    def _safe_json_block(self, target_var: str = "resp", out_var: str = "body") -> str:
        return textwrap.dedent(f"""\
            try:
                {out_var} = {target_var}.json()
            except ValueError:
                pytest.fail(f"Expected JSON response, got: {{{target_var}.text[:300]}}")
        """)

    def _schema_constraints(self, schema: dict) -> dict[str, Any]:
        return schema.get("x_constraints", {}) or {}

    def _probe_policy(self, schema: dict) -> dict[str, Any]:
        return schema.get("x_probe_policy", {}) or {}

    def _range_cases(self, schema: dict) -> list[dict[str, Any]]:
        cons = self._schema_constraints(schema)
        minimum = cons.get("minimum")
        maximum = cons.get("maximum")
        policy = self._probe_policy(schema).get("range_policy", "none")
        
        #swagger의 example를 경계값으로 추출
        example = cons.get("example")
        if example is None:
            example = schema.get("example")

        if minimum is None and maximum is None and not isinstance(example, (int, float)):
            return []

        cases: list[dict[str, Any]] = []

        if minimum is not None:
            cases.append({"value": minimum, "label": "min", "policy": "must_pass"})
            cases.append(
                {
                    "value": minimum - 1,
                    "label": "below_min",
                    "policy": "must_fail" if policy == "explicit" else "probe_only",
                }
            )
            cases.append({"value": minimum + 1, "label": "above_min", "policy": "must_pass"})

        if maximum is not None:
            cases.append({"value": maximum, "label": "max", "policy": "must_pass"})
            cases.append(
                {
                    "value": maximum + 1,
                    "label": "above_max",
                    "policy": "must_fail" if policy == "explicit" else "probe_only",
                }
            )
            cases.append({"value": maximum - 1, "label": "below_max", "policy": "must_pass"})

        if isinstance(example, (int, float)):
            step = 1 if isinstance(example, int) else 0.1

            # example 자체는 정상값 anchor
            cases.append({"value": example, "label": "example", "policy": "must_pass"})

            lower_example = example - step
            upper_example = example + step

            # range가 있으면 안쪽에 있을 때만 must_pass, 아니면 probe_only
            if minimum is None or lower_example >= minimum:
                cases.append({"value": lower_example, "label": "below_example", "policy": "must_pass"})
            else:
                cases.append({"value": lower_example, "label": "below_example", "policy": "probe_only"})

            if maximum is None or upper_example <= maximum:
                cases.append({"value": upper_example, "label": "above_example", "policy": "must_pass"})
            else:
                cases.append({"value": upper_example, "label": "above_example", "policy": "probe_only"})
        
        dedup: dict[tuple[Any, str], dict[str, Any]] = {}
        for c in cases:
            dedup[(c["value"], c["label"])] = c
        return list(dedup.values())

    def _qfe_error_assertion(self, field_name: str, label: str = "error") -> str:
        return textwrap.dedent(f"""\
            assert resp.status_code < 500, (
                f"[FAIL] {label} on '{field_name}' — server crashed\\n"
                f"  Status : {{resp.status_code}}\\n"
                f"  Body   : {{resp.text[:300]}}"
            )
            try:
                body = resp.json()
            except ValueError:
                pytest.fail(f"Expected JSON response, got: {{resp.text[:300]}}")
            assert body.get("success") == False or body.get("error_code", 0) < 0, (
                f"[FAIL] {label} on '{field_name}' — expected QFE error response\\n"
                f"  success    : {{body.get('success')}}\\n"
                f"  error_code : {{body.get('error_code')}}\\n"
                f"  msg        : {{body.get('msg')}}\\n"
                f"  Full body  : {{resp.text[:300]}}"
            )
        """)

    def _qfe_success_assertion(self) -> str:
        return textwrap.dedent("""\
            assert resp.status_code < 500, (
                f"[FAIL] expected success-like response, got crash\\n"
                f"  Status : {resp.status_code}\\n"
                f"  Body   : {resp.text[:300]}"
            )
            try:
                body = resp.json()
            except ValueError:
                pytest.fail(f"Expected JSON response, got: {resp.text[:300]}")
            assert body.get("success") == True and body.get("error_code", 0) >= 0, (
                f"[FAIL] expected QFE success response\\n"
                f"  success    : {body.get('success')}\\n"
                f"  error_code : {body.get('error_code')}\\n"
                f"  msg        : {body.get('msg')}\\n"
                f"  Full body  : {resp.text[:300]}"
            )
        """)

    def _standard_error_assertion(self, field_name: str, label: str = "error") -> str:
        return textwrap.dedent(f"""\
            assert resp.status_code in [400, 422], (
                f"[FAIL] {label} on '{field_name}' — expected 400/422\\n"
                f"  Status : {{resp.status_code}}\\n"
                f"  Body   : {{resp.text[:300]}}"
            )
        """)

    def _standard_success_assertion(self, success_statuses: list[int]) -> str:
        statuses_repr = repr(success_statuses)
        return textwrap.dedent(f"""\
            assert resp.status_code in {statuses_repr}, (
                f"[FAIL] expected success status in {statuses_repr}, got {{resp.status_code}}\\n"
                f"  Body : {{resp.text[:300]}}"
            )
        """)

    def _no_crash_assertion(self, label: str = "probe") -> str:
        return textwrap.dedent(f"""\
            assert resp.status_code < 500, (
                f"[FAIL] {label} caused server crash\\n"
                f"  Status : {{resp.status_code}}\\n"
                f"  Body   : {{resp.text[:300]}}"
            )
        """)

    def _build_policy_assertion(
        self,
        policy: str,
        field_name: str,
        label: str,
        success_statuses: list[int] | None = None,
    ) -> str:
        if policy == "must_fail":
            if self.error_mode == "qfe":
                return self._qfe_error_assertion(field_name, label)
            return self._standard_error_assertion(field_name, label)

        if policy == "must_pass":
            if self.error_mode == "qfe":
                return self._qfe_success_assertion()
            return self._standard_success_assertion(success_statuses or [200])

        return self._no_crash_assertion(label)

    def _api_test_block(
        self,
        fname: str,
        docstring: str,
        call_str: str,
        assertion_str: str,
        axis: str,
        reason_code: str,
        target_field: str,
        test_condition: str,
        expected_http: str,
        expected_app: str,
        error_detail: str,
        request_method: str,
        request_path: str,
        request_query: dict | None,
        request_headers: dict | None,
        request_body: dict | None,
        expected_status_display: str,
        rule_type: str,
    ) -> str:
        """
        Generate a complete test function with:
          - try/except wrapping (catches RequestException for runtime diag)
          - resp.json() parsing → body dict
          - build_diag() + attach_diag() for structured diagnosis
          - the actual assertion inside the try block
        """
        # Indent assertion 8 spaces (inside outer try block)
        indented_assert = textwrap.indent(assertion_str.rstrip("\n"), "        ")
        meta_block = (
            f"    request_query = {request_query!r}\n"
            f"    request_headers = {request_headers!r}\n"
            f"    request_body = {request_body!r}\n"
            f"    request_path = {request_path!r}\n"
            f"    request_method = {request_method.upper()!r}\n"
            f"    request_url = f\"{{base_url}}{request_path}\"\n"
            f"    request.node.user_properties.append((\"tc_meta\", {{\n"
            f"        \"rule_type\": {rule_type!r},\n"
            f"        \"target_param\": {target_field!r},\n"
            f"        \"condition\": {test_condition!r},\n"
            f"        \"request_method\": request_method,\n"
            f"        \"request_path\": request_path,\n"
            f"        \"request_url\": request_url,\n"
            f"        \"request_query\": request_query,\n"
            f"        \"request_headers\": request_headers,\n"
            f"        \"request_body\": request_body,\n"
            f"        \"expected_status_display\": {expected_status_display!r},\n"
            f"    }}))\n"
        )

        return (
            f"def {fname}(base_url, request):\n"
            f"    {docstring!r}\n"
            f"{meta_block}"
            f"    try:\n"
            f"        resp = {call_str}\n"
            f"        body = {{}}\n"
            f"        try:\n"
            f"            body = resp.json()\n"
            f"        except Exception:\n"
            f"            pass\n"
            f"        diag = build_diag(\n"
            f"            axis={axis!r},\n"
            f"            reason_code={reason_code!r},\n"
            f"            target_field={target_field!r},\n"
            f"            test_condition={test_condition!r},\n"
            f"            expected_http={expected_http!r},\n"
            f"            expected_app={expected_app!r},\n"
            f"            resp=resp,\n"
            f"            body=body,\n"
            f"            error_detail={error_detail!r},\n"
            f"        )\n"
            f"        attach_diag(request, diag)\n"
            f"{indented_assert}\n"
            f"    except requests.exceptions.RequestException as _exc:\n"
            f"        diag = build_diag(\n"
            f"            axis='runtime',\n"
            f"            reason_code='connection_refused',\n"
            f"            target_field={target_field!r},\n"
            f"            test_condition={test_condition!r},\n"
            f"            expected_http={expected_http!r},\n"
            f"            expected_app='server unreachable',\n"
            f"            exc=_exc,\n"
            f"            server_crash=True,\n"
            f"            error_detail='runtime.connection_refused',\n"
            f"        )\n"
            f"        attach_diag(request, diag)\n"
            f"        raise\n"
        )

    def _build_valid_body(self, req_body: dict | None) -> dict | None:
        if not req_body:
            return None
        schema = req_body.get("schema") or {}
        properties = schema.get("properties", {})
        if not properties:
            return None
        body = {field: self._good_value(field, fschema) for field, fschema in properties.items()}
        return body or None

    def _good_value(self, name: str, schema: dict) -> Any:
        cons = self._schema_constraints(schema)
        tag = schema.get("semantic_tag", "")
        ftype = schema.get("type", "string")

        if ftype == "integer":
            minimum = cons.get("minimum")
            maximum = cons.get("maximum")
            if minimum is not None and maximum is not None:
                return int((minimum + maximum) / 2)
            if minimum is not None:
                return int(minimum)
            tag_val = _GOOD_BY_TAG.get(tag)
            if tag_val is not None:
                return int(tag_val) if isinstance(tag_val, (int, float)) else 1
            return 1

        if ftype == "number":
            minimum = cons.get("minimum")
            maximum = cons.get("maximum")
            if minimum is not None and maximum is not None:
                return (minimum + maximum) / 2.0
            if minimum is not None:
                return float(minimum)
            tag_val = _GOOD_BY_TAG.get(tag)
            if tag_val is not None:
                return float(tag_val) if isinstance(tag_val, (int, float)) else 1.0
            return 1.5

        if tag:
            val = _GOOD_BY_TAG.get(tag)
            if val is not None:
                return val

        if ftype == "object":
            props = schema.get("properties", {})
            return {k: self._good_value(k, v) for k, v in props.items()} if props else {}

        if ftype == "array":
            items = schema.get("items", {})
            return [self._good_value("item", items)] if items else []

        return _GOOD.get(ftype, "test")

    # ──────────────────────────────────────────────────────────────
    # API generation
    # ──────────────────────────────────────────────────────────────

    def _generate_api(self, endpoint: dict[str, Any]) -> str:
        op_id = _safe_name(endpoint.get("operation_id", "unknown"))
        method = endpoint.get("method", "get").lower()
        path = endpoint.get("path", "/")
        params: list[dict] = endpoint.get("parameters", [])
        req_body: dict | None = endpoint.get("request_body")
        responses: dict = endpoint.get("responses", {})

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
            blocks.extend(self._boundary(op_id, method, path, params, req_body, success_statuses))
        if "invalid_enum" in self.enabled_rules:
            blocks.extend(self._invalid_enum(op_id, method, path, params, req_body))
        if "semantic_probe" in self.enabled_rules:
            blocks.extend(self._semantic_probe(op_id, method, path, params, req_body))

        return "\n\n\n".join(b for b in blocks if b)

    # r
    # Python target generation
    # ──────────────────────────────────────────────────────────────

    def _generate_python(self, endpoint: dict[str, Any]) -> str:
        op_id = _safe_name(endpoint.get("operation_id", "unknown"))
        full_path = endpoint.get("path", op_id)
        parts = full_path.rsplit(".", 1)
        module_name = parts[0] if len(parts) == 2 else "unknown_module"
        func_name = endpoint.get("operation_id", op_id)
        params: list[dict] = endpoint.get("parameters", [])

        def good_val(p: dict) -> Any:
            return _GOOD.get(p["schema"].get("type", "string"), "test")

        required_params = [p for p in params if p.get("required")]
        all_params = params

        blocks: list[str] = []

        if "positive" in self.enabled_rules:
            args_repr = ", ".join(f"{p['name']}={good_val(p)!r}" for p in required_params)
            blocks.append(textwrap.dedent(f"""\
                def test_{op_id}_positive():
                    \"\"\"[rule:positive] Call with valid args — must not raise.\"\"\"
                    import {module_name}
                    result = {module_name}.{func_name}({args_repr})
                    assert result is not None or result is None
            """))

        if "missing_required" in self.enabled_rules:
            for p in required_params:
                fname = f"test_{op_id}_missing_{_safe_name(p['name'])}"
                args_repr = ", ".join(
                    f"{pp['name']}={good_val(pp)!r}" for pp in required_params if pp["name"] != p["name"]
                )
                blocks.append(textwrap.dedent(f"""\
                    def {fname}():
                        \"\"\"[rule:missing_required] Omit '{p['name']}' → TypeError or ValueError.\"\"\"
                        import {module_name}
                        import pytest
                        with pytest.raises((TypeError, ValueError)):
                            {module_name}.{func_name}({args_repr})
                """))

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
                            \"\"\"[rule:boundary] '{p['name']}' = {probe} — must not crash.\"\"\"
                            import {module_name}
                            try:
                                {module_name}.{func_name}({args_repr})
                            except (ValueError, OverflowError):
                                pass
                    """))

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
                        pass
            """))

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

    # ──────────────────────────────────────────────────────────────
    # rule implementations
    # ──────────────────────────────────────────────────────────────

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
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "path"
        }
        query_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "query" and p.get("required")
        }
        body = self._build_valid_body(req_body)

        resolved_path = _build_url(path, path_params)
        call = _render_call(method, path, path_params, query_params, body)
        assertion = (
            self._qfe_success_assertion()
            if self.error_mode == "qfe"
            else self._standard_success_assertion(success_statuses)
        )
        exp_app = "success=true, error_code>=0" if self.error_mode == "qfe" else f"status in {success_statuses}"

        return self._api_test_block(
            fname=f"test_{op_id}_positive",
            docstring="[rule:positive] Happy-path — valid request should succeed.",
            call_str=call,
            assertion_str=assertion,
            axis="state",
            reason_code="precondition_not_met",
            target_field="",
            test_condition="Happy path — all required fields present with valid values",
            expected_http="200",
            expected_app=exp_app,
            error_detail="state.precondition_not_met",
            request_method=method,
            request_path=resolved_path,
            request_query=query_params,
            request_headers=None,
            request_body=body,
            expected_status_display=f"200 / {exp_app}",
            rule_type="positive",
        )

    def _missing_required(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
    ) -> list[str]:
        blocks: list[str] = []

        required_query_params = [p for p in params if p.get("required") and p.get("in") == "query"]
        for target_param in required_query_params:
            fname = f"test_{op_id}_missing_{_safe_name(target_param['name'])}"
            path_params = {
                p["name"]: self._good_value(p["name"], p.get("schema", {}))
                for p in params if p["in"] == "path"
            }
            query_params = {
                p["name"]: self._good_value(p["name"], p.get("schema", {}))
                for p in params
                if p["in"] == "query" and p.get("required") and p["name"] != target_param["name"]
            }

            resolved_path = _build_url(path, path_params)
            body = self._build_valid_body(req_body)
            call = _render_call(method, path, path_params, query_params, body)
            assertion = self._build_policy_assertion("must_fail", target_param["name"], "missing_required")
            blocks.append(self._api_test_block(
                fname=fname,
                docstring=f"[rule:missing_required] Omit required query param '{target_param['name']}'.",
                call_str=call,
                assertion_str=assertion,
                axis="schema",
                reason_code="missing_required",
                target_field=target_param["name"],
                test_condition=f"Required query param '{target_param['name']}' omitted from request",
                expected_http="200",
                expected_app="success=false, error_code<0",
                error_detail=f"schema.missing_required.{target_param['name']}",
                request_method=method,
                request_path=resolved_path,
                request_query=query_params,
                request_headers=None,
                request_body=body,
                expected_status_display="200 / success=false, error_code<0",
                rule_type="missing_required",
            ))

        if req_body:
            body_schema = req_body.get("schema", {})
            required_fields = body_schema.get("required", [])
            properties = body_schema.get("properties", {})
            path_params = {
                p["name"]: self._good_value(p["name"], p.get("schema", {}))
                for p in params if p["in"] == "path"
            }
            query_params = {
                p["name"]: self._good_value(p["name"], p.get("schema", {}))
                for p in params if p["in"] == "query" and p.get("required")
            }

            for field in required_fields:
                fname = f"test_{op_id}_missing_body_{_safe_name(field)}"
                partial_body = {k: self._good_value(k, v) for k, v in properties.items() if k != field}
                resolved_path = _build_url(path, path_params)
                call = _render_call(method, path, path_params, query_params, partial_body)
                assertion = self._build_policy_assertion("must_fail", field, "missing_required")
                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:missing_required] Omit required body field '{field}'.",
                    call_str=call,
                    assertion_str=assertion,
                    axis="schema",
                    reason_code="missing_required",
                    target_field=field,
                    test_condition=f"Required body field '{field}' omitted from request",
                    expected_http="200",
                    expected_app="success=false, error_code<0",
                    error_detail=f"schema.missing_required.{field}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=query_params,
                    request_headers=None,
                    request_body=partial_body,
                    expected_status_display="200 / success=false, error_code<0",
                    rule_type="missing_required",
                ))

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
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "path"
        }
        query_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "query" and p.get("required")
        }

        for p in params:
            ptype = p["schema"].get("type", "string")
            wrong = _WRONG.get(ptype)
            if wrong is None or ptype == "string":
                continue

            fname = f"test_{op_id}_wrong_type_{_safe_name(p['name'])}"
            req_query = query_params
            req_body_payload = self._build_valid_body(req_body)

            if p["in"] == "path":
                bad_path_params = {**path_params, p["name"]: wrong}
                resolved_path = _build_url(path, bad_path_params)
                call = _render_call(method, path, bad_path_params, query_params, req_body_payload)
            elif p["in"] == "query":
                query_with_bad = {**query_params, p["name"]: wrong}
                resolved_path = _build_url(path, path_params)
                req_query = query_with_bad
                call = _render_call(method, path, path_params, query_with_bad, req_body_payload)
            else:
                continue

            assertion = self._build_policy_assertion("must_fail", p["name"], "wrong_type")
            blocks.append(self._api_test_block(
                fname=fname,
                docstring=f"[rule:wrong_type] Pass wrong type for '{p['name']}' (expected {ptype}).",
                call_str=call,
                assertion_str=assertion,
                axis="schema",
                reason_code="type_mismatch",
                target_field=p["name"],
                test_condition=f"'{p['name']}' sent with wrong type (expected {ptype}, sent {_WRONG.get(ptype)!r})",
                expected_http="200",
                expected_app="success=false, error_code<0",
                error_detail=f"schema.type_mismatch.{p['name']}",
                request_method=method,
                request_path=resolved_path,
                request_query=req_query,
                request_headers=None,
                request_body=req_body_payload,
                expected_status_display="200 / success=false, error_code<0",
                rule_type="wrong_type",
            ))

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
                resolved_path = _build_url(path, path_params)
                call = _render_call(method, path, path_params, query_params, bad_body)
                assertion = self._build_policy_assertion("must_fail", field, "wrong_type")
                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:wrong_type] Pass wrong type for body field '{field}' (expected {ftype}).",
                    call_str=call,
                    assertion_str=assertion,
                    axis="schema",
                    reason_code="type_mismatch",
                    target_field=field,
                    test_condition=f"Body field '{field}' sent with wrong type (expected {ftype}, sent {wrong!r})",
                    expected_http="200",
                    expected_app="success=false, error_code<0",
                    error_detail=f"schema.type_mismatch.{field}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=query_params,
                    request_headers=None,
                    request_body=bad_body,
                    expected_status_display="200 / success=false, error_code<0",
                    rule_type="wrong_type",
                ))

        return blocks

    def _boundary(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
        success_statuses: list[int],
    ) -> list[str]:
        blocks: list[str] = []

        base_path_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "path"
        }
        base_query_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "query" and p.get("required")
        }
        base_body = self._build_valid_body(req_body) or {}

        for p in params:
            schema = p.get("schema", {})
            ptype = schema.get("type", "string")
            if ptype not in {"integer", "number"}:
                continue

            for case in self._range_cases(schema):
                probe = case["value"]
                label = case["label"]
                policy = case["policy"]
                fname = f"test_{op_id}_boundary_{_safe_name(p['name'])}_{_safe_name(str(label))}"

                req_query = base_query_params
                req_body_payload = base_body if base_body else None

                if p["in"] == "path":
                    bad_path = {**base_path_params, p["name"]: probe}
                    resolved_path = _build_url(path, bad_path)
                    call = _render_call(method, path, bad_path, base_query_params, req_body_payload)
                elif p["in"] == "query":
                    bad_query = {**base_query_params, p["name"]: probe}
                    resolved_path = _build_url(path, base_path_params)
                    req_query = bad_query
                    call = _render_call(method, path, base_path_params, bad_query, req_body_payload)
                else:
                    continue

                assertion = self._build_policy_assertion(policy, p["name"], f"boundary:{label}", success_statuses)
                exp_app = {
                    "must_pass": "success=true, error_code>=0",
                    "must_fail": "success=false, error_code<0",
                    "probe_only": "no crash (status < 500)",
                }.get(policy, "no crash (status < 500)")

                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:boundary] '{p['name']}' = {probe} ({label}, policy={policy}).",
                    call_str=call,
                    assertion_str=assertion,
                    axis="domain",
                    reason_code="range_violation",
                    target_field=p["name"],
                    test_condition=f"'{p['name']}' = {probe} (boundary: {label})",
                    expected_http="200",
                    expected_app=exp_app,
                    error_detail=f"domain.range_violation.{p['name']}.{label}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=req_query,
                    request_headers=None,
                    request_body=req_body_payload,
                    expected_status_display=f"200 / {exp_app}",
                    rule_type="boundary",
                ))

        schema = (req_body or {}).get("schema") or {}
        properties = schema.get("properties", {})
        for field, field_schema in properties.items():
            ftype = field_schema.get("type", "string")
            if ftype not in {"integer", "number"}:
                continue

            for case in self._range_cases(field_schema):
                probe = case["value"]
                label = case["label"]
                policy = case["policy"]
                fname = f"test_{op_id}_boundary_body_{_safe_name(field)}_{_safe_name(str(label))}"
                bad_body = {**base_body, field: probe}
                resolved_path = _build_url(path, base_path_params)
                call = _render_call(method, path, base_path_params, base_query_params, bad_body)
                assertion = self._build_policy_assertion(policy, field, f"boundary:{label}", success_statuses)
                exp_app = {
                    "must_pass": "success=true, error_code>=0",
                    "must_fail": "success=false, error_code<0",
                    "probe_only": "no crash (status < 500)",
                }.get(policy, "no crash (status < 500)")

                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:boundary] body field '{field}' = {probe} ({label}, policy={policy}).",
                    call_str=call,
                    assertion_str=assertion,
                    axis="domain",
                    reason_code="range_violation",
                    target_field=field,
                    test_condition=f"Body field '{field}' = {probe} (boundary: {label})",
                    expected_http="200",
                    expected_app=exp_app,
                    error_detail=f"domain.range_violation.{field}.{label}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=base_query_params,
                    request_headers=None,
                    request_body=bad_body,
                    expected_status_display=f"200 / {exp_app}",
                    rule_type="boundary",
                ))

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
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "path"
        }
        query_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "query" and p.get("required")
        }

        for p in params:
            enum_vals = p["schema"].get("enum")
            if not enum_vals:
                continue

            fname = f"test_{op_id}_invalid_enum_{_safe_name(p['name'])}"
            invalid_val = "__INVALID_ENUM_VALUE__"
            req_query = query_params
            req_body_payload = self._build_valid_body(req_body)

            if p["in"] == "path":
                bad_path = {**path_params, p["name"]: invalid_val}
                resolved_path = _build_url(path, bad_path)
                call = _render_call(method, path, bad_path, query_params, req_body_payload)
            elif p["in"] == "query":
                bad_query = {**query_params, p["name"]: invalid_val}
                resolved_path = _build_url(path, path_params)
                req_query = bad_query
                call = _render_call(method, path, path_params, bad_query, req_body_payload)
            else:
                continue

            assertion = self._build_policy_assertion("must_fail", p["name"], "invalid_enum")
            blocks.append(self._api_test_block(
                fname=fname,
                docstring=f"[rule:invalid_enum] '{p['name']}' outside allowed enum {enum_vals}.",
                call_str=call,
                assertion_str=assertion,
                axis="domain",
                reason_code="enum_violation",
                target_field=p["name"],
                test_condition=f"'{p['name']}' = '__INVALID_ENUM_VALUE__' (allowed: {enum_vals})",
                expected_http="200",
                expected_app="success=false, error_code<0",
                error_detail=f"domain.enum_violation.{p['name']}",
                request_method=method,
                request_path=resolved_path,
                request_query=req_query,
                request_headers=None,
                request_body=req_body_payload,
                expected_status_display="200 / success=false, error_code<0",
                rule_type="invalid_enum",
            ))

        if req_body:
            body_schema = req_body.get("schema", {})
            properties = body_schema.get("properties", {})
            for field, field_schema in properties.items():
                enum_vals = field_schema.get("enum")
                if not enum_vals:
                    continue

                fname = f"test_{op_id}_invalid_enum_body_{_safe_name(field)}"
                valid_body = self._build_valid_body(req_body) or {}
                bad_body = {**valid_body, field: "__INVALID_ENUM_VALUE__"}
                resolved_path = _build_url(path, path_params)
                call = _render_call(method, path, path_params, query_params, bad_body)
                assertion = self._build_policy_assertion("must_fail", field, "invalid_enum")
                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:invalid_enum] body field '{field}' outside allowed enum {enum_vals}.",
                    call_str=call,
                    assertion_str=assertion,
                    axis="domain",
                    reason_code="enum_violation",
                    target_field=field,
                    test_condition=f"Body field '{field}' = '__INVALID_ENUM_VALUE__' (allowed: {enum_vals})",
                    expected_http="200",
                    expected_app="success=false, error_code<0",
                    error_detail=f"domain.enum_violation.{field}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=query_params,
                    request_headers=None,
                    request_body=bad_body,
                    expected_status_display="200 / success=false, error_code<0",
                    rule_type="invalid_enum",
                ))

        return blocks

    def _semantic_probe(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
    ) -> list[str]:
        blocks: list[str] = []

        base_path_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "path"
        }
        base_query_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "query" and p.get("required")
        }
        base_body = self._build_valid_body(req_body) or {}

        for p in params:
            schema = p.get("schema", {})
            tag = schema.get("semantic_tag", "")
            probes = _SEMANTIC_PROBES.get(tag, [])
            if not probes:
                continue

            for probe in probes:
                probe_val = probe["value"]
                probe_label = probe["label"]
                policy = probe["policy"]
                fname = f"test_{op_id}_semantic_{_safe_name(p['name'])}_{probe_label}"

                req_query = base_query_params
                req_body_payload = base_body if base_body else None

                if p["in"] == "path":
                    bad_path = {**base_path_params, p["name"]: probe_val}
                    resolved_path = _build_url(path, bad_path)
                    call = _render_call(method, path, bad_path, base_query_params, req_body_payload)
                elif p["in"] == "query":
                    bad_query = {**base_query_params, p["name"]: probe_val}
                    resolved_path = _build_url(path, base_path_params)
                    req_query = bad_query
                    call = _render_call(method, path, base_path_params, bad_query, req_body_payload)
                else:
                    continue

                assertion = self._build_policy_assertion(policy, p["name"], f"semantic:{probe_label}")
                _s_axis, _s_rc = _SEMANTIC_PROBE_DIAG.get(
                    (tag, probe_label), ("domain", "constraint_missing_in_generator")
                )
                exp_app = "success=false, error_code<0" if policy == "must_fail" else "no crash (status < 500)"

                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:semantic_probe] param '{p['name']}' tag={tag} probe={probe_label} policy={policy}.",
                    call_str=call,
                    assertion_str=assertion,
                    axis=_s_axis,
                    reason_code=_s_rc,
                    target_field=p["name"],
                    test_condition=f"'{p['name']}' tag={tag} probe={probe_label}: value={probe_val!r}",
                    expected_http="200",
                    expected_app=exp_app,
                    error_detail=f"{_s_axis}.{_s_rc}.{p['name']}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=req_query,
                    request_headers=None,
                    request_body=req_body_payload,
                    expected_status_display=f"200 / {exp_app}",
                    rule_type="semantic_probe",
                ))

        schema = (req_body or {}).get("schema") or {}
        properties = schema.get("properties", {})

        for field, field_schema in properties.items():
            tag = field_schema.get("semantic_tag", "")
            probes = _SEMANTIC_PROBES.get(tag, [])
            if not probes:
                continue

            for probe in probes:
                probe_val = probe["value"]
                probe_label = probe["label"]
                policy = probe["policy"]
                fname = f"test_{op_id}_semantic_{_safe_name(field)}_{probe_label}"
                bad_body = {**base_body, field: probe_val}
                resolved_path = _build_url(path, base_path_params)
                call = _render_call(method, path, base_path_params, base_query_params, bad_body)
                assertion = self._build_policy_assertion(policy, field, f"semantic:{probe_label}")
                _s_axis, _s_rc = _SEMANTIC_PROBE_DIAG.get(
                    (tag, probe_label), ("domain", "constraint_missing_in_generator")
                )
                exp_app = "success=false, error_code<0" if policy == "must_fail" else "no crash (status < 500)"

                blocks.append(self._api_test_block(
                    fname=fname,
                    docstring=f"[rule:semantic_probe] body field '{field}' tag={tag} probe={probe_label} policy={policy}.",
                    call_str=call,
                    assertion_str=assertion,
                    axis=_s_axis,
                    reason_code=_s_rc,
                    target_field=field,
                    test_condition=f"'{field}' tag={tag} probe={probe_label}: value={probe_val!r}",
                    expected_http="200",
                    expected_app=exp_app,
                    error_detail=f"{_s_axis}.{_s_rc}.{field}",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=base_query_params,
                    request_headers=None,
                    request_body=bad_body,
                    expected_status_display=f"200 / {exp_app}",
                    rule_type="semantic_probe",
                ))

        return blocks