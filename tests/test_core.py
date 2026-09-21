import asyncio
import io
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from PIL import Image

from yktmon.ai import Solver, parse_answer
from yktmon.config import ConfigFile, AI, Channel
from yktmon.protocol import Client, ProtocolError, AuthExpired, image_mime, parse_slides
from yktmon.store import Store
from yktmon.runtime import Runtime, Lesson, Bus
from yktmon.notify import Notifier
from yktmon.web import create_app, DataLock

DOMAIN = "changjiang.yuketang.cn"


def picture():
    b = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(b, format="PNG")
    return b.getvalue()


def problem(id="p", **kwargs):
    return dict(
        problem_id=id,
        slide_id="s",
        presentation="ppt",
        course="课程",
        type=1,
        body="题干",
        options=["A", "B"],
        cover="https://images.example/test",
        limit=-1,
        unlocked=time.time(),
        **kwargs,
    )


def test_config_comments_secrets_and_atomic_validation(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        '# Header\ndomain="changjiang.yuketang.cn"\n[ai]\n# Key comment\napi_key="secret"\n',
        encoding="utf-8",
    )
    cfg = ConfigFile(path)
    cfg.save(cfg.prepare({"ai": {"api_key": "", "model": "new-model"}}))
    assert cfg.settings.ai.api_key == "secret"
    assert "# Key comment" in path.read_text("utf-8")
    assert cfg.public()["ai"]["api_key"] == ""
    before = path.read_bytes()
    with pytest.raises(ValueError):
        cfg.prepare({"domain": "bad.example"})
    assert path.read_bytes() == before
    assert cfg.prepare({"ai": {"api_key": "__CLEAR__"}}).ai.api_key == ""
    assert ConfigFile(tmp_path / "unrelated.toml").settings.ai.api_key == ""


@pytest.mark.parametrize(
    "patch",
    [
        {"scan_interval": 0},
        {"ai": {"base_url": "http://x"}},
        {"ai": {"model": ""}},
        {"ai": {"max_tokens": 10}},
        {"channels": [{"kind": "qqbot"}]},
    ],
)
def test_invalid_config(tmp_path, patch):
    with pytest.raises(ValueError):
        ConfigFile(tmp_path / "c.toml").prepare(patch)


def test_store_identity_recovery_and_courses(tmp_path):
    s = Store(tmp_path / "db")
    c = s.course(DOMAIN, "", "same")
    s.enable_course(DOMAIN, c["id"], False)
    assert not s.course(DOMAIN, "class1", "same")["enabled"]
    assert s.course(DOMAIN, "class2", "same")["id"] != c["id"]
    ids = []
    for domain, lesson in [(DOMAIN, "l1"), (DOMAIN, "l2"), ("pro.yuketang.cn", "l1")]:
        ids.append(s.insert(domain, lesson, problem())[0])
    assert len(set(ids)) == 3
    assert not s.insert(DOMAIN, "l1", problem())[1]
    s.update(ids[0], status="processing")
    s.delivery(ids[0], "wecom", "sending")
    s.close()
    s = Store(tmp_path / "db")
    assert s.get(ids[0])["status"] == "pending"
    assert s.get(ids[0])["deliveries"][0]["status"] == "failed"
    s.close()


def test_no_confusion_between_answers_and_reference_answers():
    raw = {
        "slides": [
            {
                "id": "s",
                "coverAlt": "https://i/a?x=1&amp;y=2",
                "problem": {
                    "problemId": "p",
                    "problemType": 2,
                    "body": "Q",
                    "answers": ["A"],
                    "options": [{"body": "A. one"}, "B. two"],
                },
            }
        ]
    }
    p = parse_slides(raw, "ppt", "course")[0]
    assert p["cover"] == "https://i/a?x=1&y=2"
    assert p["options"] == ["A. one", "B. two"]
    assert "answers" not in p


