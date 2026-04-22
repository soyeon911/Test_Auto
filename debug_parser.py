"""
Quick diagnostic: parse an OpenAPI / Swagger spec, run semantic tagging,
and show how many rule-based TC functions would be generated per endpoint.

Useful for verifying parser / tagger / generator changes without needing
the target server running.

Usage:
    python debug_parser.py [path/to/spec.json]        # heuristic tagging
    python debug_parser.py [path/to/spec.json] --ai   # AI tagging (needs API key)
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from parsers import OpenAPIParser
from agents.rule_based_generator import RuleBasedTCGenerator
from agents.schema_enricher import SemanticTagger


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_primary_media_type(req_body: dict | None) -> str | None:
    if not req_body:
        return None
    content = req_body.get("content", {})
    if not content:
        return None
    for mt in ("application/json", "multipart/form-data",
               "application/x-www-form-urlencoded"):
        if mt in content:
            return mt
    return next(iter(content.keys()), None)


def _get_body_schema(req_body: dict | None) -> dict[str, Any]:
    if not req_body:
        return {}
    mt = _get_primary_media_type(req_body)
    if not mt:
        return {}
    return req_body.get("content", {}).get(mt, {}).get("schema", {}) or {}


def _extract_tc_names(code: str) -> list[str]:
    return [
        line.strip().split("(")[0].replace("def ", "")
        for line in code.splitlines()
        if line.strip().startswith("def test_")
    ]


def _guess_rule(tc_name: str) -> str:
    tokens = tc_name.split("_")
    if "positive" in tokens:                    return "positive"
    if "missing" in tokens:                     return "missing_required"
    if "wrong" in tokens and "type" in tokens:  return "wrong_type"
    if "boundary" in tokens:                    return "boundary"
    if "invalid" in tokens and "enum" in tokens: return "invalid_enum"
    return "other"


def _schema_summary(schema: dict[str, Any]) -> str:
    if not schema:
        return "none"
    parts = [f"type={schema.get('type', 'unknown')}"]
    if schema.get("properties"):
        parts.append(f"properties={len(schema['properties'])}")
    if schema.get("required"):
        parts.append(f"required={len(schema['required'])}")
    if schema.get("enum"):
        parts.append(f"enum={len(schema['enum'])}")
    return ", ".join(parts)


# ─── Main analysis ─────────────────────────────────────────────────────────────

def analyse(source: str, use_ai: bool = False) -> None:
    print(f"\nParsing: {source}")
    parser = OpenAPIParser(source).load()
    endpoints = parser.parse()
    print(f"Total endpoints parsed: {len(endpoints)}\n")

    # Build tagger (heuristic or AI)
    llm_client = None
    if use_ai:
        try:
            from agents.llm_client import create_llm_client
            from config_loader import load_config  # type: ignore[import]
            cfg = load_config()
            llm_client = create_llm_client(cfg)
        except Exception as e:
            print(f"[AI tagger] Could not create LLM client: {e}")
            print("[AI tagger] Falling back to heuristic tagger.\n")

    tagger = SemanticTagger({}, llm_client=llm_client)
    gen    = RuleBasedTCGenerator({})

    total_tc = 0
    overall_rules: Counter[str] = Counter()
    failed: list[tuple[str, str, str]] = []

    for ep in endpoints:
        ep.setdefault("target_type", "api")
        method = ep.get("method", "get").upper()
        path   = ep.get("path", "/")

        # ── Semantic tagging ───────────────────────────────────────────────────
        tagged_ep = tagger.tag_endpoint(ep)

        # Collect tag annotations for display
        param_tags   = {
            p["name"]: p.get("schema", {}).get("semantic_tag", "—")
            for p in tagged_ep.get("parameters", [])
        }
        rb   = tagged_ep.get("request_body")
        body_schema = _get_body_schema(rb)
        body_tags = {
            fname: fschema.get("semantic_tag", "—")
            for fname, fschema in body_schema.get("properties", {}).items()
        }

        # ── TC generation ──────────────────────────────────────────────────────
        try:
            code     = gen.generate(tagged_ep)
            tc_names = _extract_tc_names(code)
        except Exception as exc:
            failed.append((method, path, repr(exc)))
            print(f"  [{method:6s}] {path}")
            print(f"           ERROR: {exc!r}\n")
            continue

        total_tc += len(tc_names)
        rule_counts = Counter(_guess_rule(n) for n in tc_names)
        overall_rules.update(rule_counts)

        # ── Output ─────────────────────────────────────────────────────────────
        n_params  = len(tagged_ep.get("parameters", []))
        n_req     = sum(1 for p in tagged_ep.get("parameters", []) if p.get("required"))
        n_path    = sum(1 for p in tagged_ep.get("parameters", []) if p.get("in") == "path")
        n_query   = sum(1 for p in tagged_ep.get("parameters", []) if p.get("in") == "query")
        n_header  = sum(1 for p in tagged_ep.get("parameters", []) if p.get("in") == "header")

        print(f"  [{method:6s}] {path}")
        print(
            f"           params: total={n_params}, required={n_req}, "
            f"path={n_path}, query={n_query}, header={n_header}"
        )
        if param_tags:
            for pname, ptag in param_tags.items():
                print(f"             param  {pname!r:30s} → {ptag}")
        if rb:
            mt = _get_primary_media_type(rb)
            print(
                f"           request_body: yes — media={mt}, "
                f"schema=({_schema_summary(body_schema)})"
            )
            for fname, ftag in body_tags.items():
                print(f"             field  {fname!r:30s} → {ftag}")
        else:
            print("           request_body: None")

        rule_str = ", ".join(f"{r}={c}" for r, c in sorted(rule_counts.items())) or "none"
        print(f"           TCs generated: {len(tc_names)}  rules: [{rule_str}]")
        for n in tc_names:
            print(f"             - {n}")
        print()

    print("=" * 72)
    print(f"Total endpoints : {len(endpoints)}")
    print(f"Total TCs       : {total_tc}")
    if overall_rules:
        print("Rule distribution:")
        for rule, cnt in sorted(overall_rules.items()):
            print(f"  {rule}: {cnt}")
    if failed:
        print("\nEndpoints with errors:")
        for m, p, e in failed:
            print(f"  [{m}] {p}: {e}")


if __name__ == "__main__":
    args = sys.argv[1:]
    ai_flag  = "--ai" in args
    src_args = [a for a in args if not a.startswith("--")]
    source   = src_args[0] if src_args else "input/QFEapi.json"
    analyse(source, use_ai=ai_flag)
