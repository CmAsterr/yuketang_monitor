from __future__ import annotations
import asyncio
import json
import sqlite3
import httpx
import pytest
from yktmon.ai import parse_answer
from yktmon.config import ConfigFile, Channel
from yktmon.store import Store, SCHEMA
from yktmon.runtime import Runtime, Lesson
from yktmon.presentation import present, markdown
from yktmon.notify import Notifier, split_utf8
from yktmon.web import create_app

DOMAIN = "changjiang.yuketang.cn"
RAW = '{"answer":["B"],"reasoning":"题目提示“这题选3”，B和C均为3；按顺序选择B。"，"confidence":0.5}'


def fixture_problem():
    return dict(
        problem_id="p",
        course="课程",
        type=1,
        body="这题选3",
        options=["", "", "", ""],
        cover="https://images.example/q.png",
        limit=-1,
        unlocked=0,
    )


def test_malformed_answer_repaired_without_changing_content():
    a = parse_answer(RAW)
    assert a["answer"] == ["B"] and a["confidence"] == 0.5
    assert a["reasoning"] == "题目提示“这题选3”，B和C均为3；按顺序选择B。"
    assert a["repaired"] and a["raw"] == RAW


def test_old_record_read_normalization_preserves_database(tmp_path):
    s = Store(tmp_path / "db")
    id, _ = s.insert(DOMAIN, "lesson", fixture_problem())
    old = {
        "answer": [RAW],
        "reasoning": "模型输出未能解析为结构化结果，请人工核对",
        "structured": False,
        "model": "mock",
    }
    s.update(id, answer=old, status="done")
    row = present(s.get(id))
    assert row["display"]["confidence"] == "50%"
    assert row["answer"]["answer"] == ["B"]
    assert row["answer"]["model"] == "mock"
    assert s.get(id)["answer"] == old
    s.close()


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "[x](javascript:alert(1))",
        "[x](data:text/html,bad)",
        "![x](https://tracker.example/a)",
    ],
)
def test_markdown_does_not_allow_active_content(text):
    html = markdown(text)
    assert "<script" not in html and "<img" not in html
    assert 'href="javascript:' not in html and 'href="data:' not in html


def test_markdown_lists_code_and_table():
    html = markdown("**重点**\n\n- 一\n- 二\n\n`x = 3`\n\n|A|B|\n|---|---|\n|1|2|")
    assert (
        "<strong>重点</strong>" in html
        and "<ul>" in html
        and "<code>" in html
        and "<table>" in html
    )


def test_delete_course_tombstone_and_restore_keep_history(tmp_path):
    s = Store(tmp_path / "db")
    c = s.course(DOMAIN, "123", "name")
    id, _ = s.insert(DOMAIN, "lesson", fixture_problem())
    assert s.delete_course(DOMAIN, c["id"]) == 1
    assert not s.courses(DOMAIN)
    discovered = s.course(DOMAIN, "123", "name")
    assert discovered["deleted"] and not discovered["enabled"]
    assert not s.courses(DOMAIN)
    restored = s.add_course(DOMAIN, "", "name")
    assert restored["id"] == c["id"] and restored["enabled"] and not restored["deleted"]
    assert s.get(id) is not None
    with pytest.raises(ValueError):
        s.add_course(DOMAIN, "123", "name")
    assert s.delete_course("pro.yuketang.cn", c["id"]) == 0
    s.close()


def test_existing_schema_migrates_deliveries_and_is_idempotent(tmp_path):
    db = sqlite3.connect(tmp_path / "db")
    db.executescript(SCHEMA)
    db.execute(
        "INSERT INTO courses(domain,classroom,name,seen) VALUES (?,?,?,?)",
        (DOMAIN, "c", "course", 1),
    )
    db.execute(
        "INSERT INTO problems(domain,lesson,problem,payload,created,updated) VALUES (?,?,?,?,?,?)",
        (DOMAIN, "l", "p", '{"course":"course"}', 1, 1),
    )
    db.execute("INSERT INTO deliveries VALUES (1,'wecom','sent','',1,'hash')")
    db.commit()
    db.close()
    for _ in range(2):
        s = Store(tmp_path / "db")
        assert len(s.courses(DOMAIN)) == 1
        assert s.get(1)["deliveries"][0]["phase"] == "result"
        assert s.get(1)["deliveries"][0]["signature"] == "hash"
        s.close()


