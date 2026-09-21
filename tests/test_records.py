from __future__ import annotations
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
import httpx
import pytest
from yktmon.ai import parse_answer
from yktmon.config import ConfigFile
from yktmon.store import Store, SCHEMA
from yktmon.runtime import Runtime
from yktmon.web import create_app
from yktmon.model_catalog import model_urls, fetch_models
from yktmon.presentation import present, notification_text

DOMAIN = "changjiang.yuketang.cn"


def p(id="p"):
    return dict(
        problem_id=id,
        course="原名",
        type=1,
        body="题目",
        options=[],
        cover="",
        limit=-1,
        unlocked=1,
    )


def test_empty_answer_is_valid_unanswerable():
    a = parse_answer(
        '{"answer":[],"reasoning":"题面只有占位模板，无法作答","confidence":0.99}'
    )
    assert a["structured"] and a["unanswerable"] and a["answer"] == []
    row = {"answer": a, "payload": p(), "problem": "p"}
    d = present(row)
    assert "无法作答" in d["display"]["answer_html"]
    assert "99%" == d["display"]["confidence"]
    assert "无法作答判断的自评" in notification_text(row)
    old = present({**row, "answer": {"answer": [json.dumps(a)], "structured": False}})
    assert old["answer"]["unanswerable"]


def test_empty_answer_without_reason_is_not_accepted():
    assert not parse_answer('{"answer":[]}')["structured"]


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://a.example",
            ["https://a.example/v1/models", "https://a.example/models"],
        ),
        ("https://a.example/v1/", ["https://a.example/v1/models"]),
        (
            "https://a.example/api/v4",
            ["https://a.example/api/v4/models", "https://a.example/api/v4/v1/models"],
        ),
        ("https://a.example/v1/chat/completions", ["https://a.example/v1/models"]),
    ],
)
def test_model_discovery_candidates(url, expected):
    assert model_urls(url) == expected


async def test_model_discovery_fallback_and_sort():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer mock-key"
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        return httpx.Response(
            200, json={"data": [{"id": "z"}, {"id": "a"}, {"id": "a"}]}
        )

    result = await fetch_models(
        "https://a.example", "mock-key", transport=httpx.MockTransport(handler)
    )
    assert result["models"] == ["a", "z"] and paths == ["/v1/models", "/models"]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_model_auth_error_does_not_probe_more_paths_or_leak_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret mock-key")

    with pytest.raises(Exception) as caught:
        await fetch_models(
            "https://a.example", "mock-key", transport=httpx.MockTransport(handler)
        )
    assert (
        len(calls) == 1
        and "secret" not in str(caught.value)
        and "mock-key" not in str(caught.value)
    )


async def test_model_slug_response():
    result = await fetch_models(
        "https://a.example/v4",
        "k",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"models": [{"slug": "x"}]})
        ),
    )
    assert result["models"] == ["x"]


def test_course_display_name_not_overwritten_by_discovery(tmp_path):
    s = Store(tmp_path / "db")
    c = s.course(DOMAIN, "id", "原名")
    s.rename_course(DOMAIN, c["id"], "数据结构")
    discovered = s.course(DOMAIN, "id", "原名更新")
    assert discovered["display_name"] == "数据结构" and discovered["name"] == "原名更新"
    r = s.create_record(DOMAIN, c["id"], "栈")
    assert r["course_name"] == "数据结构" and r["source_name"] == "原名更新"
    s.close()


def test_multiple_records_resume_new_record_and_remote_rebinding(tmp_path):
    s = Store(tmp_path / "db")
    c = s.course(DOMAIN, "id", "原名")
    first = s.create_record(DOMAIN, c["id"], "第一次", True)
    s.bind_record(DOMAIN, "remote1", first["id"])
    one, created = s.insert(DOMAIN, "remote1", p())
    assert created
    s.record_state(DOMAIN, first["id"], "archived")
    second = s.create_record(DOMAIN, c["id"], "第二次", True)
    s.bind_record(DOMAIN, "remote1", second["id"])
    duplicate, created = s.insert(DOMAIN, "remote1", p())
    assert not created and duplicate == one
    assert s.get(one)["record_id"] == first["id"]
    s.insert(DOMAIN, "remote1", p("new"))
    assert len(s.record_problems(DOMAIN, second["id"])) == 1
    s.record_state(DOMAIN, second["id"], "paused")
    s.record_state(DOMAIN, first["id"], "waiting")
    s.bind_record(DOMAIN, "remote2", first["id"])
    s.insert(DOMAIN, "remote2", p("later"))
    assert len(s.record_problems(DOMAIN, first["id"])) == 2
    with pytest.raises(ValueError):
        s.record_state(DOMAIN, second["id"], "waiting")
    assert not s.record("pro.yuketang.cn", first["id"])
    s.close()


