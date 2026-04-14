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
    "threshold_float",
    "enum_mode",
    "config_json",
    "path_user_id",
    "integer_count",
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

    def _load_file_cache(self) -> None:
        """Load semantic tagging cache from disk (JSON)."""
        if not self._cache_path.exists():
            return
        try:
            with open(self._cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in data.items():
                    if isinstance(v, str):  # 간단한 태그 형태
                        self._mem[k] = {"semantic_tag": v}
                    else:  # enriched dict 형태
                        self._mem[k] = v
        except Exception:
            pass  # 캐시 로드 실패 무시

    def _save_file_cache(self) -> None:
        """Save semantic tagging cache to disk (JSON)."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 간단한 형식으로 저장: {key: semantic_tag}
            simple_cache = {}
            for k, v in self._mem.items():
                if isinstance(v, dict) and "semantic_tag" in v:
                    simple_cache[k] = v["semantic_tag"]
                else:
                    simple_cache[k] = str(v)
            with open(self._cache_path, 'w', encoding='utf-8') as f:
                json.dump(simple_cache, f, indent=2, ensure_ascii=False)
        except Exception:
            pass  # 캐시 저장 실패 무시

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

        # enum always wins
        if enum_vals:
            return "enum_mode"

        # path id only for true user_id-like fields
        if field_location == "path" and n in {"user_id", "{user_id}"}:
            return "path_user_id"

        blob = f"{example} {desc} {n}"

        # base64 / binary-like detection
        if any(x in blob for x in ("base64", "encoded", "binary", "bytes", "blob")):
            if any(x in blob for x in ("template", "feature", "vector", "embedding", "face data", "face_data")):
                return "base64_template"
            if any(x in blob for x in ("image", "img", "photo", "jpeg", "jpg", "png", "raw image", "picture")):
                return "base64_image"

        # semantic numeric threshold / score
        if any(x in blob for x in ("threshold", "score", "confidence", "ratio", "similarity")) and ftype in {"number", "integer"}:
            return "threshold_float"

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

        if ftype == "integer":
            if any(x in n for x in ("width", "height", "channel", "count", "limit", "size", "page", "max", "min", "total", "sub_id")):
                return "integer_count"
            return "numeric_id"

        if ftype == "number":
            return "threshold_float"

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
        elif semantic_tag == "threshold_float":
            # range is especially meaningful here
            semantic_policy = "must_fail" if (has_explicit_range or has_inferred_range) else "probe_only"
        elif semantic_tag in {"integer_count", "numeric_id"}:
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