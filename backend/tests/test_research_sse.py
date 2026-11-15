"""SSE bridge tests. The agent is replaced by a scripted fake; DB is the real test PostgreSQL."""
import asyncio
import json
import uuid

import pytest

from app.config import settings
from app.ratelimit import SlidingWindowLimiter
from app.research_client import AgentEvent, AgentUnavailable
from app.routers import research as research_router

# ── helpers ─────────────────────────────────────────────────────────────────

def parse_sse(text: str) -> list[tuple[str, dict | str]]:
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        name, data = None, []
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        raw = "\n".join(data)
        try:
            events.append((name, json.loads(raw)))
        except ValueError:
            events.append((name, raw))
    return events


def names(events):
    return [n for n, _ in events]


def ev(name: str, payload) -> AgentEvent:
    return AgentEvent(name, payload if isinstance(payload, str) else json.dumps(payload))


BLOCKS = {
    "status": "completed",
    "data": {
        "query": "q",
        "blocks": [
            {"template_id": "markdown", "data": {"content": "## Answer\nBitcoin is up."}},
            {"template_id": "insight-cards", "data": {"items": [{"title": "BTC", "body": "Price: 1", "severity": "success"}]}},
        ],
        "sources": ["https://example.com/a", "https://example.com/b"],
        "follow_ups": [{"label": "More", "query": "More please", "category": "deeper"}],
    },
}


class FakeAgent:
    """Scripted stand-in for research_client.stream_research."""

    def __init__(self, script):
        self.script = script
        self.calls: list[tuple[str, list]] = []
        self.closed = False
        self.finished = False

    def __call__(self, query, history):
        self.calls.append((query, history))

        async def gen():
            try:
                for item in self.script:
                    if isinstance(item, BaseException):
                        raise item
                    if isinstance(item, (int, float)):
                        await asyncio.sleep(item)
                        continue
                    yield item
                self.finished = True
            finally:
                self.closed = True

        return gen()


@pytest.fixture()
def install_agent(monkeypatch):
    def _install(script):
        fake = FakeAgent(script)
        monkeypatch.setattr(research_router, "stream_research", fake)
        return fake

    return _install


@pytest.fixture()
def conv(client, alice):
    cid = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    return cid


def ask(client, user, cid, q="What is the Bitcoin price?"):
    return client.post(f"/api/conversations/{cid}/query", json={"query": q}, headers=user["headers"])


def db_messages(engine_rows, cid):
    return engine_rows(
        "SELECT role, content, blocks, sources, follow_ups FROM messages WHERE conversation_id=:c ORDER BY created_at", c=cid
    )


# ── framing & happy path ────────────────────────────────────────────────────

def test_sse_frame_format_and_multiline_data():
    frame = research_router._sse("blocks", "line1\nline2").decode()
    assert frame == "event: blocks\ndata: line1\ndata: line2\n\n"


def test_happy_path_event_sequence_and_persistence(client, alice, conv, install_agent, engine_rows):
    install_agent([
        ev("checkpoint", {"status": "in_progress", "content": "Researching your query..."}),
        ev("checkpoint", {"status": "in_progress", "content": "Fetching bitcoin price...", "tool": "get_crypto_price"}),
        ev("blocks", BLOCKS),
        ev("done", {"status": "completed"}),
    ])
    r = ask(client, alice, conv)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"

    events = parse_sse(r.text)
    assert names(events) == ["user_message", "checkpoint", "checkpoint", "blocks", "persisted"]
    assert events[2][1]["tool"] == "get_crypto_price"
    assert events[3][1]["data"]["sources"] == ["https://example.com/a", "https://example.com/b"]

    rows = db_messages(engine_rows, conv)
    assert [r_[0] for r_ in rows] == ["USER", "ASSISTANT"]
    assert rows[0][1] == "What is the Bitcoin price?"
    role, content, blocks, sources, fups = rows[1]
    assert content.startswith("## Answer") and [b["template_id"] for b in blocks] == ["markdown", "insight-cards"]
    assert sources == ["https://example.com/a", "https://example.com/b"] and fups[0]["label"] == "More"
    assert events[-1][1]["messageId"]  # persisted event carries the stored id


def test_user_message_event_carries_the_stored_id(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])
    events = parse_sse(ask(client, alice, conv).text)
    uid = events[0][1]["id"]
    assert engine_rows("SELECT role FROM messages WHERE id=:i", i=uid)[0][0] == "USER"