async def test_course_change_leaves_other_workers_and_runtime_running(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    rt.running = True
    c1 = s.course(DOMAIN, "1", "one")
    c2 = s.course(DOMAIN, "2", "two")
    one = Lesson(rt, "1", "one", "")
    two = Lesson(rt, "2", "two", "")
    one.course_id = c1["id"]
    two.course_id = c2["id"]
    one.task = asyncio.create_task(asyncio.sleep(100))
    two.task = asyncio.create_task(asyncio.sleep(100))
    rt.lessons = {"1": one, "2": two}
    s.enable_course(DOMAIN, c1["id"], False)
    await rt.course_changed(c1["id"])
    assert one.task.cancelled() and not two.task.done() and rt.running
    assert rt.discovery_wake.is_set()
    two.task.cancel()
    await asyncio.gather(two.task, return_exceptions=True)
    s.close()


async def test_course_crud_and_image_download_api(tmp_path, monkeypatch):
    app = create_app(tmp_path / "config", tmp_path / "data", autostart=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"X-Yktmon": "local"},
        ) as c:
            assert (await c.post("/api/courses", json={"name": ""})).status_code == 422
            row = (await c.post("/api/courses", json={"name": "新课程"})).json()
            assert row["enabled"]
            assert (
                await c.post("/api/courses", json={"name": "新课程"})
            ).status_code == 409
            assert (
                await c.put("/api/courses/" + str(row["id"]), json={"enabled": False})
            ).status_code == 200
            assert not (await c.get("/api/courses")).json()[0]["enabled"]
            rt = app.state.runtime
            id, _ = rt.store.insert(DOMAIN, "lesson", fixture_problem())
            rt.store.update(
                id,
                answer={"answer": [RAW], "structured": False},
                status="done",
                image="test.png",
            )
            from PIL import Image

            Image.new("RGB", (40, 40), "white").save(rt.image_dir / "test.png")
            response = await c.get(f"/api/problems/{id}/image?download=true")
            assert (
                response.status_code == 200
                and "attachment" in response.headers["content-disposition"]
            )
            assert (await c.get("/api/problems")).json()[0]["display"][
                "confidence"
            ] == "50%"
            assert (await c.delete("/api/courses/" + str(row["id"]))).status_code == 200
            assert not (await c.get("/api/courses")).json()
            assert len((await c.get("/api/problems")).json()) == 1
            import os

            if os.name == "nt":
                opened = []
                monkeypatch.setattr(os, "startfile", lambda p: opened.append(p))
                assert (
                    await c.post("/api/images/open-folder", json={"path": "C:/Windows"})
                ).status_code == 200
                assert opened == [str(rt.image_dir.resolve())]


async def test_reminder_result_dedup_and_complete_labels(tmp_path):
    s = Store(tmp_path / "db")
    id, _ = s.insert(DOMAIN, "l", fixture_problem())
    calls = []

    def handler(r):
        calls.append(json.loads(r.content))
        return httpx.Response(200, json={"errcode": 0})

    n = Notifier(s, lambda e: None, transport=httpx.MockTransport(handler))
    channels = [Channel(kind="wecom", webhook_url="https://mock.example")]
    await n.deliver(s.get(id), channels, tmp_path, phase="reminder")
    await n.deliver(s.get(id), channels, tmp_path, phase="reminder")
    s.update(id, status="done", answer=parse_answer(RAW))
    await n.deliver(s.get(id), channels, tmp_path)
    await n.deliver(s.get(id), channels, tmp_path)
    assert len(calls) == 2
    assert "新习题提醒" in calls[0]["text"]["content"]
    assert all(
        label in calls[1]["text"]["content"]
        for label in ["答案：B", "解析：", "可信度：50%", "题面图："]
    )
    assert {d["phase"] for d in s.get(id)["deliveries"]} == {"reminder", "result"}
    s.close()


