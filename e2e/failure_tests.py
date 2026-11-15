"""Failure & recovery tests against the REAL stack (PostgreSQL, Redis, backend, agent) with a fake LLM / internet.

Outages are real: the isolated test containers are stopped and restarted, the agent process is killed
and relaunched, a rogue agent serves malformed SSE, clients drop connections mid-stream, and many users
run concurrently. Only the project's throwaway *test* containers (lumen-test-pg / lumen-test-redis) are touched.

    python e2e/failure_tests.py
"""
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run  # noqa: E402  (orchestrator helpers; its main() is not executed on import)

API = ("127.0.0.1", run.P_API)
API2 = ("127.0.0.1", 18081)   # short timeout + tight rate limit
API3 = ("127.0.0.1", 18082)   # points at the rogue agent
ROGUE = 18090
results: list[tuple[str, bool, str]] = []

GENERIC = "The research service hit an error. Please try again."
UNAVAILABLE = "The research service is currently unavailable. Please try again shortly."
TIMEOUT = "The research took too long and was stopped. Try a narrower question."
INTERRUPTED = "The research was interrupted before it finished. Please try again."


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if ok or not detail else '  -> ' + str(detail)[:240]}", flush=True)


# ── tiny HTTP/SSE client (stdlib) ───────────────────────────────────────────

def call(addr, method, path, body=None, token=None, timeout=15):
    c = http.client.HTTPConnection(*addr, timeout=timeout)
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"null"), dict(r.getheaders())
        except ValueError:
            return r.status, raw.decode(errors="replace"), dict(r.getheaders())
    finally:
        c.close()


def parse_sse(text):
    out = []
    for frame in text.split("\n\n"):
        name, data = None, []
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if name:
            raw = "\n".join(data)
            try:
                out.append((name, json.loads(raw)))
            except ValueError:
                out.append((name, raw))
    return out


