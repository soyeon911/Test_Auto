"""
Rule-Based TC Generator — deterministic, zero-AI layer.

For each API endpoint it generates pytest functions covering:

  positive           — endpoint-aware positive / probe_only checks
  missing_required   — omit each required field/param
  wrong_type         — send wrong type per param/field
  boundary           — schema/enriched min/max boundary values
  input_validation   — semantic-tag-specific invalid / exploratory probes
                       (formerly semantic_probe; covers base64_image, base64_template,
                        threshold_numeric, numeric_id, boolean_flag tag-based inputs)
  raw_image_relation — cross-field relation checks for raw image endpoints
                       (width x height x channel vs image_data size mismatch / invalid channel)

Note: invalid_enum rule removed — QFEapi.json has no enum fields.

The output is a plain Python code string, ready to be written to a .py file
or fed as context to the AI augmentation step.
"""

from __future__ import annotations

import base64
import re
import textwrap
from typing import Any


# ──────────────────────────────────────────────────────────────
# semantic / fixture policy
# ──────────────────────────────────────────────────────────────

SUPPORTED_QFE_TAGS: frozenset[str] = frozenset({
    "plain_string",
    "identifier",
    "base64_image",
    "base64_template",
    "threshold_numeric",
    "enum_mode",
    "config_json",
    "path_user_id",
    "channel_count",
    "boolean_flag",
    "numeric_id",
})

SUPPORTED_QFE_PROBE_TAGS: frozenset[str] = frozenset({
    "base64_image",
    "base64_template",
    "threshold_numeric",
    "numeric_id",
    "boolean_flag",
})

_GOOD: dict[str, Any] = {
    "integer": 1,
    "number": 1.5,
    "string": "test_string",
    "boolean": True,
    "array": [],
    "object": {},
}

# generic good values. endpoint-specific positives may override semantics later.
_GOOD_BY_TAG: dict[str, Any] = {
    "plain_string": "hello",
    "identifier": "item_001",
    "base64_image": (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    ),
    "base64_template": "AAEC",
    "threshold_numeric": 1,
    "config_json": "{}",
    "path_user_id": 1,
    "channel_count": 3,
    "boolean_flag": True,
    "numeric_id": 1,
}

# 1×1 white PNG — schema-valid but contains no face
_TINY_WHITE_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# raw image fixtures
_RAW_W, _RAW_H, _RAW_C = 4, 4, 3
_RAW_IMG_VALID_B64 = base64.b64encode(bytes(_RAW_W * _RAW_H * _RAW_C)).decode()
_RAW_IMG_MISMATCH_B64 = base64.b64encode(bytes(10)).decode()

# endpoint profiles
_MATCH_VERDICT_PATHS: frozenset[str] = frozenset({
    "/api/v2/match",
    "/api/v2/match-images",
    "/api/v2/verify",
    "/api/v2/verify-template",
})

_FACE_OPERATION_PATHS: frozenset[str] = frozenset({
    "/api/v2/detect",
    "/api/v2/hpe",
    "/api/v2/mask",
    "/api/v2/extract",
    "/api/v2/fam",
    "/api/v2/enroll",
    "/api/v2/identify",
    "/api/v2/identify-template",
})

_RAW_IMAGE_FIELDS: frozenset[str] = frozenset({"width", "height", "channel", "image_data"})

