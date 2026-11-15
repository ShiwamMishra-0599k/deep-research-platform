"""Tier B component evaluation: the citation-support baseline, its labelled set, and the exact numbers quoted in the documentation."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "component"))
sys.path.insert(0, str(HERE))
import citation_support as cs  # noqa: E402
import run_citation_eval as ev  # noqa: E402


def test_dataset_is_balanced_split_and_well_formed():
    pairs = json.loads((HERE / "component" / "citation_pairs.json").read_text(encoding="utf-8"))["pairs"]
    assert len(pairs) == 36 and len({p["id"] for p in pairs}) == 36
    for label in ("supported", "unsupported", "contradicted"):
        assert sum(p["label"] == label for p in pairs) == 12
        assert sum(p["label"] == label and p["split"] == "dev" for p in pairs) == 6          # every class is in both splits
        assert sum(p["label"] == label and p["split"] == "test" for p in pairs) == 6
    assert all(p["claim"] and p["passage"] for p in pairs)


def test_obvious_cases():
    p = "The Goods and Services Tax came into force across India on 1 July 2017."
    assert cs.judge("The Goods and Services Tax came into force in India on 1 July 2017.", p)["label"] == "supported"
    assert cs.judge("The Goods and Services Tax came into force in India on 1 July 2014.", p)["label"] == "contradicted"   # a different number
    assert cs.judge("India has a space agency called ISRO.", p)["label"] == "unsupported"                              # silent passage


def test_numbers_must_match_exactly_not_approximately():
    assert cs.judge("The peak is 8,850 metres high.", "The peak is 8,848.86 metres high.")["label"] == "contradicted"
    assert cs.split("It is 8,848.86 metres")[1] == {"8848.86"}                                                          # thousands separator removed, decimals kept


def test_negation_mismatch_with_the_same_words_is_a_contradiction():
    assert cs.judge("The service stores passwords in plain text.", "The service does not store passwords in plain text.")["label"] == "contradicted"


def test_nothing_in_common_is_unsupported_and_empty_input_does_not_crash():
    assert cs.judge("Quantum computers factor integers quickly.", "The monsoon arrives in June.")["label"] == "unsupported"
    assert cs.judge("", "anything")["label"] == "unsupported" and cs.judge("claim words", "")["label"] == "unsupported"


def test_recorded_results_are_exactly_what_the_docs_quote():
    """If this fails the heuristic or the data changed: re-run run_citation_eval.py and update docs/EVALUATION_PLAN.md and ZERO_BUDGET_PLAN.md."""
    dev = ev.evaluate(ev.load("dev"))
    test = ev.evaluate(ev.load("test"))
    assert (dev["n"], round(dev["accuracy"] * 18), dev["macro_f1"]) == (18, 16, 0.882)
    assert (test["n"], round(test["accuracy"] * 18), test["macro_f1"]) == (18, 14, 0.783)
    assert test["accuracy_ci95"] == (0.548, 0.91)
    assert {e["id"] for e in test["errors"]} == {"sup-02", "sup-06", "con-06", "con-10"}      # the four known misses (paraphrase, swapped RFC number, entity swap)
