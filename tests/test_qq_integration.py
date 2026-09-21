"""Production QQ integration checks. Temporary stores and simulated QQ only."""

import asyncio
import io
import json
import httpx
import pytest
from PIL import Image
from yktmon.config import ConfigFile
from yktmon.store import Store
from yktmon.qq_store import QQStore
from yktmon.qq_client import QQClient, QQError
from yktmon.qq_service import QQService, result_markdown
from yktmon.runtime import Runtime
from yktmon.ai import SYSTEM, parse_answer
from yktmon.web import create_app

D = "changjiang.yuketang.cn"


def png():
    out = io.BytesIO()
    Image.new("RGB", (30, 30), "white").save(out, format="PNG")
    return out.getvalue()


def setup(tmp):
    cfg = ConfigFile(tmp / "config.toml")
    cfg.save(
        cfg.prepare(
            {"qq": {"enabled": True, "app_id": "123456", "secret": "dummy-qq-secret"}}
        )
    )
    store = Store(tmp / "db")
    images = tmp / "images"
    images.mkdir(exist_ok=True)
    q = QQService(cfg, store, images, check_pilot=False)
    course = store.course(D, "c", "课程")
    record = store.create_record(D, course["id"], "本节", True)
    store.bind_record(D, "l", record["id"])
    id, _ = store.insert(
        D,
        "l",
        {
            "problem_id": "p",
            "type": 1,
            "course": "课程",
            "body": "题干",
            "options": [],
            "cover": "",
            "limit": -1,
            "unlocked": 1,
        },
    )
    return cfg, store, q, course, id


class FakeClient:
    def __init__(self, app_id="123456", secret="", event=None):
        self.app_id = app_id
        self.event = event
        self.state = "已连接"
        self.error = ""
        self.name = "mock"
        self.last_refresh = 0
        self.last_heartbeat = 0
        self.last_reconnect = 0
        self.calls = []

    def start(self):
        pass

    async def close(self):
        self.state = "已断开"

    async def send(self, group, payload, images):
        self.calls.append((group, payload))
        return "m" + str(len(self.calls))


