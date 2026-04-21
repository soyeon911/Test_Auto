"""
ExperimentRunner — orchestrates Step 1 / 2 / 3 experiments across multiple AI providers.

Step 1 : AI 단독 TC 생성   (rule-based 없음)
Step 2 : Rule기반 TC 수정 + AI 동시 생성, 중복 허용 → 중복 개수 리포트
Step 3 : Rule기반 TC 제외 + AI 동시 생성, AI 중복 제거 후 저장

CLAUDE.md Experiment Mode rules:
  Step 1  — Use AI only to generate test cases.
  Step 2  — Use both. Allow duplicates and report the duplicate count.
  Step 3  — Use both. Exclude any AI TC that duplicates a rule-based TC.

Output layout
  tests/generated/
    step{N}/
      {provider}/
        rule/   test_{op_id}.py   (empty for step 1)
        ai/     test_{op_id}.py

Reports
  reports/experiment_report.json     ← summary per provider
  reports/experiment_tc_report.csv   ← per-TC detail (CLAUDE.md reporting rules)
"""

from __future__ import annotations

import ast
import csv
import json
import sys
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .duplicate_detector import DuplicateDetector
from .llm_client import BaseLLMClient, create_llm_client
from .rule_based_generator import RuleBasedTCGenerator
from .tc_generator import (
    _AI_SYSTEM_API,
    _AI_SYSTEM_PYTHON,
    _AI_USER_TEMPLATE,
    _endpoint_fingerprint,
    _is_valid_python,
    _safe_name,
    _strip_fences,
)


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class EndpointResult:
    """Per-endpoint generation result for a single provider run."""
    operation_id:    str
    endpoint:        str        # "METHOD /path"
    rule_tc_count:   int = 0
    ai_tc_count:     int = 0
    duplicate_count: int = 0
    rule_file:       str = ""
    ai_file:         str = ""
    error:           str = ""


@dataclass
class ProviderResult:
    """Aggregated result for one provider across all endpoints."""
    provider:        str
    model:           str
    step:            int
    endpoint_count:  int
    rule_tc_count:   int   = 0
    ai_tc_count:     int   = 0
    duplicate_count: int   = 0
    output_files:    list[str] = field(default_factory=list)
    errors:          list[str] = field(default_factory=list)
    per_endpoint:    list[EndpointResult] = field(default_factory=list)