def test_title_is_set_from_first_query_only(client, alice, conv, install_agent):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])
    ask(client, alice, conv, "First question about EVs")
    ask(client, alice, conv, "Second question")
    assert client.get(f"/api/conversations/{conv}", headers=alice["headers"]).json()["title"] == "First question about EVs"


def test_long_first_query_title_is_truncated(client, alice, conv, install_agent):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])
    ask(client, alice, conv, "w" * 200)
    t = client.get(f"/api/conversations/{conv}", headers=alice["headers"]).json()["title"]
    assert len(t) == 60 and t.endswith("...")


def test_history_is_forwarded_to_the_agent_in_order(client, alice, conv, install_agent):
    fake = install_agent([ev("blocks", BLOCKS), ev("done", {})])
    ask(client, alice, conv, "first")
    ask(client, alice, conv, "second")
    assert fake.calls[0] == ("first", [])
    assert fake.calls[1][0] == "second"
    assert [(h["role"], h["content"][:2]) for h in fake.calls[1][1]] == [("user", "fi"), ("assistant", "##")]


def test_conversation_history_never_crosses_users(client, alice, bob, install_agent):
    fake = install_agent([ev("blocks", BLOCKS), ev("done", {})])
    a = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    b = client.post("/api/conversations", json={}, headers=bob["headers"]).json()["id"]
    ask(client, alice, a, "alice private question")
    ask(client, bob, b, "bob question")
    assert fake.calls[1] == ("bob question", [])


def test_clarification_is_persisted_as_text(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("clarification", {"status": "completed", "content": "Which city?"}), ev("done", {})])
    events = parse_sse(ask(client, alice, conv).text)
    assert names(events) == ["user_message", "clarification", "persisted"]
    rows = db_messages(engine_rows, conv)
    assert rows[1][1] == "Which city?" and rows[1][2] is None


# ── failures: the user always gets a meaningful, safe error ─────────────────

def _assert_error_flow(client, alice, conv, engine_rows, r, expected_text, forbidden=()):
    events = parse_sse(r.text)
    assert names(events)[0] == "user_message" and "error" in names(events) and names(events)[-1] == "persisted"
    err = next(p for n, p in events if n == "error")
    assert err == {"status": "failed", "content": expected_text}
    for bad in forbidden:
        assert bad not in r.text
    rows = db_messages(engine_rows, conv)
    assert [x[0] for x in rows] == ["USER", "ASSISTANT"]  # the question is never silently dropped
    assert rows[1][1] == expected_text and rows[1][2] is None


def test_agent_error_event_is_replaced_with_a_generic_message(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("error", {"status": "failed", "content": "Traceback: OPENROUTER_API_KEY=sk-or-hunter2 rejected"}), ev("done", {})])
    _assert_error_flow(client, alice, conv, engine_rows, ask(client, alice, conv), research_router.GENERIC_ERROR, ("hunter2", "Traceback"))


def test_unexpected_exception_in_agent_stream_is_sanitised(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("checkpoint", {"status": "in_progress", "content": "x"}), RuntimeError("db password is hunter2")])
    r = ask(client, alice, conv)
    _assert_error_flow(client, alice, conv, engine_rows, r, research_router.GENERIC_ERROR, ("hunter2",))


def test_agent_unavailable(client, alice, conv, install_agent, engine_rows):
    install_agent([AgentUnavailable("ConnectError")])
    r = ask(client, alice, conv)
    assert r.status_code == 200  # the stream itself works; the failure is reported in-band
    _assert_error_flow(client, alice, conv, engine_rows, r, research_router.UNAVAILABLE_ERROR, ("ConnectError",))


def test_malformed_json_event_becomes_an_error(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("blocks", "{this is not json"), ev("done", {})])
    _assert_error_flow(client, alice, conv, engine_rows, ask(client, alice, conv), research_router.GENERIC_ERROR)


def test_non_object_json_event_becomes_an_error(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("checkpoint", "[1,2,3]"), ev("done", {})])
    _assert_error_flow(client, alice, conv, engine_rows, ask(client, alice, conv), research_router.GENERIC_ERROR)


def test_blocks_with_invalid_shape_are_rejected(client, alice, conv, install_agent, engine_rows):
    bad = {"status": "completed", "data": {"blocks": [{"no_template": 1}, "str"]}}
    install_agent([ev("blocks", bad), ev("done", {})])
    _assert_error_flow(client, alice, conv, engine_rows, ask(client, alice, conv), research_router.GENERIC_ERROR)