async def test_groups_mapping_survive_restart_credentials_redacted(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    group = q.repo.bind("123456", "internal-group", "同学群")
    q.repo.set_mapping(c["id"], "123456", [group])
    q.capture(s.get(id))
    before = q.repo.history("123456")
    assert len(before) == 1
    assert "dummy-qq-secret" not in json.dumps(
        q.status()
    ) and "internal-group" not in json.dumps(q.status())
    s.close()
    cfg2 = ConfigFile(tmp_path / "config.toml")
    s2 = Store(tmp_path / "db")
    q2 = QQService(
        cfg2, s2, tmp_path / "images", client_factory=FakeClient, check_pilot=False
    )
    assert cfg2.settings.qq.secret == "dummy-qq-secret"
    assert (
        q2.repo.mapping(c["id"], "123456") == [group]
        and q2.repo.groups("123456")[0]["name"] == "同学群"
    )
    await q2.start()
    assert q2.client.state == "已连接"
    await q2.stop()
    assert q2.repo.groups("123456")
    s2.close()


async def test_binding_sessions_expiry_wrong_codes_multiple_groups(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    q.client = FakeClient()
    binding = q.new_binding()

    def event(group, code):
        q.event(
            {
                "t": "GROUP_AT_MESSAGE_CREATE",
                "d": {"group_openid": group, "content": "<@123> 绑定 " + code},
            }
        )

    event("g1", "wrong")
    assert not q.binding["candidates"]
    event("g1", binding["code"])
    event("g1", binding["code"])
    event("g2", binding["code"])
    assert len(q.binding["candidates"]) == 2
    candidate = next(iter(q.binding["candidates"]))
    with pytest.raises(ValueError):
        q.confirm_binding("old-session", candidate, "name")
    q.confirm_binding(binding["id"], candidate, "同学群")
    assert len(q.repo.groups("123456")) == 1 and q.binding is None
    q.new_binding()
    q.binding["until"] = 0
    event("g3", q.binding["code"])
    assert not q.binding["candidates"]
    with pytest.raises(ValueError):
        q.confirm_binding(q.binding["id"], "none", "name")
    # Ordinary chat is never persisted or answered.
    q.event(
        {
            "t": "GROUP_MESSAGE_CREATE",
            "d": {"group_openid": "g", "content": "private ordinary conversation"},
        }
    )
    assert "private ordinary conversation" not in json.dumps(q.status())
    await q.stop()
    s.close()


async def test_snapshot_targets_multi_group_stage_order_dedupe(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g1 = q.repo.bind("123456", "g1", "一群")
    g2 = q.repo.bind("123456", "g2", "二群")
    g3 = q.repo.bind("123456", "g3", "三群")
    q.repo.set_mapping(c["id"], "123456", [g1, g2])
    q.capture(s.get(id))
    q.repo.set_mapping(c["id"], "123456", [g3])
    (q.image_root / "p.png").write_bytes(png())
    s.update(id, image="p.png")
    q.enqueue_phase(s.get(id), "image")
    s.update(
        id,
        status="done",
        answer={
            "answer": ["B"],
            "reasoning": "$$\n\\frac{1}{3}\n$$",
            "confidence": 0.9,
        },
    )
    q.enqueue_phase(s.get(id), "result")
    q.enqueue_phase(s.get(id), "result")
    q.capture(s.get(id))
    assert len(q.repo.history("123456")) == 6
    fake = FakeClient()
    q.client = fake
    await asyncio.gather(q.drain(g1), q.drain(g2))
    assert len(fake.calls) == 6
    for group in ("g1", "g2"):
        assert [p["kind"] for g, p in fake.calls if g == group] == [
            "markdown",
            "image",
            "markdown",
        ]
    assert all(g != "g3" for g, p in fake.calls)
    assert all(r["state"] == "sent" for r in q.repo.history("123456"))
    await q.stop()
    s.close()


async def test_no_targets_no_history_backfill(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    q.capture(s.get(id))
    group = q.repo.bind("123456", "g", "later")
    q.repo.set_mapping(c["id"], "123456", [group])
    q.capture(s.get(id))
    q.enqueue_phase(s.get(id), "result")
    assert q.repo.history("123456") == []
    await q.stop()
    s.close()


async def test_unknown_is_not_retried_and_restart_preserves_it(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.capture(s.get(id))

    class F(FakeClient):
        async def send(self, *a):
            raise QQError("response lost", unknown=True)

    q.client = F()
    await q.drain(g)
    row = q.repo.history("123456")[0]
    assert row["state"] == "unknown"
    with pytest.raises(ValueError):
        q.retry(row["id"])
    assert q.repo.next(g, "123456") is None
    q.retry(row["id"], True)
    assert q.repo.get(row["id"])["state"] == "pending"
    q.repo.claim(row["id"])
    await q.stop()
    s.close()
    s = Store(tmp_path / "db")
    repo = QQStore(s)
    assert repo.get(row["id"])["state"] == "unknown"
    s.close()


async def test_one_group_failure_does_not_block_other(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g1 = q.repo.bind("123456", "g1", "one")
    g2 = q.repo.bind("123456", "g2", "two")
    q.repo.set_mapping(c["id"], "123456", [g1, g2])
    q.capture(s.get(id))

    class F(FakeClient):
        async def send(self, group, *a):
            if group == "g1":
                raise QQError("forbidden", code=403)
            return "ok"

    q.client = F()
    await asyncio.gather(q.drain(g1), q.drain(g2))
    rows = q.repo.history("123456")
    assert {r["state"] for r in rows} == {"failed", "sent"}
    await q.stop()
    s.close()


async def test_disconnect_during_send_unknown_without_losing_binding(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.capture(s.get(id))
    entered = asyncio.Event()

    class F(FakeClient):
        async def send(self, *a):
            entered.set()
            await asyncio.sleep(100)

    q.client = F()
    q.workers[g] = asyncio.create_task(q.drain(g))
    await entered.wait()
    await q.stop()
    assert q.repo.history("123456")[0]["state"] == "unknown"
    assert q.repo.groups("123456") and cfg.settings.qq.secret
    s.close()


async def test_token_lock_proactive_upload_markdown_no_reply_ids(tmp_path):
    calls = []

    def handler(r):
        payload = json.loads(r.content) if r.content else {}
        calls.append((r.url.path, payload))
        if r.url.host == "bots.qq.com":
            return httpx.Response(
                200, json={"access_token": "dummy-token", "expires_in": 7200}
            )
        if r.url.path.endswith("/files"):
            return httpx.Response(200, json={"file_info": "media"})
        return httpx.Response(200, json={"id": "message"})

    client = QQClient(
        "123456", "secret", lambda e: None, transport=httpx.MockTransport(handler)
    )
    await asyncio.gather(*(client.auth() for _ in range(8)))
    assert len(calls) == 1
    (tmp_path / "q.png").write_bytes(png())
    await client.send(
        "group", {"kind": "image", "image": "q.png", "caption": "题1"}, tmp_path
    )
    await client.send(
        "group", {"kind": "markdown", "text": "$$\n\\frac{1}{3}\n$$"}, tmp_path
    )
    messages = [p for path, p in calls if path.endswith("/messages")]
    assert [m["msg_type"] for m in messages] == [7, 2]
    assert all("msg_id" not in m and "event_id" not in m for m in messages)
    assert [p for path, p in calls if path.endswith("/files")][0][
        "srv_send_msg"
    ] is False
    assert messages[1]["markdown"]["content"] == "$$\n\\frac{1}{3}\n$$"
    client.expiry = 0
    await client.auth()
    assert len([1 for path, p in calls if path.endswith("getAppAccessToken")]) == 2
    await client.close()


@pytest.mark.parametrize(
    "http_status,unknown,retry",
    [(403, False, False), (429, False, True), (503, True, False)],
)
async def test_send_errors_classified(http_status, unknown, retry):
    def handler(r):
        if r.url.host == "bots.qq.com":
            return httpx.Response(
                200, json={"access_token": "dummy", "expires_in": 7200}
            )
        return httpx.Response(
            http_status,
            json={"code": 123, "message": "secret material should not leak"},
        )

    c = QQClient("123456", "s", lambda e: None, transport=httpx.MockTransport(handler))
    with pytest.raises(QQError) as exc:
        await c.request("POST", "/messages", sending=True, json={})
    assert exc.value.unknown == unknown and exc.value.retry == retry
    assert "secret material" not in str(exc.value)
    await c.close()


async def test_401_explicit_rejection_refreshes_once():
    tokens = 0
    requests = 0

    def handler(r):
        nonlocal tokens, requests
        if r.url.host == "bots.qq.com":
            tokens += 1
            return httpx.Response(
                200, json={"access_token": "dummy" + str(tokens), "expires_in": 7200}
            )
        requests += 1
        return (
            httpx.Response(401, json={"code": 401})
            if requests == 1
            else httpx.Response(200, json={"id": "m"})
        )

    c = QQClient("123456", "s", lambda e: None, transport=httpx.MockTransport(handler))
    assert (await c.request("POST", "/messages", sending=True, json={}))["id"] == "m"
    assert tokens == 2 and requests == 2
    await c.close()


async def test_api_persistence_secret_guard_mapping_and_preview(tmp_path, monkeypatch):
    app = create_app(tmp_path / "c", tmp_path / "data", autostart=False)
    async with app.router.lifespan_context(app):
        rt = app.state.runtime
        rt.qq.client_factory = FakeClient
        rt.qq.check_pilot = False
        course = rt.store.course(D, "c", "course")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"X-Yktmon": "local"},
        ) as c:
            r = await c.post(
                "/api/qq/config",
                json={"app_id": "123456", "secret": "dummy-private", "enabled": True},
            )
            assert r.status_code == 200 and "dummy-private" not in r.text
            b = (await c.post("/api/qq/binding")).json()["binding"]
            rt.qq.event(
                {
                    "t": "GROUP_AT_MESSAGE_CREATE",
                    "d": {"group_openid": "g", "content": "绑定 " + b["code"]},
                }
            )
            candidate = rt.qq.status()["binding"]["candidates"][0]["id"]
            r = await c.post(
                "/api/qq/binding/confirm",
                json={"id": b["id"], "candidate": candidate, "name": "测试群"},
            )
            assert r.status_code == 200
            gid = r.json()["groups"][0]["id"]
            assert (
                await c.put(
                    f"/api/courses/{course['id']}/qq-targets", json={"group_ids": [gid]}
                )
            ).status_code == 200
            await c.post("/api/qq/disconnect")
            assert (await c.get("/api/qq/status")).json()["groups"]
            assert (
                await c.post(
                    "/api/qq/config",
                    json={"app_id": "987654", "secret": "", "enabled": True},
                )
            ).status_code == 422
            assert (await c.get("/api/config")).json()["qq"]["secret"] == ""
            await c.post("/api/qq/clear")
            assert ConfigFile(tmp_path / "c").settings.qq.secret == ""
            assert rt.qq.repo.groups("123456")


async def test_formula_prompt_empty_answer_and_json_backslashes(tmp_path):
    assert "单美元" in SYSTEM and "双美元" in SYSTEM and "JSON" in SYSTEM
    cfg, s, q, c, id = setup(tmp_path)
    text = json.dumps(
        {
            "answer": [],
            "reasoning": r"占位模板，无法求出 $\frac{1}{3}$",
            "confidence": 0.99,
        },
        ensure_ascii=False,
    )
    a = parse_answer(text)
    assert "\\frac" in a["reasoning"] and "\x0c" not in a["reasoning"]
    s.update(id, answer=a)
    row = s.get(id)
    record = s.record(D, row["record_id"])
    md = result_markdown(row, record)
    assert "无法作答" in md and "99%" in md and "无法作答判断" in md and "\\frac" in md
    await q.stop()
    s.close()


class Gateway:
    def __init__(self, events):
        self.events = asyncio.Queue()
        self.sent = []
        for event in events:
            self.events.put_nowait(json.dumps(event))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def recv(self):
        return await self.events.get()

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        pass


def gateway_transport(r):
    if r.url.host == "bots.qq.com":
        return httpx.Response(
            200, json={"access_token": "mock-token", "expires_in": 7200}
        )
    return httpx.Response(200, json={"url": "wss://gateway.example/ws"})


async def test_gateway_resume_invalid_session_and_stop():
    sockets = [
        Gateway(
            [
                {"op": 10, "d": {"heartbeat_interval": 5000}},
                {
                    "op": 0,
                    "t": "READY",
                    "s": 10,
                    "d": {"session_id": "session", "user": {"username": "bot"}},
                },
                {"op": 7},
            ]
        ),
        Gateway(
            [
                {"op": 10, "d": {"heartbeat_interval": 5000}},
                {"op": 0, "t": "RESUMED", "s": 11, "d": {}},
            ]
        ),
    ]

    def connector(*a, **kw):
        return sockets.pop(0)

    first, second = sockets
    c = QQClient(
        "123456",
        "secret",
        lambda e: None,
        transport=httpx.MockTransport(gateway_transport),
        connector=connector,
    )
    c.start()
    async with asyncio.timeout(3):
        while c.seq != 11:
            await asyncio.sleep(0.01)
    assert first.sent[0]["op"] == 2 and second.sent[0]["op"] == 6
    assert (
        second.sent[0]["d"]["session_id"] == "session"
        and second.sent[0]["d"]["seq"] == 10
    )
    assert c.state == "已连接" and c.error == ""
    await c.close()
    assert c.task is None


async def test_gateway_invalid_session_reidentifies():
    first = Gateway([{"op": 10, "d": {"heartbeat_interval": 5000}}, {"op": 9}])
    second = Gateway(
        [
            {"op": 10, "d": {"heartbeat_interval": 5000}},
            {"op": 0, "t": "READY", "s": 2, "d": {"session_id": "new"}},
        ]
    )
    sockets = [first, second]
    c = QQClient(
        "123456",
        "secret",
        lambda e: None,
        transport=httpx.MockTransport(gateway_transport),
        connector=lambda *a, **kw: sockets.pop(0),
    )
    c.session = "old"
    c.seq = 1
    c.start()
    async with asyncio.timeout(3):
        while c.session != "new":
            await asyncio.sleep(0.01)
    assert first.sent[0]["op"] == 6 and second.sent[0]["op"] == 2
    await c.close()


async def test_heartbeat_exception_is_observed_and_reconnected():
    first = Gateway(
        [
            {"op": 10, "d": {"heartbeat_interval": 1000}},
            {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "s"}},
        ]
    )
    second = Gateway(
        [
            {"op": 10, "d": {"heartbeat_interval": 5000}},
            {"op": 0, "t": "RESUMED", "s": 2, "d": {}},
        ]
    )
    sockets = [first, second]
    c = QQClient(
        "123456",
        "secret",
        lambda e: None,
        transport=httpx.MockTransport(gateway_transport),
        connector=lambda *a, **kw: sockets.pop(0),
    )
    c.start()
    async with asyncio.timeout(5):
        while c.seq != 2:
            await asyncio.sleep(0.01)
    assert c.last_reconnect and c.state == "已连接"
    await c.close()


@pytest.mark.parametrize("ttl", [0, 1, 2, "bad"])
async def test_short_or_invalid_token_lifetime_stops_without_loop(ttl):
    c = QQClient(
        "123456",
        "secret",
        lambda e: None,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"access_token": "token", "expires_in": ttl}
            )
        ),
    )
    with pytest.raises(QQError) as exc:
        await c.auth()
    assert exc.value.permanent and not c.token
    await c.close()


async def test_permanent_auth_failure_stops_gateway():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(401, json={})

    c = QQClient(
        "123456", "secret", lambda e: None, transport=httpx.MockTransport(handler)
    )
    c.start()
    await asyncio.wait_for(c.task, 1)
    assert len(calls) == 1 and c.state == "凭据或网关配置错误"
    await c.close()


async def test_rate_limit_retries_are_bounded(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.capture(s.get(id))

    class F(FakeClient):
        async def send(self, *a):
            raise QQError("rate limited", retry=True, code=429)

    q.client = F()
    for _ in range(3):
        with s.db:
            s.db.execute("UPDATE qq_outbox SET next_at=0")
        await q.drain(g)
    row = q.repo.history("123456")[0]
    assert row["state"] == "failed" and row["attempts"] == 3
    await q.stop()
    s.close()


async def test_app_switch_cancels_old_target_and_group_disable_cancels_pending(
    tmp_path,
):
    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.capture(s.get(id))
    q.repo.cancel_app("123456")
    assert q.repo.history("123456")[0]["state"] == "cancelled"
    cfg.save(cfg.prepare({"qq": {"app_id": "987654", "secret": "new-secret"}}))
    s.update(id, status="done", answer={"answer": ["A"]})
    q.enqueue_phase(s.get(id), "result")
    assert all(r["state"] == "cancelled" for r in q.repo.history("123456"))
    assert q.repo.groups("987654") == [] and q.repo.mapping(c["id"], "987654") == []
    await q.stop()
    s.close()


async def test_single_app_instance_guard(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    q.client_factory = FakeClient
    other = QQService(
        cfg, s, tmp_path / "images", client_factory=FakeClient, check_pilot=False
    )
    await q.start()
    await other.start()
    assert q.client and other.client is None and "其他正式" in other.error
    await q.stop()
    await other.start()
    assert other.client
    await other.stop()
    s.close()


async def test_runtime_end_to_end_enqueues_existing_solver_result(
    tmp_path, monkeypatch
):
    cfg = ConfigFile(tmp_path / "config")
    cfg.save(
        cfg.prepare({"qq": {"app_id": "123456", "secret": "dummy", "enabled": True}})
    )
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    rt.running = True
    course = s.course(D, "c", "课程")
    record = s.create_record(D, course["id"], "第一节", True)
    s.bind_record(D, "remote", record["id"])
    g = rt.qq.repo.bind("123456", "g", "群")
    rt.qq.repo.set_mapping(course["id"], "123456", [g])

    async def image(*a, **kw):
        return png(), "image/png"

    calls = []

    class Solver:
        def __init__(self, *a):
            pass

        async def solve(self, *a):
            calls.append(1)
            return {"answer": ["B"], "reasoning": "$x^2$", "confidence": 0.8}

    monkeypatch.setattr("yktmon.runtime.image_bytes", image)
    monkeypatch.setattr("yktmon.runtime.Solver", Solver)
    await rt.accept(
        "remote",
        {
            "problem_id": "new",
            "course": "课程",
            "type": 1,
            "body": "Q",
            "options": [],
            "cover": "https://example.com/p",
            "limit": -1,
            "unlocked": 1,
        },
    )
    id = s.recent(D)[0]["id"]
    await rt.process(id)
    history = rt.qq.repo.history("123456")
    assert {x["phase"] for x in history} == {"reminder", "image", "result"}
    assert len(calls) == 1
    # Merely reopening records doesn't invoke QQ or model and doesn't enqueue anything.
    s.record_problems(D, record["id"])
    assert len(rt.qq.repo.history("123456")) == 3
    await rt.close()
    s.close()


async def test_restart_reconciles_only_snapshots_without_resending_on_rename(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.capture(s.get(id))
    s.update(
        id,
        status="done",
        answer={"answer": ["B"], "reasoning": "ok", "confidence": 0.9},
    )
    # Simulate a crash after result persistence but before result enqueue.
    await q.stop()
    s.close()
    s = Store(tmp_path / "db")
    q = QQService(cfg, s, tmp_path / "images", check_pilot=False)
    assert {r["phase"] for r in q.repo.history("123456")} == {"reminder", "result"}
    q.client = FakeClient()
    await q.drain(g)
    course = s.courses(D)[0]
    s.rename_course(D, course["id"], "renamed")
    q.enqueue_phase(s.get(id), "result")
    q.enqueue_phase(s.get(id), "reminder")
    assert len(q.repo.history("123456")) == 2 and all(
        r["state"] == "sent" for r in q.repo.history("123456")
    )
    await q.stop()
    s.close()


async def test_test_request_idempotency_and_disable_pending(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.client = FakeClient()
    a = q.test(g, "text", "stable-click-123")
    b = q.test(g, "text", "stable-click-123")
    assert a == b
    await q.update_group(g, enabled=False)
    assert q.repo.get(a)["state"] == "cancelled"
    with pytest.raises(ValueError):
        q.test(g, "text", "second-click")
    await q.stop()
    s.close()


async def test_three_message_templates_match_live_preview_and_do_not_resend(tmp_path):
    from yktmon.qq_service import question_caption, reminder_markdown
    from yktmon.presentation import markdown

    cfg, store, service, course, problem_id = setup(tmp_path)
    group_id = service.repo.bind("123456", "g", "测试群")
    service.repo.set_mapping(course["id"], "123456", [group_id])
    (service.image_root / "sample.png").write_bytes(png())
    store.update(
        problem_id,
        image="sample.png",
        status="done",
        answer={
            "answer": ["B"],
            "reasoning": "保持原解析 $x^2$。",
            "confidence": 0.9,
        },
    )
    row = store.get(problem_id)
    record = store.record(D, row["record_id"])
    caption = question_caption(row, record, service.display_number(row))
    expected_reminder = (
        f"# 新题提醒！\n\n**{caption}**\n\n题型：单选（*大肥鱼正在做题喵～*）"
    )
    assert caption == "课程 · 本节 · 题目 1"
    assert reminder_markdown(row, record, 1) == expected_reminder
    service.capture(row)
    service.enqueue_phase(row, "image")
    service.enqueue_phase(row, "result")
    messages = {
        entry["phase"]: json.loads(entry["payload"])
        for entry in store.db.execute("SELECT phase,payload FROM qq_outbox")
    }
    assert messages["reminder"]["text"] == expected_reminder
    assert messages["image"]["caption"] == caption
    assert "原题图片" not in messages["image"]["caption"]
    assert messages["result"]["text"].startswith(f"**{caption}**\n\n**答案**")
    assert not messages["result"]["text"].startswith("#")
    assert "**解析**\n\n保持原解析 $x^2$。" in messages["result"]["text"]
    assert "**可信度**：90%（模型自评）" in messages["result"]["text"]
    # Reopening the app must preserve sent payloads and their dedupe keys.
    for entry in service.repo.history("123456"):
        service.repo.update(entry["id"], "sent", message_id="mock-sent")
    service.capture(row)
    service.enqueue_phase(row, "image")
    service.enqueue_phase(row, "result")
    assert len(service.repo.history("123456")) == 3
    await service.stop()
    store.close()
    app = create_app(
        tmp_path / "config.toml", tmp_path, autostart=False, qq_autostart=False
    )
    # create_app uses monitor.db; point the saved test DB at that expected name.
    (tmp_path / "db").rename(tmp_path / "monitor.db")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            response = await client.get(
                f"/api/problems/{problem_id}/notification-preview"
            )
            assert response.status_code == 200
            preview = response.json()
            assert preview["reminder"] == messages["reminder"]["text"]
            assert preview["result"] == messages["result"]["text"]
            assert preview["image_caption"] == messages["image"]["caption"]
            assert preview["reminder_html"] == markdown(preview["reminder"])
            assert "<h1>新题提醒！</h1>" in preview["reminder_html"]
            assert "<em>大肥鱼正在做题喵～</em>" in preview["reminder_html"]
            assert "<h1>" not in preview["result_html"]
            assert len(app.state.runtime.qq.repo.history("123456")) == 3


async def test_real_image_send_uses_markdown_for_bold_caption(tmp_path):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.host == "bots.qq.com":
            return httpx.Response(
                200, json={"access_token": "mock", "expires_in": 7200}
            )
        return httpx.Response(200, json={"id": "bold-image"})

    (tmp_path / "p.png").write_bytes(png())
    c = QQClient(
        "123456", "mock", lambda e: None, transport=httpx.MockTransport(handler)
    )
    result = await c.send(
        "g",
        {
            "kind": "image",
            "image": "p.png",
            "caption": "课程 · 本节 · 题目 1",
            "markdown_url": "https://images.example/p.png?token=dummy",
            "prefer_bold": True,
        },
        tmp_path,
    )
    sent = [d for path, d in calls if path.endswith("/messages")]
    assert len(sent) == 1 and sent[0]["msg_type"] == 2
    assert sent[0]["markdown"]["content"].endswith("**课程 · 本节 · 题目 1**")
    assert "![题面 #" in sent[0]["markdown"]["content"]
    assert sent[0]["markdown"]["force_verify_image_resource"] is True
    assert "content" not in sent[0] and "media" not in sent[0]
    assert not any(path.endswith("/files") for path, d in calls)
    assert result == "bold-image" and result.note == ""
    await c.close()


async def test_explicit_markdown_image_rejection_has_visible_native_fallback(tmp_path):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.host == "bots.qq.com":
            return httpx.Response(
                200, json={"access_token": "mock", "expires_in": 7200}
            )
        if payload.get("msg_type") == 2:
            return httpx.Response(400, json={"code": 123})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"file_info": "native"})
        return httpx.Response(200, json={"id": "fallback"})

    (tmp_path / "p.png").write_bytes(png())
    c = QQClient(
        "123456", "mock", lambda e: None, transport=httpx.MockTransport(handler)
    )
    result = await c.send(
        "g",
        {
            "kind": "image",
            "image": "p.png",
            "caption": "课 · 题目 1",
            "markdown_url": "https://images.example/p.png",
        },
        tmp_path,
    )
    sent = [d for path, d in calls if path.endswith("/messages")]
    assert [d["msg_type"] for d in sent] == [2, 7]
    assert "未加粗" in result.note and result == "fallback"
    assert sent[1]["content"] == "课 · 题目 1"
    await c.close()


@pytest.mark.parametrize("failure", ["timeout", "server", "missing-id"])
async def test_ambiguous_markdown_image_send_never_falls_back(tmp_path, failure):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.host == "bots.qq.com":
            return httpx.Response(
                200, json={"access_token": "mock", "expires_in": 7200}
            )
        assert payload["msg_type"] == 2
        if failure == "timeout":
            raise httpx.ReadTimeout("private url", request=request)
        if failure == "server":
            return httpx.Response(503, json={"code": 503})
        return httpx.Response(200, json={})

    (tmp_path / "p.png").write_bytes(png())
    c = QQClient(
        "123456", "mock", lambda e: None, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(QQError) as exc:
        await c.send(
            "g",
            {
                "kind": "image",
                "image": "p.png",
                "caption": "课",
                "markdown_url": "https://images.example/p.png",
            },
            tmp_path,
        )
    assert exc.value.unknown
    assert len([p for p in calls if p.endswith("/messages")]) == 1
    assert not any(p.endswith("/files") for p in calls)
    await c.close()


async def test_render_downgrade_is_recorded_as_sent_with_note(tmp_path):
    from yktmon.qq_client import DeliveryID

    cfg, s, q, c, id = setup(tmp_path)
    g = q.repo.bind("123456", "g", "group")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.capture(s.get(id))

    class F(FakeClient):
        async def send(self, *args):
            return DeliveryID("m", "已降级为本地图片；未加粗")

    q.client = F()
    await q.drain(g)
    result = q.repo.history("123456")[0]
    assert (
        result["state"] == "sent"
        and result["message_id"] == "m"
        and "未加粗" in result["error"]
    )
    await q.stop()
    s.close()


async def test_lesson_notices_durable_and_separate_from_manual_actions(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    record = s.get(id)["record_id"]
    g = q.repo.bind("123456", "g", "群")
    q.repo.set_mapping(c["id"], "123456", [g])
    q.lesson_notice(D, "remote", record)
    q.lesson_notice(D, "remote", record)
    assert len(q.repo.history("123456")) == 1
    entry = q.repo.history("123456")[0]
    payload = json.loads(q.repo.get(entry["id"])["payload"])
    assert payload["text"].startswith("# 上课啦！") and "大肥鱼" in payload["text"]
    s.record_state(D, record, "paused")
    s.record_state(D, record, "archived")
    assert len(q.repo.history("123456")) == 1
    q.lesson_notice(D, "remote", record, finished=True)
    q.lesson_notice(D, "remote", record, finished=True)
    assert {r["phase"] for r in q.repo.history("123456")} == {
        "lesson_started",
        "lesson_finished",
    }
    await q.stop()
    s.close()
    s = Store(tmp_path / "db")
    q = QQService(cfg, s, tmp_path / "images", check_pilot=False)
    q.lesson_notice(D, "remote", record)
    q.lesson_notice(D, "remote", record, finished=True)
    assert len(q.repo.history("123456")) == 2
    q.lesson_notice(D, "next-remote", record)
    assert len(q.repo.history("123456")) == 3
    await q.stop()
    s.close()


async def test_lesson_notice_uses_only_course_targets(tmp_path):
    cfg, s, q, c, id = setup(tmp_path)
    record = s.get(id)["record_id"]
    selected = q.repo.bind("123456", "chosen", "选中的群")
    q.repo.bind("123456", "other", "未选择的群")
    q.lesson_notice(D, "before-binding", record)
    q.repo.set_mapping(c["id"], "123456", [selected])
    q.lesson_notice(D, "before-binding", record)
    assert q.repo.history("123456") == []
    q.lesson_notice(D, "new-lesson", record)
    assert [r["group_name"] for r in q.repo.history("123456")] == ["选中的群"]
    await q.stop()
    s.close()
