"""Run the full prompt pipeline with Claude and report cost, time and outcome.

    pip install -r requirements.txt anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python run_claude.py examples/request.json                 # one request
    python run_claude.py my_cases/ --out results/              # every *.json in a folder
    python run_claude.py examples/request.json --assess-model claude-haiku-5-5

Outputs per request (in --out): <run_id>.result.json, <run_id>.resume.md, <run_id>.usage.json,
plus summary.csv across all requests.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import traceback
from pathlib import Path

from app.claude_adapter import ClaudeAdapter
from app.runner import load_json, run_pipeline


def run_one(path: Path, out: Path, args, client) -> dict:
    request = load_json(path)
    adapter = ClaudeAdapter(client=client, force_tool=not args.no_force_tool)
    if args.assess_model:
        adapter.stage_models["ASSESS"] = args.assess_model
    if args.normalize_model:
        adapter.stage_models["NORMALIZE"] = args.normalize_model
    run_id = request.get("run_id", path.stem)
    row = {"file": path.name, "run_id": run_id}
    try:
        result = run_pipeline(request, adapter)
        (out / f"{run_id}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if result["resume_markdown"]:
            (out / f"{run_id}.resume.md").write_text(result["resume_markdown"])
        a = result["assessment"]
        row.update(status=result["delivery_status"],
                   bullets=sum(len(r["bullets"]) for r in result["draft"]["roles"]),
                   open_findings=sum(1 for f in a["findings"] if not f["resolved"]),
                   blockers=sum(1 for f in a["findings"] if not f["resolved"] and f["severity"] == "BLOCKER"),
                   gaps=len(result["draft"]["gaps"]))
    except Exception as e:  # contract failures are expected outcomes worth recording
        row.update(status="FAILED", error=f"{type(e).__name__}: {e}")
        traceback.print_exc()
    report = adapter.usage_report()
    (out / f"{run_id}.usage.json").write_text(json.dumps(report, indent=2))
    t = report["total"]
    row.update(calls=len(report["calls"]), seconds=t["seconds"], cost_usd=t["cost_usd"],
               input_tokens=t["input"], output_tokens=t["output"],
               cache_read_tokens=t["cache_read"], cache_write_tokens=t["cache_write"])
    print(f"{path.name}: {row.get('status')}  ${t['cost_usd']:.3f}  {t['seconds']}s  "
          f"({len(report['calls'])} calls)")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="request JSON file or folder of them")
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--assess-model", help="e.g. claude-haiku-5-5 to test a cheaper assessor")
    ap.add_argument("--normalize-model")
    ap.add_argument("--no-force-tool", action="store_true",
                    help="use tool_choice=auto if forced tool use is rejected")
    args = ap.parse_args()

    import anthropic
    client = anthropic.Anthropic()
    args.out.mkdir(parents=True, exist_ok=True)
    files = (sorted(p for p in args.input.glob("*.json") if p.name != "INDEX.json")
             if args.input.is_dir() else [args.input])
    rows = [run_one(f, args.out, args, client) for f in files]

    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open(args.out / "summary.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    ok = [r for r in rows if r.get("status") != "FAILED"]
    if ok:
        print(f"\nAverage per resume: ${sum(r['cost_usd'] for r in ok)/len(ok):.3f}, "
              f"{sum(r['seconds'] for r in ok)/len(ok):.0f}s across {len(ok)} run(s)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