def test_oversized_blocks_are_rejected_not_stored(client, alice, conv, install_agent, engine_rows, monkeypatch):
    monkeypatch.setattr(settings, "max_persisted_blocks_bytes", 500)
    big = {"status": "completed", "data": {"blocks": [{"template_id": "markdown", "data": {"content": "x" * 5000}}]}}
    install_agent([ev("blocks", big), ev("done", {})])
    r = ask(client, alice, conv)
    _assert_error_flow(client, alice, conv, engine_rows, r, research_router.TOO_LARGE_ERROR)
    assert "xxxxx" not in r.text


def test_unknown_event_names_are_dropped_not_forwarded(client, alice, conv, install_agent):
    install_agent([ev("evil", {"x": 1}), ev("checkpoint", {"status": "in_progress", "content": "ok"}), ev("blocks", BLOCKS), ev("done", {})])
    assert "evil" not in names(parse_sse(ask(client, alice, conv).text))


def test_stream_ending_without_a_result_is_reported_as_interrupted(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("checkpoint", {"status": "in_progress", "content": "working"})])  # then the stream just ends
    _assert_error_flow(client, alice, conv, engine_rows, ask(client, alice, conv), research_router.INTERRUPTED_ERROR)


def test_research_timeout_is_reported(client, alice, conv, install_agent, engine_rows, monkeypatch):
    monkeypatch.setattr(settings, "research_timeout_seconds", 0.3)
    fake = install_agent([ev("checkpoint", {"status": "in_progress", "content": "slow"}), 5])
    _assert_error_flow(client, alice, conv, engine_rows, ask(client, alice, conv), research_router.TIMEOUT_ERROR)
    assert fake.closed  # the upstream agent request was cancelled


def test_partial_failure_keeps_checkpoints_already_sent(client, alice, conv, install_agent):
    install_agent([ev("checkpoint", {"status": "in_progress", "content": "step 1"}), RuntimeError("boom")])
    seq = names(parse_sse(ask(client, alice, conv).text))
    assert seq == ["user_message", "checkpoint", "error", "persisted"]


def test_persistence_failure_emits_persist_error_and_does_not_crash(client, alice, conv, install_agent, monkeypatch):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])

    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(research_router, "_persist_assistant", broken)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(research_router.asyncio, "sleep", lambda *_: real_sleep(0))  # skip retry backoff
    seq = names(parse_sse(ask(client, alice, conv).text))
    assert seq == ["user_message", "blocks", "persist_error"]


# ── limits ──────────────────────────────────────────────────────────────────

def test_query_rate_limit(client, alice, conv, install_agent, monkeypatch):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])
    monkeypatch.setattr(research_router, "query_limiter", SlidingWindowLimiter("t-query-" + uuid.uuid4().hex[:8], 2, 60))
    assert [ask(client, alice, conv).status_code for _ in range(3)] == [200, 200, 429]
    assert int(ask(client, alice, conv).headers["Retry-After"]) >= 1


def test_rate_limit_is_per_user(client, alice, bob, install_agent, monkeypatch):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])
    monkeypatch.setattr(research_router, "query_limiter", SlidingWindowLimiter("t-query1-" + uuid.uuid4().hex[:8], 1, 60))
    a = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    b = client.post("/api/conversations", json={}, headers=bob["headers"]).json()["id"]
    assert ask(client, alice, a).status_code == 200
    assert ask(client, alice, a).status_code == 429
    assert ask(client, bob, b).status_code == 200  # alice's limit does not affect bob


def test_concurrent_stream_cap_and_slot_release(client, alice, conv, install_agent, engine_rows):
    install_agent([ev("blocks", BLOCKS), ev("done", {})])
    key = alice["id"]
    for _ in range(settings.max_concurrent_streams_per_user):
        assert research_router.stream_guard.acquire(key) is not None
    r = ask(client, alice, conv)
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert engine_rows("SELECT count(*) FROM messages")[0][0] == 0  # rejected before persisting anything
    research_router.stream_guard.reset()
    assert ask(client, alice, conv).status_code == 200
    assert research_router.stream_guard.active(key) == 0  # released after a normal stream


def test_slot_released_after_failures(client, alice, conv, install_agent):
    install_agent([RuntimeError("x")])
    for _ in range(settings.max_concurrent_streams_per_user + 2):
        assert ask(client, alice, conv).status_code == 200
    assert research_router.stream_guard.active(alice["id"]) == 0


def test_unknown_conversation_releases_slot(client, alice, install_agent):
    install_agent([])
    assert client.post(f"/api/conversations/{uuid.uuid4()}/query", json={"query": "x"}, headers=alice["headers"]).status_code == 404
    assert research_router.stream_guard.active(alice["id"]) == 0


# ── client cancellation (real ASGI disconnect) ──────────────────────────────