def test_legacy_history_grouping_is_idempotent_and_readonly(tmp_path):
    db = sqlite3.connect(tmp_path / "db")
    db.executescript(SCHEMA)
    db.execute(
        "INSERT INTO courses(domain,classroom,name,seen) VALUES(?,?,?,?)",
        (DOMAIN, "id", "原名", 1),
    )
    for lid, pid in [("l1", "p1"), ("l1", "p2"), ("l2", "p3")]:
        db.execute(
            "INSERT INTO problems(domain,lesson,problem,payload,created,updated) VALUES(?,?,?,?,?,?)",
            (DOMAIN, lid, pid, json.dumps(p(pid)), 100, 100),
        )
    db.commit()
    db.close()
    for _ in range(2):
        s = Store(tmp_path / "db")
        records = s.records(DOMAIN)
        assert len(records) == 2 and sorted(r["problem_count"] for r in records) == [
            1,
            2,
        ]
        assert all(r["state"] == "archived" and not r["listening"] for r in records)
        assert s.pending(DOMAIN) == []
        s.close()


async def test_record_api_readonly_resume_and_cross_site(tmp_path, monkeypatch):
    cfg = tmp_path / "c"
    app = create_app(cfg, tmp_path / "data", autostart=False)
    async with app.router.lifespan_context(app):
        rt = app.state.runtime
        s = rt.store
        c = s.course(DOMAIN, "id", "原名")
        r = s.create_record(DOMAIN, c["id"], "旧课")
        s.bind_record(DOMAIN, "l", r["id"])
        id, _ = s.insert(DOMAIN, "l", p())
        s.update(id, status="done", answer={"answer": ["A"]})

        async def start():
            rt.running = True

        monkeypatch.setattr(rt, "start", start)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"X-Yktmon": "local"},
        ) as client:
            assert (await client.post(f"/api/problems/{id}/retry")).status_code == 409
            assert (await client.post(f"/api/problems/{id}/notify")).status_code == 409
            assert (
                len((await client.get(f"/api/problems?record_id={r['id']}")).json())
                == 1
            )
            assert (await client.post(f"/api/records/{r['id']}/resume")).json()[
                "listening"
            ]
            assert (await client.post(f"/api/problems/{id}/retry")).status_code == 200
            assert (await client.post(f"/api/records/{r['id']}/finish")).json()[
                "state"
            ] == "archived"
            assert (
                await client.patch(f"/api/records/{r['id']}", json={"title": "新标题"})
            ).status_code == 200
            assert (
                await client.put(
                    f"/api/courses/{c['id']}", json={"display_name": "自定义课程"}
                )
            ).status_code == 200
            assert (await client.get("/api/records")).json()[0][
                "course_name"
            ] == "自定义课程"
            assert (
                await client.get("/api/problems?record_id=99999")
            ).status_code == 404
            assert (
                await client.post(
                    "/api/records", json={"course_id": c["id"], "title": "下一课"}
                )
            ).status_code == 200


async def test_discovery_only_connects_explicit_records_and_supports_two_courses(
    tmp_path, monkeypatch
):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    s.save_session(DOMAIN, "mock")
    courses = [s.course(DOMAIN, str(i), "课" + str(i)) for i in range(3)]
    r1 = s.create_record(DOMAIN, courses[0]["id"], "one", True)
    r2 = s.create_record(DOMAIN, courses[1]["id"], "two", True)

    @asynccontextmanager
    async def client(*args):
        class Mock:
            async def api(self, *a, **kw):
                return {
                    "onLessonClassrooms": [
                        {
                            "lessonId": "l" + str(i),
                            "classroomId": str(i),
                            "courseName": "课" + str(i),
                        }
                        for i in range(3)
                    ]
                }

        yield Mock()

    async def run(worker):
        await asyncio.sleep(100)

    async def once(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr("yktmon.runtime.ClientContext", client)
    monkeypatch.setattr("yktmon.runtime.Lesson.run", run)
    monkeypatch.setattr(rt, "wait_discovery", once)
    with pytest.raises(asyncio.CancelledError):
        await rt.discover()
    assert len(rt.lessons) == 2
    assert {w.record_id for w in rt.lessons.values()} == {r1["id"], r2["id"]}
    await rt.set_record_state(r1["id"], "paused")
    assert not rt.lessons["l1"].task.done()
    await rt.stop()
    s.close()


async def test_saved_model_key_not_sent_to_new_destination(tmp_path, monkeypatch):
    cfg = ConfigFile(tmp_path / "c")
    cfg.save(cfg.prepare({"ai": {"api_key": "private-key"}}))
    app = create_app(tmp_path / "c", tmp_path / "data", autostart=False)
    calls = []

    async def catalog(base, key):
        calls.append((base, key))
        return {"models": ["m"], "message": ""}

    monkeypatch.setattr("yktmon.web.fetch_models", catalog)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"X-Yktmon": "local"},
        ) as c:
            assert (
                await c.post("/api/ai/models", json={"base_url": "https://new.example"})
            ).status_code == 422
            assert not calls
            r = await c.post(
                "/api/ai/models", json={"base_url": "https://api.deepseek.com"}
            )
            assert r.status_code == 200 and "private-key" not in r.text
            assert calls[0][1] == "private-key"
            assert (
                await c.post(
                    "/api/ai/models",
                    json={"base_url": "https://new.example", "api_key": "new-key"},
                )
            ).status_code == 200
            assert calls[-1][1] == "new-key"
            assert ConfigFile(tmp_path / "c").settings.ai.api_key == "private-key"