async def test_token_rotation_and_http_errors():
    seen = []

    def handler(r):
        seen.append(r.headers.get("Authorization"))
        return httpx.Response(
            200, headers={"Set-Auth": f"token{len(seen)}"}, json={"code": 0, "data": {}}
        )

    c = Client(DOMAIN, transport=httpx.MockTransport(handler))
    await c.api("GET", "/first")
    await c.api("GET", "/second", jwt=True)
    assert seen == [None, "Bearer token1"]
    assert c.jwt == "token2"
    await c.close()
    c = Client(
        DOMAIN,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 50000})
        ),
    )
    with pytest.raises(AuthExpired):
        await c.api("GET", "/")
    await c.close()


@pytest.mark.parametrize(
    "text,expected",
    [
        ('```json\n{"answer":["A"],"confidence":0.9}\n```', ["A"]),
        ('prefix {garbage} {"answer":"BC"} suffix', ["BC"]),
        ("无法辨认", ["无法辨认"]),
    ],
)
def test_parse_ai(text, expected):
    assert parse_answer(text)["answer"] == expected


async def test_ai_length_retry_and_user_image():
    requests = []

    def handler(r):
        p = json.loads(r.content)
        requests.append(p)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if len(requests) == 1 else "stop",
                        "message": {
                            "content": "" if len(requests) == 1 else '{"answer":["A"]}'
                        },
                    }
                ]
            },
        )

    ai = Solver(
        AI(api_key="test", max_tokens=1024), transport=httpx.MockTransport(handler)
    )
    answer = await ai.solve(problem(), picture(), "image/png")
    assert answer["answer"] == ["A"]
    assert [r["max_tokens"] for r in requests] == [1024, 2048]
    assert requests[0]["messages"][1]["role"] == "user"
    assert requests[0]["messages"][1]["content"][1]["type"] == "image_url"


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "stop", "message": {"content": ""}},
        {"finish_reason": "length", "message": {"content": "partial"}},
        {"finish_reason": "content_filter", "message": {"content": ""}},
    ],
)
async def test_ai_200_is_not_success(choice):
    ai = Solver(
        AI(api_key="test"),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [choice]})
        ),
    )
    with pytest.raises(ProtocolError):
        await ai.complete("test")


async def test_diagnosis_does_not_accept_fake_vision():
    def handler(r):
        if r.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "text-only"}]})
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]},
        )

    result = await Solver(
        AI(api_key="test"), transport=httpx.MockTransport(handler)
    ).diagnose()
    assert result["checks"][0]["ok"]
    assert not result["checks"][1]["ok"]
    assert not result["ok"]
    assert result["available_models"] == ["text-only"]


@pytest.mark.parametrize("data", [b"<html>Login</html>", b"not an image"])
def test_image_validation(data):
    with pytest.raises(ProtocolError):
        image_mime(data)


async def test_lesson_ppt_refresh_unresolved_and_dedupe(tmp_path):
    cfg = ConfigFile(tmp_path / "config")
    store = Store(tmp_path / "db")
    rt = Runtime(cfg, store, tmp_path)
    lesson = Lesson(rt, "l", "course", "cookie")
    calls = []

    class Mock:
        async def api(self, *args, **kwargs):
            calls.append(1)
            return {
                "slides": [
                    {
                        "id": "slide",
                        "cover": "https://i/image",
                        "problem": {
                            "problemId": "p",
                            "problemType": 1,
                            "body": f"version{len(calls)}",
                        },
                    }
                ]
            }

    lesson.client = Mock()
    await lesson.unlock({"prob": "p", "sid": "slide", "pres": "ppt"})
    assert len(store.recent(DOMAIN)) == 1
    await lesson.unlock({"prob": "p", "sid": "slide", "pres": "ppt"})
    assert len(store.recent(DOMAIN)) == 1
    await lesson.handle({"op": "presentationupdated", "presentation": "ppt"})
    assert lesson.cache["p"]["body"] == "version2"
    await lesson.unlock({"prob": "future"})
    assert "future" in lesson.pending
    store.close()


