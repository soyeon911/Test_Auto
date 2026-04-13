"""
SemanticTagger — AI-powered field classifier.

For each parameter / body-field in an endpoint, the tagger asks the LLM
to assign exactly one semantic tag that describes the *meaning* of the
field, not just its JSON Schema type.

These tags are injected into the schema dict under the key "semantic_tag".
The downstream RuleBasedTCGenerator reads this key to pick tag-appropriate
probe values instead of generic ones.

Supported tags
--------------
  plain_string     generic text, no special encoding
  identifier       string ID referencing a resource (not a UUID)
  base64_image     base64-encoded image bytes (JPEG, PNG …)
  base64_template  base64-encoded binary: face template, model, etc.
  threshold_float  numeric threshold / confidence score (0.0–1.0 typical)
  enum_mode        value that must come from a declared enum
  config_json      JSON-encoded configuration / options object
  path_user_id     user/entity ID embedded in a URL path segment
  integer_count    count, limit, page number, size
  boolean_flag     true / false toggle
  datetime_string  ISO 8601 / RFC 3339 date-time string
  email_string     e-mail address
  password_string  password or secret token
  file_path        filesystem path
  url_string       HTTP/HTTPS URL
  uuid_string      UUID-format string
  numeric_id       integer identifier (user_id, face_id, group_id …)

If the LLM is unavailable, heuristic rules provide a best-effort tag
so the pipeline degrades gracefully without failing.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


# ─── Tag registry ─────────────────────────────────────────────────────────────

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


# ─── AI prompts ───────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are a semantic field classifier for API test case generation.

Given ONE field/parameter from an API specification, return EXACTLY ONE
semantic tag from the list below. Nothing else — no punctuation, no
explanation.

Tags:
  plain_string     — generic text with no special encoding or format
  identifier       — string that references a resource by name/key (not a UUID, not an integer PK)
  base64_image     — base64-encoded image data (JPEG, PNG, etc.)
  base64_template  — base64-encoded binary blob: face template, model, vector, etc.
  threshold_float  — numeric threshold, confidence score, ratio (0.0–1.0 range typical)
  enum_mode        — value that MUST come from a fixed enum / allowed-values list
  config_json      — JSON-encoded configuration or options object stored as a string
  path_user_id     — user / entity ID appearing in a URL path segment  ({userId}, etc.)
  integer_count    — count, limit, size, page number, max/min quantity
  boolean_flag     — true / false toggle
  datetime_string  — ISO 8601 or RFC 3339 date-time string
  email_string     — e-mail address
  password_string  — password, secret, or auth token
  file_path        — filesystem path
  url_string       — HTTP / HTTPS URL
  uuid_string      — UUID-format string (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
  numeric_id       — integer identifier (user_id, face_id, group_id, template_id, …)

Priority rules:
  • If enum values are declared → always return enum_mode
  • If description or name strongly suggests base64 binary → base64_image or base64_template
  • integer type + name ends in _id → numeric_id
  • When unsure → plain_string
""".strip()

_USER_TEMPLATE = (
    "name       : {name}\n"
    "type       : {ftype}\n"
    "format     : {fmt}\n"
    "description: {desc}\n"
    "enum_values: {enum_vals}"
)


# ─── Main class ───────────────────────────────────────────────────────────────

