import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quality_grading as Q  # noqa: E402

SET = json.loads((Path(__file__).resolve().parent.parent / "quality_set.json").read_text(encoding="utf-8"))["cases"]


def graded_case(kind="factual", hits=(True, True), forbidden=0, claims=(10, 1), cites=((True, True, 1),), relevance=2, **extra):
    g = {"id": "x", "kind": kind, "key_facts_hit": list(hits), "asserted_forbidden": forbidden, "claims_total": claims[0],
         "claims_unsupported": claims[1], "relevance": relevance, "grader": "human",
         "citations": [{"url": "https://e.com", "resolves": r, "supports": s, "tier": t} for r, s, t in cites]}
    g.update(extra)
    return g


def test_dataset_is_well_formed():
    ids = [c["id"] for c in SET]
    assert len(ids) == len(set(ids)) >= 15
    kinds = {"factual", "multi_hop", "false_premise", "unanswerable", "conflicting", "time_sensitive", "multilingual"}
    for c in SET:
        assert c["query"] and c["key_facts"] and c["kind"] in kinds
        assert c["reference_status"] in ("stable", "record_at_run_time")
        if c["reference_status"] == "record_at_run_time":
            assert c["reference_source"]
    assert {"false_premise", "unanswerable", "conflicting"} <= {c["kind"] for c in SET}   # the hallucination and nuance probes exist


def test_wilson_interval_is_wide_for_small_n_and_contains_the_rate():
    lo, hi = Q.wilson(8, 10)
    assert lo < 0.8 < hi and hi - lo > 0.3
    assert Q.wilson(0, 0) is None
    lo, hi = Q.wilson(0, 10)
    assert lo == 0.0 and hi < 0.35           # zero successes still leaves a non-trivial upper bound
    lo, hi = Q.wilson(10, 10)
    assert hi == 1.0 and lo > 0.65


def test_report_metrics_are_computed_from_the_grades():
    gs = [
        graded_case(hits=(True, True), claims=(10, 0), cites=((True, True, 1), (True, False, 3))),
        graded_case(hits=(True, False), forbidden=1, claims=(10, 4), cites=((False, None, 3),)),
        graded_case(kind="false_premise", hits=(True,), premise_handled=True, claims=(2, 0), cites=()),
        graded_case(kind="false_premise", hits=(False,), premise_handled=False, forbidden=1, claims=(2, 2), cites=()),
        graded_case(kind="conflicting", hits=(True, True), calibrated=True, claims=(4, 0), cites=()),
    ]
    r = Q.report(gs)
    assert r["graded_cases"] == 5
    assert r["fact_recall"]["k"] == 6 and r["fact_recall"]["n"] == 8
    assert r["fully_correct_answers"]["k"] == 3 and r["fully_correct_answers"]["n"] == 5
    assert r["answers_asserting_a_forbidden_claim"]["k"] == 2
    assert r["unsupported_claim_rate"] == Q.prop(6, 28)
    assert r["citation_resolves"]["k"] == 2 and r["citation_resolves"]["n"] == 3
    assert r["citation_supports_claim"]["k"] == 1                        # resolving is not the same as supporting
    assert r["premise_or_unknown_handled"]["k"] == 1 and r["premise_or_unknown_handled"]["n"] == 2
    assert r["conflict_calibrated"]["k"] == 1 and r["source_tiers"] == {1: 1, 2: 0, 3: 1}


def test_ungraded_cases_are_reported_pending_and_not_counted():
    blank = {"id": "p", "kind": "factual", "key_facts_hit": [None, None], "relevance": None, "grader": None, "citations": []}
    r = Q.report([graded_case(), blank])
    assert r["graded_cases"] == 1 and r["pending_cases"] == ["p"]
    assert Q.report([blank])["fact_recall"]["rate"] is None             # no grades -> no invented numbers


def test_sheet_carries_the_answer_and_blank_fields_for_every_key_fact():
    res = [{"id": SET[0]["id"], "answer_text": "A", "source_urls": ["https://a.com"], "latency_s": 3.0, "passed": True}]
    sh = Q.make_sheet(res, {c["id"]: c for c in SET})
    assert sh[0]["answer_text"] == "A" and len(sh[0]["key_facts_hit"]) == len(SET[0]["key_facts"])
    assert all(x is None for x in sh[0]["key_facts_hit"]) and sh[0]["citations"][0]["supports"] is None
    assert not Q.graded(sh[0])                                           # a blank sheet scores nothing
