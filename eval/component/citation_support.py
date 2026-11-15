"""A deterministic baseline for "does this source passage support this claim?".

This is a TIER B component: real code evaluated on real labelled inputs, with no language model anywhere. It is used to PRE-FILL suggestions for a human grader
(`quality_grading.py` citation checks); it never replaces the grader. It is a lexical heuristic, so it is weak by design on paraphrase and on semantic contradiction;
`run_citation_eval.py` measures exactly how weak, on a held-out split.

Labels:
  supported    the passage states the claim (high content-word overlap and every number in the claim appears in the passage)
  contradicted the passage covers the claim's topic but a number differs, or exactly one side is negated
  unsupported  the passage does not contain enough of the claim (including topical but silent passages)
"""
from __future__ import annotations

import re

STOP = frozenset(
    "a an the of in on at to for from by with and or but is are was were be been being it its this that these those as into over about than then there their his her "
    "which who whom whose while also has have had do does did can could would should may might will shall not no nor".split()
)
NEGATIONS = frozenset({"not", "no", "never", "cannot", "without", "neither", "nor", "n't"})
_WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*(?:'t)?")
_NUM = re.compile(r"^[0-9][0-9.,]*$")

# Thresholds. Chosen on the DEV split only (see run_citation_eval.py); the test split was never used to choose them.
SUPPORT_OVERLAP = 0.75
TOPIC_OVERLAP = 0.5


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    out = []
    for raw in _WORD.findall(text.lower().replace("’", "'")):
        w = raw.strip(".,")
        if w:
            out.append(w)
    return out


def split(text: str) -> tuple[set[str], set[str], bool]:
    """(content words, numbers, has negation)"""
    toks = tokens(text)
    neg = any(t in NEGATIONS or t.endswith("n't") for t in toks)
    words, nums = set(), set()
    for t in toks:
        if _NUM.match(t):
            nums.add(t.replace(",", ""))
        elif t not in STOP and t not in NEGATIONS:
            words.add(_stem(t))
    return words, nums, neg


def judge(claim: str, passage: str) -> dict:
    cw, cn, c_neg = split(claim)
    pw, pn, p_neg = split(passage)
    overlap = len(cw & pw) / len(cw) if cw else 0.0
    nums_ok = cn <= pn
    detail = {"overlap": round(overlap, 3), "claim_numbers": sorted(cn), "numbers_missing": sorted(cn - pn), "negation_mismatch": c_neg != p_neg}
    if overlap >= TOPIC_OVERLAP and cn and not nums_ok and pn:
        return {"label": "contradicted", "why": "same topic, but the claim's number is not in the passage", **detail}
    if overlap >= SUPPORT_OVERLAP and nums_ok and c_neg == p_neg:
        return {"label": "supported", "why": "content words and numbers found", **detail}
    if overlap >= SUPPORT_OVERLAP and c_neg != p_neg:
        return {"label": "contradicted", "why": "same words, but exactly one side is negated", **detail}
    return {"label": "unsupported", "why": "not enough of the claim appears in the passage", **detail}
