"""
OpenAPI / Swagger parser.

Supports:
  - Local file: YAML or JSON  (openapi 3.x, swagger 2.x)
  - Remote URL: fetches and parses on the fly

Output is a list of EndpointInfo dicts:
  {
    "path": "/users/{id}",
    "method": "get",
    "operation_id": "getUser",
    "summary": "...",
    "tags": ["users"],
    "parameters": [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ],
    "request_body": None | {"content_type": "application/json", "schema": {...}},
    "responses": {
        "200": {"description": "OK", "schema": {...}},
        "404": {"description": "Not found", "schema": None},
    },
  }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
import yaml


class OpenAPIParser:
    def __init__(self, source: str):
        """
        Args:
            source: local file path OR http(s) URL to the spec.
        """
        self.source = source
        self._raw: dict[str, Any] = {}
        self._spec_version: str = ""

    # ─── public API ──────────────────────────────────────────────────────────

    def load(self) -> "OpenAPIParser":
        """Load the spec from file or URL, return self for chaining."""
        if self.source.startswith(("http://", "https://")):
            self._raw = self._fetch_url(self.source)
        else:
            self._raw = self._read_file(self.source)

        self._spec_version = self._detect_version(self._raw)
        return self

    def parse(self) -> list[dict[str, Any]]:
        """Return a structured list of endpoint descriptors."""
        if not self._raw:
            raise RuntimeError("Call .load() first.")

        endpoints: list[dict[str, Any]] = []
        paths: dict = self._raw.get("paths", {})
        _HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            # TODO-3: path-level parameters (shared across all operations on this path)
            path_level_params: list = path_item.get("parameters", [])

            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS:
                    continue
                if not isinstance(operation, dict):
                    continue

                # Merge path-level params with operation-level params.
                # Operation-level takes precedence (same name+in wins).
                op_params: list = operation.get("parameters", [])
                merged = self._merge_parameters(path_level_params, op_params)
                operation = {**operation, "parameters": merged}

                endpoints.append(self._parse_operation(path, method, operation))

        return endpoints

    # ─── internal helpers ─────────────────────────────────────────────────────

    def _parse_operation(self, path: str, method: str, op: dict) -> dict[str, Any]:
        raw_params = op.get("parameters", [])

        # ── Swagger 2.0: pull out in:body parameter before resolving ────────────
        # (OpenAPI 3.x uses requestBody; Swagger 2.0 puts the body inside parameters)
        swagger_body: dict | None = None
        non_body_raw: list = []
        for p in raw_params:
            resolved_p = self._resolve_ref(p["$ref"]) if "$ref" in p else p
            if resolved_p.get("in") == "body":
                swagger_body = resolved_p
            else:
                non_body_raw.append(p)

        parameters = self._resolve_parameters(non_body_raw)

        # ── request_body: prefer OpenAPI 3.x requestBody, fall back to Swagger body param
        request_body = self._parse_request_body(op.get("requestBody"))
        if request_body is None and swagger_body is not None:
            request_body = self._parse_swagger_body_param(swagger_body)

        responses = self._parse_responses(op.get("responses", {}))

        return {
            "path": path,
            "method": method.lower(),
            "operation_id": op.get("operationId", f"{method}_{path}"),
            "summary": op.get("summary", ""),
            "description": op.get("description", ""),
            "tags": op.get("tags", []),
            "parameters": parameters,
            "request_body": request_body,
            "responses": responses,
        }

    def _merge_parameters(self, path_params: list, op_params: list) -> list:
        """
        Merge path-level and operation-level parameter lists.
        Operation-level entries override path-level entries with the same (name, in) key.
        """
        # Resolve $refs first so we can key on name+in
        resolved_path = [self._resolve_ref(p["$ref"]) if "$ref" in p else p for p in path_params]
        resolved_op   = [self._resolve_ref(p["$ref"]) if "$ref" in p else p for p in op_params]

        merged: dict[tuple, dict] = {}
        for p in resolved_path:
            key = (p.get("name", ""), p.get("in", ""))
            merged[key] = p
        for p in resolved_op:          # op wins on collision
            key = (p.get("name", ""), p.get("in", ""))
            merged[key] = p
        return list(merged.values())

    def _resolve_parameters(self, params: list) -> list[dict]:
        resolved = []
        for p in params:
            if "$ref" in p:
                p = self._resolve_ref(p["$ref"])
            resolved.append({
                "name": p.get("name", ""),
                "in": p.get("in", "query"),     # path | query | header | cookie
                "required": p.get("required", False),
                "description": p.get("description", ""),
                "schema": p.get("schema", {}),
            })
        return resolved

    def _parse_request_body(self, body: dict | None) -> dict | None:
        """OpenAPI 3.x requestBody → normalised dict."""
        if not body:
            return None
        content = body.get("content", {})
        for content_type, media in content.items():
            schema = media.get("schema", {})
            if "$ref" in schema:
                schema = self._resolve_ref(schema["$ref"])
            return {
                "content_type": content_type,
                "schema": schema,
                "required": body.get("required", False),
            }
        return None

    def _parse_swagger_body_param(self, param: dict) -> dict | None:
        """
        Swagger 2.0: parameters[].in == 'body'  →  same normalised dict as
        _parse_request_body so the rest of the pipeline is format-agnostic.

        The parameter schema may be:
          - a $ref  → resolve to the definition object
          - an inline object schema  → use as-is
          - a primitive / missing  → wrap in {"type": "object", "properties": {}}

        Nested $refs inside properties are also resolved one level deep.
        """
        schema = param.get("schema", {})

        # top-level $ref
        if "$ref" in schema:
            schema = self._resolve_ref(schema["$ref"])
            # deep-copy so we don't mutate the raw spec
            import copy
            schema = copy.deepcopy(schema)

        # resolve $refs inside properties (one level)
        for prop_name, prop_schema in schema.get("properties", {}).items():
            if "$ref" in prop_schema:
                schema["properties"][prop_name] = self._resolve_ref(prop_schema["$ref"])

        if not schema:
            return None

        return {
            "content_type": "application/json",
            "schema":       schema,
            "required":     param.get("required", False),
        }

    def _parse_responses(self, responses: dict) -> dict[str, dict]:
        result = {}
        for status_code, resp in responses.items():
            if "$ref" in resp:
                resp = self._resolve_ref(resp["$ref"])
            schema = None
            content = resp.get("content", {})
            for _, media in content.items():
                raw_schema = media.get("schema", {})
                if "$ref" in raw_schema:
                    raw_schema = self._resolve_ref(raw_schema["$ref"])
                schema = raw_schema
                break
            result[str(status_code)] = {
                "description": resp.get("description", ""),
                "schema": schema,
            }
        return result

    def _resolve_ref(self, ref: str) -> dict:
        """Simple $ref resolver for local definitions (#/components/schemas/Foo)."""
        if not ref.startswith("#/"):
            return {}
        parts = ref.lstrip("#/").split("/")
        node = self._raw
        for part in parts:
            node = node.get(part, {})
        return node

    @staticmethod
    def _detect_version(raw: dict) -> str:
        if "openapi" in raw:
            return f"openapi-{raw['openapi']}"
        if "swagger" in raw:
            return f"swagger-{raw['swagger']}"
        return "unknown"

    @staticmethod
    def _fetch_url(url: str) -> dict:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        text = resp.text
        if "yaml" in content_type or url.endswith((".yaml", ".yml")):
            return yaml.safe_load(text)
        return json.loads(text)

    @staticmethod
    def _read_file(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Spec file not found: {path}")
        text = p.read_text(encoding="utf-8")
        if p.suffix in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        return json.loads(text)


# ─── CLI helper ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import pprint

    source = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/openapi.json"
    parser = OpenAPIParser(source).load()
    endpoints = parser.parse()
    print(f"Parsed {len(endpoints)} endpoint(s) from {source}\n")
    pprint.pprint(endpoints[:3])
