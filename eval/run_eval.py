"""Run the golden questions against a research agent's POST /research (SSE) and score the traces.

    python eval/run_eval.py --url http://127.0.0.1:18004 --hermetic          # harness/protocol check (fake LLM stack)
    EVAL_ALLOW_SPEND=1 python eval/run_eval.py --url ... --live --max-cases 5 # REAL providers: spends money

The agent URL and key must point at an agent you started yourself. Live mode refuses to run unless EVAL_ALLOW_SPEND=1 is set
and a case limit is given, because every case makes paid LLM calls (and possibly paid search calls).
Token counts and cost are NOT visible through the agent's SSE, so this tool does not report cost; read it from the provider dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring  # noqa: E402

HERE = Path(__file__).resolve().parent


def parse_sse(lines) -> list[tuple[str, object]]:
    events, name, data = [], None, []
    for line in lines:
        if line == "":
            if name is not None:
                payload = "\n".join(data)
                try:
                    events.append((name, json.loads(payload)))
                except ValueError:
                    events.append((name, payload))
            name, data = None, []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    return events


def run_case(url: str, key: str, case: dict, timeout: float) -> dict:
    t0 = time.monotonic()
    first = None
    lines: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as c:
        with c.stream("POST", f"{url.rstrip('/')}/research", json={"query": case["query"]}, headers={"X-Internal-Token": key}) as r:
            if r.status_code != 200:
                return {"events": [("error", {"content": f"HTTP {r.status_code}"}), ("done", {})], "latency_s": time.monotonic() - t0, "first_checkpoint_s": None}
            for line in r.iter_lines():
                if first is None and line.startswith("event: checkpoint"):
                    first = round(time.monotonic() - t0, 2)
                lines.append(line)
    return {"events": parse_sse(lines), "latency_s": time.monotonic() - t0, "first_checkpoint_s": first}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-env", default="AGENT_API_KEY", help="environment variable holding the agent's X-Internal-Token")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hermetic", action="store_true", help="only cases the fake LLM can route (plumbing check, not quality)")
    mode.add_argument("--live", action="store_true", help="the whole set against REAL providers (spends money)")
    ap.add_argument("--cases", default="golden.json", help="case file in eval/ (golden.json | quality_set.json)")
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--out", default=str(HERE / "results"))
    a = ap.parse_args()

    if a.live and (os.environ.get("EVAL_ALLOW_SPEND") != "1" or not a.max_cases):
        print("refusing: --live spends money. Set EVAL_ALLOW_SPEND=1 and pass --max-cases N (start with 3-5).", file=sys.stderr)
        return 2
    key = os.environ.get(a.key_env, "")
    if not key:
        print(f"environment variable {a.key_env} is not set", file=sys.stderr)
        return 2

    cases = json.loads((HERE / a.cases).read_text(encoding="utf-8"))["cases"]
    if a.hermetic:
        cases = [c for c in cases if c.get("hermetic")]
    if a.max_cases:
        cases = cases[: a.max_cases]

    results = []
    for case in cases:
        try:
            trace = run_case(a.url, key, case, a.timeout)
        except httpx.HTTPError as e:
            trace = {"events": [("error", {"content": f"transport error: {type(e).__name__}"}), ("done", {})], "latency_s": 0.0, "first_checkpoint_s": None}
        r = scoring.score_case(case, trace)
        results.append(r)
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<24} {r['latency_s']:>6.1f}s  tools={r['tools']}  blocks={r['blocks']}")
        for k, v in r["checks"].items():
            if not v["ok"]:
                print(f"        - {k}: {v['detail']}")

    summary = scoring.summarise(results)
    report = {"mode": "hermetic" if a.hermetic else "live", "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "summary": summary, "results": results}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval-{report['mode']}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{summary['passed']}/{summary['cases']} cases passed | latency p50 {summary['latency_s']['p50']}s p95 {summary['latency_s']['p95']}s")
    print("per-check pass rate:", summary["per_check_pass_rate"])
    if summary["manual_review_pending"]:
        print("needs human review:", summary["manual_review_pending"])
    print("report:", path)
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
