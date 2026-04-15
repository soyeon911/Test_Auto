from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


KNOWN_TAGS: frozenset[str] = frozenset({
    "plain_string",
    "identifier",
    "base64_image",
    "base64_template",
    "threshold_numeric",
    "enum_mode",
    "config_json",
    "path_user_id",
    "integer_count",
    "channel_count",
    "boolean_flag",
    "datetime_string",
    "email_string",
    "password_string",
    "file_path",
    "url_string",
    "uuid_string",
    "numeric_id",
})


class SchemaEnricher:
    """
    Backward-compatible name, but now acts as a schema enricher.

    It injects:
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

        # 캐시 강제 초기화
        if config.get("tc_generation", {}).get("semantic_tagging", {}).get("reset_cache"):
            self._mem = {}
            if self._cache_path.exists():
                try:
                    self._cache_path.unlink()
                except Exception:
                    pass

    def _load_file_cache(self) -> None:
        """Load full enrich cache from disk (JSON)."""
        if not self._cache_path.exists():
            return
        try:
            with open(self._cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            self._mem[k] = v
        except Exception:
            pass
    
    # 저장 시 semantic tag만 남기고 다시 읽을 때 x_constraints, x_probe_policy가 사라질 수 있는 문제점
    # 한 번 캐시된 field는 다음 실행부터 semantic_tag만 남은 반쪽 데이터로 재사용될 수 있으며 이럴 경우 boundary/policy 생성이 흔들림

    def _save_file_cache(self) -> None:
        """Save full enrich cache to disk (JSON)."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._cache_path, 'w', encoding='utf-8') as f:
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
        """
        Recursively enrich:
          - object.properties
          - array.items
          - array.items.properties
        """
        if not schema:
            return

        # object properties
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

        # array items
        items = schema.get("items")
        if isinstance(items, dict):
            # enrich array item object itself if meaningful
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
        tag = self._heuristic(name, schema, field_location, field_description)
        return tag, ["field_name", "description", "example", "format", "type"]

    # threshold, score, confidence가 있으면 integer도 threshold_float으로 강제
    # channel은 따로 안 잡고 integer_count로 보내버리는 문제

    # channel은 channel_count / threshold는 threshold_numeric으로 분류 -> 일반 number는 무조건 threshold가 아님
    def _heuristic(
        self,
        name: str,
        schema: dict,
        field_location: str,
        field_description: str,
    ) -> str:
        ftype = schema.get("type", "string")
        fmt = schema.get("format", "")
        enum_vals = schema.get("enum")
        desc = f"{schema.get('description', '')} {field_description}".lower()
        example = str(schema.get("example", "")).lower()
        n = name.lower()

        if enum_vals:
            return "enum_mode"

        if field_location == "path" and re.search(r"(^|_)(user|face|group|template|device|person|subject)_?id$", n):
            return "path_user_id" if n in {"user_id", "{user_id}"} else ("numeric_id" if ftype == "integer" else "identifier")

        blob = f"{example} {desc} {n}"

        if any(x in blob for x in ("base64", "encoded", "binary", "bytes", "blob")):
            if any(x in blob for x in ("template", "feature", "vector", "embedding", "face data", "face_data")):
                return "base64_template"
            if any(x in blob for x in ("image", "img", "photo", "jpeg", "jpg", "png", "raw image", "picture")):
                return "base64_image"

        if any(x in blob for x in ("email", "e-mail")):
            return "email_string"

        if any(x in blob for x in ("password", "secret", "token", "passwd", "credential")):
            return "password_string"

        if any(x in blob for x in ("url", "uri", "endpoint", "host", "link")):
            return "url_string"

        if any(x in blob for x in ("path", "dir", "file", "folder", "directory", ".cfg", ".json", ".yaml", ".yml")):
            return "file_path"

        if fmt == "date-time" or any(x in n for x in ("datetime", "timestamp", "date_time")):
            return "datetime_string"

        if fmt == "email":
            return "email_string"

        if fmt == "uuid" or "uuid" in n:
            return "uuid_string"

        if re.search(r"(^|_)(user|face|group|template|device|person|subject)_?id$", n):
            return "numeric_id" if ftype == "integer" else "identifier"

        if n.endswith("_id") or n == "id":
            return "numeric_id" if ftype == "integer" else "identifier"

        # channel은 integer_count와 분리
        if ftype == "integer" and "channel" in n:
            return "channel_count"

        # threshold/score/confidence 등은 숫자 의미 필드로만 분류
        if any(x in blob for x in ("threshold", "score", "confidence", "ratio", "similarity")) and ftype in {"number", "integer"}:
            return "threshold_numeric"

        if ftype == "integer":
            if any(x in n for x in ("width", "height", "count", "limit", "size", "page", "max", "min", "total", "sub_id")):
                return "integer_count"
            return "numeric_id"

        # 일반 number를 threshold로 몰지 않는다
        if ftype == "number":
            return "plain_string"  # 아래에서 별도 처리하지 않으면 fallback
            # 더 안전하게는 "numeric_id" 같은 태그를 새로 만드는 것도 가능

        if ftype == "boolean":
            return "boolean_flag"

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

        if semantic_tag == "email_string":
            inferred_format = "email"
        elif semantic_tag == "datetime_string":
            inferred_format = "date-time"
        elif semantic_tag == "uuid_string":
            inferred_format = "uuid"
        elif semantic_tag == "url_string":
            inferred_format = "uri"

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

        if semantic_tag in {"base64_image", "base64_template", "email_string", "uuid_string", "datetime_string", "url_string"}:
            semantic_policy = "must_fail"
        elif semantic_tag == "threshold_numeric":
            semantic_policy = "must_fail" if (has_explicit_range or has_inferred_range) else "probe_only"
        elif semantic_tag in {"integer_count", "channel_count", "numeric_id"}:
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

        # ±90 degrees → (-90, 90)
        pm = re.search(r"[±+-]\s*(\d+(?:\.\d+)?)", lower)
        if pm and ("degree" in lower or "angle" in lower or "range" in lower):
            v = float(pm.group(1))
            return (-v, v)

        patterns = [
            # (0-100000), (0 ~ 100000)
            r"\(\s*(-?\d+(?:\.\d+)?)\s*[-~]\s*(-?\d+(?:\.\d+)?)\s*\)",
            # range: 0 to 100000 / range 0-100000
            r"range\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:to|[-~])\s*(-?\d+(?:\.\d+)?)",
            # between 0 and 100000
            r"between\s*(-?\d+(?:\.\d+)?)\s*and\s*(-?\d+(?:\.\d+)?)",
            # 0 <= x <= 100000
            r"(-?\d+(?:\.\d+)?)\s*<=\s*[a-z_]+\s*<=\s*(-?\d+(?:\.\d+)?)",
            # generic 0 to 100000 / 0-100000 / 0~100000
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
            name,
            schema.get("type", ""),
            schema.get("format", ""),
            (field_description or "")[:80],
            str(sorted(schema.get("enum", []) or [])),
        )
        return "|".join(str(p) for p in parts)