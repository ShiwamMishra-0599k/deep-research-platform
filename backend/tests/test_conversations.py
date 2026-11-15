import uuid

import pytest


def _create(client, user, title=None):
    r = client.post("/api/conversations", json={"title": title} if title else {}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _seed_message(engine_rows, conv_id, role="USER", content="hello"):
    engine_rows(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (:i, :c, :r, :t, now())",
        i=str(uuid.uuid4()), c=conv_id, r=role, t=content,
    )


# ── CRUD ────────────────────────────────────────────────────────────────────

def test_create_defaults_title_and_returns_camel_case(client, alice):
    c = _create(client, alice)
    assert c["title"] == "New chat"
    assert {"id", "title", "createdAt", "updatedAt"} <= set(c)


def test_create_with_title_trims(client, alice):
    assert _create(client, alice, "  Research plan  ")["title"] == "Research plan"


def test_create_blank_title_falls_back_to_default(client, alice):
    assert _create(client, alice, "   ")["title"] == "New chat"


def test_create_rejects_overlong_title(client, alice):
    r = client.post("/api/conversations", json={"title": "x" * 201}, headers=alice["headers"])
    assert r.status_code == 422


def test_list_is_newest_first(client, alice):
    first = _create(client, alice, "first")
    second = _create(client, alice, "second")
    client.patch(f"/api/conversations/{first['id']}", json={"title": "first (renamed)"}, headers=alice["headers"])
    ids = [c["id"] for c in client.get("/api/conversations", headers=alice["headers"]).json()]
    assert set(ids) == {first["id"], second["id"]}


def test_get_one(client, alice):
    c = _create(client, alice, "mine")
    r = client.get(f"/api/conversations/{c['id']}", headers=alice["headers"])
    assert r.status_code == 200 and r.json()["title"] == "mine"


def test_rename(client, alice):
    c = _create(client, alice)
    assert client.patch(f"/api/conversations/{c['id']}", json={"title": " New name "}, headers=alice["headers"]).status_code == 204
    assert client.get(f"/api/conversations/{c['id']}", headers=alice["headers"]).json()["title"] == "New name"


@pytest.mark.parametrize("body,code", [({"title": ""}, 422), ({"title": "   "}, 422), ({}, 422), ({"title": "x" * 201}, 422)])
def test_rename_validation(client, alice, body, code):
    c = _create(client, alice)
    assert client.patch(f"/api/conversations/{c['id']}", json=body, headers=alice["headers"]).status_code == code


def test_delete_removes_conversation_and_cascades_messages(client, alice, engine_rows):
    c = _create(client, alice)
    _seed_message(engine_rows, c["id"])
    assert client.delete(f"/api/conversations/{c['id']}", headers=alice["headers"]).status_code == 204
    assert client.get(f"/api/conversations/{c['id']}", headers=alice["headers"]).status_code == 404
    assert engine_rows("SELECT count(*) FROM messages")[0][0] == 0


def test_messages_listed_in_order_with_lowercase_roles(client, alice, engine_rows):
    c = _create(client, alice)
    _seed_message(engine_rows, c["id"], "USER", "q")
    _seed_message(engine_rows, c["id"], "ASSISTANT", "a")
    msgs = client.get(f"/api/conversations/{c['id']}/messages", headers=alice["headers"]).json()
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "q"), ("assistant", "a")]
    assert {"id", "blocks", "sources", "followUps", "createdAt"} <= set(msgs[0])


def test_invalid_uuid_is_422_not_500(client, alice):
    for path in ("/api/conversations/not-a-uuid", "/api/conversations/123/messages"):
        assert client.get(path, headers=alice["headers"]).status_code == 422


def test_unknown_conversation_404(client, alice):
    assert client.get(f"/api/conversations/{uuid.uuid4()}", headers=alice["headers"]).status_code == 404


# ── authorization / IDOR ────────────────────────────────────────────────────

def test_unauthenticated_requests_are_rejected_everywhere(client, alice):
    c = _create(client, alice)
    cid = c["id"]
    calls = [
        ("get", "/api/conversations"), ("post", "/api/conversations"),
        ("get", f"/api/conversations/{cid}"), ("patch", f"/api/conversations/{cid}"),
        ("delete", f"/api/conversations/{cid}"), ("get", f"/api/conversations/{cid}/messages"),
        ("post", f"/api/conversations/{cid}/query"),
    ]
    for method, path in calls:
        r = getattr(client, method)(path, **({"json": {"query": "x", "title": "x"}} if method in ("post", "patch") else {}))
        assert r.status_code == 401, (method, path, r.status_code)


def test_user_cannot_see_or_touch_another_users_conversation(client, alice, bob, engine_rows):
    c = _create(client, alice, "alice private")
    _seed_message(engine_rows, c["id"], "USER", "alice secret question")
    cid = c["id"]
    h = bob["headers"]

    assert client.get(f"/api/conversations/{cid}", headers=h).status_code == 404
    assert client.get(f"/api/conversations/{cid}/messages", headers=h).status_code == 404
    assert client.patch(f"/api/conversations/{cid}", json={"title": "pwned"}, headers=h).status_code == 404
    assert client.post(f"/api/conversations/{cid}/query", json={"query": "hi"}, headers=h).status_code == 404
    assert client.delete(f"/api/conversations/{cid}", headers=h).status_code == 404

    # 404 (not 403) so existence is not revealed; and nothing changed
    assert cid not in [x["id"] for x in client.get("/api/conversations", headers=h).json()]
    assert client.get(f"/api/conversations/{cid}", headers=alice["headers"]).json()["title"] == "alice private"
    assert engine_rows("SELECT count(*) FROM messages WHERE conversation_id=:c", c=cid)[0][0] == 1  # no message injected


def test_failed_cross_user_query_does_not_persist_anything(client, alice, bob, engine_rows):
    c = _create(client, alice)
    client.post(f"/api/conversations/{c['id']}/query", json={"query": "intruder"}, headers=bob["headers"])
    assert engine_rows("SELECT count(*) FROM messages")[0][0] == 0


def test_list_contains_only_own_conversations(client, alice, bob):
    _create(client, alice, "a1"); _create(client, alice, "a2"); _create(client, bob, "b1")
    assert sorted(c["title"] for c in client.get("/api/conversations", headers=alice["headers"]).json()) == ["a1", "a2"]
    assert [c["title"] for c in client.get("/api/conversations", headers=bob["headers"]).json()] == ["b1"]


def test_sql_injection_strings_are_stored_literally(client, alice, engine_rows):
    nasty = "'; DROP TABLE users; --"
    c = _create(client, alice, nasty)
    assert client.get(f"/api/conversations/{c['id']}", headers=alice["headers"]).json()["title"] == nasty
    assert engine_rows("SELECT count(*) FROM users")[0][0] == 1
    r = client.post("/api/auth/login", json={"email": "alice@example.com' OR '1'='1", "password": "x" * 10})
    assert r.status_code == 422  # not a valid email at all
