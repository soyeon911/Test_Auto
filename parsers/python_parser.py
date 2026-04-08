"""
Python function signature parser using the `inspect` module.

Use this when the target is a Python module (not a web API with Swagger).

Output matches the same EndpointInfo shape as OpenAPIParser so the TC
generator can treat both sources uniformly:
  {
    "path": "module.function_name",
    "method": "call",
    "operation_id": "function_name",
    "summary": docstring first line,
    "parameters": [
        {"name": "x", "in": "arg", "required": True, "schema": {"type": "integer"}}
    ],
    "request_body": None,
    "responses": {"return": {"description": return annotation, "schema": {}}},
  }
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Callable, get_type_hints


class PythonFunctionParser:
    def __init__(self, target: str):
        """
        Args:
            target: either
                - a dotted module path:  "myapp.api.users"
                - a file path:           "./src/api/users.py"
        """
        self.target = target
        self._module = None

    # ─── public API ──────────────────────────────────────────────────────────

    def load(self) -> "PythonFunctionParser":
        """Import the target module."""
        if self.target.endswith(".py") or "/" in self.target or "\\" in self.target:
            self._module = self._load_from_file(self.target)
        else:
            self._module = importlib.import_module(self.target)
        return self

    def parse(self) -> list[dict[str, Any]]:
        """Return endpoint descriptors for every public function in the module."""
        if self._module is None:
            raise RuntimeError("Call .load() first.")

        results = []
        for name, obj in inspect.getmembers(self._module, inspect.isfunction):
            if name.startswith("_"):
                continue
            results.append(self._parse_function(name, obj))
        return results

    def parse_function(self, func: Callable) -> dict[str, Any]:
        """Parse a single function object directly."""
        return self._parse_function(func.__name__, func)

    # ─── internal helpers ─────────────────────────────────────────────────────

    def _parse_function(self, name: str, func: Callable) -> dict[str, Any]:
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""

        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}

        parameters = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            annotation = hints.get(param_name, inspect.Parameter.empty)
            type_name = self._annotation_to_type(annotation)
            has_default = param.default is not inspect.Parameter.empty

            parameters.append({
                "name": param_name,
                "in": "arg",
                "required": not has_default,
                "description": "",
                "schema": {"type": type_name},
                "default": None if has_default is False else (
                    param.default if param.default is not inspect.Parameter.empty else None
                ),
            })

        return_annotation = hints.get("return", inspect.Parameter.empty)
        return_type = self._annotation_to_type(return_annotation)

        module_name = getattr(self._module, "__name__", "unknown") if self._module else "unknown"

        return {
            "path": f"{module_name}.{name}",
            "method": "call",
            "operation_id": name,
            "summary": doc.split("\n")[0] if doc else name,
            "description": doc,
            "tags": [module_name],
            "parameters": parameters,
            "request_body": None,
            "responses": {
                "return": {
                    "description": return_type,
                    "schema": {"type": return_type},
                }
            },
        }

    @staticmethod
    def _annotation_to_type(annotation: Any) -> str:
        if annotation is inspect.Parameter.empty:
            return "any"
        mapping = {
            int: "integer",
            float: "number",
            str: "string",
            bool: "boolean",
            list: "array",
            dict: "object",
            None: "null",
            type(None): "null",
        }
        return mapping.get(annotation, str(annotation))

    @staticmethod
    def _load_from_file(file_path: str):
        p = Path(file_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        spec = importlib.util.spec_from_file_location(p.stem, p)
        module = importlib.util.module_from_spec(spec)
        sys.modules[p.stem] = module
        spec.loader.exec_module(module)
        return module


# ─── CLI helper ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import pprint

    target = sys.argv[1] if len(sys.argv) > 1 else "os.path"
    parser = PythonFunctionParser(target).load()
    endpoints = parser.parse()
    print(f"Parsed {len(endpoints)} function(s) from '{target}'\n")
    pprint.pprint(endpoints[:3])
