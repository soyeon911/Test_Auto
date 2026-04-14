"""
Swagger 2.0 parser only.

Purpose
-------
This parser is dedicated to Swagger 2.0 specs and normalizes them into the
endpoint structure expected by the downstream pipeline, especially
SemanticTagger.tag_endpoint().

Normalized endpoint shape
-------------------------
{
    "path": "/api/v2/detect",
    "method": "post",
    "operation_id": "post_/api/v2/detect",
    "summary": "...",
    "description": "...",
    "tags": ["Algorithm"],
    "parameters": [
        {
            "name": "user_id",
            "in": "path",
            "required": True,
            "description": "User ID",
            "schema": {"type": "integer", ...}
        }
    ],
    "request_body": {
        "content_type": "application/json",
        "required": True,
        "description": "Base64 encoded image data",
        "schema": {
            "type": "object",
            "properties": {
                "image_data": {
                    "type": "string",
                    "example": "base64_encoded_image",
                    "description": "Base64 encoded image data"
                }
            },
            "required": [...]
        }
    } | None,
    "responses": {
        "200": {
            "description": "...",
            "schema": {...} | None
        }
    }
}
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import requests
import yaml


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


class APIParser:
    def __init__(self, source: str):
        self.source = source
        self._raw: dict[str, Any] = {}

    # ──────────────────────────────────────────────────────────────
    # public API
    # ──────────────────────────────────────────────────────────────

    def load(self) -> "APIParser":
        if self.source.startswith(("http://", "https://")):
            self._raw = self._fetch_url(self.source)
        else:
            self._raw = self._read_file(self.source)

        self._validate_swagger_2()
        return self

    def parse(self) -> list[dict[str, Any]]:
        if not self._raw:
            raise RuntimeError("Call .load() first.")

        endpoints: list[dict[str, Any]] = []
        paths: dict[str, Any] = self._raw.get("paths", {})

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            path_level_params = path_item.get("parameters", [])

            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS:
                    continue
                if not isinstance(operation, dict):
                    continue

                op_params = operation.get("parameters", [])
                merged_params = self._merge_parameters(path_level_params, op_params)
                normalized_op = {**operation, "parameters": merged_params}
                endpoints.append(self._parse_operation(path, method, normalized_op))

        return endpoints

    # ──────────────────────────────────────────────────────────────
    # validation
    # ──────────────────────────────────────────────────────────────

    def _validate_swagger_2(self) -> None:
        version = str(self._raw.get("swagger", "")).strip()
        if version != "2.0":
            raise ValueError(
                f"This parser only supports Swagger 2.0. "
                f"Detected swagger={version!r}"
            )

    # ──────────────────────────────────────────────────────────────
    # operation parsing
    # ──────────────────────────────────────────────────────────────

    def _parse_operation(self, path: str, method: str, op: dict[str, Any]) -> dict[str, Any]:
        raw_params = op.get("parameters", [])

        swagger_body: dict[str, Any] | None = None
        non_body_params: list[dict[str, Any]] = []

        for raw_param in raw_params:
            resolved_param = self._resolve_ref_obj(raw_param)
            if resolved_param.get("in") == "body":
                swagger_body = resolved_param
            else:
                non_body_params.append(resolved_param)

        parameters = self._resolve_parameters(non_body_params)
        request_body = self._parse_swagger_body_param(swagger_body)
        responses = self._parse_responses(op.get("responses", {}))

        return {
            "path": path,
            "method": method.lower(),
            "operation_id": op.get("operationId", f"{method.lower()}_{path}"),
            "summary": op.get("summary", ""),
            "description": op.get("description", ""),
            "tags": op.get("tags", []),
            "parameters": parameters,
            "request_body": request_body,
            "responses": responses,
        }

    def _merge_parameters(
        self,
        path_params: list[dict[str, Any]],
        op_params: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resolved_path = [self._resolve_ref_obj(p) for p in path_params]
        resolved_op = [self._resolve_ref_obj(p) for p in op_params]

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for p in resolved_path:
            key = (p.get("name", ""), p.get("in", ""))
            merged[key] = p
        for p in resolved_op:
            key = (p.get("name", ""), p.get("in", ""))
            merged[key] = p

        return list(merged.values())

    def _resolve_parameters(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []

        for p in params:
            p = self._resolve_ref_obj(p)

            schema = p.get("schema")
            if not schema:
                # Swagger 2.0 non-body params often define type directly on the param
                schema = {
                    "type": p.get("type", "string")
                }
                if "format" in p:
                    schema["format"] = p["format"]
                if "enum" in p:
                    schema["enum"] = p["enum"]
                if "minimum" in p:
                    schema["minimum"] = p["minimum"]
                if "maximum" in p:
                    schema["maximum"] = p["maximum"]
                if "minLength" in p:
                    schema["minLength"] = p["minLength"]
                if "maxLength" in p:
                    schema["maxLength"] = p["maxLength"]
                if "pattern" in p:
                    schema["pattern"] = p["pattern"]
                if "items" in p:
                    schema["items"] = self._resolve_schema(p["items"])

            schema = self._resolve_schema(schema)

            resolved.append({
                "name": p.get("name", ""),
                "in": p.get("in", "query"),
                "required": p.get("required", False),
                "description": p.get("description", ""),
                "schema": schema,
            })

        return resolved

    def _parse_swagger_body_param(self, param: dict[str, Any] | None) -> dict[str, Any] | None:
        if not param:
            return None

        schema = self._resolve_schema(param.get("schema", {}))
        if not schema:
            return None

        body_description = param.get("description", "")

        # Important: propagate body-param description down into fields when they lack descriptions.
        # This helps semantic_tagger infer meaning from Swagger 2.0 specs where the field itself
        # is bare but the body parameter description is informative.
        schema = self._propagate_body_description(schema, body_description)

        return {
            "content_type": "application/json",
            "schema": schema,
            "required": param.get("required", False),
            "description": body_description,
            "name": param.get("name", ""),
        }

    def _parse_responses(self, responses: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        for status_code, resp in responses.items():
            resp = self._resolve_ref_obj(resp)

            schema = None
            if "schema" in resp and resp["schema"]:
                schema = self._resolve_schema(resp["schema"])

            result[str(status_code)] = {
                "description": resp.get("description", ""),
                "schema": schema,
            }

        return result

    # ──────────────────────────────────────────────────────────────
    # schema resolution
    # ──────────────────────────────────────────────────────────────

    def _resolve_ref_obj(self, obj: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obj, dict):
            return obj
        if "$ref" in obj:
            return self._resolve_ref(obj["$ref"])
        return obj

    def _resolve_schema(self, schema: Any) -> Any:
        """
        Recursively resolve Swagger 2.0 schema objects.

        Supports:
          - top-level $ref
          - nested properties
          - array items
          - allOf
        """
        if not isinstance(schema, dict):
            return schema

        if "$ref" in schema:
            resolved = copy.deepcopy(self._resolve_ref(schema["$ref"]))
            return self._resolve_schema(resolved)

        resolved = copy.deepcopy(schema)

        if "properties" in resolved and isinstance(resolved["properties"], dict):
            new_props: dict[str, Any] = {}
            for name, prop_schema in resolved["properties"].items():
                new_props[name] = self._resolve_schema(prop_schema)
            resolved["properties"] = new_props

        if "items" in resolved:
            resolved["items"] = self._resolve_schema(resolved["items"])

        if "allOf" in resolved and isinstance(resolved["allOf"], list):
            merged: dict[str, Any] = {}
            required: list[str] = []

            for part in resolved["allOf"]:
                part_resolved = self._resolve_schema(part)
                if not isinstance(part_resolved, dict):
                    continue

                for k, v in part_resolved.items():
                    if k == "properties":
                        merged.setdefault("properties", {})
                        merged["properties"].update(v)
                    elif k == "required":
                        required.extend(v)
                    else:
                        merged[k] = v

            if required:
                merged["required"] = sorted(set(required))

            resolved.pop("allOf", None)
            resolved.update(merged)

        return resolved

    def _propagate_body_description(self, schema: dict[str, Any], body_description: str) -> dict[str, Any]:
        """
        If a Swagger 2.0 body parameter has a useful description but the referenced model
        fields do not, copy that description into leaf properties conservatively.

        This is especially useful for specs like:
          body param description = "Base64 encoded image data"
          schema property        = image_data: {type: string, example: ...}

        We only fill missing descriptions. Existing field descriptions win.
        """
        if not body_description:
            return schema

        schema = copy.deepcopy(schema)

        props = schema.get("properties", {})
        if not isinstance(props, dict):
            return schema

        for field_name, field_schema in props.items():
            if not isinstance(field_schema, dict):
                continue

            if "description" not in field_schema or not field_schema.get("description"):
                field_schema["description"] = body_description

            # recurse
            if "properties" in field_schema:
                props[field_name] = self._propagate_body_description(field_schema, body_description)
            elif "items" in field_schema and isinstance(field_schema["items"], dict):
                field_schema["items"] = self._propagate_body_description(field_schema["items"], body_description)

        schema["properties"] = props
        return schema

    def _resolve_ref(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            return {}
        node: Any = self._raw
        for part in ref.lstrip("#/").split("/"):
            if not isinstance(node, dict):
                return {}
            node = node.get(part, {})
        return node if isinstance(node, dict) else {}

    # ──────────────────────────────────────────────────────────────
    # input loading
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_url(url: str) -> dict[str, Any]:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        text = resp.text

        if "yaml" in content_type or url.endswith((".yaml", ".yml")):
            return yaml.safe_load(text)
        return json.loads(text)

    @staticmethod
    def _read_file(path: str) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Spec file not found: {path}")

        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        return json.loads(text)


if __name__ == "__main__":
    import pprint
    import sys

    source = sys.argv[1] if len(sys.argv) > 1 else "swagger.json"
    parser = APIParser(source).load()
    endpoints = parser.parse()
    print(f"Parsed {len(endpoints)} endpoint(s) from {source}\n")
    pprint.pprint(endpoints[:3])