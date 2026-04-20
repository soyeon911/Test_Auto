from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


# 캐시 버전.
# semantic 정책이 바뀌면 이 값을 올려서 예전 캐시를 무효화한다.
SEMANTIC_SCHEMA_VERSION = "qfe_semantic_v2"


# 전체적으로 "알고 있는" semantic tag 집합.
# 단, 실제 활성화 여부는 _QFE_ACTIVE_TAGS에서 한 번 더 제한한다.
KNOWN_TAGS: frozenset[str] = frozenset({
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
    # generic tags (known but disabled by default in QFE)
    "datetime_string",
    "email_string",
    "password_string",
    "file_path",
    "url_string",
    "uuid_string",
})


# QFE Swagger에서 실제로 관찰되었거나, 현재 generator가 지원해야 하는 tag만 활성화.
# 이 집합에 없는 tag는 최종적으로 plain_string으로 fallback.
_QFE_ACTIVE_TAGS: frozenset[str] = frozenset({
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


# QFE Swagger에서 허용할 Swagger format.
# format이 있어도 여기 없는 값은 semantic 확정 근거로 사용하지 않는다.
_QFE_ACTIVE_FORMATS: frozenset[str] = frozenset({
    "byte",
    "binary",
    "int32",
    "int64",
})


class SchemaEnricher:
    """
    Enriches parsed endpoint schemas with:
      - semantic_tag
      - x_constraints
      - x_probe_policy
      - x_inferred_from
    """

    def __init__(self, config: dict, llm_client=None):
        self._llm = llm_client
        self._mem: dict[str, dict[str, Any]] = {}
        self._cache_path = Path(
            config.get("tc_generation", {})
                  .get("semantic_tagging", {})
                  .get("cache_file", ".cache/semantic_tags.json")
        )
        self._load_file_cache()

        if config.get("tc_generation", {}).get("semantic_tagging", {}).get("reset_cache"):
            self._mem = {}
            if self._cache_path.exists():
                try:
                    self._cache_path.unlink()
                except Exception:
                    pass

    def _load_file_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            self._mem[k] = v
        except Exception:
            pass

    def _save_file_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._mem, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────
    # public
    # ──────────────────────────────────────────────────────────────

    def tag_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        ep = copy.deepcopy(endpoint)

        for p in ep.get("parameters", []):
            schema = p.setdefault("schema", {})
            enriched = self._enrich_field(
                name=p.get("name", ""),
                schema=schema,
                field_location=p.get("in", ""),
                field_description=p.get("description", ""),
            )
            schema.update(enriched)

            self._tag_nested_schema(
                schema=schema,
                field_location=p.get("in", ""),
            )

        req_body = ep.get("request_body")
        if req_body:
            body_schema = req_body.get("schema") or {}
            self._tag_nested_schema(body_schema, field_location="body")

            for media_obj in req_body.get("content", {}).values():
                raw_schema = media_obj.get("schema") or {}
                self._tag_nested_schema(raw_schema, field_location="body")

        return ep

    # ──────────────────────────────────────────────────────────────
    # enrich core
    # ──────────────────────────────────────────────────────────────

    def _tag_nested_schema(self, schema: dict, field_location: str = "body") -> None:
        if not schema:
            return

        properties = schema.get("properties", {})
        for field_name, field_schema in properties.items():
            enriched = self._enrich_field(
                name=field_name,
                schema=field_schema,
                field_location=field_location,
                field_description=field_schema.get("description", ""),
            )
            field_schema.update(enriched)
            self._tag_nested_schema(field_schema, field_location=field_location)

        items = schema.get("items")
        if isinstance(items, dict):
            self._tag_nested_schema(items, field_location=field_location)

    def _enrich_field(
        self,
        name: str,
        schema: dict,
        field_location: str = "",
        field_description: str = "",
    ) -> dict[str, Any]:
        key = self._cache_key(name, schema, field_location, field_description)
        if key in self._mem:
            return copy.deepcopy(self._mem[key])

        semantic_tag, inferred_from = self._classify(
            name=name,
            schema=schema,
            field_location=field_location,
            field_description=field_description,
        )
        constraints = self._infer_constraints(
            name=name,
            schema=schema,
            semantic_tag=semantic_tag,
            field_location=field_location,
            field_description=field_description,
        )
        probe_policy = self._infer_probe_policy(
            schema=schema,
            semantic_tag=semantic_tag,
            constraints=constraints,
        )

        enriched = {
            "semantic_tag": semantic_tag,
            "x_constraints": constraints,
            "x_probe_policy": probe_policy,
            "x_inferred_from": inferred_from,
        }

        self._mem[key] = copy.deepcopy(enriched)
        self._save_file_cache()
        return enriched

    # ──────────────────────────────────────────────────────────────
    # classification
    # ──────────────────────────────────────────────────────────────

    def _classify(
        self,
        name: str,
        schema: dict,
        field_location: str,
        field_description: str,
    ) -> tuple[str, list[str]]:
        # Priority 0: x-semantic-tag 명시값
        explicit_tag = schema.get("x-semantic-tag") or schema.get("x_semantic_tag")
        if explicit_tag and explicit_tag in KNOWN_TAGS:
            normalized = self._normalize_tag(explicit_tag)
            return normalized, ["x-semantic-tag"]

        # Priority 1: Swagger format
        swagger_tag = self._tag_from_swagger_format(name, schema)
        if swagger_tag:
            normalized = self._normalize_tag(swagger_tag)
            return normalized, ["swagger_format"]

        # Priority 2: heuristic
        tag = self._heuristic(name, schema, field_location, field_description)
        normalized = self._normalize_tag(tag)
        return normalized, ["field_name", "description", "example", "format", "type"]

    def _normalize_tag(self, tag: str) -> str:
        # unknown / inactive tag는 전부 plain_string으로 내린다.
        if not tag:
            return "plain_string"
        if tag not in KNOWN_TAGS:
            return "plain_string"
        if tag not in _QFE_ACTIVE_TAGS:
            return "plain_string"
        return tag

    # ──────────────────────────────────────────────────────────────
    # swagger-format tagging
    # ──────────────────────────────────────────────────────────────

    _FORMAT_TAG_MAP: dict[str, str] = {
        # QFE에서 바로 의미를 부여할 수 있는 것은 최소화
        # byte / binary는 아래에서 base64_image / base64_template로 세분화
    }

    def _tag_from_swagger_format(self, name: str, schema: dict) -> str:
        fmt = (schema.get("format") or "").lower()
        n = name.lower()

        if not fmt or fmt not in _QFE_ACTIVE_FORMATS:
            return ""

        # format: byte / binary → base64 계열
        if fmt in {"byte", "binary"}:
            desc_name_blob = f"{schema.get('description', '')} {n}".lower()
            if any(x in desc_name_blob for x in ("template", "feature", "vector", "embedding", "face_data")):
                return "base64_template"
            return "base64_image"

        # format: int32/int64 + ID 패턴
        if fmt in {"int32", "int64"}:
            if re.search(r"(^|_)(user|face|group|template|device|person|subject)_?id$", n):
                return "numeric_id"
            if n.endswith("_id") or n == "id":
                return "numeric_id"

        mapped = self._FORMAT_TAG_MAP.get(fmt)
        if mapped:
            return mapped

        return ""

    # ──────────────────────────────────────────────────────────────
    # heuristic tagging
    # ──────────────────────────────────────────────────────────────

    def _heuristic(
        self,
        name: str,
        schema: dict,
        field_location: str,
        field_description: str,
    ) -> str:
        ftype = schema.get("type", "string")
        enum_vals = schema.get("enum")
        desc = f"{schema.get('description', '')} {field_description}".lower()
        n = name.lower()

        if enum_vals:
            return "enum_mode"

        # path param ID
        if field_location == "path" and re.search(
            r"(^|_)(user|face|group|template|device|person|subject)_?id$", n
        ):
            return "path_user_id" if n in {"user_id", "{user_id}"} else (
                "numeric_id" if ftype == "integer" else "identifier"
            )

        # base64 / image / template
        name_has_blob_signal = any(x in n for x in ("base64", "encoded", "image", "template", "photo", "img"))
        desc_has_blob_signal = any(x in desc for x in ("base64", "encoded", "binary", "bytes", "blob"))

        if ftype == "string" and (name_has_blob_signal or desc_has_blob_signal):
            desc_name = f"{desc} {n}"
            if any(x in desc_name for x in ("template", "feature", "vector", "embedding", "face_data")):
                return "base64_template"
            if any(x in desc_name for x in ("image", "img", "photo", "jpeg", "jpg", "png", "picture", "raw")):
                return "base64_image"

        # id / identifier
        if re.search(r"(^|_)(user|face|group|template|device|person|subject)_?id$", n):
            return "numeric_id" if ftype == "integer" else "identifier"
        if n.endswith("_id") or n == "id":
            return "numeric_id" if ftype == "integer" else "identifier"

        # channel
        if ftype == "integer" and "channel" in n:
            return "channel_count"

        # threshold / score / confidence
        if ftype == "number" and (
            any(x in n for x in ("threshold", "score", "confidence", "ratio", "similarity"))
            or any(x in desc for x in ("threshold", "similarity score", "confidence score"))
        ):
            return "threshold_numeric"

        # boolean
        if ftype == "boolean":
            return "boolean_flag"

        # config-like json string
        if ftype == "string":
            if any(x in n for x in ("json", "config", "setting", "option")):
                return "config_json"

        return "plain_string"

    # ──────────────────────────────────────────────────────────────
    # constraint inference
    # ──────────────────────────────────────────────────────────────

    def _infer_constraints(
        self,
        name: str,
        schema: dict,
        semantic_tag: str,
        field_location: str,
        field_description: str,
    ) -> dict[str, Any]:
        desc = f"{schema.get('description', '')} {field_description}".strip()
        example = schema.get("example")
        ftype = schema.get("type", "string")

        explicit_min = schema.get("minimum")
        explicit_max = schema.get("maximum")
        explicit_min_len = schema.get("minLength")
        explicit_max_len = schema.get("maxLength")
        explicit_pattern = schema.get("pattern")
        enum_vals = schema.get("enum")

        inferred_min = None
        inferred_max = None
        inferred_format = None
        inferred_encoding = None

        if explicit_min is None or explicit_max is None:
            minmax = self._extract_numeric_range(desc)
            if minmax:
                inferred_min, inferred_max = minmax

        if semantic_tag in {"base64_image", "base64_template"}:
            inferred_encoding = "base64"

        # QFE 활성 tag 기준에서는 email/url/uuid/datetime는 plain_string으로 내려오므로
        # 여기서는 실제 활성 semantic만 처리하면 된다.
        if semantic_tag == "threshold_numeric":
            inferred_format = "numeric-threshold"

        return {
            "minimum": explicit_min if explicit_min is not None else inferred_min,
            "maximum": explicit_max if explicit_max is not None else inferred_max,
            "minimum_source": "explicit" if explicit_min is not None else ("description" if inferred_min is not None else None),
            "maximum_source": "explicit" if explicit_max is not None else ("description" if inferred_max is not None else None),
            "min_length": explicit_min_len,
            "max_length": explicit_max_len,
            "pattern": explicit_pattern,
            "enum": enum_vals,
            "format_hint": inferred_format,
            "encoding_hint": inferred_encoding,
            "field_location": field_location,
            "example": example,
            "type": ftype,
            "description_text": desc[:200],
        }

    def _infer_probe_policy(
        self,
        schema: dict,
        semantic_tag: str,
        constraints: dict[str, Any],
    ) -> dict[str, Any]:
        minimum = constraints.get("minimum")
        maximum = constraints.get("maximum")
        min_source = constraints.get("minimum_source")
        max_source = constraints.get("maximum_source")

        has_explicit_range = min_source == "explicit" or max_source == "explicit"
        has_inferred_range = min_source == "description" or max_source == "description"

        if semantic_tag in {"base64_image", "base64_template"}:
            semantic_policy = "must_fail"
        elif semantic_tag == "threshold_numeric":
            semantic_policy = "must_fail" if (has_explicit_range or has_inferred_range) else "probe_only"
        elif semantic_tag in {"channel_count", "numeric_id", "path_user_id"}:
            semantic_policy = "probe_only"
        else:
            semantic_policy = "probe_only"

        return {
            "has_explicit_range": has_explicit_range,
            "has_inferred_range": has_inferred_range,
            "range_policy": "explicit" if has_explicit_range else ("inferred" if has_inferred_range else "none"),
            "semantic_policy": semantic_policy,
            "minimum": minimum,
            "maximum": maximum,
        }

    # ──────────────────────────────────────────────────────────────
    # parsing helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_numeric_range(text: str) -> tuple[float, float] | None:
        if not text:
            return None

        lower = text.lower().strip()

        pm = re.search(r"[±+-]\s*(\d+(?:\.\d+)?)", lower)
        if pm and ("degree" in lower or "angle" in lower or "range" in lower):
            v = float(pm.group(1))
            return (-v, v)

        patterns = [
            r"\(\s*(-?\d+(?:\.\d+)?)\s*[-~]\s*(-?\d+(?:\.\d+)?)\s*\)",
            r"range\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:to|[-~])\s*(-?\d+(?:\.\d+)?)",
            r"between\s*(-?\d+(?:\.\d+)?)\s*and\s*(-?\d+(?:\.\d+)?)",
            r"(-?\d+(?:\.\d+)?)\s*<=\s*[a-z_]+\s*<=\s*(-?\d+(?:\.\d+)?)",
            r"(-?\d+(?:\.\d+)?)\s*(?:to|[-~])\s*(-?\d+(?:\.\d+)?)",
        ]

        for pat in patterns:
            m = re.search(pat, lower)
            if m:
                a = float(m.group(1))
                b = float(m.group(2))
                return (a, b) if a <= b else (b, a)

        return None

    @staticmethod
    def _cache_key(name: str, schema: dict, field_location: str, field_description: str) -> str:
        parts = (
            SEMANTIC_SCHEMA_VERSION,
            name,
            schema.get("type", ""),
            schema.get("format", ""),
            (field_description or "")[:80],
            str(sorted(schema.get("enum", []) or [])),
            str(schema.get("x-semantic-tag") or schema.get("x_semantic_tag") or ""),
        )
        return "|".join(str(p) for p in parts)