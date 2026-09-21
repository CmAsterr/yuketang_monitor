"""Application QQ service: persistent groups and per-group durable delivery workers."""

import asyncio
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
import httpx
from PIL import Image, ImageDraw
from .qq_client import QQClient, QQError
from .qq_store import QQStore
from .presentation import normalize_answer, confidence_label

PROBLEM_TYPE_LABELS = {1: "单选", 2: "多选", 3: "投票", 4: "填空", 5: "主观"}


def question_caption(row, record, display_number=None):
    number = (
        display_number if display_number is not None else row.get("display_number", 1)
    )
    return f"{record['course_name']} · {record['title']} · 题目 {number}"


def result_markdown(row, record, display_number=None):
    a = normalize_answer(row.get("answer"))
    title = question_caption(row, record, display_number)
    values = a.get("answer") or []
    answer = (
        "无法作答"
        if a.get("unanswerable")
        else "；".join(values)
        if values
        else row.get("error") or "未获得答案"
    )
    confidence = confidence_label(a)
    if a.get("unanswerable") and a.get("confidence") is not None:
        confidence = confidence.replace("（模型自评）", "（对无法作答判断的模型自评）")
    return f"**{title}**\n\n**答案**\n\n{answer}\n\n**解析**\n\n{a.get('reasoning') or row.get('error') or '未提供'}\n\n**可信度**：{confidence}"


def reminder_markdown(row, record, display_number=None):
    ptype = PROBLEM_TYPE_LABELS.get(int(row["payload"].get("type") or 0), "习题")
    title = question_caption(row, record, display_number)
    return f"# 新题提醒！\n\n**{title}**\n\n题型：{ptype}（*大肥鱼正在做题喵～*）"


