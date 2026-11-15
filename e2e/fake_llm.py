"""A tiny OpenAI-compatible chat-completions server used ONLY for integration tests.

It lets the real LangGraph / Deep Agents loop run end to end with no paid LLM calls.
Behaviour is deterministic and keyword-driven:

  * first turn  -> request the relevant tool(s) (several at once for multi-topic queries)
  * after tools -> compose a final answer from the tool outputs (it never invents data)
  * "clarify"   -> "[CLARIFICATION] Which city are you searching in?"
  * "llm-fail"  -> HTTP 500 (simulates provider outage)
  * "slow"      -> long pause before answering (timeout / cancellation tests)

Run:  python e2e/fake_llm.py [port]        (default 18999)
"""
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS: list[dict] = []  # exposed at GET /_requests for assertions


def _last(messages, role):
    for m in reversed(messages):
        if m.get("role") == role:
            return m
    return None


def _text(m) -> str:
    c = (m or {}).get("content", "")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _user_query(messages) -> str:
    # The agent appends "\n\nContext:\nToday's date: ..." to the latest user message.
    q = _text(_last(messages, "user"))
    return q.split("\n\nContext:")[0].strip()


def _tools_after_last_user(messages):
    idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    return [m for m in messages[idx + 1 :] if m.get("role") == "tool"]


def _tool_calls_for(query: str):
    q = query.lower()
    calls = []

    def add(name, args):
        calls.append({"id": "call_" + uuid.uuid4().hex[:10], "type": "function",
                      "function": {"name": name, "arguments": json.dumps(args)}})

    if re.search(r"\b(ev|charging|charger|chargers)\b", q):
        add("nearby_places", {"place": "Lucknow", "place_type": "charging_station", "radius_meters": 5000})
    if "bitcoin" in q or "btc" in q:
        add("get_crypto_price", {"coin_id": "bitcoin"})
    if "usd" in q and "inr" in q:
        add("get_forex_rate", {"from_currency": "USD", "to_currency": "INR", "amount": 1000})
    if "stock" in q or "reliance" in q:
        add("get_stock_price", {"symbol": "RELIANCE.NS"})
    if "rbi" in q or "news" in q:
        add("latest_news", {"topic": "RBI"})
    if "badcoin" in q:
        add("get_crypto_price", {"coin_id": "badcoin"})
    if "wiki" in q or "what is" in q or "who is" in q:
        add("wiki_search", {"query": "Reserve Bank of India"})
    if "ddg-fail" in q or "search" in q or "research" in q or (not calls and "clarify" not in q):
        add("web_search", {"query": query[:200]})
    return calls


def _final_answer(messages) -> str:
    parts = ["Here is what I found."]
    for t in _tools_after_last_user(messages):
        for line in _text(t).splitlines():
            line = line.strip()
            if not line or re.match(r"^(PLACE|NEWS|STOCK|FOREX|CRYPTO)\|", line):
                continue  # structured lines are rendered as blocks, not repeated in prose
            parts.append("- " + line[:220])
            if len(parts) > 14:
                break
    return "\n".join(parts)


def _chunk(delta, finish=None, cid="chatcmpl-fake"):
    return {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/_requests":
            return self._json(200, REQUESTS[-50:])
        if self.path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        messages = body.get("messages", [])
        query = _user_query(messages)
        REQUESTS.append({"query": query, "n_messages": len(messages), "stream": bool(body.get("stream"))})
        q = query.lower()

        if "llm-fail" in q:
            return self._json(500, {"error": {"message": "fake provider outage", "type": "server_error"}})
        if "slow" in q:
            time.sleep(25)

        have_tool_results = bool(_tools_after_last_user(messages))
        if have_tool_results:
            tool_calls, text = [], _final_answer(messages)
        elif "clarify" in q:
            tool_calls, text = [], "[CLARIFICATION] Which city are you searching in?"
        else:
            tool_calls = _tool_calls_for(query)
            text = ""

        cid = "chatcmpl-" + uuid.uuid4().hex[:8]
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send(obj):
                data = ("data: " + (obj if isinstance(obj, str) else json.dumps(obj)) + "\n\n").encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            send(_chunk({"role": "assistant", "content": ""}, cid=cid))
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    send(_chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                                 "function": {"name": tc["function"]["name"], "arguments": ""}}]}, cid=cid))
                    send(_chunk({"tool_calls": [{"index": i, "function": {"arguments": tc["function"]["arguments"]}}]}, cid=cid))
                send(_chunk({}, "tool_calls", cid))
            else:
                for i in range(0, len(text), 40):
                    send(_chunk({"content": text[i:i + 40]}, cid=cid))
                send(_chunk({}, "stop", cid))
            send("[DONE]")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        message = {"role": "assistant", "content": text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": "fake-model",
                         "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})


def serve(port: int = 18999):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18999
    print(f"fake LLM on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