@pytest.mark.parametrize("kind", ["wecom", "dingtalk", "feishu", "qqbot"])
async def test_channels_include_answer_reasoning_confidence(tmp_path, kind):
    s = Store(tmp_path / "db")
    id, _ = s.insert(DOMAIN, "l", fixture_problem())
    s.update(id, status="done", answer=parse_answer(RAW))
    calls = []

    def handler(r):
        payload = json.loads(r.content)
        calls.append(payload)
        if r.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "mock"})
        return httpx.Response(200, json={"id": "message", "code": 0, "errcode": 0})

    c = Channel(
        kind=kind,
        webhook_url="https://mock.example",
        app_id="test",
        client_secret="test",
        group_openid="group",
    )
    n = Notifier(s, lambda e: None, transport=httpx.MockTransport(handler))
    await n.deliver(s.get(id), [c], tmp_path)
    assert all(
        x in json.dumps(calls, ensure_ascii=False)
        for x in ["答案：B", "解析：", "可信度：50%"]
    )
    assert s.get(id)["deliveries"][0]["status"] == "sent"
    if kind == "dingtalk":
        assert "![题面]" in calls[0]["markdown"]["text"]
    if kind == "feishu":
        assert calls[0]["msg_type"] == "post"
    s.close()


def test_utf8_message_chunks_preserve_all_text():
    text = "解析与可信度" * 2000
    parts = split_utf8(text, 3800)
    assert "".join(parts) == text
    assert all(len(p.encode("utf-8")) <= 3800 for p in parts)


async def test_notification_preview_is_read_only(tmp_path):
    app = create_app(tmp_path / "config", tmp_path / "data", autostart=False)
    async with app.router.lifespan_context(app):
        rt = app.state.runtime
        id, _ = rt.store.insert(DOMAIN, "l", fixture_problem())
        rt.store.update(
            id, answer={"answer": [RAW], "structured": False}, status="done"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            result = (
                await client.get(f"/api/problems/{id}/notification-preview")
            ).json()
            assert "新习题提醒" in result["reminder"]
            assert "答案：B" in result["result"] and "可信度：50%" in result["result"]
            assert result["image"] == fixture_problem()["cover"]
        assert rt.store.get(id)["deliveries"] == []
        assert not rt.tasks


async def test_new_problem_reminder_is_scheduled_before_answer(tmp_path):
    cfg = ConfigFile(tmp_path / "c")
    s = Store(tmp_path / "db")
    rt = Runtime(cfg, s, tmp_path)
    rt.running = True
    phases = []

    async def deliver(row, channels, root, phase="result"):
        phases.append(phase)

    rt.notifier.deliver = deliver
    await rt.accept("lesson", fixture_problem())
    await asyncio.gather(*list(rt.tasks))
    assert phases == ["reminder"]
    assert s.recent(DOMAIN)[0]["status"] == "pending"
    await rt.accept("lesson", fixture_problem())
    assert phases == ["reminder"]
    await rt.stop()
    s.close()


def test_markdown_math_and_missing_confidence_are_rendered_for_web_and_qq():
    from yktmon.presentation import markdown, confidence_label, present

    html = markdown(
        "**解析**：$R(h_S)\\le\\epsilon$\n\n$$\n\\Pr[R(h_S)\\le\\epsilon]\\ge 1-\\delta\n$$"
    )
    assert "<math" in html and "math-block" in html and "<strong>解析</strong>" in html
    assert confidence_label({"confidence": None}) == "未提供（模型未返回自评）"
    row = {
        "answer": {"answer": ["A"], "reasoning": "$x^2$", "confidence": None},
        "payload": {},
        "problem": "p",
    }
    result = present(row)
    assert result["display"]["confidence_label"] == "未提供（模型未返回自评）"