class QQService:
    def __init__(
        self, config, store, image_root, *, client_factory=QQClient, check_pilot=True
    ):
        self.config = config
        self.store = store
        self.repo = QQStore(store)
        self.image_root = image_root
        self.client_factory = client_factory
        self.check_pilot = check_pilot
        self.client = None
        self.supervisor = None
        self.workers = {}
        self.control = asyncio.Lock()
        self.send_slots = asyncio.Semaphore(4)
        self.binding = None
        self.error = ""
        self.instance_lock = None
        self.disconnected = False
        # Reconcile only explicitly snapshotted new questions, never arbitrary history.
        for entry in self.repo.db.execute(
            "SELECT problem_id FROM qq_snapshots"
        ).fetchall():
            row = self.store.get(entry[0])
            if not row:
                continue
            self.enqueue_phase(row, "reminder")
            if row["image"]:
                self.enqueue_phase(row, "image")
            if row["status"] in ("done", "failed", "skipped"):
                self.enqueue_phase(row, "result")

    @property
    def cfg(self):
        return self.config.settings.qq

    def status(self):
        client = self.client
        binding = self.binding
        valid = (
            binding
            and binding["until"] > time.time()
            and binding["app_id"] == self.cfg.app_id
        )
        return {
            "app_id": self.cfg.app_id,
            "enabled": self.cfg.enabled,
            "secret_configured": bool(self.cfg.secret),
            "state": client.state
            if client
            else "已断开"
            if self.disconnected
            else "未配置"
            if not self.cfg.app_id or not self.cfg.secret
            else "未连接",
            "error": self.error or (client.error if client else ""),
            "name": client.name if client else "",
            "groups": self.repo.groups(self.cfg.app_id),
            "history": self.repo.history(self.cfg.app_id, limit=10),
            "history_limit": 10,
            "binding": {
                "id": binding["id"],
                "code": binding["code"],
                "seconds": max(0, int(binding["until"] - time.time())),
                "candidates": [
                    {"id": key, "label": value.get("name") or f"候选群 {i + 1}"}
                    for i, (key, value) in enumerate(binding["candidates"].items())
                ],
            }
            if valid
            else None,
            "diagnostics": {
                "token_refreshed_at": client.last_refresh if client else 0,
                "heartbeat_at": client.last_heartbeat if client else 0,
                "reconnect_at": client.last_reconnect if client else 0,
            },
        }

    async def start(self):
        await self.stop()
        self.disconnected = False
        self.error = ""
        if not self.cfg.enabled:
            return
        if not self.cfg.app_id or not self.cfg.secret:
            self.error = "请先保存 QQ AppID 和 AppSecret"
            return
        if self.check_pilot:
            try:
                async with httpx.AsyncClient(timeout=1, trust_env=False) as h:
                    r = await h.get("http://127.0.0.1:8789/status")
                    if r.status_code == 200 and r.json().get("state") not in (
                        "未连接",
                        "已断开",
                    ):
                        self.error = "QQ 试验网关仍在运行，请先在 8789 测试页断开，再连接正式机器人"
                        return
            except (httpx.HTTPError, ValueError):
                pass
        # Shared by production instances on this machine; pilot conflict is checked above.
        from .web import DataLock
        import tempfile

        lock_path = (
            Path(tempfile.gettempdir())
            / "yktmon-qq-locks"
            / (hashlib.sha256(self.cfg.app_id.encode()).hexdigest() + ".lock")
        )
        lock = DataLock(lock_path)
        try:
            lock.acquire()
        except RuntimeError:
            self.error = "此机器人已有其他正式服务实例连接"
            return
        self.instance_lock = lock
        self.client = self.client_factory(self.cfg.app_id, self.cfg.secret, self.event)
        self.client.start()
        self.supervisor = asyncio.create_task(self.run_outbox())

    async def stop(self):
        tasks = [
            *(self.workers.values()),
            *([self.supervisor] if self.supervisor else []),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.supervisor = None
        if self.client:
            await self.client.close()
            self.client = None
        if self.instance_lock:
            self.instance_lock.release()
            self.instance_lock = None
        self.binding = None
        self.disconnected = True

    async def update_group(self, id, *, name=None, enabled=None):
        self.repo.update_group(id, self.cfg.app_id, name=name, enabled=enabled)
        if enabled is False:
            task = self.workers.pop(id, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def new_binding(self):
        if not self.client or self.client.state != "已连接":
            raise ValueError("请先连接 QQ 机器人")
        self.binding = {
            "id": secrets.token_urlsafe(16),
            "app_id": self.cfg.app_id,
            "code": secrets.token_hex(4).upper(),
            "until": time.time() + 600,
            "candidates": {},
        }
        return self.status()["binding"]

    def event(self, event):
        kind = event.get("t")
        d = event.get("d") or {}
        if not isinstance(d, dict):
            return
        group = d.get("group_openid")
        if not isinstance(group, str) or not group or len(group) > 256:
            return
        if kind in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            binding = self.binding
            if (
                not binding
                or binding["until"] <= time.time()
                or binding["app_id"] != self.cfg.app_id
            ):
                return
            text = re.sub(r"<@!?\w+>", "", str(d.get("content", ""))).strip()
            if (
                text == "绑定 " + binding["code"]
                and group
                not in [value.get("openid") for value in binding["candidates"].values()]
                and len(binding["candidates"]) < 10
            ):
                group_name = d.get("group_name") or d.get("groupName") or d.get("name")
                binding["candidates"][secrets.token_urlsafe(12)] = {
                    "openid": group,
                    "name": str(group_name).strip() if group_name else "",
                }
        elif kind in ("GROUP_MSG_RECEIVE", "GROUP_MSG_REJECT", "GROUP_DEL_ROBOT"):
            self.repo.permission(
                self.cfg.app_id,
                group,
                "允许"
                if kind == "GROUP_MSG_RECEIVE"
                else "机器人已移除"
                if kind == "GROUP_DEL_ROBOT"
                else "主动消息已关闭",
            )

    def confirm_binding(self, id, candidate, name):
        b = self.binding
        if (
            not b
            or b["id"] != id
            or b["app_id"] != self.cfg.app_id
            or b["until"] <= time.time()
            or candidate not in b["candidates"]
        ):
            raise ValueError("绑定会话无效或过期，请重新生成绑定码")
        candidate_data = b["candidates"][candidate]
        openid = (
            candidate_data.get("openid")
            if isinstance(candidate_data, dict)
            else candidate_data
        )
        group_id = self.repo.bind(self.cfg.app_id, openid, name)
        self.binding = None
        return group_id

    def display_number(self, row):
        if not row.get("record_id"):
            return 0
        found = self.store.db.execute(
            "SELECT COUNT(*) FROM problems WHERE record_id=? AND id<=?",
            (row["record_id"], row["id"]),
        ).fetchone()[0]
        return max(1, int(found))

    def lesson_notice(self, domain, lesson_id, record_id, *, finished=False):
        """One notice per remote lesson transition, durable across reconnects/restarts."""
        record = self.store.record(domain, record_id)
        if not record or not self.cfg.enabled:
            return
        phase = "lesson_finished" if finished else "lesson_started"
        key = (self.cfg.app_id, domain, str(lesson_id), phase)
        if self.repo.db.execute(
            "SELECT 1 FROM qq_lesson_events WHERE app_id=? AND domain=? AND lesson=? AND phase=?",
            key,
        ).fetchone():
            return
        title = "下课啦！" if finished else "上课啦！"
        text = (
            "大肥鱼收好小本本，辛苦大家啦！休息一下，下次上课再见喵～"
            if finished
            else "大肥鱼带着小本本来陪大家上课啦！有新题会马上提醒大家喵～"
        )
        caption = f"{record['course_name']} · {record['title']}"
        payload = {
            "kind": "markdown",
            "text": f"# {title}\n\n**{caption}**\n\n*{text}*",
        }
        version = hashlib.sha256(
            json.dumps(key, ensure_ascii=False).encode()
        ).hexdigest()
        for group_id in self.repo.mapping(record["course_id"], self.cfg.app_id):
            group = self.repo.group(group_id, self.cfg.app_id)
            if group and group["enabled"]:
                self.repo.enqueue(
                    {
                        "app_id": self.cfg.app_id,
                        "group_id": group_id,
                        "openid": group["openid"],
                    },
                    phase,
                    payload,
                    attempt=version,
                )
        # Mark even when no groups are selected: later edits must not replay old starts.
        with self.repo.db:
            self.repo.db.execute(
                "INSERT OR IGNORE INTO qq_lesson_events VALUES(?,?,?,?,?)",
                (*key, time.time()),
            )

    def capture(self, row):
        record = (
            self.store.record(row["domain"], row["record_id"])
            if row.get("record_id")
            else None
        )
        row = dict(row)
        row["display_number"] = self.display_number(row)
        self.repo.snapshot(
            row["id"],
            record["course_id"] if record else None,
            self.cfg.app_id,
            self.cfg.enabled,
        )
        self.enqueue_phase(row, "reminder")

    def enqueue_phase(self, row, phase):
        row = dict(row)
        row["display_number"] = self.display_number(row)
        targets = self.repo.targets(row["id"])
        if not targets:
            return
        record = (
            self.store.record(row["domain"], row["record_id"])
            if row.get("record_id")
            else None
        )
        if not record:
            return
        number = row["display_number"]
        caption = question_caption(row, record, number)
        if phase == "reminder":
            payload = {
                "kind": "markdown",
                "text": reminder_markdown(row, record, number),
            }
        elif phase == "image":
            if not row["image"]:
                return
            payload = {
                "kind": "image",
                "image": row["image"],
                "caption": caption,
                "markdown_url": row["payload"].get("cover", ""),
                "prefer_bold": True,
            }
        else:
            payload = {"kind": "markdown", "text": result_markdown(row, record, number)}
        for target in targets:
            # Snapshot targets from another app are never reinterpreted under new credentials.
            version = (
                phase
                if phase != "result"
                else hashlib.sha256(
                    json.dumps(
                        [row["status"], row["answer"], row["error"]],
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            )
            id = self.repo.enqueue(
                target, phase, payload, problem_id=row["id"], attempt=version
            )
            if (
                target["app_id"] != self.cfg.app_id
                and self.repo.get(id)["state"] == "pending"
            ):
                self.repo.update(id, "cancelled", error="机器人已变更，旧目标取消")

    async def run_outbox(self):
        while True:
            for group_id in self.repo.pending_groups(self.cfg.app_id):
                if group_id not in self.workers or self.workers[group_id].done():
                    previous = self.workers.get(group_id)
                    if previous and not previous.cancelled():
                        previous.exception()
                    self.workers[group_id] = asyncio.create_task(self.drain(group_id))
            await asyncio.sleep(0.3)

    async def drain(self, group_id):
        client = self.client
        app_id = client.app_id
        while True:
            if client.state != "已连接":
                await asyncio.sleep(0.5)
                continue
            row = self.repo.next(group_id, app_id)
            if not row:
                return
            group = self.repo.group(group_id, app_id)
            if (
                not group
                or not group["enabled"]
                or group["permission"] in ("主动消息已关闭", "机器人已移除")
            ):
                self.repo.update(
                    row["id"], "cancelled", error="群已停用、权限关闭或机器人已移除"
                )
                continue
            if not self.repo.claim(row["id"]):
                continue
            try:
                async with self.send_slots:
                    msgid = await client.send(
                        row["openid"], json.loads(row["payload"]), self.image_root
                    )
                self.repo.update(
                    row["id"],
                    "sent",
                    message_id=str(msgid),
                    error=getattr(msgid, "note", ""),
                )
            except asyncio.CancelledError:
                self.repo.update(
                    row["id"], "unknown", error="发送期间服务中断，请核对群消息后再重试"
                )
                raise
            except QQError as exc:
                if exc.unknown:
                    self.repo.update(
                        row["id"], "unknown", error=str(exc), code=exc.code
                    )
                elif exc.retry and row["attempts"] < 2:
                    self.repo.update(
                        row["id"],
                        "pending",
                        error=str(exc),
                        code=exc.code,
                        next_at=time.time() + 5,
                    )
                else:
                    self.repo.update(row["id"], "failed", error=str(exc), code=exc.code)
            except Exception:
                self.repo.update(
                    row["id"], "unknown", error="发送异常，结果未知；请核对后再重试"
                )
            await asyncio.sleep(0.2)

    def retry(self, id, confirm_unknown=False):
        row = self.repo.get(id)
        if not row or row["app_id"] != self.cfg.app_id:
            raise ValueError("投递记录不存在或属于旧机器人")
        if row["state"] == "unknown" and not confirm_unknown:
            raise ValueError("请先核对群消息，并确认可能重复发送")
        if row["state"] not in ("failed", "unknown"):
            raise ValueError("只有失败或结果未知的投递可重试")
        group = self.repo.group(row["group_id"], self.cfg.app_id)
        if not group or not group["enabled"]:
            raise ValueError("群已停用")
        self.repo.update(id, "pending")
        with self.repo.db:
            self.repo.db.execute("UPDATE qq_outbox SET attempts=0 WHERE id=?", (id,))

    def cancel(self, id):
        row = self.repo.get(id)
        if not row or row["app_id"] != self.cfg.app_id or row["state"] != "pending":
            raise ValueError("只能取消当前机器人尚未发送的任务")
        self.repo.update(id, "cancelled", error="用户取消")

    def test(self, group_id, kind, attempt):
        if not self.client or self.client.state != "已连接":
            raise ValueError("请先连接机器人")
        group = self.repo.group(group_id, self.cfg.app_id)
        if not group or not group["enabled"]:
            raise ValueError("请选择启用的已绑定群")
        if kind == "image":
            path = self.image_root / "qq-notification-test.png"
            if not path.exists():
                image = Image.new("RGB", (600, 220), "white")
                draw = ImageDraw.Draw(image)
                draw.text(
                    (30, 50),
                    "YKT QQ notification test\nSample answer: B | Confidence: 95%",
                    fill="black",
                    font_size=25,
                )
                image.save(path)
            payload = {
                "kind": "image",
                "image": path.name,
                "caption": "【雨课堂通知测试】本地示例图片（非真实习题）",
            }
        elif kind == "markdown":
            payload = {
                "kind": "markdown",
                "text": "# 雨课堂通知测试\n\n**答案**：B（模拟）\n\n**解析**：积分示例：\n\n$$\n\\int_0^1 x^2\\,dx=\\frac{1}{3}\n$$\n\n**可信度**：95%（模拟值）",
            }
        elif kind == "text":
            payload = {
                "kind": "text",
                "text": "【雨课堂通知测试】主动提醒发送测试。本条不是对群内消息的回复。",
            }
        else:
            raise ValueError("未知测试消息类型")
        return self.repo.enqueue(
            {
                "app_id": self.cfg.app_id,
                "group_id": group_id,
                "openid": group["openid"],
            },
            "test-" + kind,
            payload,
            attempt=attempt,
        )
