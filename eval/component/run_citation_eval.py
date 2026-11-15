"""Score the citation-support baseline on the hand-labelled pairs.

    python eval/component/run_citation_eval.py --split dev      # use while building/tuning
    python eval/component/run_citation_eval.py --split test     # held out: look at it once, at the end

TIER B evidence (no language model): it measures this heuristic on 36 author-written pairs. It does NOT measure Lumen's citation quality, which needs real model
output and a human grader (docs/EVALUATION_PLAN.md). The set is small, so every figure carries a 95% Wilson interval.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import citation_support as cs  # noqa: E402
from quality_grading import wilson  # noqa: E402

LABELS = ("supported", "unsupported", "contradicted")


def evaluate(pairs: list[dict]) -> dict:
    conf = {g: {p: 0 for p in LABELS} for g in LABELS}
    wrong = []
    for p in pairs:
        pred = cs.judge(p["claim"], p["passage"])["label"]
        conf[p["label"]][pred] += 1
        if pred != p["label"]:
            wrong.append({"id": p["id"], "gold": p["label"], "predicted": pred, "claim": p["claim"]})
    n = len(pairs)
    correct = sum(conf[g][g] for g in LABELS)
    per = {}
    for g in LABELS:
        tp = conf[g][g]
        fp = sum(conf[o][g] for o in LABELS if o != g)
        fn = sum(conf[g][o] for o in LABELS if o != g)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0 if (prec is not None and rec is not None) else None
        per[g] = {"precision": None if prec is None else round(prec, 3), "recall": None if rec is None else round(rec, 3), "f1": None if f1 is None else round(f1, 3)}
    f1s = [v["f1"] for v in per.values() if v["f1"] is not None]
    return {"n": n, "accuracy": round(correct / n, 3) if n else None, "accuracy_ci95": wilson(correct, n), "macro_f1": round(sum(f1s) / len(f1s), 3) if f1s else None,
            "per_class": per, "confusion_gold_by_predicted": conf, "errors": wrong}


def load(split: str) -> list[dict]:
    pairs = json.loads((HERE / "citation_pairs.json").read_text(encoding="utf-8"))["pairs"]
    return [p for p in pairs if split == "all" or p["split"] == split]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--errors", action="store_true", help="list the misclassified pairs")
    a = ap.parse_args()
    r = evaluate(load(a.split))
    if not a.errors:
        r = {k: v for k, v in r.items() if k != "errors"}
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