def stream(addr, conv, query, token, timeout=90, drop_after_bytes=None):
    """POST a query; returns (status, events, elapsed). Optionally drops the connection early."""
    c = http.client.HTTPConnection(*addr, timeout=timeout)
    t0 = time.time()
    try:
        c.request("POST", f"/api/conversations/{conv}/query", json.dumps({"query": query}),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        r = c.getresponse()
        if r.status != 200:
            r.read()
            return r.status, [], time.time() - t0
        buf = b""
        while True:
            chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
            if not chunk:
                break
            buf += chunk
            if drop_after_bytes is not None and len(buf) >= drop_after_bytes:
                c.sock.shutdown(socket.SHUT_RDWR)   # abrupt browser disconnect
                break
        return 200, parse_sse(buf.decode(errors="replace")), time.time() - t0
    except (socket.timeout, TimeoutError):
        return -1, [], time.time() - t0
    finally:
        c.close()


def names(evs):
    return [n for n, _ in evs]


def new_user(addr=API):
    email = f"f-{uuid.uuid4().hex[:10]}@example.com"
    st, body, _ = call(addr, "POST", "/api/auth/register", {"email": email, "password": "failure-test-pw1", "name": "F"})
    assert st == 200, (st, body)
    return body["token"], email


def new_conv(token, addr=API):
    return call(addr, "POST", "/api/conversations", {}, token)[1]["id"]


def messages(token, conv, addr=API):
    return call(addr, "GET", f"/api/conversations/{conv}/messages", None, token)[1]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def wait(cond, timeout=60, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# ── rogue agent: speaks the agent protocol badly, to test the backend's defences ────────────

class Rogue(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        q = json.loads(self.rfile.read(n) or b"{}").get("query", "")
        if "status500" in q:
            body = b"internal: OPENROUTER_API_KEY=sk-secret-leak"
            self.send_response(500); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if "redirect" in q:
            self.send_response(302); self.send_header("Location", "http://169.254.169.254/latest/meta-data/"); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        w = self.wfile.write
        if "badjson" in q:
            w(b'event: blocks\ndata: {not json at all\n\n')
        elif "unknown" in q:
            w(b'event: evil\ndata: {"x":1}\n\nevent: checkpoint\ndata: {"status":"in_progress","content":"ok"}\n\n'
              b'event: blocks\ndata: {"status":"completed","data":{"query":"q","blocks":[{"template_id":"markdown","data":{"content":"fine"}}]}}\n\nevent: done\ndata: {}\n\n')
        elif "agenterror" in q:
            w(b'event: error\ndata: {"status":"failed","content":"Traceback (most recent call last): secret=hunter2"}\n\n')
        elif "oversize" in q:
            w(b"event: blocks\ndata: " + b"x" * (3 * 1024 * 1024) + b"\n\n")
        elif "truncated" in q:
            w(b'event: checkpoint\ndata: {"status":"in_progress","content":"working"}\n\nevent: blocks\ndata: {"status":"comp')
        elif "badshape" in q:
            w(b'event: blocks\ndata: {"status":"completed","data":{"blocks":"not-a-list"}}\n\n')
        elif "html" in q:   # script injection attempt travelling inside a legitimate block
            w(b'event: blocks\ndata: {"status":"completed","data":{"query":"q","blocks":[{"template_id":"markdown","data":{"content":"<img src=x onerror=alert(1)> [x](javascript:alert(1))"}}],"sources":["javascript:alert(1)","https://ok.example"]}}\n\nevent: done\ndata: {}\n\n')
        self.wfile.flush()


# ── the tests ───────────────────────────────────────────────────────────────

def main():
    for p in (run.BACKEND_PY, run.AGENT_PY):
        assert p.exists(), p
    run.prepare_database()

    run.start("fake_llm", [run.AGENT_PY, run.ROOT / "e2e" / "fake_llm.py", run.P_LLM])
    agent_cmd = [run.AGENT_PY, run.ROOT / "e2e" / "run_agent_fake_internet.py", run.P_AGENT]
    agent = run.start("agent", agent_cmd, run.AGENT_ENV, cwd=run.ROOT / "research-agent")
    uv = lambda port: [run.BACKEND_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port]
    run.start("backend", uv(run.P_API), run.backend_env(), cwd=run.ROOT / "backend")
    run.start("backend2", uv(18081), run.backend_env(RESEARCH_TIMEOUT_SECONDS=6, QUERIES_PER_USER_PER_MINUTE=4), cwd=run.ROOT / "backend")
    rogue_srv = ThreadingHTTPServer(("127.0.0.1", ROGUE), Rogue)
    threading.Thread(target=rogue_srv.serve_forever, daemon=True).start()
    run.start("backend3", uv(18082), {**run.backend_env(), "RESEARCH_AGENT_URL": f"http://127.0.0.1:{ROGUE}"}, cwd=run.ROOT / "backend")

    assert run.wait_http(f"http://127.0.0.1:{run.P_LLM}/health")
    assert run.wait_http(f"http://127.0.0.1:{run.P_AGENT}/health", timeout=240), "agent did not start"
    for port in (run.P_API, 18081, 18082):
        assert run.wait_http(f"http://127.0.0.1:{port}/health/ready", timeout=120), f"backend {port}"
    print("stack up (real PG + Redis; fake LLM/internet; rogue agent on :18090)\n", flush=True)

    token, email = new_user()
    conv = new_conv(token)

    # 1. baseline
    st, ev, dt = stream(API, conv, "bitcoin price in INR", token)
    check("baseline research succeeds", st == 200 and "checkpoint" in names(ev) and "blocks" in names(ev) and names(ev)[-1] == "persisted", names(ev))
    blocks = next((p for n, p in ev if n == "blocks"), {})
    check("baseline answer has structured insight card", any(b["template_id"] == "insight-cards" for b in blocks.get("data", {}).get("blocks", [])))
    check("follow-up suggestions generated (mocked OpenAI)", len(blocks.get("data", {}).get("follow_ups", [])) >= 1)

    # 2. Redis outage
    docker("stop", "lumen-test-redis")
    time.sleep(1)
    conv2 = new_conv(token)
    st, ev, dt = stream(API, conv2, "bitcoin price please", token, timeout=120)
    check("Redis DOWN: research still answers (uncached), no crash", st == 200 and "blocks" in names(ev), (st, names(ev)))
    check("Redis DOWN: agent process still alive and healthy", agent.poll() is None and call(("127.0.0.1", run.P_AGENT), "GET", "/health")[0] == 200)
    docker("start", "lumen-test-redis")
    time.sleep(12)   # agent reconnects lazily (at most every ~10 s)
    st, ev, dt = stream(API, new_conv(token), "bitcoin price once more", token, timeout=120)
    check("Redis RESTORED: agent recovers without restart", st == 200 and "blocks" in names(ev))

    # 3/4. partial failure & invalid upstream data
    st, ev, dt = stream(API, new_conv(token), "ddg-fail and bitcoin price in INR", token, timeout=120)
    bl = next((p for n, p in ev if n == "blocks"), None)
    check("search provider DOWN: other tools still produce results (partial failure)", bl and any(b["template_id"] == "insight-cards" for b in bl["data"]["blocks"]), names(ev))
    check("search provider DOWN: no error event, no internals in text", "error" not in names(ev) and "simulated" not in json.dumps(ev) and "Traceback" not in json.dumps(ev))
    st, ev, dt = stream(API, new_conv(token), "price of badcoin", token, timeout=120)
    check("invalid upstream response (HTML instead of JSON) handled gracefully", st == 200 and ("blocks" in names(ev) or "error" in names(ev)) and "Traceback" not in json.dumps(ev) and "JSONDecodeError" not in json.dumps(ev), names(ev))

    # 5. LLM provider outage
    c5 = new_conv(token)
    st, ev, dt = stream(API, c5, "llm-fail please", token, timeout=120)
    err = next((p for n, p in ev if n == "error"), {})
    check("LLM provider DOWN: user gets the generic error", err.get("content") == GENERIC and "fake provider" not in json.dumps(ev), err)
    m = messages(token, c5)
    check("LLM provider DOWN: question + error persisted", [x["role"] for x in m] == ["user", "assistant"] and m[1]["content"] == GENERIC)

    # 6. Agent outage and recovery
    agent.terminate(); agent.wait(timeout=15)
    c6 = new_conv(token)
    st, ev, dt = stream(API, c6, "bitcoin price", token)
    err = next((p for n, p in ev if n == "error"), {})
    check("Agent DOWN: clear 'unavailable' error, fast", err.get("content") == UNAVAILABLE and dt < 15, (err, round(dt, 1)))
    check("Agent DOWN: backend itself stays ready", call(API, "GET", "/health/ready")[0] == 200)
    check("Agent DOWN: error persisted for the conversation", messages(token, c6)[-1]["content"] == UNAVAILABLE)
    agent = run.start("agent_restarted", agent_cmd, run.AGENT_ENV, cwd=run.ROOT / "research-agent")
    check("Agent RESTORED: service returns", run.wait_http(f"http://127.0.0.1:{run.P_AGENT}/health", timeout=240))
    st, ev, dt = stream(API, new_conv(token), "bitcoin price", token, timeout=120)
    check("Agent RESTORED: research works again with no backend restart", st == 200 and "blocks" in names(ev))

    # 7. PostgreSQL outage and recovery (data must survive)
    before = call(API, "GET", "/api/conversations", None, token)[1]
    docker("stop", "lumen-test-pg")
    t0 = time.time()
    st, body, _ = call(API, "POST", "/api/auth/login", {"email": email, "password": "failure-test-pw1"}, timeout=30)
    check("PostgreSQL DOWN: login -> 503 with a clean message, fast", st == 503 and body == {"detail": "Service temporarily unavailable"} and time.time() - t0 < 15, (st, body, round(time.time() - t0, 1)))
    check("PostgreSQL DOWN: readiness 503, liveness 200", call(API, "GET", "/health/ready")[0] == 503 and call(API, "GET", "/health")[0] == 200)
    st, ev, dt = stream(API, conv, "bitcoin", token, timeout=30)
    check("PostgreSQL DOWN: research request refused cleanly (no hang, no 500)", st == 503, st)
    docker("start", "lumen-test-pg")
    check("PostgreSQL RESTORED: backend recovers by itself", wait(lambda: call(API, "GET", "/health/ready")[0] == 200, 90))
    st, body, _ = call(API, "POST", "/api/auth/login", {"email": email, "password": "failure-test-pw1"})
    after = call(API, "GET", "/api/conversations", None, token)[1]
    check("PostgreSQL RESTORED: login works and NO data was lost", st == 200 and [c["id"] for c in after] == [c["id"] for c in before], (st, len(before), len(after)))

    # 8. Request timeout (backend2: 6 s)
    t2, _ = new_user(API2)
    c8 = new_conv(t2, API2)
    st, ev, dt = stream(API2, c8, "slow research please", t2, timeout=60)
    err = next((p for n, p in ev if n == "error"), {})
    check("Research TIMEOUT: clear timeout message within the limit", err.get("content") == TIMEOUT and 5 < dt < 20, (err, round(dt, 1)))
    check("Research TIMEOUT: persisted", messages(t2, c8)[-1]["content"] == TIMEOUT)

    # 9. Rate limit (backend2: 4/min)
    t3, _ = new_user(API2)
    c9 = new_conv(t3, API2)
    codes = [stream(API2, c9, f"clarify {i}", t3, timeout=60)[0] for i in range(6)]
    check("Rate limit: excess requests get 429", codes[:4] == [200] * 4 and 429 in codes[4:], codes)
    st, body, hdr = call(API2, "POST", f"/api/conversations/{c9}/query", {"query": "again"}, t3)
    check("Rate limit: Retry-After header present", st == 429 and int({k.lower(): v for k, v in hdr.items()}.get("retry-after", "0")) >= 1, (st, hdr))

    # 10. Browser disconnects: slots must not leak (cap = 2 concurrent per user)
    t4, _ = new_user()
    for i in range(4):
        stream(API, new_conv(t4), "slow research disconnect", t4, drop_after_bytes=60, timeout=30)
    time.sleep(1.5)
    st, ev, dt = stream(API, new_conv(t4), "clarify me", t4, timeout=60)
    check("Browser DISCONNECT x4: concurrency slots released (next request OK, not 429)", st == 200 and "clarification" in names(ev), (st, names(ev)))

    # 11. Concurrency: many users, parallel streams, no cross-contamination
    users = [new_user() for _ in range(6)]
    jobs = []
    for ui, (tk, _) in enumerate(users):
        for k in range(2):
            jobs.append((tk, new_conv(tk), f"search bitcoin job-{ui}-{k}"))

    def run_job(j):
        tk, cv, q = j
        st, ev, dt = stream(API, cv, q, tk, timeout=180)
        return j, st, ev

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        outs = list(ex.map(run_job, jobs))
    ok_status = all(st == 200 for _, st, _ in outs)
    own_answer = all(next((p for n, p in ev if n == "blocks"), {}).get("data", {}).get("query") == j[2] for j, _, ev in outs)
    check(f"CONCURRENCY: 12 parallel research streams across 6 users all complete ({time.time()-t0:.0f}s)", ok_status and all("blocks" in names(ev) for _, _, ev in outs), [st for _, st, _ in outs])
    check("CONCURRENCY: each stream carried its own query (no cross-talk)", own_answer)
    persisted = all([x["role"] for x in messages(j[0], j[1])] == ["user", "assistant"] and messages(j[0], j[1])[0]["content"] == j[2] for j, _, _ in outs)
    check("CONCURRENCY: every conversation persisted exactly its own pair", persisted)

    # 12. Rogue agent: malformed / hostile protocol behaviour (backend3 -> rogue)
    t5, _ = new_user(API3)

    def rogue(q):
        c = new_conv(t5, API3)
        st, ev, dt = stream(API3, c, q, t5, timeout=60)
        return c, st, ev

    c, st, ev = rogue("badjson")
    check("Malformed SSE (invalid JSON) -> generic error, persisted", next((p for n, p in ev if n == "error"), {}).get("content") == GENERIC and messages(t5, c, API3)[-1]["content"] == GENERIC, names(ev))
    c, st, ev = rogue("unknown event names")
    check("Unknown agent events dropped; valid events still delivered", "evil" not in names(ev) and "blocks" in names(ev), names(ev))
    c, st, ev = rogue("agenterror")
    check("Agent-supplied error text never reaches the client", "hunter2" not in json.dumps(ev) and "Traceback" not in json.dumps(ev) and next((p for n, p in ev if n == "error"), {}).get("content") == GENERIC)
    c, st, ev = rogue("oversize")
    check("Oversized agent event rejected (no memory blow-up)", "error" in names(ev) and "xxxxxxxx" not in json.dumps(ev), names(ev))
    c, st, ev = rogue("truncated")
    check("Stream cut mid-event -> 'interrupted' error, nothing half-saved as an answer", next((p for n, p in ev if n == "error"), {}).get("content") in (INTERRUPTED, GENERIC) and messages(t5, c, API3)[-1]["content"] in (INTERRUPTED, GENERIC), names(ev))
    c, st, ev = rogue("badshape")
    check("Structurally invalid blocks rejected", next((p for n, p in ev if n == "error"), {}).get("content") == GENERIC and not any(m.get("blocks") for m in messages(t5, c, API3)))
    c, st, ev = rogue("status500")
    check("Agent HTTP 500 body never leaks to client", "sk-secret-leak" not in json.dumps(ev) and next((p for n, p in ev if n == "error"), {}).get("content") == UNAVAILABLE, names(ev))
    c, st, ev = rogue("redirect")
    check("Agent redirect (to cloud metadata) is NOT followed", next((p for n, p in ev if n == "error"), {}).get("content") == UNAVAILABLE, names(ev))
    c, st, ev = rogue("html injection")
    stored = messages(t5, c, API3)[-1]
    check("Hostile markdown/links pass through as DATA (stored verbatim; frontend must escape)", "onerror" in (stored["content"] or "") and "blocks" in names(ev))

    blocked = Path(run.OUT) / "blocked-requests.log"
    leaked = blocked.read_text().strip() if blocked.exists() else ""
    check("HERMETIC: no request left the machine (agent sealed at socket level)", leaked == "", leaked[:300])

    print("\n--- summary")
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("ABORTED:", e)
    finally:
        # leave the throwaway test containers running for later suites
        for name in ("lumen-test-pg", "lumen-test-redis"):
            docker("start", name)
        for p in run.procs:
            p.terminate()
        for p in run.procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
    sys.exit(code)