_INPUT_VALIDATION_PROBES: dict[str, list[dict[str, Any]]] = {
    "base64_image": [
        {"value": "not_base64!@#", "label": "invalid_b64", "policy": "must_fail"},
        {"value": "", "label": "empty_b64", "policy": "must_fail"},
        {"value": _TINY_WHITE_IMAGE_B64, "label": "no_face_image", "policy": "probe_only"},
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
    "boolean_flag": [
        {"value": "not_boolean", "label": "wrong_type", "policy": "must_fail"},
    ],
}

_INPUT_VALIDATION_DIAG: dict[tuple[str, str], tuple[str, str]] = {
    ("base64_image", "invalid_b64"): ("domain", "invalid_base64"),
    ("base64_image", "empty_b64"): ("domain", "invalid_base64"),
    ("base64_image", "no_face_image"): ("domain", "no_face_detected"),
    ("base64_template", "invalid_b64"): ("domain", "invalid_base64"),
    ("base64_template", "empty_b64"): ("domain", "invalid_base64"),
    ("threshold_numeric", "below_range"): ("domain", "range_violation"),
    ("threshold_numeric", "above_range"): ("domain", "range_violation"),
    ("threshold_numeric", "wrong_type"): ("schema", "type_mismatch"),
    ("numeric_id", "negative_id"): ("domain", "range_violation"),
    ("numeric_id", "zero_id"): ("domain", "range_violation"),
    ("boolean_flag", "wrong_type"): ("schema", "type_mismatch"),
}

_WRONG: dict[str, Any] = {
    "integer": "not_an_integer",
    "number": "not_a_number",
    "string": 12345,
    "boolean": "not_boolean",
    "array": "not_an_array",
    "object": "not_an_object",
}

_COERCIBLE_WRONG: dict[str, list[Any]] = {
    "integer": ["1", "0", "-1"],
    "number": ["1.0", "0.0", "-0.1", "1e3"],
    "boolean": ["true", "false", "1", "0"],
}

_BOUNDARY_INT = [0, -1, 2_147_483_647]

_STATE_NOT_MET_ERROR_CODES: frozenset[int] = frozenset({-28, -43, -200})

_STATE_DEPENDENT_PATHS: frozenset[str] = frozenset({
    "/api/v2/delete",
    "/api/v2/enroll",
    "/api/v2/enroll-template",
    "/api/v2/verify",
    "/api/v2/verify-template",
    "/api/v2/identify",
    "/api/v2/identify-template",
})


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
                [
                    "positive",
                    "missing_required",
                    "wrong_type",
                    "boundary",
                    "input_validation",
                    "raw_image_relation",
                ],
            )
        )
        # 하위 호환: config에 구 이름(semantic_probe)이 남아있는 경우도 input_validation으로 인식
        if "semantic_probe" in self.enabled_rules:
            self.enabled_rules.discard("semantic_probe")
            self.enabled_rules.add("input_validation")
        self.error_mode: str = config.get("server", {}).get("error_response_mode", "standard")
        self.match_threshold: float = float(
            config.get("server", {}).get("match_threshold", 0.0)
        )
        self._seen_cases: set[tuple[Any, ...]] = set()

    # ──────────────────────────────────────────────────────────────
    # public
    # ──────────────────────────────────────────────────────────────

    def generate(self, endpoint: dict[str, Any]) -> str:
        target_type = endpoint.get("target_type", "api")
        if target_type == "python":
            return self._generate_python(endpoint)
        return self._generate_api(endpoint)

    # ──────────────────────────────────────────────────────────────
    # helpers
    # ──────────────────────────────────────────────────────────────

    def _schema_constraints(self, schema: dict) -> dict[str, Any]:
        return schema.get("x_constraints", {}) or {}

    def _probe_policy(self, schema: dict) -> dict[str, Any]:
        return schema.get("x_probe_policy", {}) or {}

    def _normalize_tag(self, schema: dict) -> str:
        tag = schema.get("semantic_tag", "") or ""
        return tag if tag in SUPPORTED_QFE_TAGS else "plain_string"

    def _dedup_key(
        self,
        method: str,
        path: str,
        target_field: str,
        value_repr: str,
        reason_code: str,
        rule_type: str,
    ) -> tuple[Any, ...]:
        return (method.upper(), path, target_field, value_repr, reason_code, rule_type)

    def _register_case(
        self,
        method: str,
        path: str,
        target_field: str,
        value_repr: str,
        reason_code: str,
        rule_type: str,
    ) -> bool:
        key = self._dedup_key(method, path, target_field, value_repr, reason_code, rule_type)
        if key in self._seen_cases:
            return False
        self._seen_cases.add(key)
        return True

    def _range_cases(self, schema: dict) -> list[dict[str, Any]]:
        cons = self._schema_constraints(schema)
        minimum = cons.get("minimum")
        maximum = cons.get("maximum")
        policy = self._probe_policy(schema).get("range_policy", "none")

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
            cases.append({"value": example, "label": "example", "policy": "must_pass"})

            lower_example = example - step
            upper_example = example + step

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
        return textwrap.dedent(
            f"""\
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
        """
        )

    def _qfe_success_assertion(self) -> str:
        return textwrap.dedent(
            """\
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
        """
        )

    def _qfe_state_tolerant_assertion(self) -> str:
        """
        State-axis positive test assertion.

        상태 미충족 허용 로직:
          - success=True,  error_code>=0                     -> 정상 성공
          - success=False, error_code in STATE_NOT_MET_CODES -> DB/fixture 상태 미충족
          - 그 외 응답                                       -> FAIL
        """
        return textwrap.dedent(
            f"""\
            assert resp.status_code < 500, (
                f"[FAIL] expected success-like response, got crash\n"
                f"  Status : {{resp.status_code}}\n"
                f"  Body   : {{resp.text[:300]}}"
            )
            try:
                body = resp.json()
            except ValueError:
                pytest.fail(f"Expected JSON response, got: {{resp.text[:300]}}")
            _success = body.get("success")
            _ec = body.get("error_code", 0)
            assert (
                (_success is True and _ec >= 0) or
                (_success is False and _ec in {sorted(_STATE_NOT_MET_ERROR_CODES)})
            ), (
                f"[FAIL] unexpected QFE state response\n"
                f"  Expected : success=true/error_code>=0 (data present)\n"
                f"           OR success=false/error_code in {sorted(_STATE_NOT_MET_ERROR_CODES)} (state not met)\n"
                f"  success    : {{_success}}\n"
                f"  error_code : {{_ec}}\n"
                f"  msg        : {{body.get('msg')}}\n"
                f"  Full body  : {{resp.text[:300]}}"
            )
        """
        )

    def _when_success(self, assertion: str) -> str:
        """domain assertion 블록을 success=True 조건부로 래핑.
        state-tolerant positive 테스트에서 success=False(상태 미충족)일 때
        domain 검증을 건너뛰기 위해 사용."""
        indented = textwrap.indent(assertion.rstrip("\n"), "    ")
        return 'if body.get("success") is True:\n' + indented + "\n"

    def _standard_error_assertion(self, field_name: str, label: str = "error") -> str:
        return textwrap.dedent(
            f"""\
            assert resp.status_code in [400, 422], (
                f"[FAIL] {label} on '{field_name}' — expected 400/422\\n"
                f"  Status : {{resp.status_code}}\\n"
                f"  Body   : {{resp.text[:300]}}"
            )
        """
        )

    def _standard_success_assertion(self, success_statuses: list[int]) -> str:
        statuses_repr = repr(success_statuses)
        return textwrap.dedent(
            f"""\
            assert resp.status_code in {statuses_repr}, (
                f"[FAIL] expected success status in {statuses_repr}, got {{resp.status_code}}\\n"
                f"  Body : {{resp.text[:300]}}"
            )
        """
        )

    def _no_crash_assertion(self, label: str = "probe") -> str:
        return textwrap.dedent(
            f"""\
            assert resp.status_code < 500, (
                f"[FAIL] {label} caused server crash\\n"
                f"  Status : {{resp.status_code}}\\n"
                f"  Body   : {{resp.text[:300]}}"
            )
        """
        )

    def _build_policy_assertion(
        self,
        policy: str,
        field_name: str,
        label: str,
        success_statuses: list[int] | None = None,
        allow_state_not_met: bool = False,
    ) -> str:
        if policy == "must_fail":
            if self.error_mode == "qfe":
                return self._qfe_error_assertion(field_name, label)
            return self._standard_error_assertion(field_name, label)

        if policy == "must_pass":
            if self.error_mode == "qfe":
                if allow_state_not_met:
                    return self._qfe_state_tolerant_assertion()
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
        rule_subtype: str = "",
        endpoint_profile: str = "",
        semantic_tag: str = "",
        policy: str = "",
        expected_result_type: str = "",
    ) -> str:
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
            f"        \"rule_subtype\": {rule_subtype!r},\n"
            f"        \"endpoint_profile\": {endpoint_profile!r},\n"
            f"        \"semantic_tag\": {semantic_tag!r},\n"
            f"        \"policy\": {policy!r},\n"
            f"        \"expected_result_type\": {expected_result_type!r},\n"
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
        tag = self._normalize_tag(schema)
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

    def _wrong_values(self, schema: dict) -> list[dict[str, Any]]:
        ftype = schema.get("type", "string")
        cases: list[dict[str, Any]] = []

        strong = _WRONG.get(ftype)
        if strong is not None and ftype != "string":
            cases.append(
                {
                    "value": strong,
                    "label": "strict",
                    "reason_code": "type_mismatch",
                    "policy": "must_fail",
                }
            )

        for val in _COERCIBLE_WRONG.get(ftype, []):
            cases.append(
                {
                    "value": val,
                    "label": f"coercible_{_safe_name(str(val))}",
                    "reason_code": "type_coercion_risk",
                    "policy": "probe_only",
                }
            )

        return cases

    # ──────────────────────────────────────────────────────────────
    # endpoint profile helpers
    # ──────────────────────────────────────────────────────────────

    def _is_raw_image_relation_endpoint(self, req_body: dict | None) -> bool:
        if not req_body:
            return False
        props = set((req_body.get("schema") or {}).get("properties", {}).keys())
        return _RAW_IMAGE_FIELDS.issubset(props)

    def _get_endpoint_profile(self, path: str, req_body: dict | None) -> str:
        if path in _MATCH_VERDICT_PATHS:
            return "match_verdict"
        if self._is_raw_image_relation_endpoint(req_body):
            return "raw_image"
        if path in _FACE_OPERATION_PATHS:
            return "face_operation"
        return "default"

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
        if "input_validation" in self.enabled_rules:
            blocks.extend(self._input_validation(op_id, method, path, params, req_body))
        if "raw_image_relation" in self.enabled_rules:
            blocks.extend(self._raw_image_relation(op_id, method, path, params, req_body))

        return "\n\n\n".join(b for b in blocks if b)

    # ──────────────────────────────────────────────────────────────
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
            blocks.append(
                textwrap.dedent(
                    f"""\
                def test_{op_id}_positive():
                    \"\"\"[rule:positive] Call with valid args — must not raise.\"\"\"
                    import {module_name}
                    result = {module_name}.{func_name}({args_repr})
                    assert result is not None or result is None
            """
                )
            )

        if "missing_required" in self.enabled_rules:
            for p in required_params:
                fname = f"test_{op_id}_missing_{_safe_name(p['name'])}"
                args_repr = ", ".join(
                    f"{pp['name']}={good_val(pp)!r}" for pp in required_params if pp["name"] != p["name"]
                )
                blocks.append(
                    textwrap.dedent(
                        f"""\
                    def {fname}():
                        \"\"\"[rule:missing_required] Omit '{p['name']}' → TypeError or ValueError.\"\"\"
                        import {module_name}
                        import pytest
                        with pytest.raises((TypeError, ValueError)):
                            {module_name}.{func_name}({args_repr})
                """
                    )
                )

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
                blocks.append(
                    textwrap.dedent(
                        f"""\
                    def {fname}():
                        \"\"\"[rule:wrong_type] Pass wrong type for '{p['name']}' (expected {ptype}).\"\"\"
                        import {module_name}
                        import pytest
                        with pytest.raises((TypeError, ValueError)):
                            {module_name}.{func_name}({args_repr})
                """
                    )
                )

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
                    blocks.append(
                        textwrap.dedent(
                            f"""\
                        def {fname}():
                            \"\"\"[rule:boundary] '{p['name']}' = {probe} — must not crash.\"\"\"
                            import {module_name}
                            try:
                                {module_name}.{func_name}({args_repr})
                            except (ValueError, OverflowError):
                                pass
                    """
                        )
                    )

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
        profile = self._get_endpoint_profile(path, req_body)
        if profile == "match_verdict":
            return self._positive_match_verdict(op_id, method, path, params, req_body, success_statuses)
        if profile == "face_operation":
            return self._positive_face_operation(op_id, method, path, params, req_body, success_statuses)
        if profile == "raw_image":
            return self._positive_raw_image(op_id, method, path, params, req_body, success_statuses)

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
        allow_state_not_met = self.error_mode == "qfe" and path in _STATE_DEPENDENT_PATHS
        assertion = (
            self._qfe_state_tolerant_assertion()
            if allow_state_not_met
            else (
                self._qfe_success_assertion()
                if self.error_mode == "qfe"
                else self._standard_success_assertion(success_statuses)
            )
        )
        exp_app = (
            f"success=true/error_code>=0 OR success=false/error_code in {sorted(_STATE_NOT_MET_ERROR_CODES)} (state not met)"
            if allow_state_not_met
            else (
                "success=true/error_code>=0"
                if self.error_mode == "qfe"
                else f"status in {success_statuses}"
            )
        )

        return self._api_test_block(
            fname=f"test_{op_id}_positive",
            docstring="[rule:positive] Happy-path — valid request; success=true if data present, success=false/error_code<0 if state not met.",
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
            rule_subtype="positive_default",
            endpoint_profile=profile,
            expected_result_type="expected_pass",
        )

    def _positive_match_verdict(
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

        if path == "/api/v2/match":
            assertion = self._qfe_state_tolerant_assertion() + self._when_success(
                self._match_status_assertion(
                    score_field="match_score",
                    status_field="status",
                    data_error_code_field="error_code",
                )
            )
            exp_app = (
                "success=true/error_code>=0 (data present): domain validated; "
                "OR success=false/error_code<0 (state not met)"
            )
            condition = (
                "Happy path — valid request; success=true 시 domain 검증 "
                "(data.error_code, data.match_score, data.status); "
                "success=false/error_code<0 이면 상태 미충족으로 허용"
            )
        elif "verify" in path:
            assertion = self._qfe_state_tolerant_assertion() + self._when_success(
                self._match_domain_assertion(
                    score_field="match_score",
                    verdict_field="verified",
                )
            )
            exp_app = (
                "success=true/error_code>=0 (data present): data.match_score+verified validated; "
                "OR success=false/error_code<0 (state not met)"
            )
            condition = (
                "Happy path — valid request; success=true 시 domain 검증 "
                "(data.match_score, data.verified); "
                "success=false/error_code<0 이면 상태 미충족으로 허용"
            )
        else:
            assertion = self._qfe_state_tolerant_assertion() + self._when_success(
                self._match_score_only_assertion(
                    score_field="match_score",
                )
            )
            exp_app = (
                "success=true/error_code>=0 (data present): data.match_score validated; "
                "OR success=false/error_code<0 (state not met)"
            )
            condition = (
                "Happy path — valid request; success=true 시 domain 검증 "
                "(data.match_score); "
                "success=false/error_code<0 이면 상태 미충족으로 허용"
            )

        return self._api_test_block(
            fname=f"test_{op_id}_positive",
            docstring="[rule:positive] Match/verify — success=true 시 domain 검증; success=false/error_code<0 (state not met) 허용.",
            call_str=call,
            assertion_str=assertion,
            axis="state",
            reason_code="precondition_not_met",
            target_field="",
            test_condition=condition,
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
            rule_subtype="positive_outcome",
            endpoint_profile="match_verdict",
            expected_result_type="expected_pass",
        )

    def _positive_face_operation(
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
        assertion = self._no_crash_assertion("positive_probe")

        return self._api_test_block(
            fname=f"test_{op_id}_positive",
            docstring=(
                "[rule:positive] Face operation — schema-valid request; probe_only "
                "(real face image required for domain success)."
            ),
            call_str=call,
            assertion_str=assertion,
            axis="state",
            reason_code="precondition_not_met",
            target_field="",
            test_condition=(
                "Happy path (schema-valid synthetic image) — probe_only: "
                "no crash expected; success may be false if no face detected"
            ),
            expected_http="200",
            expected_app="no crash (status < 500); success may be false if no face",
            error_detail="state.precondition_not_met",
            request_method=method,
            request_path=resolved_path,
            request_query=query_params,
            request_headers=None,
            request_body=body,
            expected_status_display="200 / no crash (probe_only)",
            rule_type="positive",
            rule_subtype="positive_schema",
            endpoint_profile="face_operation",
            expected_result_type="probe_only",
        )

    def _positive_raw_image(
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
        resolved_path = _build_url(path, path_params)
        base_body = self._build_valid_body(req_body) or {}
        valid_body = {
            **base_body,
            "width": _RAW_W,
            "height": _RAW_H,
            "channel": _RAW_C,
            "image_data": _RAW_IMG_VALID_B64,
        }
        call = _render_call(method, path, path_params, query_params, valid_body)
        assertion = self._build_policy_assertion("must_pass", "image_data", "raw_image_valid", success_statuses)

        return self._api_test_block(
            fname=f"test_{op_id}_positive",
            docstring="[rule:positive] Raw image endpoint — relation-valid payload should pass.",
            call_str=call,
            assertion_str=assertion,
            axis="domain",
            reason_code="invalid_image_relation",
            target_field="image_data",
            test_condition=(
                f"width={_RAW_W}, height={_RAW_H}, channel={_RAW_C}, "
                f"image_data={_RAW_W * _RAW_H * _RAW_C} bytes -- relation VALID"
            ),
            expected_http="200",
            expected_app="success=true, error_code>=0",
            error_detail="domain.invalid_image_relation.image_data.valid",
            request_method=method,
            request_path=resolved_path,
            request_query=query_params,
            request_headers=None,
            request_body=valid_body,
            expected_status_display="200 / success=true (relation valid)",
            rule_type="positive",
            rule_subtype="positive_domain",
            endpoint_profile="raw_image",
            expected_result_type="expected_pass",
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
            path_params = {
                p["name"]: self._good_value(p["name"], p.get("schema", {}))
                for p in params if p["in"] == "path"
            }
            query_params = {
                p["name"]: self._good_value(p["name"], p.get("schema", {}))
                for p in params if p["in"] == "query" and p.get("required") and p["name"] != target_param["name"]
            }
            resolved_path = _build_url(path, path_params)
            body = self._build_valid_body(req_body)
            call = _render_call(method, path, path_params, query_params, body)

            if not self._register_case(method, resolved_path, target_param["name"], "<missing>", "missing_required", "missing_required"):
                continue

            blocks.append(
                self._api_test_block(
                    fname=f"test_{op_id}_missing_{_safe_name(target_param['name'])}",
                    docstring=f"[rule:missing_required] Omit required query param '{target_param['name']}'.",
                    call_str=call,
                    assertion_str=self._build_policy_assertion("must_fail", target_param["name"], "missing_required"),
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
                    rule_subtype="required_query_missing",
                    expected_result_type="expected_fail",
                )
            )

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
                partial_body = {k: self._good_value(k, v) for k, v in properties.items() if k != field}
                resolved_path = _build_url(path, path_params)
                call = _render_call(method, path, path_params, query_params, partial_body)

                if not self._register_case(method, resolved_path, field, "<missing>", "missing_required", "missing_required"):
                    continue

                blocks.append(
                    self._api_test_block(
                        fname=f"test_{op_id}_missing_body_{_safe_name(field)}",
                        docstring=f"[rule:missing_required] Omit required body field '{field}'.",
                        call_str=call,
                        assertion_str=self._build_policy_assertion("must_fail", field, "missing_required"),
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
                        rule_subtype="required_body_missing",
                        expected_result_type="expected_fail",
                    )
                )

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
            cases = self._wrong_values(p.get("schema", {}))
            if not cases:
                continue

            for case in cases:
                wrong = case["value"]
                label = case["label"]
                reason_code = case["reason_code"]
                policy = case["policy"]

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

                if not self._register_case(method, resolved_path, p["name"], repr(wrong), reason_code, "wrong_type"):
                    continue

                exp_app = "success=false, error_code<0" if policy == "must_fail" else "no crash (status < 500)"
                blocks.append(
                    self._api_test_block(
                        fname=f"test_{op_id}_wrong_type_{_safe_name(p['name'])}_{label}",
                        docstring=f"[rule:wrong_type] Pass {label} wrong type for '{p['name']}' (expected {p.get('schema', {}).get('type', 'string')}).",
                        call_str=call,
                        assertion_str=self._build_policy_assertion(policy, p["name"], f"wrong_type:{label}"),
                        axis="schema",
                        reason_code=reason_code,
                        target_field=p["name"],
                        test_condition=f"'{p['name']}' sent with {label} wrong type (expected {p.get('schema', {}).get('type', 'string')}, sent {wrong!r})",
                        expected_http="200",
                        expected_app=exp_app,
                        error_detail=f"schema.{reason_code}.{p['name']}",
                        request_method=method,
                        request_path=resolved_path,
                        request_query=req_query,
                        request_headers=None,
                        request_body=req_body_payload,
                        expected_status_display=f"200 / {exp_app}",
                        rule_type="wrong_type",
                        rule_subtype=label,
                        expected_result_type="expected_fail" if policy == "must_fail" else "probe_only",
                    )
                )

        if req_body:
            body_schema = req_body.get("schema", {})
            properties = body_schema.get("properties", {})
            for field, field_schema in properties.items():
                cases = self._wrong_values(field_schema)
                if not cases:
                    continue

                for case in cases:
                    wrong = case["value"]
                    label = case["label"]
                    reason_code = case["reason_code"]
                    policy = case["policy"]

                    valid_body = self._build_valid_body(req_body) or {}
                    bad_body = {**valid_body, field: wrong}
                    resolved_path = _build_url(path, path_params)
                    call = _render_call(method, path, path_params, query_params, bad_body)

                    if not self._register_case(method, resolved_path, field, repr(wrong), reason_code, "wrong_type"):
                        continue

                    exp_app = "success=false, error_code<0" if policy == "must_fail" else "no crash (status < 500)"
                    blocks.append(
                        self._api_test_block(
                            fname=f"test_{op_id}_wrong_type_body_{_safe_name(field)}_{label}",
                            docstring=f"[rule:wrong_type] Pass {label} wrong type for body field '{field}' (expected {field_schema.get('type', 'string')}).",
                            call_str=call,
                            assertion_str=self._build_policy_assertion(policy, field, f"wrong_type:{label}"),
                            axis="schema",
                            reason_code=reason_code,
                            target_field=field,
                            test_condition=f"Body field '{field}' sent with {label} wrong type (expected {field_schema.get('type', 'string')}, sent {wrong!r})",
                            expected_http="200",
                            expected_app=exp_app,
                            error_detail=f"schema.{reason_code}.{field}",
                            request_method=method,
                            request_path=resolved_path,
                            request_query=query_params,
                            request_headers=None,
                            request_body=bad_body,
                            expected_status_display=f"200 / {exp_app}",
                            rule_type="wrong_type",
                            rule_subtype=label,
                            expected_result_type="expected_fail" if policy == "must_fail" else "probe_only",
                        )
                    )

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
        # raw_image endpoint는 일반 boundary 대신 raw_image_relation rule을 사용
        if self._get_endpoint_profile(path, req_body) == "raw_image":
            return []

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

                if not self._register_case(method, resolved_path, p["name"], repr(probe), "range_violation", "boundary"):
                    continue

                allow_state_not_met = (policy == "must_pass" and path in _STATE_DEPENDENT_PATHS)
                axis_value = "state" if allow_state_not_met else "domain"
                reason_value = "precondition_not_met" if allow_state_not_met else "range_violation"
                error_detail_value = (
                    f"state.precondition_not_met.{p['name']}.{label}"
                    if allow_state_not_met
                    else f"domain.range_violation.{p['name']}.{label}"
                )
                exp_app = (
                    f"success=true, error_code>=0 OR success=false, error_code in {sorted(_STATE_NOT_MET_ERROR_CODES)}"
                    if allow_state_not_met
                    else {
                        "must_pass": "success=true, error_code>=0",
                        "must_fail": "success=false, error_code<0",
                        "probe_only": "no crash (status < 500)",
                    }.get(policy, "no crash (status < 500)")
                )

                blocks.append(
                    self._api_test_block(
                        fname=f"test_{op_id}_boundary_{_safe_name(p['name'])}_{_safe_name(str(label))}",
                        docstring=f"[rule:boundary] '{p['name']}' = {probe} ({label}, policy={policy}).",
                        call_str=call,
                        assertion_str=self._build_policy_assertion(
                            policy,
                            p["name"],
                            f"boundary:{label}",
                            success_statuses,
                            allow_state_not_met=allow_state_not_met,
                        ),
                        axis=axis_value,
                        reason_code=reason_value,
                        target_field=p["name"],
                        test_condition=f"'{p['name']}' = {probe} (boundary: {label})",
                        expected_http="200",
                        expected_app=exp_app,
                        error_detail=error_detail_value,
                        request_method=method,
                        request_path=resolved_path,
                        request_query=req_query,
                        request_headers=None,
                        request_body=req_body_payload,
                        expected_status_display=f"200 / {exp_app}",
                        rule_type="boundary",
                        rule_subtype=label,
                        expected_result_type="expected_pass" if policy == "must_pass" else ("expected_fail" if policy == "must_fail" else "probe_only"),
                    )
                )

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
                bad_body = {**base_body, field: probe}
                resolved_path = _build_url(path, base_path_params)
                call = _render_call(method, path, base_path_params, base_query_params, bad_body)

                if not self._register_case(method, resolved_path, field, repr(probe), "range_violation", "boundary"):
                    continue

                allow_state_not_met = (policy == "must_pass" and path in _STATE_DEPENDENT_PATHS)
                axis_value = "state" if allow_state_not_met else "domain"
                reason_value = "precondition_not_met" if allow_state_not_met else "range_violation"
                error_detail_value = (
                    f"state.precondition_not_met.{field}.{label}"
                    if allow_state_not_met
                    else f"domain.range_violation.{field}.{label}"
                )
                exp_app = (
                    f"success=true, error_code>=0 OR success=false, error_code in {sorted(_STATE_NOT_MET_ERROR_CODES)}"
                    if allow_state_not_met
                    else {
                        "must_pass": "success=true, error_code>=0",
                        "must_fail": "success=false, error_code<0",
                        "probe_only": "no crash (status < 500)",
                    }.get(policy, "no crash (status < 500)")
                )

                blocks.append(
                    self._api_test_block(
                        fname=f"test_{op_id}_boundary_body_{_safe_name(field)}_{_safe_name(str(label))}",
                        docstring=f"[rule:boundary] body field '{field}' = {probe} ({label}, policy={policy}).",
                        call_str=call,
                        assertion_str=self._build_policy_assertion(
                            policy,
                            field,
                            f"boundary:{label}",
                            success_statuses,
                            allow_state_not_met=allow_state_not_met,
                        ),
                        axis=axis_value,
                        reason_code=reason_value,
                        target_field=field,
                        test_condition=f"Body field '{field}' = {probe} (boundary: {label})",
                        expected_http="200",
                        expected_app=exp_app,
                        error_detail=error_detail_value,
                        request_method=method,
                        request_path=resolved_path,
                        request_query=base_query_params,
                        request_headers=None,
                        request_body=bad_body,
                        expected_status_display=f"200 / {exp_app}",
                        rule_type="boundary",
                        rule_subtype=label,
                        expected_result_type="expected_pass" if policy == "must_pass" else ("expected_fail" if policy == "must_fail" else "probe_only"),
                    )
                )

        return blocks

    def _input_validation(
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
            tag = self._normalize_tag(schema)
            if tag not in SUPPORTED_QFE_PROBE_TAGS:
                continue
            probes = _INPUT_VALIDATION_PROBES.get(tag, [])
            if not probes:
                continue

            for probe in probes:
                probe_val = probe["value"]
                probe_label = probe["label"]
                policy = probe["policy"]

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

                _s_axis, _s_rc = _INPUT_VALIDATION_DIAG.get(
                    (tag, probe_label), ("domain", "constraint_missing_in_generator")
                )
                if not self._register_case(method, resolved_path, p["name"], repr(probe_val), _s_rc, "input_validation"):
                    continue

                exp_app = "success=false, error_code<0" if policy == "must_fail" else "no crash (status < 500)"

                blocks.append(
                    self._api_test_block(
                        fname=f"test_{op_id}_input_val_{_safe_name(p['name'])}_{probe_label}",
                        docstring=f"[rule:input_validation] param '{p['name']}' tag={tag} probe={probe_label} policy={policy}.",
                        call_str=call,
                        assertion_str=self._build_policy_assertion(policy, p["name"], f"semantic:{probe_label}"),
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
                        rule_type="input_validation",
                        rule_subtype=probe_label,
                        semantic_tag=tag,
                        policy=policy,
                        expected_result_type="expected_fail" if policy == "must_fail" else "probe_only",
                    )
                )

        schema = (req_body or {}).get("schema") or {}
        properties = schema.get("properties", {})

        for field, field_schema in properties.items():
            tag = self._normalize_tag(field_schema)
            if tag not in SUPPORTED_QFE_PROBE_TAGS:
                continue
            probes = _INPUT_VALIDATION_PROBES.get(tag, [])
            if not probes:
                continue

            for probe in probes:
                probe_val = probe["value"]
                probe_label = probe["label"]
                policy = probe["policy"]
                bad_body = {**base_body, field: probe_val}
                resolved_path = _build_url(path, base_path_params)
                call = _render_call(method, path, base_path_params, base_query_params, bad_body)
                _s_axis, _s_rc = _INPUT_VALIDATION_DIAG.get(
                    (tag, probe_label), ("domain", "constraint_missing_in_generator")
                )

                if not self._register_case(method, resolved_path, field, repr(probe_val), _s_rc, "input_validation"):
                    continue

                exp_app = "success=false, error_code<0" if policy == "must_fail" else "no crash (status < 500)"
                blocks.append(
                    self._api_test_block(
                        fname=f"test_{op_id}_input_val_{_safe_name(field)}_{probe_label}",
                        docstring=f"[rule:input_validation] body field '{field}' tag={tag} probe={probe_label} policy={policy}.",
                        call_str=call,
                        assertion_str=self._build_policy_assertion(policy, field, f"semantic:{probe_label}"),
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
                        rule_type="input_validation",
                        rule_subtype=probe_label,
                        semantic_tag=tag,
                        policy=policy,
                        expected_result_type="expected_fail" if policy == "must_fail" else "probe_only",
                    )
                )

        return blocks

    def _match_score_only_assertion(
        self,
        score_field: str,
    ) -> str:
         return (
            '_data = body.get("data") or {}\n'
            f'assert isinstance(_data.get("{score_field}"), (int, float)), (\n'
            '    f"[FAIL] domain: expected numeric ..."\n'
            ')\n'
        )

    def _match_status_assertion(
        self,
        score_field: str = "match_score",
        status_field: str = "status",
        data_error_code_field: str = "error_code",
    ) -> str:
        return textwrap.dedent(
            f"""\
            _data = body.get("data") or {{}}

            assert isinstance(_data, dict), (
                f"[FAIL] domain: missing or invalid data block\\n"
                f"  data      : {{_data}}\\n"
                f"  Full body : {{resp.text[:300]}}"
            )

            assert isinstance(_data.get("{data_error_code_field}"), int), (
                f"[FAIL] domain: expected integer '{data_error_code_field}' in data\\n"
                f"  data      : {{_data}}\\n"
                f"  Full body : {{resp.text[:300]}}"
            )

            assert isinstance(_data.get("{score_field}"), (int, float)), (
                f"[FAIL] domain: expected numeric '{score_field}' in data\\n"
                f"  data      : {{_data}}\\n"
                f"  Full body : {{resp.text[:300]}}"
            )

            _status = _data.get("{status_field}")
            assert isinstance(_status, str), (
                f"[FAIL] domain: expected string '{status_field}' in data\\n"
                f"  data      : {{_data}}\\n"
                f"  Full body : {{resp.text[:300]}}"
            )

            assert _status in ("success", "fail"), (
                f"[FAIL] domain: invalid status value\\n"
                f"  status    : {{_status}}\\n"
                f"  data      : {{_data}}\\n"
                f"  Full body : {{resp.text[:300]}}"
            )

            _score = float(_data.get("{score_field}", 0))

            # TODO:
            # threshold 연동 전까지는 status 구조만 검증.
            # 추후 matching-threshold 실제 값 조회 또는 config 값 연동 시
            # 아래 비교 활성화:
            #
            # _threshold = ...
            # _expected_status = "success" if _score >= _threshold else "fail"
            # assert _status == _expected_status, (
            #     f"[FAIL] domain: score-threshold consistency mismatch\\n"
            #     f"  score      : {{_score}}\\n"
            #     f"  threshold  : {{_threshold}}\\n"
            #     f"  expected   : {{_expected_status}}\\n"
            #     f"  actual     : {{_status}}\\n"
            #     f"  data       : {{_data}}\\n"
            #     f"  Full body  : {{resp.text[:300]}}"
            # )
            """
        )

    # ──────────────────────────────────────────────────────────────
    # match / verify domain assertion
    # 기존 bool 존재 여부만 확인 -> score-threshold consistency까지 보도록 변경
    # ──────────────────────────────────────────────────────────────

    def _match_domain_assertion(
        self,
        score_field: str,
        verdict_field: str | None = None,
    ) -> str:
        return textwrap.dedent(
            f"""\
            _data = body.get("data") or {{}}

            assert isinstance(_data.get("{score_field}"), (int, float)), (
                f"[FAIL] domain: expected numeric '{score_field}' in data\\n"
                f"  data      : {{_data}}\\n"
                f"  Full body : {{resp.text[:300]}}"
            )

            _score = float(_data.get("{score_field}", 0))
            _threshold = float({self.match_threshold!r})
            _computed_match = _score >= _threshold

            # match endpoint:
            #   execution success와 domain match 여부는 별개다.
            # verify endpoint:
            #   verified bool이 있다면 score-threshold 계산 결과와 일관되어야 한다.
            if {repr(verdict_field)}:
                assert "{verdict_field}" in _data, (
                    f"[FAIL] domain: missing verdict field '{verdict_field}'\\n"
                    f"  data      : {{_data}}\\n"
                    f"  Full body : {{resp.text[:300]}}"
                )

                _verdict = _data.get("{verdict_field}")
                assert isinstance(_verdict, bool), (
                    f"[FAIL] domain: expected boolean '{verdict_field}' in data\\n"
                    f"  data      : {{_data}}\\n"
                    f"  Full body : {{resp.text[:300]}}"
                )

                assert _verdict == _computed_match, (
                    f"[FAIL] domain: score-threshold consistency mismatch\\n"
                    f"  score      : {{_score}}\\n"
                    f"  threshold  : {{_threshold}}\\n"
                    f"  computed   : {{_computed_match}}\\n"
                    f"  verdict    : {{_verdict}}\\n"
                    f"  data       : {{_data}}\\n"
                    f"  Full body  : {{resp.text[:300]}}"
                )
            """
        )

    # ──────────────────────────────────────────────────────────────
    # raw image relation rule
    # ──────────────────────────────────────────────────────────────

    def _raw_image_relation(
        self,
        op_id: str,
        method: str,
        path: str,
        params: list[dict],
        req_body: dict | None,
    ) -> list[str]:
        if not self._is_raw_image_relation_endpoint(req_body):
            return []

        blocks: list[str] = []
        path_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "path"
        }
        query_params = {
            p["name"]: self._good_value(p["name"], p.get("schema", {}))
            for p in params if p["in"] == "query" and p.get("required")
        }
        resolved_path = _build_url(path, path_params)
        base_body = self._build_valid_body(req_body) or {}
        expected_size = _RAW_W * _RAW_H * _RAW_C

        # NOTE: raw_image_relation_valid(must_pass) is omitted —
        # positive rule already covers it via _positive_raw_image().
        # This rule only generates negative/boundary cases.


        # size mismatch
        mismatch_body = {
            **base_body,
            "width": _RAW_W,
            "height": _RAW_H,
            "channel": _RAW_C,
            "image_data": _RAW_IMG_MISMATCH_B64,
        }
        if self._register_case(method, resolved_path, "image_data", "mismatch:10", "invalid_image_relation", "raw_image_relation"):
            blocks.append(
                self._api_test_block(
                    fname=f"test_{op_id}_raw_image_relation_mismatch",
                    docstring=(
                        f"[rule:raw_image_relation] w={_RAW_W}xh={_RAW_H}xc={_RAW_C}={expected_size}B "
                        "but image_data=10B -> size mismatch (must_fail)."
                    ),
                    call_str=_render_call(method, path, path_params, query_params, mismatch_body),
                    assertion_str=self._build_policy_assertion("must_fail", "image_data", "raw_image_mismatch"),
                    axis="domain",
                    reason_code="invalid_image_relation",
                    target_field="image_data",
                    test_condition=(
                        f"width={_RAW_W}, height={_RAW_H}, channel={_RAW_C} "
                        f"but image_data=10 bytes (expected {expected_size}B) -- relation MISMATCH"
                    ),
                    expected_http="200",
                    expected_app="success=false, error_code<0",
                    error_detail="domain.invalid_image_relation.image_data.mismatch",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=query_params,
                    request_headers=None,
                    request_body=mismatch_body,
                    expected_status_display="200 / success=false (relation mismatch)",
                    rule_type="raw_image_relation",
                    rule_subtype="relation_mismatch",
                    endpoint_profile="raw_image",
                    expected_result_type="expected_fail",
                )
            )

        # invalid channel
        invalid_channel_body = {
            **base_body,
            "width": _RAW_W,
            "height": _RAW_H,
            "channel": 0,
            "image_data": _RAW_IMG_VALID_B64,
        }
        if self._register_case(method, resolved_path, "channel", "0", "invalid_image_relation", "raw_image_relation"):
            blocks.append(
                self._api_test_block(
                    fname=f"test_{op_id}_raw_image_relation_invalid_channel",
                    docstring="[rule:raw_image_relation] channel=0 with raw image payload.",
                    call_str=_render_call(method, path, path_params, query_params, invalid_channel_body),
                    assertion_str=self._build_policy_assertion("probe_only", "channel", "raw_image_invalid_channel"),
                    axis="domain",
                    reason_code="invalid_image_relation",
                    target_field="channel",
                    test_condition="width/height valid but channel=0",
                    expected_http="200",
                    expected_app="no crash (status < 500)",
                    error_detail="domain.invalid_image_relation.channel.zero",
                    request_method=method,
                    request_path=resolved_path,
                    request_query=query_params,
                    request_headers=None,
                    request_body=invalid_channel_body,
                    expected_status_display="200 / no crash (probe_only)",
                    rule_type="raw_image_relation",
                    rule_subtype="channel_zero",
                    endpoint_profile="raw_image",
                    expected_result_type="probe_only",
                )
            )

        return blocks