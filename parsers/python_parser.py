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
        {
          "name": "x",
          "in": "arg",
          "required": True,
          "nullable": False,       # True when Optional[T] or Union[T, None]
          "description": "...",    # from docstring Args: section
          "schema": {
            "type": "integer",
            "enum": [...],         # present when Literal[...] or enum.Enum subclass
          },
          "default": None,
        }
    ],
    "request_body": None,
    "responses": {"return": {"description": return_type, "schema": {"type": ...}}},
  }
"""

from __future__ import annotations

import enum
import importlib
import importlib.util
import inspect
import re
import sys
import typing
from pathlib import Path
from typing import Any, Callable, get_type_hints


# ─── type resolution helpers ──────────────────────────────────────────────────

_PRIMITIVE_MAP: dict[Any, str] = {
    int:        "integer",
    float:      "number",
    str:        "string",
    bool:       "boolean",
    list:       "array",
    dict:       "object",
    bytes:      "string",   # treat as string for test-gen purposes
    None:       "null",
    type(None): "null",
}


def _resolve_annotation(annotation: Any) -> dict:
    """
    Convert a Python type annotation to a schema dict.

    Returns e.g.:
      {"type": "integer"}
      {"type": "string", "nullable": True}
      {"type": "string", "enum": ["a", "b"]}
      {"type": "array", "items": {"type": "string"}}
    """
    if annotation is inspect.Parameter.empty:
        return {"type": "any"}

    origin = getattr(annotation, "__origin__", None)
    args   = getattr(annotation, "__args__", ()) or ()

    # Optional[T]  →  Union[T, None]
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        nullable = type(None) in args
        if len(non_none) == 1:
            schema = _resolve_annotation(non_none[0])
            schema["nullable"] = nullable
            return schema
        # Union[X, Y, ...] — use first non-None type
        schema = _resolve_annotation(non_none[0]) if non_none else {"type": "any"}
        schema["nullable"] = nullable
        schema["union_types"] = [_resolve_annotation(a)["type"] for a in non_none]
        return schema

    # Literal["a", "b", ...]
    if origin is typing.Literal:
        literal_vals = list(args)
        base_type = _primitive_type(type(literal_vals[0])) if literal_vals else "string"
        return {"type": base_type, "enum": literal_vals}

    # list[T] / List[T]
    if origin in (list, typing.List):
        item_schema = _resolve_annotation(args[0]) if args else {"type": "any"}
        return {"type": "array", "items": item_schema}

    # dict[K, V] / Dict[K, V]
    if origin in (dict, typing.Dict):
        return {"type": "object"}

    # Primitive types
    if annotation in _PRIMITIVE_MAP:
        return {"type": _PRIMITIVE_MAP[annotation]}

    # enum.Enum subclasses
    if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
        values = [m.value for m in annotation]
        base_type = _primitive_type(type(values[0])) if values else "string"
        return {"type": base_type, "enum": values}

    return {"type": str(annotation)}


def _primitive_type(py_type: type) -> str:
    return _PRIMITIVE_MAP.get(py_type, "string")


# ─── docstring parser (Google / NumPy / reStructuredText) ────────────────────

def _parse_docstring_args(doc: str) -> dict[str, str]:
    """
    Extract per-argument descriptions from a docstring.
    Supports Google-style ('Args:') and NumPy-style ('Parameters\n----------').

    Returns:  {"param_name": "description text"}
    """
    if not doc:
        return {}

    descriptions: dict[str, str] = {}

    # Google-style: 'Args:\n    name (type): description'
    google_section = re.search(
        r"(?:Args|Arguments|Parameters)\s*:\s*\n((?:[ \t]+\S.*\n?)*)",
        doc,
        re.IGNORECASE,
    )
    if google_section:
        for line in google_section.group(1).splitlines():
            m = re.match(r"\s+(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)", line)
            if m:
                descriptions[m.group(1)] = m.group(2).strip()

    # NumPy-style: 'param_name : type\n    description'
    for m in re.finditer(
        r"^(\w+)\s*:\s*\S.*\n\s{4}(.+)",
        doc,
        re.MULTILINE,
    ):
        descriptions.setdefault(m.group(1), m.group(2).strip())

    # reST-style: ':param name: description'
    for m in re.finditer(r":param\s+(\w+)\s*:\s*(.+)", doc):
        descriptions.setdefault(m.group(1), m.group(2).strip())

    return descriptions


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
            hints = get_type_hints(func, include_extras=True)
        except Exception:
            hints = {}

        arg_descriptions = _parse_docstring_args(doc)

        parameters = []
        for param_name, param in sig.parameters.items():
            if param_name in {"self", "cls"}:
                continue

            annotation = hints.get(param_name, inspect.Parameter.empty)
            schema     = _resolve_annotation(annotation)
            nullable   = schema.pop("nullable", False)
            has_default = param.default is not inspect.Parameter.empty
            default_val = param.default if has_default else None

            parameters.append({
                "name":        param_name,
                "in":          "arg",
                "required":    not has_default,
                "nullable":    nullable,
                "description": arg_descriptions.get(param_name, ""),
                "schema":      schema,
                "default":     default_val,
            })

        return_annotation = hints.get("return", inspect.Parameter.empty)
        return_schema     = _resolve_annotation(return_annotation)
        module_name       = getattr(self._module, "__name__", "unknown") if self._module else "unknown"

        return {
            "path":         f"{module_name}.{name}",
            "method":       "call",
            "operation_id": name,
            "summary":      doc.split("\n")[0] if doc else name,
            "description":  doc,
            "tags":         [module_name],
            "parameters":   parameters,
            "request_body": None,
            "responses": {
                "return": {
                    "description": return_schema.get("type", "any"),
                    "schema":      return_schema,
                }
            },
        }

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