@dataclass
class ExperimentReport:
    generated_at:          str
    step:                  int
    results_by_provider:   dict[str, ProviderResult]

    def to_json(self, path: Path) -> None:
        def _ser(obj: Any) -> Any:
            if isinstance(obj, ProviderResult):
                return asdict(obj)
            if isinstance(obj, EndpointResult):
                return asdict(obj)
            raise TypeError(f"Not serializable: {type(obj)}")

        data = {
            "generated_at": self.generated_at,
            "step": self.step,
            "results_by_provider": {
                k: asdict(v) for k, v in self.results_by_provider.items()
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Experiment] Summary report → {path}")

    def print_summary(self) -> None:
        print(f"\n{'='*70}")
        print(f"  Experiment Summary — Step {self.step}")
        print(f"{'='*70}")
        print(f"  {'Provider':<18} {'Model':<28} {'Rule':>6} {'AI':>6} {'Dup':>6}")
        print(f"  {'-'*64}")
        for name, r in self.results_by_provider.items():
            print(
                f"  {r.provider:<18} {r.model:<28} "
                f"{r.rule_tc_count:>6} {r.ai_tc_count:>6} {r.duplicate_count:>6}"
            )
        print(f"{'='*70}\n")


# ─── ExperimentRunner ─────────────────────────────────────────────────────────

class ExperimentRunner:
    """
    Drives Step 1 / 2 / 3 experiments, possibly across multiple AI providers.

    Usage:
        runner = ExperimentRunner(config)
        report = runner.run(endpoints)
        # report.to_json(...)  already called internally
    """

    def __init__(self, config: dict):
        self.config    = config
        exp_cfg        = config.get("experiment", {})
        self.step: int = int(exp_cfg.get("step", 3))
        self.max_extra: int = int(exp_cfg.get("max_extra_tc", 5))

        raw_providers: list[dict] = exp_cfg.get("providers", [])
        if not raw_providers:
            # Fallback: single-provider from agent section
            a = config.get("agent", {})
            raw_providers = [{
                "provider":    a.get("provider", "gemini"),
                "model":       a.get("model", "gemini-2.0-flash"),
                "api_key_env": a.get("api_key_env", "GEMINI_API_KEY"),
                "max_tokens":  int(a.get("max_tokens", 4096)),
            }]
        self.providers_cfg = raw_providers

        self.output_base  = Path(exp_cfg.get("output_base", "./tests/generated"))
        self.report_path  = Path(exp_cfg.get("report_path", "./reports/experiment_report.json"))
        self.tc_report_path = Path(exp_cfg.get("tc_report_path", "./reports/experiment_tc_report.csv"))

        #self._rule_gen    = RuleBasedTCGenerator(config)

    # ── public ────────────────────────────────────────────────────────────────

    def run(self, endpoints: list[dict]) -> ExperimentReport:
        results: dict[str, ProviderResult] = {}
        tc_rows: list[dict] = []          # for CSV report

        for p_cfg in self.providers_cfg:
            provider = p_cfg["provider"]
            print(f"\n[Experiment] ── Step {self.step} | provider={provider} "
                  f"| model={p_cfg.get('model','')} ──")
            result, rows = self._run_provider(endpoints, p_cfg)
            results[provider] = result
            tc_rows.extend(rows)

        report = ExperimentReport(
            generated_at=datetime.now().isoformat(),
            step=self.step,
            results_by_provider=results,
        )
        report.to_json(self.report_path)
        self._write_tc_report(tc_rows)
        report.print_summary()
        return report

    # ── per-provider ──────────────────────────────────────────────────────────

    def _run_provider(
        self,
        endpoints: list[dict],
        p_cfg: dict,
    ) -> tuple[ProviderResult, list[dict]]:
        provider = p_cfg["provider"]
        model    = p_cfg.get("model", "")

        # Build a merged config so create_llm_client can read agent.*
        merged = dict(self.config)
        merged["agent"] = {
            "provider":    provider,
            "model":       model,
            "max_tokens":  int(p_cfg.get("max_tokens", 4096)),
            "api_key_env": p_cfg.get("api_key_env", f"{provider.upper()}_API_KEY"),
        }

        # Output dirs: tests/generated/step{N}/{provider}/rule  &  .../ai
        out_root = self.output_base / f"step{self.step}" / provider
        rule_dir = out_root / "rule"
        ai_dir   = out_root / "ai"
        rule_dir.mkdir(parents=True, exist_ok=True)
        ai_dir.mkdir(parents=True, exist_ok=True)

        # Initialise LLM — skip AI steps if unavailable
        llm: BaseLLMClient | None = None
        try:
            llm = create_llm_client(merged)
        except EnvironmentError as e:
            print(f"[Experiment] {provider}: API key not set — AI generation skipped. ({e})")
        except ImportError as e:
            print(f"[Experiment] {provider}: SDK not installed — AI generation skipped. ({e})")

        pr = ProviderResult(
            provider=provider,
            model=model,
            step=self.step,
            endpoint_count=len(endpoints),
        )
        all_tc_rows: list[dict] = []

        rule_gen = RuleBasedTCGenerator(self.config)   # provider별 새로 생성
        
        for ep in endpoints:
            try:
                ep_result, tc_rows = self._generate_endpoint(ep, llm, rule_gen, rule_dir, ai_dir, provider)
            except Exception as exc:
                op = ep.get("operation_id", "?")
                print(f"[Experiment] {provider}/{op}: {exc}")
                ep_result = EndpointResult(
                    operation_id=ep.get("operation_id", "?"),
                    endpoint=f"{ep.get('method','').upper()} {ep.get('path','')}",
                    error=str(exc),
                )
                tc_rows = []

            pr.rule_tc_count   += ep_result.rule_tc_count
            pr.ai_tc_count     += ep_result.ai_tc_count
            pr.duplicate_count += ep_result.duplicate_count
            if ep_result.rule_file:
                pr.output_files.append(ep_result.rule_file)
            if ep_result.ai_file:
                pr.output_files.append(ep_result.ai_file)
            if ep_result.error:
                pr.errors.append(ep_result.error)
            pr.per_endpoint.append(ep_result)
            all_tc_rows.extend(tc_rows)

        return pr, all_tc_rows

    # ── per-endpoint ──────────────────────────────────────────────────────────

    def _generate_endpoint(
        self,
        endpoint: dict,
        llm: BaseLLMClient | None,
        rule_gen: RuleBasedTCGenerator,
        rule_dir: Path,
        ai_dir: Path,
        provider: str,
    ) -> tuple[EndpointResult, list[dict]]:
        op_id = _safe_name(endpoint.get("operation_id", "unknown"))
        fingerprint = _endpoint_fingerprint(endpoint)
        target_type = endpoint.get("target_type", "api")
        sys_prompt = _AI_SYSTEM_PYTHON if target_type == "python" else _AI_SYSTEM_API

        rule_code: str = ""
        final_rule_code: str = ""
        ai_code: str = ""
        dup_names: set[str] = set()
        duplicate_count: int = 0

        if self.step == 1:
            final_rule_code = ""
            ai_code = self._call_llm(
                llm, sys_prompt, endpoint,
                rule_context="(Step 1: AI-only — no rule-based context)",
                max_extra=self.max_extra * 3,
            )

        elif self.step == 2:
            rule_code = rule_gen.generate(endpoint)

            patch_result = self._call_llm_for_rule_patch(
                llm=llm,
                endpoint=endpoint,
                rule_code=rule_code,
            )

            final_rule_code = patch_result.get("revised_rule_code") or rule_code
            ai_code = patch_result.get("extra_ai_code", "") or ""

            if final_rule_code and ai_code:
                dup_names = set(DuplicateDetector.find_duplicates(final_rule_code, ai_code))
                duplicate_count = len(dup_names)

        elif self.step == 3:
            rule_code = rule_gen.generate(endpoint)
            final_rule_code = rule_code
            ai_raw = self._call_llm(llm, sys_prompt, endpoint, rule_code, self.max_extra)
            if final_rule_code and ai_raw:
                ai_code, duplicate_count = DuplicateDetector.filter_duplicates(final_rule_code, ai_raw)
                if duplicate_count:
                    print(f"[Experiment] Step3 | {provider}/{op_id}: filtered {duplicate_count} duplicate(s)")
            else:
                ai_code = ai_raw

        rule_path = self._write_file(
            rule_dir, op_id, fingerprint, final_rule_code, layer="rule", endpoint=endpoint, provider=provider
        )
        ai_path = self._write_file(
            ai_dir, op_id, fingerprint, ai_code, layer=f"ai-step{self.step}", endpoint=endpoint, provider=provider
        )

        rule_tc = _count_fns(final_rule_code)
        ai_tc = _count_fns(ai_code)

        tc_rows: list[dict] = []
        tc_rows.extend(
            DuplicateDetector.extract_tc_records(final_rule_code, f"{provider}/rule", endpoint)
        )
        tc_rows.extend(
            DuplicateDetector.extract_tc_records(ai_code, f"{provider}/ai-step{self.step}", endpoint, dup_names)
        )
        for row in tc_rows:
            row["provider"] = provider
            row["step"] = self.step

        return (
            EndpointResult(
                operation_id=op_id,
                endpoint=f"{endpoint.get('method','').upper()} {endpoint.get('path','')}",
                rule_tc_count=rule_tc,
                ai_tc_count=ai_tc,
                duplicate_count=duplicate_count,
                rule_file=str(rule_path) if rule_path else "",
                ai_file=str(ai_path) if ai_path else "",
            ),
            tc_rows,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _call_llm_for_rule_patch(
        self,
        llm: BaseLLMClient | None,
        endpoint: dict,
        rule_code: str,
    ) -> dict[str, Any]:
        """
        Step 2 전용:
        - 기존 rule_code를 검토
        - 보완/수정된 revised_rule_code 생성
        - 추가 AI test(extra_ai_code) 생성

        반환 형식:
        {
            "revised_rule_code": str,
            "extra_ai_code": str,
            "change_summary": list[str],
        }
        """
        if llm is None:
            return {
                "revised_rule_code": rule_code,
                "extra_ai_code": "",
                "change_summary": ["LLM unavailable; fallback to original rule_code"],
            }

        system_prompt = textwrap.dedent("""
        You are a senior QA engineer refining rule-based pytest test cases.

        TASK:
        1. Review the existing rule-based test code.
        2. Improve or correct the rule-based tests if needed.
        3. Add a small number of extra AI-generated edge-case tests that are not already covered.

        OUTPUT FORMAT:
        Return JSON only with this schema:
        {
        "revised_rule_code": "<python code>",
        "extra_ai_code": "<python code>",
        "change_summary": ["...", "..."]
        }

        RULES:
        - Return valid JSON only.
        - revised_rule_code and extra_ai_code must each be valid Python code strings.
        - Do not include markdown fences.
        - Preserve existing valid tests unless there is a strong reason to change them.
        - extra_ai_code should contain only additional tests, not duplicates of revised_rule_code.
        """).strip()

        user_prompt = textwrap.dedent(f"""
        ENDPOINT:
        {json.dumps(endpoint, indent=2, ensure_ascii=False)}

        ORIGINAL_RULE_CODE:
        {rule_code}

        Please refine the rule-based code and generate additional edge-case tests.
        Return JSON only.
        """).strip()

        for attempt in range(1, 4):
            try:
                raw = llm.generate(system_prompt, user_prompt).strip()
                data = json.loads(raw)

                revised_rule_code = _strip_fences(data.get("revised_rule_code", "") or "")
                extra_ai_code = _strip_fences(data.get("extra_ai_code", "") or "")
                change_summary = data.get("change_summary", [])

                if not isinstance(change_summary, list):
                    change_summary = [str(change_summary)]

                if revised_rule_code and not _is_valid_python(revised_rule_code):
                    print(f"[Experiment] Rule patch attempt {attempt}: revised_rule_code syntax invalid")
                    revised_rule_code = rule_code

                if extra_ai_code and not _is_valid_python(extra_ai_code):
                    print(f"[Experiment] Rule patch attempt {attempt}: extra_ai_code syntax invalid")
                    extra_ai_code = ""

                return {
                    "revised_rule_code": revised_rule_code or rule_code,
                    "extra_ai_code": extra_ai_code,
                    "change_summary": change_summary,
                }

            except json.JSONDecodeError as exc:
                print(f"[Experiment] Rule patch attempt {attempt}: invalid JSON ({exc})")
            except Exception as exc:
                print(f"[Experiment] Rule patch attempt {attempt}: {exc}")

        return {
            "revised_rule_code": rule_code,
            "extra_ai_code": "",
            "change_summary": ["LLM patch failed; fallback to original rule_code"],
        }
    def _call_llm(
        self,
        llm:         BaseLLMClient | None,
        sys_prompt:  str,
        endpoint:    dict,
        rule_context: str = "",
        max_extra:   int  = 5,
    ) -> str:
        if llm is None:
            return ""

        user_prompt = textwrap.dedent(f"""
        ENDPOINT:
        {json.dumps(endpoint, indent=2, ensure_ascii=False)}

        RULE_CONTEXT:
        {rule_context or "(none)"}

        Generate up to {max_extra} additional edge-case pytest tests.

        Focus:
        - combinational inputs
        - boundary violations
        - semantic edge cases
        - NOT simple missing/wrong-type duplicates

        Return Python code only.
        """).strip()

        for attempt in range(1, 4):
            try:
                raw  = llm.generate(sys_prompt, user_prompt)
                code = _strip_fences(raw)
                if _is_valid_python(code):
                    return code
                print(f"[Experiment] Attempt {attempt}: syntax error — retrying…")
                user_prompt += "\n\nPlease fix all syntax errors in the previous output."
            except Exception as exc:
                print(f"[Experiment] LLM error on attempt {attempt}: {exc}")
                break

        return ""

    @staticmethod
    def _write_file(
        directory: Path,
        op_id: str,
        fingerprint: str,
        code: str,
        layer: str,
        endpoint: dict,
        provider: str,
    ) -> Path | None:
        if not code.strip():
            return None

        header = textwrap.dedent(f"""\
            # Auto-generated by ExperimentRunner [{layer}]
            # provider  : {provider}
            # {endpoint.get('method', '').upper()} {endpoint.get('path', '')}
            # operation : {endpoint.get('operation_id', '')}
            # spec_hash : {fingerprint}
            # ───────────────────────────────────────────────────────
            import json
            import pytest
            import requests
            from tests.helpers.diag import build_diag, attach_diag

        """)

        path = directory / f"test_{provider}_{op_id}.py"
        path.write_text(header + code, encoding="utf-8")
        print(f"[Experiment] ✓ [{layer}] {path}")
        return path

    def _write_tc_report(self, rows: list[dict]) -> None:
        if not rows:
            return

        # CLAUDE.md Reporting Rule columns
        fieldnames = [
            "provider",
            "step",
            "source",
            "endpoint",
            "operation_id",
            "function_name",
            "intent_type",
            "target_field",
            "description",
            "test_case_type",
            "execution_result",
            "failure_classification",
            "is_duplicate",
        ]

        self.tc_report_path.parent.mkdir(parents=True, exist_ok=True)
        with self.tc_report_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        print(f"[Experiment] TC detail report → {self.tc_report_path} ({len(rows)} rows)")


# ─── Utility ──────────────────────────────────────────────────────────────────

def _count_fns(code: str) -> int:
    """Count test_ functions in a code block."""
    if not code.strip():
        return 0
    try:
        tree = ast.parse(code)
        return sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        )
    except SyntaxError:
        return 0
