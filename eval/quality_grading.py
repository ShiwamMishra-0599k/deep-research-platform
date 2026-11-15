"""Answer-quality grading for the real-provider evaluation (quality_set.json).

    python eval/quality_grading.py sheet  --results eval/results/<run>.json [--out grades.json]
    python eval/quality_grading.py report --grades grades.json [--results eval/results/<run>.json]

`sheet` turns a run into a blank grade sheet: for each case the answer text, its source URLs, the key facts to look for and the
forbidden claims. A HUMAN (or an LLM judge, whose use is a separate paid step) fills in the fields. `report` aggregates the filled
sheet. Nothing in this file calls a model or the network.

Fields graded per case (see docs/EVALUATION_PLAN.md for the exact definitions):
  key_facts_hit        list[bool], one per key_fact (null = not graded yet)
  asserted_forbidden   int, how many must_not_claim statements the answer asserted
  claims_total / claims_unsupported   ints, factual claims in the answer and how many no cited source supports
  premise_handled      bool|null  (false_premise and unanswerable cases only: corrected the premise / declined to invent)
  calibrated           bool|null  (conflicting cases only: presented both sides, no false certainty)
  citations            [{"url", "resolves": bool, "supports": bool, "tier": 1|2|3}]   1 primary/official, 2 reputable secondary, 3 weak/unknown
  relevance            0 | 1 | 2   (off-topic / partly / answers the question asked)
  grader               "human" or "llm:<model>"
"""
import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scoring  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion. With a few dozen cases this is wide, and that is the honest answer."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(max(0.0, (c - h) / d), 3), round(min(1.0, (c + h) / d), 3))


def prop(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}


def make_sheet(results: list[dict], cases: dict[str, dict]) -> list[dict]:
    sheet = []
    for r in results:
        c = cases.get(r["id"], {})
        kf = c.get("key_facts", [])
        sheet.append({
            "id": r["id"], "kind": c.get("kind"), "query": c.get("query"),
            "reference_status": c.get("reference_status"), "reference_source": c.get("reference_source"),
            "recorded_reference": None if c.get("reference_status") == "record_at_run_time" else "n/a",
            "key_facts": kf, "must_not_claim": c.get("must_not_claim", []), "grading_note": c.get("grading_note"),
            "answer_text": r.get("answer_text", ""), "source_urls": r.get("source_urls", []),
            "latency_s": r.get("latency_s"), "terminal_ok": r.get("passed"),
            "key_facts_hit": [None] * len(kf), "asserted_forbidden": None, "claims_total": None, "claims_unsupported": None,
            "premise_handled": None, "calibrated": None,
            "citations": [{"url": u, "resolves": None, "supports": None, "tier": None} for u in r.get("source_urls", [])],
            "relevance": None, "grader": None,
        })
    return sheet


def graded(g: dict) -> bool:
    hits = g.get("key_facts_hit") or [None]
    return bool(g.get("grader")) and all(x is not None for x in hits) and g.get("relevance") is not None


def report(grades: list[dict], results: list[dict] | None = None) -> dict:
    done = [g for g in grades if graded(g)]
    pending = [g["id"] for g in grades if not graded(g)]
    hits = sum(sum(1 for x in g["key_facts_hit"] if x) for g in done)
    total_facts = sum(len(g["key_facts_hit"]) for g in done)
    full = sum(1 for g in done if g["key_facts_hit"] and all(g["key_facts_hit"]))
    claims = sum(g.get("claims_total") or 0 for g in done)
    unsupported = sum(g.get("claims_unsupported") or 0 for g in done)
    with_forbidden = sum(1 for g in done if (g.get("asserted_forbidden") or 0) > 0)
    cites = [c for g in done for c in g.get("citations", []) if c.get("resolves") is not None]
    resolves = sum(1 for c in cites if c["resolves"])
    supports = sum(1 for c in cites if c["resolves"] and c.get("supports"))
    tier = {t: sum(1 for c in cites if c["resolves"] and c.get("tier") == t) for t in (1, 2, 3)}   # an unreachable page cannot be judged
    premise = [g for g in done if g.get("kind") in ("false_premise", "unanswerable") and g.get("premise_handled") is not None]
    conflict = [g for g in done if g.get("kind") == "conflicting" and g.get("calibrated") is not None]
    by_kind: dict[str, dict] = {}
    for g in done:
        d = by_kind.setdefault(g.get("kind") or "?", {"facts": 0, "hit": 0})
        d["facts"] += len(g["key_facts_hit"])
        d["hit"] += sum(1 for x in g["key_facts_hit"] if x)
    rep = {
        "graded_cases": len(done), "pending_cases": pending, "graders": sorted({g["grader"] for g in done}),
        "fact_recall": prop(hits, total_facts),
        "fully_correct_answers": prop(full, len(done)),
        "answers_asserting_a_forbidden_claim": prop(with_forbidden, len(done)),
        "unsupported_claim_rate": prop(unsupported, claims),
        "citation_resolves": prop(resolves, len(cites)),
        "citation_supports_claim": prop(supports, len(cites)),
        "source_tiers": tier,
        "premise_or_unknown_handled": prop(sum(1 for g in premise if g["premise_handled"]), len(premise)),
        "conflict_calibrated": prop(sum(1 for g in conflict if g["calibrated"]), len(conflict)),
        "relevance_mean_of_2": round(sum(g["relevance"] for g in done) / len(done), 2) if done else None,
        "by_kind_fact_recall": {k: prop(v["hit"], v["facts"]) for k, v in by_kind.items()},
    }
    if results is not None:
        lat = [r["latency_s"] for r in results if r.get("latency_s") is not None]
        rep["latency_s"] = {"p50": scoring.percentile(lat, 50), "p95": scoring.percentile(lat, 95), "n": len(lat)}
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("--results", required=True)
    s.add_argument("--cases", default="quality_set.json")
    s.add_argument("--out", default="grades.json")
    r = sub.add_parser("report")
    r.add_argument("--grades", required=True)
    r.add_argument("--results")
    a = ap.parse_args()
    if a.cmd == "sheet":
        cases = {c["id"]: c for c in json.loads((HERE / a.cases).read_text(encoding="utf-8"))["cases"]}
        res = json.loads(Path(a.results).read_text(encoding="utf-8"))["results"]
        Path(a.out).write_text(json.dumps(make_sheet(res, cases), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {a.out}: {len(res)} cases to grade")
        return 0
    grades = json.loads(Path(a.grades).read_text(encoding="utf-8"))
    res = json.loads(Path(a.results).read_text(encoding="utf-8"))["results"] if a.results else None
    print(json.dumps(report(grades, res), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