async def test_failed_presentation_can_retry(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    lesson = Lesson(rt, "l", "course", "c")

    class Mock:
        async def api(self, *a, **kw):
            raise ProtocolError("network failed")

    lesson.client = Mock()
    with pytest.raises(ProtocolError):
        await lesson.fetch("pres")
    assert "pres" not in lesson.loaded
    s.close()


async def test_cancel_long_job_is_fast_and_recoverable(tmp_path, monkeypatch):
    cfg = ConfigFile(tmp_path / "c")
    cfg.settings.ai.api_key = "test"
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    entered = asyncio.Event()

    async def stuck(*a, **kw):
        entered.set()
        await asyncio.sleep(100)

    monkeypatch.setattr("yktmon.runtime.image_bytes", stuck)
    await rt.start()
    await rt.accept("l", problem())
    await asyncio.wait_for(entered.wait(), 2)
    started = time.monotonic()
    await rt.stop()
    assert time.monotonic() - started < 1
    assert s.recent(DOMAIN)[0]["status"] == "pending"
    s.close()


async def test_missing_cover_is_explicit_failure(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    p = problem()
    p["cover"] = ""
    id, _ = s.insert(DOMAIN, "l", p)
    await rt.process(id)
    assert s.get(id)["status"] == "failed"
    assert "缺少题面图片" in s.get(id)["error"]
    s.close()


async def test_poll_not_sent_to_ai(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    p = problem()
    p.update(type=3, cover="")
    id, _ = s.insert(DOMAIN, "l", p)
    await rt.process(id)
    assert s.get(id)["status"] == "skipped"
    assert s.get(id)["answer"] == {}
    s.close()


async def test_notifications_are_isolated_and_deduped(tmp_path):
    s = Store(tmp_path / "db")
    id, _ = s.insert(DOMAIN, "l", problem())
    s.update(id, status="done", answer={"answer": ["A"]})
    calls = []

    def handler(r):
        calls.append(r.url.host)
        return httpx.Response(
            200, json={"errcode": 0} if r.url.host == "ok.example" else {"code": 999}
        )

    n = Notifier(s, lambda e: None, transport=httpx.MockTransport(handler))
    channels = [
        Channel(kind="wecom", webhook_url="https://ok.example"),
        Channel(kind="feishu", webhook_url="https://fail.example"),
    ]
    await n.deliver(s.get(id), channels, tmp_path)
    await n.deliver(s.get(id), channels, tmp_path)
    assert calls.count("ok.example") == 1
    assert calls.count("fail.example") == 2
    assert {x["status"] for x in s.get(id)["deliveries"]} == {"sent", "failed"}
    s.close()


async def test_api_configuration_guard_and_isolation(tmp_path):
    path = tmp_path / "config.toml"
    app = create_app(path, tmp_path / "data", autostart=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as c:
            assert (await c.get("/")).status_code == 200
            cfg = (await c.get("/api/config")).json()
            assert cfg["servers"]
            assert cfg["ai"]["api_key"] == ""
            assert (
                await c.put("/api/config", json={"ai": {"api_key": "secret"}})
            ).status_code == 403
            r = await c.put(
                "/api/config",
                headers={"X-Yktmon": "local"},
                json={"ai": {"api_key": "secret"}},
            )
            assert r.status_code == 200
            assert r.json()["ai"]["api_key"] == ""
            assert (
                await c.put(
                    "/api/config",
                    headers={"X-Yktmon": "local", "Origin": "https://evil.example"},
                    json={},
                )
            ).status_code == 403
            assert (
                await c.put(
                    "/api/config",
                    headers={"X-Yktmon": "local"},
                    json={"ai": {"max_tokens": 0}},
                )
            ).status_code == 422
            assert (await c.get("/api/problems?limit=-1")).status_code == 422
            assert (await c.get("/api/problems/123/image")).status_code == 404
    assert path.exists()
    assert (tmp_path / "data" / "monitor.db").exists()


def test_single_instance_lock(tmp_path):
    a = DataLock(tmp_path / "lock")
    b = DataLock(tmp_path / "lock")
    a.acquire()
    try:
        with pytest.raises(RuntimeError):
            b.acquire()
    finally:
        a.release()
    b.acquire()
    b.release()


async def test_bus_backpressure_resync():
    b = Bus()
    q = asyncio.Queue(maxsize=1)
    b.subscribers.add(q)
    b.publish({"type": "x"})
    b.publish({"type": "y"})
    assert await q.get() == {"type": "resync"}


async def test_successful_retry_sends_answer_after_failure_notice(tmp_path):
    s = Store(tmp_path / "db")
    id, _ = s.insert(DOMAIN, "l", problem())
    s.update(id, status="failed", error="no key")
    calls = []

    def handler(r):
        calls.append(json.loads(r.content))
        return httpx.Response(200, json={"errcode": 0})

    n = Notifier(s, lambda e: None, transport=httpx.MockTransport(handler))
    ch = [Channel(kind="wecom", webhook_url="https://mock.example")]
    await n.deliver(s.get(id), ch, tmp_path)
    s.update(id, status="done", answer={"answer": ["B"]}, error="")
    await n.deliver(s.get(id), ch, tmp_path)
    await n.deliver(s.get(id), ch, tmp_path)
    assert len(calls) == 2
    assert "答案：B" in calls[1]["text"]["content"]
    s.close()


@pytest.mark.parametrize("status", [200, 302])
async def test_login_exchange_requires_valid_cookie(status):
    def handler(r):
        if r.method == "POST":
            return httpx.Response(
                status,
                headers={
                    "Set-Cookie": "sessionid=testcookie; Path=/; Secure; HttpOnly"
                },
                text="ok",
            )
        return httpx.Response(200, json={"code": 0, "data": {"name": "test"}})

    c = Client(DOMAIN, transport=httpx.MockTransport(handler))
    assert await c.exchange("user", "auth") == "testcookie"
    await c.close()
    c = Client(
        DOMAIN,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text='<html data-status_code="401">')
        ),
    )
    with pytest.raises(AuthExpired):
        await c.exchange("user", "invalid")
    await c.close()


async def test_real_websocket_reconnect_pipeline(tmp_path, monkeypatch):
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect

    cfg = ConfigFile(tmp_path / "config")
    cfg.settings.ai.api_key = "test"
    store = Store(tmp_path / "db")
    rt = Runtime(cfg, store, tmp_path)
    connections = []
    received = []
    second_connected = asyncio.Event()

    async def server(ws):
        hello = json.loads(await ws.recv())
        received.append(hello)
        connections.append(1)
        await ws.send(
            json.dumps({"op": "hello", "presentation": "pres", "unlockedproblem": []})
        )
        await ws.send(
            json.dumps(
                {
                    "op": "unlockproblem",
                    "problem": {"prob": "p", "sid": "s", "pres": "pres"},
                }
            )
        )
        if len(connections) == 1:
            await ws.close()
            return
        second_connected.set()
        await ws.wait_closed()

    @asynccontextmanager
    async def fake_http(domain, cookie=""):
        def handler(r):
            if r.url.path.endswith("/checkin"):
                return httpx.Response(
                    200,
                    headers={"Set-Auth": "jwt"},
                    json={
                        "code": 0,
                        "data": {"identityId": "uid", "lessonToken": "tok"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "slides": [
                            {
                                "id": "s",
                                "cover": "https://images.example/x",
                                "problem": {
                                    "problemId": "p",
                                    "problemType": 1,
                                    "body": "Q",
                                    "options": ["A", "B"],
                                },
                            }
                        ]
                    },
                },
            )

        c = Client(domain, cookie, transport=httpx.MockTransport(handler))
        try:
            yield c
        finally:
            await c.close()

    async def fake_image(*a, **kw):
        return picture(), "image/png"

    def fake_solver(config):
        def handler(r):
            payload = json.loads(r.content)
            assert payload["messages"][-1]["content"][1]["type"] == "image_url"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": '{"answer":["B"],"reasoning":"mock"}'
                            },
                        }
                    ]
                },
            )

        return Solver(config, transport=httpx.MockTransport(handler))

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]

        @asynccontextmanager
        async def local_socket(domain, cookie=""):
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                yield ws

        monkeypatch.setattr("yktmon.runtime.ClientContext", fake_http)
        monkeypatch.setattr("yktmon.runtime.socket", local_socket)
        monkeypatch.setattr("yktmon.runtime.image_bytes", fake_image)
        monkeypatch.setattr("yktmon.runtime.Solver", fake_solver)
        rt.running = True
        rt.spawn(rt.solve_loop())
        rt.spawn(rt.recover_jobs())
        worker = Lesson(rt, "lesson", "course", "cookie")
        rt.lessons["lesson"] = worker
        worker.task = rt.spawn(worker.run())
        await asyncio.wait_for(second_connected.wait(), 4)
        async with asyncio.timeout(3):
            while (
                not store.recent(DOMAIN) or store.recent(DOMAIN)[0]["status"] != "done"
            ):
                await asyncio.sleep(0.01)
        rows = store.recent(DOMAIN)
        assert len(rows) == 1
        assert rows[0]["answer"]["answer"] == ["B"]
        assert rows[0]["image"]
        assert received[0]["auth"] == "tok"
        assert received[0]["userid"] == "uid"
        await rt.stop()
        assert not rt.tasks
    store.close()


