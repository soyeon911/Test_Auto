#!/usr/bin/env python3
"""
Build static dashboard JSON from pytest-json-report output.

Input  : reports/report.json or a GitHub Actions artifact zip containing report.json
Output : web/data/latest.json
         web/data/runs.json
         web/data/runs/<run_id>/summary.json
         web/data/runs/<run_id>/failures.json
         web/data/runs/<run_id>/tests.json

Usage:
  python scripts/build_dashboard_data.py --input reports/report.json --out web/data
  python scripts/build_dashboard_data.py --input test-reports.zip --out web/data
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def load_report(input_path: Path) -> Dict[str, Any]:
    if input_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(input_path) as zf:
            candidates = [n for n in zf.namelist() if n.endswith("report.json")]
            if not candidates:
                raise FileNotFoundError("report.json not found in zip artifact")
            # Prefer root-level report.json if present.
            name = "report.json" if "report.json" in candidates else candidates[0]
            return json.loads(zf.read(name).decode("utf-8"))
    return json.loads(input_path.read_text(encoding="utf-8"))


def parse_user_properties(test: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for item in test.get("user_properties") or []:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def safe_percent(n: int, d: int) -> float:
    return round((n / d * 100.0), 2) if d else 0.0


def norm_endpoint(method: str, path: str) -> str:
    method = method or "UNKNOWN"
    path = path or "UNKNOWN"
    return f"{method} {path}"


def shorten(text: Any, limit: int = 500) -> str:
    s = "" if text is None else str(text)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def extract_failure_message(test: Dict[str, Any]) -> str:
    call = test.get("call") or {}
    longrepr = call.get("longrepr")
    if isinstance(longrepr, dict):
        return shorten(longrepr.get("message") or longrepr, 900)
    return shorten(longrepr, 900)




def sanitize_for_display(value: Any, key: str = "") -> Any:
    """Return a dashboard-safe, compact representation of request input data."""
    key_l = (key or "").lower()
    if isinstance(value, dict):
        return {str(k): sanitize_for_display(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        clipped = [sanitize_for_display(v, key) for v in value[:20]]
        if len(value) > 20:
            clipped.append(f"... ({len(value) - 20} more)")
        return clipped
    if isinstance(value, str):
        looks_payload = any(tok in key_l for tok in ("image", "template", "base64", "data"))
        if looks_payload and len(value) > 80:
            return f"<payload len={len(value)} prefix={value[:24]}...>"
        if len(value) > 250:
            return value[:247] + "..."
        return value
    return value


def json_compact(value: Any, limit: int = 1200) -> str:
    if value is None or value == "":
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def sanitize_condition(text: Any, limit: int = 260) -> str:
    """Compact long probe condition text for dashboard cells."""
    s = shorten(text, 2000)

    def repl(m: re.Match[str]) -> str:
        quote = m.group(1)
        value = m.group(2)
        if len(value) <= 48:
            return m.group(0)
        return f"value={quote}<payload len={len(value)} prefix={value[:18]}...>{quote}"

    s = re.sub(r"value=([\"'])(.*?)(\1)", lambda m: repl(m), s)
    return shorten(s, limit)


def build_input_view(meta: Dict[str, Any], diag: Dict[str, Any]) -> Dict[str, str]:
    """Build structured input data for dashboard display.

    Keep only body, target, and condition. The endpoint path is already shown in
    a separate table column, and including it again makes the failure table noisy.
    """
    body = meta.get("request_body")
    if body in (None, ""):
        body = diag.get("request_body")

    target = diag.get("target_field") or meta.get("target_param") or ""
    condition = meta.get("condition") or diag.get("test_condition") or ""

    body_text = json_compact(sanitize_for_display(body), 1200) if body not in (None, {}, "") else "-"
    return {
        "body": body_text,
        "target": target or "-",
        "condition": sanitize_condition(condition) if condition else "-",
    }


def build_input_data(meta: Dict[str, Any], diag: Dict[str, Any]) -> str:
    """Backward-compatible plain text form of input_view."""
    view = build_input_view(meta, diag)
    return "\n".join([
        f"body: {view['body']}",
        f"target: {view['target']}",
        f"condition: {view['condition']}",
    ])

def build_dashboard(report: Dict[str, Any], run_id: str | None = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    created_ts = float(report.get("created") or 0)
    created_dt = datetime.fromtimestamp(created_ts) if created_ts else datetime.now()
    if not run_id:
        run_id = created_dt.strftime("%Y-%m-%d_%H-%M-%S")

    tests = report.get("tests") or []
    total = int(report.get("summary", {}).get("total") or len(tests))
    passed = int(report.get("summary", {}).get("passed") or 0)
    failed = int(report.get("summary", {}).get("failed") or 0)
    skipped = int(report.get("summary", {}).get("skipped") or 0)
    xfailed = int(report.get("summary", {}).get("xfailed") or 0)
    xpassed = int(report.get("summary", {}).get("xpassed") or 0)
    error = int(report.get("summary", {}).get("error") or 0)

    outcome_c = Counter()
    axis_c = Counter()
    reason_c = Counter()
    rule_c = Counter()
    policy_c = Counter()
    method_c = Counter()
    http_c = Counter()
    error_code_c = Counter()
    endpoint_total_c = Counter()
    endpoint_failed_c = Counter()

    test_rows: List[Dict[str, Any]] = []
    failure_rows: List[Dict[str, Any]] = []

    for t in tests:
        props = parse_user_properties(t)
        meta = props.get("tc_meta") or {}
        diag = props.get("diag") or {}

        outcome = t.get("outcome") or "unknown"
        method = meta.get("request_method") or diag.get("request_method") or ""
        path = meta.get("request_path") or diag.get("request_path") or ""
        endpoint = norm_endpoint(method, path)
        axis = diag.get("axis") or meta.get("expected_error_family") or "unknown"
        reason = diag.get("reason_code") or "unknown"
        rule_type = meta.get("rule_type") or "unknown"
        policy = meta.get("policy") or "unknown"
        actual_status = diag.get("actual_status")
        response_error_code = diag.get("response_error_code")

        outcome_c[outcome] += 1
        axis_c[axis] += 1
        reason_c[reason] += 1
        rule_c[rule_type] += 1
        policy_c[policy] += 1
        method_c[method or "UNKNOWN"] += 1
        http_c[str(actual_status) if actual_status is not None else "unknown"] += 1
        error_code_c[str(response_error_code) if response_error_code is not None else "unknown"] += 1
        endpoint_total_c[endpoint] += 1

        row = {
            "nodeid": t.get("nodeid"),
            "lineno": t.get("lineno"),
            "outcome": outcome,
            "duration_sec": round(float((t.get("call") or {}).get("duration") or 0), 4),
            "method": method,
            "path": path,
            "endpoint": endpoint,
            "rule_type": rule_type,
            "rule_subtype": meta.get("rule_subtype") or "",
            "policy": policy,
            "axis": axis,
            "reason_code": reason,
            "target_field": diag.get("target_field") or meta.get("target_param") or "",
            "condition": meta.get("condition") or diag.get("test_condition") or "",
            "input_view": build_input_view(meta, diag),
            "input_data": build_input_data(meta, diag),
            "expected_http": meta.get("expected_http") or diag.get("expected_http") or "",
            "actual_status": actual_status,
            "response_success": diag.get("response_success"),
            "response_error_code": response_error_code,
            "response_snippet": shorten(diag.get("response_snippet"), 700),
        }
        test_rows.append(row)

        if outcome not in ("passed", "skipped"):
            endpoint_failed_c[endpoint] += 1
            failure = dict(row)
            failure["failure_message"] = extract_failure_message(t)
            failure_rows.append(failure)

    top_failed_endpoints = [
        {
            "endpoint": endpoint,
            "failed": count,
            "total": endpoint_total_c[endpoint],
            "fail_rate": safe_percent(count, endpoint_total_c[endpoint]),
        }
        for endpoint, count in endpoint_failed_c.most_common(20)
    ]

    summary = {
        "run_id": run_id,
        "created_at": created_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": created_ts,
        "duration_sec": round(float(report.get("duration") or 0), 3),
        "exitcode": report.get("exitcode"),
        "root": report.get("root"),
        "total": total,
        "collected": int(report.get("summary", {}).get("collected") or total),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "error": error,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "pass_rate": safe_percent(passed, total),
        "fail_rate": safe_percent(failed, total),
        "outcomes": dict(outcome_c),
        "by_axis": dict(axis_c.most_common()),
        "by_reason": dict(reason_c.most_common()),
        "by_rule_type": dict(rule_c.most_common()),
        "by_policy": dict(policy_c.most_common()),
        "by_method": dict(method_c.most_common()),
        "by_http_status": dict(http_c.most_common()),
        "by_error_code": dict(error_code_c.most_common(30)),
        "top_failed_endpoints": top_failed_endpoints,
        "report_links": {
            "summary": f"data/runs/{run_id}/summary.json",
            "failures": f"data/runs/{run_id}/failures.json",
            "tests": f"data/runs/{run_id}/tests.json",
        },
    }
    return summary, failure_rows, test_rows


def read_runs_index(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="pytest report.json or GitHub Actions artifact zip")
    ap.add_argument("--out", default="web/data", help="dashboard data output directory")
    ap.add_argument("--run-id", default=None, help="optional run id")
    args = ap.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = load_report(input_path)
    summary, failures, tests = build_dashboard(report, args.run_id)
    run_id = summary["run_id"]

    run_dir = out_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "tests.json").write_text(json.dumps(tests, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    runs_path = out_dir / "runs.json"
    runs = read_runs_index(runs_path)
    runs = [r for r in runs if r.get("run_id") != run_id]
    runs.insert(0, {
        "run_id": run_id,
        "created_at": summary["created_at"],
        "total": summary["total"],
        "passed": summary["passed"],
        "failed": summary["failed"],
        "pass_rate": summary["pass_rate"],
        "duration_sec": summary["duration_sec"],
        "exitcode": summary["exitcode"],
    })
    runs = runs[:100]
    runs_path.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[dashboard] run_id={run_id}")
    print(f"[dashboard] latest={out_dir / 'latest.json'}")
    print(f"[dashboard] failures={len(failures)} tests={len(tests)}")


if __name__ == "__main__":
    main()