async def _drive(app, token, cid, body, disconnect_when):
    """Run one request through the ASGI app; drop the connection when `disconnect_when(events_so_far)` is true."""
    payload = json.dumps(body).encode()
    disconnect = asyncio.Event()
    sent_body = False
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            if disconnect_when(parse_sse(b"".join(chunks).decode())):
                disconnect.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": f"/api/conversations/{cid}/query", "raw_path": f"/api/conversations/{cid}/query".encode(),
        "query_string": b"", "root_path": "", "server": ("test", 80), "client": ("1.2.3.4", 5555),
        "headers": [(b"host", b"test"), (b"content-type", b"application/json"), (b"authorization", f"Bearer {token}".encode()),
                    (b"content-length", str(len(payload)).encode())],
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=15)
    return parse_sse(b"".join(chunks).decode())


async def test_disconnect_before_result_cancels_agent_and_releases_slot(client, alice, conv, install_agent, engine_rows):
    from app.main import app

    fake = install_agent([ev("checkpoint", {"status": "in_progress", "content": "working"}), 30])
    events = await _drive(app, alice["token"], conv, {"query": "hello"}, lambda evs: "checkpoint" in names(evs))
    await asyncio.sleep(0.3)
    assert "checkpoint" in names(events)
    assert fake.closed and not fake.finished            # upstream request cancelled, not left running
    assert research_router.stream_guard.active(alice["id"]) == 0
    rows = db_messages(engine_rows, conv)
    assert [r[0] for r in rows] == ["USER"]              # the question is kept, no empty assistant row


async def test_disconnect_after_final_answer_still_saves_the_answer(client, alice, conv, install_agent, engine_rows):
    from app.main import app

    fake = install_agent([ev("blocks", BLOCKS), 30])    # answer arrives, then the agent lingers
    await _drive(app, alice["token"], conv, {"query": "hello"}, lambda evs: "blocks" in names(evs))
    await asyncio.sleep(0.5)
    assert fake.closed
    rows = db_messages(engine_rows, conv)
    assert [r[0] for r in rows] == ["USER", "ASSISTANT"]
    assert rows[1][1].startswith("## Answer") and rows[1][2][0]["template_id"] == "markdown"
    assert research_router.stream_guard.active(alice["id"]) == 0


async def test_many_concurrent_streams_are_isolated(client, make_user, install_agent, engine_rows):
    from app.main import app

    install_agent([ev("checkpoint", {"status": "in_progress", "content": "x"}), 0.05, ev("blocks", BLOCKS), ev("done", {})])
    users = [make_user() for _ in range(5)]
    cids = [client.post("/api/conversations", json={}, headers=u["headers"]).json()["id"] for u in users]
    results = await asyncio.gather(*[
        _drive(app, u["token"], c, {"query": f"q-{i}"}, lambda evs: False) for i, (u, c) in enumerate(zip(users, cids))
    ])
    for evs in results:
        assert names(evs) == ["user_message", "checkpoint", "blocks", "persisted"]
    for i, c in enumerate(cids):
        rows = db_messages(engine_rows, c)
        assert [r[0] for r in rows] == ["USER", "ASSISTANT"] and rows[0][1] == f"q-{i}"


# ── database connections (found by the local load test) ─────────────────────

def test_a_research_request_never_holds_two_pool_connections_and_none_while_streaming(client, alice, conv, install_agent, monkeypatch):
    """Regression: authentication kept its session (a pooled connection) open for the whole request while the handler opened a second one.
    Under burst load every in-flight request held one and waited for another, exhausting the pool (20) until the 30 s timeout: 500s."""
    from app.db import engine

    seen = {}
    real_start = research_router._start_turn

    def probe_start(*a, **kw):
        seen["at_start_turn"] = engine.pool.checkedout()
        return real_start(*a, **kw)

    monkeypatch.setattr(research_router, "_start_turn", probe_start)

    fake = install_agent([
        ev("checkpoint", {"status": "in_progress", "content": "Researching your query..."}),
        ev("blocks", BLOCKS),
        ev("done", {"status": "completed"}),
    ])
    original_call = fake.__call__

    def probing(query, history):
        seen["at_agent_call"] = engine.pool.checkedout()
        return original_call(query, history)

    monkeypatch.setattr(research_router, "stream_research", probing)

    assert ask(client, alice, conv).status_code == 200
    assert seen["at_start_turn"] == 0, "authentication must release its connection before the handler needs another"
    assert seen["at_agent_call"] == 0, "no connection may be held while the (long) agent stream runs"