async def test_pending_cover_refresh_preserves_original_deadline(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    store = Store(tmp_path / "db")
    rt = Runtime(cfg, store, tmp_path)
    original = problem()
    original.update(limit=60, unlocked=1234)
    await rt.accept("l", original)
    updated = problem()
    updated["cover"] = "https://images.example/refreshed"
    await rt.accept("l", updated)
    rows = store.recent(DOMAIN)
    assert len(rows) == 1
    assert rows[0]["payload"]["cover"] == updated["cover"]
    assert rows[0]["payload"]["unlocked"] == 1234
    assert rows[0]["payload"]["limit"] == 60
    store.close()


@pytest.mark.parametrize(
    "failures,expected", [(0, 30), (1, 1), (2, 2), (3, 5), (4, 5), (5, 5), (10, 5)]
)
def test_discovery_retry_is_independent_of_scan_interval(failures, expected):
    from yktmon.runtime import discovery_delay

    assert discovery_delay(30, failures) == expected


async def test_discovery_timeout_retries_without_redundant_user_lookup(
    tmp_path, monkeypatch
):
    cfg = ConfigFile(tmp_path / "c")
    store = Store(tmp_path / "db")
    store.save_session(DOMAIN, "mock-cookie")
    rt = Runtime(cfg, store, tmp_path)
    calls, delays = [], []

    @asynccontextmanager
    async def mock_client(*args):
        class Mock:
            async def api(self, method, path):
                calls.append(path)
                if len(calls) == 1:
                    raise ProtocolError("等待响应超时")
                return {"onLessonClassrooms": []}

        yield Mock()

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("yktmon.runtime.ClientContext", mock_client)
    monkeypatch.setattr(rt, "wait_discovery", sleep)
    with pytest.raises(asyncio.CancelledError):
        await rt.discover()
    assert delays == [1, 30]
    assert calls == ["/api/v3/classroom/on-lesson-upcoming-exam"] * 2
    assert rt.last_error == ""
    assert rt.login_state == "已登录"
    store.close()


async def test_http_timeout_reports_safe_endpoint_and_phase():
    def handler(request):
        raise httpx.ReadTimeout("sensitive-cookie-and-url", request=request)

    client = Client(DOMAIN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProtocolError) as caught:
            await client.api("GET", "/api/v3/example?token=secret")
        assert str(caught.value) == "GET /api/v3/example：等待响应超时"
        assert "secret" not in str(caught.value)
    finally:
        await client.close()