class SemanticTagger:
    """
    Classify every parameter and body field of an endpoint with a semantic tag.

    Parameters
    ----------
    config      : full pipeline config dict (reads ``tc_generation.semantic_tagging``)
    llm_client  : optional pre-built BaseLLMClient; if None, heuristics are used
    """

    def __init__(self, config: dict, llm_client=None):
        self._llm  = llm_client
        self._mem:  dict[str, str] = {}          # runtime cache
        self._cache_path = Path(
            config.get("tc_generation", {})
                  .get("semantic_tagging", {})
                  .get("cache_file", ".cache/semantic_tags.json")
        )
        self._load_file_cache()

    # ─── public ──────────────────────────────────────────────────────────────

    def tag_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        """
        Return a deep copy of *endpoint* with ``semantic_tag`` injected into
        every parameter schema and every body-field schema.
        """
        ep = copy.deepcopy(endpoint)

        # ── parameters ────────────────────────────────────────────────────────
        for p in ep.get("parameters", []):
            schema = p.setdefault("schema", {})
            if "semantic_tag" not in schema:
                schema["semantic_tag"] = self._tag_field(
                    name=p.get("name", ""),
                    schema=schema,
                )

        # ── request body fields ────────────────────────────────────────────────
        # OpenAPIParser normalises request_body to {"schema": {...}, ...}
        # (no "content" nesting). Support both formats for robustness.
        req_body = ep.get("request_body")
        if req_body:
            # normalised format: req_body["schema"]["properties"]
            body_schema = req_body.get("schema") or {}
            self._tag_properties(body_schema.get("properties", {}))

            # raw OpenAPI 3.x format (fallback): req_body["content"][ct]["schema"]
            for media_obj in req_body.get("content", {}).values():
                raw_schema = media_obj.get("schema") or {}
                self._tag_properties(raw_schema.get("properties", {}))

        return ep

    # ─── internal ─────────────────────────────────────────────────────────────

    def _tag_properties(self, properties: dict) -> None:
        """In-place: inject semantic_tag into each property schema that lacks one."""
        for field_name, field_schema in properties.items():
            if "semantic_tag" not in field_schema:
                field_schema["semantic_tag"] = self._tag_field(
                    name=field_name,
                    schema=field_schema,
                )
            # recurse into nested objects
            nested = field_schema.get("properties", {})
            if nested:
                self._tag_properties(nested)

    def _tag_field(self, name: str, schema: dict) -> str:
        key = self._cache_key(name, schema)
        if key in self._mem:
            return self._mem[key]

        tag = self._classify(name, schema)
        self._mem[key] = tag
        self._save_file_cache()
        return tag

    def _classify(self, name: str, schema: dict) -> str:
        """Try AI; fall back to heuristic."""
        if self._llm is not None:
            try:
                raw = self._llm.generate(
                    _SYSTEM_PROMPT,
                    _USER_TEMPLATE.format(
                        name=name,
                        ftype=schema.get("type", "string"),
                        fmt=schema.get("format", ""),
                        desc=(schema.get("description") or "")[:200],
                        enum_vals=schema.get("enum") or "none",
                    ),
                )
                tag = re.sub(r"[^a-z_]", "", raw.strip().lower())
                if tag in KNOWN_TAGS:
                    print(f"[SemanticTagger] {name!r:30s} → {tag}  (AI)")
                    return tag
                print(f"[SemanticTagger] AI returned unknown tag {tag!r} for {name!r} — using heuristic")
            except Exception as exc:
                print(f"[SemanticTagger] AI classify failed for {name!r}: {exc}")

        tag = self._heuristic(name, schema)
        print(f"[SemanticTagger] {name!r:30s} → {tag}  (heuristic)")
        return tag

    # ─── heuristic classifier ──────────────────────────────────────────────────

    def _heuristic(self, name: str, schema: dict) -> str:
        ftype     = schema.get("type", "string")
        fmt       = schema.get("format", "")
        enum_vals = schema.get("enum")
        desc      = (schema.get("description") or "").lower()
        example   = str(schema.get("example", "")).lower()
        n         = name.lower()

        # Enum always wins
        if enum_vals:
            return "enum_mode"

        # ── Example value analysis (before other heuristics) ──────────────────
        # If schema has an example value that hints at the real type, use it
        if example:
            if "base64" in example:
                if any(x in example for x in ("template", "model", "face")):
                    return "base64_template"
                if any(x in example for x in ("image", "img", "photo", "png", "jpeg")):
                    return "base64_image"
                # generic base64 → could be either; lean on name/desc
                if any(x in n or x in desc for x in ("template", "model", "face_data")):
                    return "base64_template"
                return "base64_image"
            if "uuid" in example or re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-.*", example):
                return "uuid_string"
            if "@" in example or "example.com" in example:
                return "email_string"
            if example.startswith("http://") or example.startswith("https://"):
                return "url_string"
            if "{" in example and "}" in example and ":" in example:
                return "config_json"

        # Format hints
        if fmt == "date-time" or any(x in n for x in ("datetime", "date_time", "timestamp")):
            return "datetime_string"
        if fmt == "email" or "email" in n:
            return "email_string"
        if fmt in {"password", "secret"} or any(x in n for x in ("password", "passwd", "secret")):
            return "password_string"
        if fmt == "uuid" or re.fullmatch(r".*uuid.*", n):
            return "uuid_string"
        if fmt in {"binary", "byte"} or any(
            x in n or x in desc
            for x in ("base64", "image", "img", "photo", "picture", "jpeg", "png")
        ):
            if any(x in n or x in desc for x in ("template", "model", "face_data", "facedata", "vector")):
                return "base64_template"
            return "base64_image"

        # Semantic name hints
        if any(x in n for x in ("threshold", "score", "confidence", "ratio", "rate", "weight")):
            return "threshold_float"
        if any(x in n for x in ("url", "uri", "endpoint", "host", "link")):
            return "url_string"
        if any(x in n for x in ("path", "dir", "file", "folder", "directory")):
            return "file_path"
        if "json" in n or "config" in n or "setting" in n or "option" in n:
            return "config_json"

        # ID patterns
        if re.search(r"(^|_)(user|face|group|template|device|person|subject)_?id$", n):
            return "numeric_id" if ftype == "integer" else "identifier"
        if n.endswith("_id") or n in {"id", "user_id", "face_id"}:
            return "numeric_id" if ftype == "integer" else "identifier"

        # Path param heuristic
        if re.search(r"\{.*id.*\}", n):
            return "path_user_id"

        # Type-based fallback
        if ftype == "integer":
            # image/video dimension keywords → integer_count (not an ID)
            if any(x in n for x in ("width", "height", "channel", "depth",
                                    "count", "limit", "size", "page",
                                    "num", "max", "min", "total")):
                return "integer_count"
            return "numeric_id"
        if ftype == "number":
            return "threshold_float"
        if ftype == "boolean":
            return "boolean_flag"
        if ftype == "integer":
            if any(x in n for x in ("mode", "operation", "type", "kind")):
                return "enum_mode"
            
        return "plain_string"

    # ─── cache helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(name: str, schema: dict) -> str:
        parts = (
            name,
            schema.get("type", ""),
            schema.get("format", ""),
            (schema.get("description") or "")[:80],
            str(sorted(schema["enum"]) if schema.get("enum") else []),
        )
        return "|".join(parts)

    def _load_file_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._mem = json.loads(
                    self._cache_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._mem = {}

    def _save_file_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._mem, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[SemanticTagger] Cache save failed: {exc}")