async def test_lesson_finished_archives_own_record_only(tmp_path, monkeypatch):
    from yktmon.runtime import Lesson

    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    first = s.course(DOMAIN, "1", "one")
    second = s.course(DOMAIN, "2", "two")
    r1 = s.create_record(DOMAIN, first["id"], "one", True)
    r2 = s.create_record(DOMAIN, second["id"], "two", True)

    @asynccontextmanager
    async def client(*args):
        class Mock:
            async def join(self, id):
                return {"identityId": "u", "lessonToken": "t"}

        yield Mock()

    @asynccontextmanager
    async def socket(*args):
        class Mock:
            async def send(self, data):
                pass

        yield Mock()

    async def read(self, ws, q):
        await q.put({"op": "hello"})
        await q.put({"op": "lessonfinished"})
        await asyncio.sleep(100)

    monkeypatch.setattr("yktmon.runtime.ClientContext", client)
    monkeypatch.setattr("yktmon.runtime.socket", socket)
    monkeypatch.setattr(Lesson, "read", read)
    worker = Lesson(rt, "remote", "one", "cookie")
    worker.record_id = r1["id"]
    rt.lessons["remote"] = worker
    await worker.run()
    assert s.record(DOMAIN, r1["id"])["state"] == "archived"
    assert s.record(DOMAIN, r2["id"])["listening"]
    assert "remote" not in rt.lessons
    s.close()


async def test_pause_cancels_own_solver_keeps_pool_and_other_record(
    tmp_path, monkeypatch
):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    c1 = s.course(DOMAIN, "1", "one")
    c2 = s.course(DOMAIN, "2", "two")
    r1 = s.create_record(DOMAIN, c1["id"], "one", True)
    r2 = s.create_record(DOMAIN, c2["id"], "two", True)
    s.bind_record(DOMAIN, "l1", r1["id"])
    s.bind_record(DOMAIN, "l2", r2["id"])
    a, _ = s.insert(DOMAIN, "l1", p())
    b, _ = s.insert(DOMAIN, "l2", p())
    entered = asyncio.Event()
    processed = []

    async def process(id):
        if id == a:
            entered.set()
            await asyncio.sleep(100)
        processed.append(id)

    monkeypatch.setattr(rt, "process", process)
    worker = rt.spawn(rt.solve_loop())
    rt.enqueue(a)
    await asyncio.wait_for(entered.wait(), 1)
    await rt.set_record_state(r1["id"], "paused")
    rt.enqueue(b)
    async with asyncio.timeout(1):
        while b not in processed:
            await asyncio.sleep(0.01)
    assert not worker.done() and s.record(DOMAIN, r2["id"])["listening"]
    await rt.stop()
    s.close()


async def test_recovered_record_error_is_cleared_without_clearing_other_record(
    tmp_path,
):
    from yktmon.runtime import Lesson

    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    c1 = s.course(DOMAIN, "1", "one")
    c2 = s.course(DOMAIN, "2", "two")
    r1 = s.create_record(DOMAIN, c1["id"], "one", True)
    r2 = s.create_record(DOMAIN, c2["id"], "two", True)
    rt.record_errors = {r1["id"]: "closed", r2["id"]: "other closed"}
    worker = Lesson(rt, "remote", "one", "")
    worker.record_id = r1["id"]
    worker.state_changed("实时监听中")
    assert r1["id"] not in rt.status()["record_errors"]
    assert rt.record_errors[r2["id"]] == "other closed"
    rt.record_errors[r1["id"]] = "closed again"
    await rt.set_record_state(r1["id"], "paused")
    assert r1["id"] not in rt.record_errors
    rt.record_errors[r1["id"]] = "old error"
    await rt.set_record_state(r1["id"], "archived")
    assert r1["id"] not in rt.record_errors

    async def start():
        rt.running = True

    rt.start = start
    rt.record_errors[r1["id"]] = "stale error"
    await rt.set_record_state(r1["id"], "waiting")
    assert r1["id"] not in rt.record_errors and r2["id"] in rt.record_errors
    await rt.stop()
    s.close()


async def test_saved_key_reveal_guard_and_redacted_general_config(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    cfg.save(cfg.prepare({"ai": {"api_key": "dummy-for-local-test"}}))
    app = create_app(tmp_path / "c", tmp_path / "data", autostart=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as c:
            assert (await c.post("/api/ai/key")).status_code == 403
            assert (await c.get("/api/ai/key")).status_code == 405
            result = await c.post("/api/ai/key", headers={"X-Yktmon": "local"})
            assert result.json() == {"api_key": "dummy-for-local-test"}
            assert result.headers["cache-control"] == "no-store"
            assert (await c.get("/api/config")).json()["ai"]["api_key"] == ""
            assert (
                await c.post(
                    "/api/ai/key",
                    headers={"X-Yktmon": "local", "Origin": "https://example.com"},
                )
            ).status_code == 403
