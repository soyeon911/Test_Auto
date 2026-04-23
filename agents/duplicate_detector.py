"""
DuplicateDetector — semantic duplicate detection between rule-based and AI-generated test sets.

A test case is considered a duplicate when it shares the same (endpoint, intent_type, target)
triple as an already-existing rule-based test.

Detection strategy (ordered):
  1. Exact function-name match    → definite duplicate
  2. Intent-pattern match         → normalised name pattern comparison
  3. Structural match (optional)  → call-site param overlap (heuristic)

Intent normalisation examples
  test_getUserById_positive              → ("getUserById", "positive", "")
  test_getUserById_missing_id            → ("getUserById", "missing_required", "id")
  test_getUserById_missing_body_name     → ("getUserById", "missing_required", "name")
  test_getUserById_wrong_type_age        → ("getUserById", "wrong_type", "age")
  test_getUserById_boundary_age_0        → ("getUserById", "boundary", "age")
  test_getUserById_invalid_enum_status   → ("getUserById", "invalid_enum", "status")
  test_getUserById_none_age              → ("getUserById", "nullable", "age")

CLAUDE.md rule:
  Duplicate detection must consider endpoint, intent, input structure, and expected outcome.
"""

from __future__ import annotations

import ast
import re
from typing import NamedTuple


# --- Intent key --------------------------------------------------------------

class IntentKey(NamedTuple):
    op_id:       str    # normalised operation id fragment
    intent_type: str    # positive | missing_required | wrong_type | boundary | invalid_enum | nullable | unknown
    target:      str    # field / param name the test exercises (empty for "positive")


# Regex that matches standard rule-based function naming conventions
_RULE_PATTERN = re.compile(
    r"^test_(?P<op>.+?)_"
    r"(?:"
    r"(?P<pos>positive)"
    r"|missing_body_(?P<mf2>\w+)"
    r"|missing_(?P<mf1>\w+)"
    r"|wrong_type_body_(?P<wt2>\w+)"
    r"|wrong_type_(?P<wt1>\w+)"
    r"|boundary_body_(?P<bb>\w+?)_\w+$"
    r"|boundary_(?P<bf>\w+?)_\w+$"
    r"|input_val_(?P<iv>\w+?)_\w+$"
    r"|raw_image_relation_(?P<ri>\w+)"
    r"|invalid_enum_(?P<ef>\w+)"
    r"|none_(?P<nf>\w+)"
    r")$"
)


def _parse_intent(func_name: str) -> IntentKey:
    """Parse a test function name into a normalised IntentKey."""
    m = _RULE_PATTERN.match(func_name)
    if not m:
        return IntentKey(op_id=func_name, intent_type="unknown", target="")

    op = m.group("op")
    if m.group("pos"):
        return IntentKey(op, "positive", "")
    if m.group("mf2"):
        return IntentKey(op, "missing_required", m.group("mf2"))
    if m.group("mf1"):
        return IntentKey(op, "missing_required", m.group("mf1"))
    if m.group("wt2"):
        return IntentKey(op, "wrong_type", m.group("wt2"))
    if m.group("wt1"):
        return IntentKey(op, "wrong_type", m.group("wt1"))
    if m.group("bb"):
        return IntentKey(op, "boundary", m.group("bb"))
    if m.group("bf"):
        return IntentKey(op, "boundary", m.group("bf"))
    if m.group("iv"):
        return IntentKey(op, "input_validation", m.group("iv"))
    if m.group("ri"):
        return IntentKey(op, "raw_image_relation", m.group("ri"))
    if m.group("ef"):
        return IntentKey(op, "invalid_enum", m.group("ef"))
    if m.group("nf"):
        return IntentKey(op, "nullable", m.group("nf"))
    return IntentKey(op, "unknown", "")


def _extract_functions(code: str) -> dict[str, ast.FunctionDef]:
    """Parse code and return {func_name: AST node} for all test_ functions."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


# --- Main detector -----------------------------------------------------------

class DuplicateDetector:
    """
    Stateless helper that compares a rule-based code block against an AI-generated
    code block and identifies / filters duplicates.
    """

    @classmethod
    def find_duplicates(cls, rule_code: str, ai_code: str) -> list[str]:
        """
        Return the list of AI function names that are duplicates of rule-based tests.

        A duplicate is:
          (a) exact function name already exists in rule set, OR
          (b) normalised IntentKey matches a rule IntentKey
        """
        rule_funcs  = _extract_functions(rule_code)
        ai_funcs    = _extract_functions(ai_code)

        rule_names   = set(rule_funcs.keys())
        rule_intents = {_parse_intent(n) for n in rule_names}

        duplicates: list[str] = []
        for ai_name in ai_funcs:
            if ai_name in rule_names:
                duplicates.append(ai_name)
                continue
            ai_intent = _parse_intent(ai_name)
            if ai_intent in rule_intents and ai_intent.intent_type != "unknown":
                duplicates.append(ai_name)

        return duplicates

    @classmethod
    def filter_duplicates(cls, rule_code: str, ai_code: str) -> tuple[str, int]:
        """
        Remove duplicate functions from *ai_code*.

        Returns:
          (filtered_code, duplicate_count)

        Step 3 uses this to ensure the merged set has no overlapping coverage.
        """
        dup_names = set(cls.find_duplicates(rule_code, ai_code))
        if not dup_names:
            return ai_code, 0

        try:
            tree = ast.parse(ai_code)
        except SyntaxError:
            return ai_code, 0

        lines = ai_code.splitlines(keepends=True)
        exclude: set[int] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in dup_names:
                continue
            start = (
                node.decorator_list[0].lineno - 1
                if node.decorator_list
                else node.lineno - 1
            )
            end = node.end_lineno
            for i in range(start, end):
                exclude.add(i)

        kept = [ln for i, ln in enumerate(lines) if i not in exclude]
        return "".join(kept).strip(), len(dup_names)

    @classmethod
    def count_by_intent(cls, code: str) -> dict[str, int]:
        """
        Return a count of test functions grouped by intent_type.
        Useful for per-provider TC distribution reporting.
        """
        funcs = _extract_functions(code)
        counts: dict[str, int] = {}
        for name in funcs:
            intent = _parse_intent(name).intent_type
            counts[intent] = counts.get(intent, 0) + 1
        return counts

    @classmethod
    def extract_tc_records(
        cls,
        code: str,
        source: str,
        endpoint: dict,
        dup_names: set[str] | None = None,
    ) -> list[dict]:
        """
        Extract per-TC metadata rows for the detailed CSV report.

        Each row follows CLAUDE.md Reporting Rules:
          - rule name or generation source
          - rule purpose
          - generated test case description
          - test case type
          - execution result  (pending until tests run)
          - failure classification  (pending)
          - duplicate 여부
        """
        funcs = _extract_functions(code)
        dup_names = dup_names or set()
        records = []

        for name, node in funcs.items():
            intent = _parse_intent(name)
            docstring = ast.get_docstring(node) or ""
            records.append({
                "source":                 source,
                "endpoint":               f"{endpoint.get('method','').upper()} {endpoint.get('path','')}",
                "operation_id":           endpoint.get("operation_id", ""),
                "function_name":          name,
                "intent_type":            intent.intent_type,
                "target_field":           intent.target,
                "description":            docstring,
                "test_case_type":         intent.intent_type,
                "execution_result":       "pending",
                "failure_classification": "",
                "is_duplicate":           name in dup_names,
            })

        return records
