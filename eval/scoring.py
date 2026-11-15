"""Pure scoring functions for the research-quality evaluation. No network, no I/O, fully unit-tested.

A *trace* is what one /research call produced:
    {"events": [(event_name, data), ...], "latency_s": float, "first_checkpoint_s": float | None}
"""
from __future__ import annotations

import math
from urllib.parse import urlparse

TERMINAL = ("blocks", "clarification", "error")


def terminal_events(trace: dict) -> list[str]:
    return [name for name, _ in trace["events"] if name in TERMINAL]


def protocol_ok(trace: dict) -> tuple[bool, str]:
    """Exactly one terminal event, then `done`, and nothing after done."""
    names = [n for n, _ in trace["events"]]
    term = [n for n in names if n in TERMINAL]
    if len(term) != 1:
        return False, f"expected exactly 1 terminal event, got {term}"
    if "done" not in names:
        return False, "no `done` event"
    if names[-1] != "done":
        return False, f"events after done or missing done at end: {names[-3:]}"
    if names.index("done") < names.index(term[0]):
        return False, "`done` before the terminal event"
    return True, ""


def tools_called(trace: dict) -> list[str]:
    seen: list[str] = []
    for name, data in trace["events"]:
        if name == "checkpoint" and isinstance(data, dict) and data.get("tool"):
            if data["tool"] not in seen:
                seen.append(data["tool"])
    return seen


def response_of(trace: dict) -> dict | None:
    for name, data in trace["events"]:
        if name == "blocks" and isinstance(data, dict):
            return data.get("data")
    return None


def block_types(trace: dict) -> list[str]:
    r = response_of(trace)
    return [b.get("template_id") for b in (r or {}).get("blocks", [])]


def sources(trace: dict) -> list[str]:
    return list((response_of(trace) or {}).get("sources", []))


def answer_text(trace: dict) -> str:
    """All text the user would read: markdown blocks, clarification and error content."""
    parts: list[str] = []
    for name, data in trace["events"]:
        if name in ("clarification", "error") and isinstance(data, dict):
            parts.append(str(data.get("content", "")))
    for b in (response_of(trace) or {}).get("blocks", []):
        d = b.get("data") or {}
        if isinstance(d, dict) and isinstance(d.get("content"), str):
            parts.append(d["content"])
    return "\n".join(parts)


def tool_selection(trace: dict, expect_tools: list[list[str]]) -> tuple[bool, str]:
    """Every group must be satisfied by at least one of its alternatives."""
    called = set(tools_called(trace))
    missing = [g for g in expect_tools if not called.intersection(g)]
    return (not missing), (f"missing one of {missing}; called {sorted(called)}" if missing else "")


def block_structure(trace: dict, expect_blocks: list[str]) -> tuple[bool, str]:
    have = set(block_types(trace))
    missing = [b for b in expect_blocks if b not in have]
    return (not missing), (f"missing blocks {missing}; got {sorted(have)}" if missing else "")


def citations(trace: dict) -> tuple[bool, str]:
    srcs = sources(trace)
    if not srcs:
        return False, "no sources"
    bad = [s for s in srcs if urlparse(s).scheme not in ("http", "https") or not urlparse(s).netloc]
    return (not bad), (f"non-http(s) sources: {bad}" if bad else "")


def forbidden_absent(trace: dict, forbidden: list[str]) -> tuple[bool, str]:
    text = answer_text(trace).lower()
    hit = [f for f in forbidden if f.lower() in text]
    return (not hit), (f"answer contains forbidden strings {hit}" if hit else "")


def terminal_matches(trace: dict, expected: str) -> tuple[bool, str]:
    term = terminal_events(trace)
    return (term == [expected]), ("" if term == [expected] else f"expected terminal {expected}, got {term}")


def no_error(trace: dict) -> tuple[bool, str]:
    return ("error" not in terminal_events(trace)), ("agent returned an error event" if "error" in terminal_events(trace) else "")


def score_case(case: dict, trace: dict) -> dict:
    """Run every applicable scorer. `manual_review` cases are reported but never auto-passed."""
    checks: dict[str, tuple[bool, str]] = {"protocol": protocol_ok(trace)}
    expected_terminal = case.get("expect_terminal", "blocks")
    checks["terminal"] = terminal_matches(trace, expected_terminal)
    if expected_terminal == "blocks":
        checks["no_error"] = no_error(trace)
        if case.get("expect_tools"):
            checks["tool_selection"] = tool_selection(trace, case["expect_tools"])
        if case.get("expect_blocks"):
            checks["block_structure"] = block_structure(trace, case["expect_blocks"])
        if case.get("needs_citations"):
            checks["citations"] = citations(trace)
    if case.get("forbidden"):
        checks["forbidden_absent"] = forbidden_absent(trace, case["forbidden"])
    passed = all(ok for ok, _ in checks.values())
    return {
        "id": case["id"], "category": case.get("category"), "passed": passed,
        "manual_review": case.get("manual_review"),
        "checks": {k: {"ok": ok, "detail": d} for k, (ok, d) in checks.items()},
        "tools": tools_called(trace), "blocks": block_types(trace), "n_sources": len(sources(trace)),
        "latency_s": round(trace["latency_s"], 2), "first_checkpoint_s": trace.get("first_checkpoint_s"),
        "answer_text": answer_text(trace), "source_urls": sources(trace),   # for graders; results/ is git-ignored
    }


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
    return round(s[k], 2)


def summarise(results: list[dict]) -> dict:
    n = len(results)
    lat = [r["latency_s"] for r in results]
    by_check: dict[str, list[bool]] = {}
    for r in results:
        for k, v in r["checks"].items():
            by_check.setdefault(k, []).append(v["ok"])
    return {
        "cases": n,
        "passed": sum(r["passed"] for r in results),
        "pass_rate": round(sum(r["passed"] for r in results) / n, 3) if n else None,
        "per_check_pass_rate": {k: round(sum(v) / len(v), 3) for k, v in sorted(by_check.items())},
        "latency_s": {"p50": percentile(lat, 50), "p95": percentile(lat, 95), "max": max(lat) if lat else None},
        "manual_review_pending": [r["id"] for r in results if r.get("manual_review")],
    }

