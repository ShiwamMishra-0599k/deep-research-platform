"""Unit tests for the evaluation scorers: each scorer must PASS a good trace and FAIL a specific bad one."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scoring as S  # noqa: E402
from run_eval import parse_sse  # noqa: E402


def trace(*events, latency=1.0):
    return {"events": list(events), "latency_s": latency, "first_checkpoint_s": 0.1}


def good(tool="get_crypto_price", blocks=("markdown", "insight-cards"), sources=("https://example.com/a",)):
    return trace(
        ("checkpoint", {"content": "Researching"}),
        ("checkpoint", {"content": "calling", "tool": tool}),
        ("blocks", {"status": "completed", "data": {
            "query": "q", "sources": list(sources),
            "blocks": [{"template_id": b, "data": {"content": "answer text"}} for b in blocks]}}),
        ("done", {}),
    )


CASE = {"id": "c", "category": "crypto", "expect_tools": [["get_crypto_price"]], "expect_blocks": ["markdown", "insight-cards"]}


def test_a_good_trace_passes_every_check():
    r = S.score_case(CASE, good())
    assert r["passed"], r["checks"]
    assert r["tools"] == ["get_crypto_price"] and r["blocks"] == ["markdown", "insight-cards"]


def test_wrong_tool_fails_tool_selection_only():
    r = S.score_case(CASE, good(tool="web_search"))
    assert not r["passed"]
    assert not r["checks"]["tool_selection"]["ok"] and r["checks"]["block_structure"]["ok"]


def test_any_of_tool_group_accepts_either_alternative():
    case = {**CASE, "expect_tools": [["wiki_search", "web_search"]]}
    assert S.score_case(case, good(tool="web_search"))["passed"]
    assert S.score_case(case, good(tool="wiki_search"))["passed"]
    assert not S.score_case(case, good(tool="get_crypto_price"))["passed"]


def test_every_group_must_be_satisfied():
    case = {**CASE, "expect_tools": [["get_crypto_price"], ["get_forex_rate"]]}
    assert not S.score_case(case, good())["passed"]


def test_missing_block_type_fails_structure():
    r = S.score_case(CASE, good(blocks=("markdown",)))
    assert not r["checks"]["block_structure"]["ok"] and "insight-cards" in r["checks"]["block_structure"]["detail"]


def test_error_terminal_fails_even_with_nothing_else_expected():
    t = trace(("checkpoint", {"content": "x"}), ("error", {"content": "boom"}), ("done", {}))
    r = S.score_case({"id": "e", "category": "x"}, t)
    assert not r["passed"] and not r["checks"]["terminal"]["ok"] and not r["checks"]["no_error"]["ok"]


def test_clarification_expected_and_received_passes():
    t = trace(("checkpoint", {"content": "x"}), ("clarification", {"content": "Which city?"}), ("done", {}))
    assert S.score_case({"id": "k", "expect_terminal": "clarification"}, t)["passed"]


def test_clarification_expected_but_answered_fails():
    assert not S.score_case({"id": "k", "expect_terminal": "clarification"}, good())["passed"]


def test_protocol_rejects_two_terminals_missing_done_and_events_after_done():
    two = trace(("blocks", {"data": {"blocks": []}}), ("error", {}), ("done", {}))
    no_done = trace(("blocks", {"data": {"blocks": []}}))
    after = trace(("blocks", {"data": {"blocks": []}}), ("done", {}), ("checkpoint", {}))
    for t in (two, no_done, after):
        assert not S.protocol_ok(t)[0]
    assert S.protocol_ok(good())[0]


def test_citations_required_and_must_be_http():
    case = {**CASE, "needs_citations": True}
    assert S.score_case(case, good())["checks"]["citations"]["ok"]
    assert not S.score_case(case, good(sources=()))["checks"]["citations"]["ok"]
    assert not S.score_case(case, good(sources=("javascript:alert(1)",)))["checks"]["citations"]["ok"]
    assert not S.score_case(case, good(sources=("https://",)))["checks"]["citations"]["ok"]


def test_forbidden_strings_are_detected_case_insensitively():
    leaky = trace(("checkpoint", {"tool": "web_search"}),
                  ("blocks", {"data": {"sources": [], "blocks": [{"template_id": "markdown", "data": {"content": "The key is Sk-abc123"}}]}}), ("done", {}))
    r = S.score_case({"id": "i", "expect_blocks": ["markdown"], "forbidden": ["sk-"]}, leaky)
    assert not r["checks"]["forbidden_absent"]["ok"]
    assert S.score_case({"id": "i", "expect_blocks": ["markdown"], "forbidden": ["sk-"]}, good(blocks=("markdown",)))["passed"]


def test_percentile_and_summary():
    assert S.percentile([], 50) is None
    assert S.percentile([1, 2, 3, 4, 5], 50) == 3 and S.percentile([1, 2, 3, 4, 5], 95) == 5
    results = [S.score_case(CASE, good()), S.score_case(CASE, good(tool="web_search"))]
    s = S.summarise(results)
    assert s["cases"] == 2 and s["passed"] == 1 and s["pass_rate"] == 0.5
    assert s["per_check_pass_rate"]["tool_selection"] == 0.5 and s["per_check_pass_rate"]["block_structure"] == 1.0


def test_manual_review_cases_are_listed_not_hidden():
    r = S.score_case({**CASE, "manual_review": "judge by a human"}, good())
    assert S.summarise([r])["manual_review_pending"] == ["c"]


def test_sse_parser_handles_multiline_data_crlf_free_and_bad_json():
    lines = ["event: checkpoint", 'data: {"a": 1}', "", "event: blocks", 'data: {"x":', 'data: 2}', "", "event: weird", "data: not json", "", ""]
    ev = parse_sse(lines)
    assert ev[0] == ("checkpoint", {"a": 1}) and ev[1] == ("blocks", {"x": 2}) and ev[2] == ("weird", "not json")


def test_usage_is_taken_from_the_done_event_and_missing_counts_are_not_zeroes():
    with_usage = trace(("checkpoint", {}), ("blocks", {"status": "completed", "data": {"blocks": [], "sources": []}}),
                       ("done", {"status": "completed", "usage": {"token_source": "provider", "input_tokens": 100, "output_tokens": 10, "llm_calls": 2, "tool_calls": 1}}))
    without = trace(("checkpoint", {}), ("blocks", {"status": "completed", "data": {"blocks": [], "sources": []}}), ("done", {"status": "completed"}))
    rs = [S.score_case({"id": "a"}, with_usage), S.score_case({"id": "b"}, without)]
    assert rs[0]["usage"]["input_tokens"] == 100 and rs[1]["usage"] is None
    u = S.summarise(rs)["usage"]
    assert u["cases_with_provider_tokens"] == 1 and u["cases_without_token_counts"] == 1
    assert u["input_tokens_total"] == 100 and u["llm_calls_total"] == 2
    assert S.summarise([rs[1]])["usage"]["input_tokens_total"] is None      # nothing reported: null, not 0
