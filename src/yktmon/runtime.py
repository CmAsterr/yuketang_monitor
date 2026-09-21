from __future__ import annotations
import asyncio
import copy
import logging
import time
from pathlib import Path

from .ai import Solver
from .config import ConfigFile
from .notify import Notifier
from .qq_service import QQService
from .protocol import (
    AuthExpired,
    ClientContext,
    ProtocolError,
    failure,
    ident,
    image_bytes,
    image_mime,
    message,
    parse_slides,
    qr_login,
    send,
    socket,
)

log = logging.getLogger("yktmon")


def discovery_delay(interval, failures):
    """Retry transient discovery failures promptly; normal scans keep their configured cadence."""
    return (1, 2, 5)[min(failures - 1, 2)] if failures else interval


class Bus:
    def __init__(self):
        self.subscribers = set()

    def publish(self, event):
        for q in tuple(self.subscribers):
            if q.full():
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync"})
            else:
                q.put_nowait(event)


class Runtime:
    def __init__(self, config: ConfigFile, store, data_dir: Path):
        self.config, self.store, self.data_dir = config, store, data_dir
        self.image_dir = data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.bus = Bus()
        self.running = False
        self.tasks = set()
        self.lessons = {}
        self.finished = set()
        self.login_task = None
        self.login_state = "未登录"
        self.qr = ""
        self.last_error = ""
        self.record_errors = {}
        self.discovery_state = "等待启动"
        self.jobs = asyncio.Queue(maxsize=256)
        self.enqueued = set()
        self.active_jobs = {}
        self.control = asyncio.Lock()
        self.discovery_wake = asyncio.Event()
        self.notifier = Notifier(store, self.bus.publish)
        self.qq = QQService(config, store, self.image_dir)
        self.diagnostic = {"state": "idle"}
        self.diag_task = None

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.error("后台任务", t.exception())

        task.add_done_callback(done)
        return task

    def error(self, where, exc):
        self.last_error = where + "：" + failure(exc)
        log.warning("%s", self.last_error)
        self.emit()

    def status(self):
        cfg = self.config.settings
        return {
            "running": self.running,
            "domain": cfg.domain,
            "login": self.login_state,
            "qr": self.qr,
            "has_session": bool(self.store.session(cfg.domain)),
            "last_error": self.last_error,
            "record_errors": dict(self.record_errors),
            "discovery_state": self.discovery_state,
            "lessons": [
                {
                    "id": k,
                    "course": w.course,
                    "state": w.state,
                    "record_id": getattr(w, "record_id", None),
                }
                for k, w in self.lessons.items()
            ],
            "ai_configured": bool(cfg.ai.api_key),
            "model": cfg.ai.model,
            "diagnostic": self.diagnostic,
        }

    def emit(self):
        self.bus.publish({"type": "status", "data": self.status()})

    async def start(self):
        if self.running:
            return
        self.store.recover()
        self.running = True
        self.last_error = ""
        self.emit()
        for _ in range(2):
            self.spawn(self.solve_loop())
        self.spawn(self.recover_jobs())
        self.spawn(self.discover())
        log.info("监控启动：%s", self.config.settings.domain)

    async def stop(self):
        self.running = False
        self.discovery_state = "课堂发现已暂停"
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.record_errors.clear()
        self.lessons.clear()
        self.finished.clear()
        self.enqueued.clear()
        self.jobs = asyncio.Queue(maxsize=256)
        self.store.recover()
        self.emit()
        log.info("监控已停止")

    async def cancel_login(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        self.login_task = None
        self.qr = ""

    async def close(self):
        await self.qq.stop()
        await self.cancel_login()
        if self.diag_task:
            self.diag_task.cancel()
            await asyncio.gather(self.diag_task, return_exceptions=True)
        await self.stop()

    async def apply(self, settings):
        changed = settings.domain != self.config.settings.domain
        old_qq = self.config.settings.qq.model_copy(deep=True)
        # Persist first; validation/save failure must leave the running service intact.
        if settings.qq != old_qq:
            async with self.qq.control:
                await self.qq.stop()
                self.config.save(settings)
                if old_qq.app_id != settings.qq.app_id:
                    self.qq.repo.cancel_app(old_qq.app_id)
                await self.qq.start()
        else:
            self.config.save(settings)
        if self.diag_task:
            self.diag_task.cancel()
            await asyncio.gather(self.diag_task, return_exceptions=True)
            self.diag_task = None
        self.diagnostic = {"state": "idle"}
        active = self.running
        await self.stop()
        if changed:
            await self.cancel_login()
            self.login_state = (
                "待验证登录态" if self.store.session(settings.domain) else "未登录"
            )
        if active:
            await self.start()
        self.emit()

    async def begin_login(self):
        await self.cancel_login()
        domain = self.config.settings.domain

        def status(state, qr):
            self.login_state, self.qr = state, qr
            self.emit()

        def save(cookie):
            self.store.save_session(domain, cookie)

        async def run():
            try:
                await qr_login(domain, status, save)
                # Replace existing lesson connections after changing account/session.
                async with self.control:
                    active = self.running
                    await self.stop()
                    if active:
                        await self.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status("登录失败：" + failure(exc), "")

        status("正在连接登录服务", "")
        self.login_task = asyncio.create_task(run())

    async def logout(self):
        await self.cancel_login()
        await self.stop()
        self.store.save_session(self.config.settings.domain, "")
        self.login_state = "已退出登录"
        self.emit()

    async def diagnose(self):
        if self.diag_task and not self.diag_task.done():
            return
        self.diagnostic = {"state": "running"}
        self.emit()
        cfg = self.config.settings.ai.model_copy(deep=True)

        async def run():
            try:
                async with asyncio.timeout(cfg.timeout * 4 + 15):
                    result = await Solver(cfg).diagnose()
                self.diagnostic = {"state": "done", **result}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.diagnostic = {"state": "done", "ok": False, "error": failure(exc)}
            self.emit()

        self.diag_task = asyncio.create_task(run())

    async def discover(self):
        failures = 0
        while True:
            cfg = self.config.settings
            cookie = self.store.session(cfg.domain)
            if not cookie:
                if self.discovery_state != "等待扫码登录":
                    self.discovery_state = "等待扫码登录"
                    self.emit()
                await asyncio.sleep(1)
                continue
            try:
                self.discovery_state = "正在获取课堂列表"
                self.emit()
                async with ClientContext(cfg.domain, cookie) as client:
                    data = await client.api(
                        "GET", "/api/v3/classroom/on-lesson-upcoming-exam"
                    )
                    lessons = data.get("onLessonClassrooms") or []
                    if not isinstance(lessons, list):
                        raise ProtocolError("课堂列表结构不正确")
                    seen = set()
                    for lesson in lessons:
                        if not isinstance(lesson, dict):
                            continue
                        lid = ident(lesson.get("lessonId") or lesson.get("lesson_id"))
                        if not lid:
                            continue
                        seen.add(lid)
                        name = str(
                            lesson.get("courseName")
                            or lesson.get("course_name")
                            or "未命名课程"
                        )
                        course = self.store.course(
                            cfg.domain,
                            lesson.get("classroomId") or lesson.get("classroom_id"),
                            name,
                        )
                        if course.get("deleted") or not course["enabled"]:
                            continue
                        record = self.store.wanted_record(cfg.domain, course["id"])
                        if not record:
                            continue
                        if lid in self.lessons or lid in self.finished:
                            continue
                        if any(
                            getattr(w, "record_id", None) == record["id"]
                            for w in self.lessons.values()
                        ):
                            continue
                        self.store.bind_record(cfg.domain, lid, record["id"])
                        worker = Lesson(
                            self, lid, course.get("display_name") or name, cookie
                        )
                        worker.course_id = course["id"]
                        worker.record_id = record["id"]
                        self.lessons[lid] = worker
                        worker.task = self.spawn(worker.run())
                    self.finished.intersection_update(seen)
                    for lid, w in list(self.lessons.items()):
                        w.missing = w.missing + 1 if lid not in seen else 0
                        if w.missing >= 3:
                            if getattr(w, "record_id", None):
                                self.qq.lesson_notice(
                                    cfg.domain, lid, w.record_id, finished=True
                                )
                                self.store.record_state(
                                    cfg.domain, w.record_id, "archived"
                                )
                            w.task.cancel()
                            await asyncio.gather(w.task, return_exceptions=True)
                    failures = 0
                    self.login_state = "已登录"
                    self.discovery_state = (
                        f"课堂列表已更新；{cfg.scan_interval:g} 秒后检查新课堂"
                    )
                    if self.last_error.startswith("发现课堂"):
                        self.last_error = ""
                    self.emit()
            except AuthExpired as exc:
                self.store.save_session(cfg.domain, "")
                self.login_state = "登录已失效，请重新扫码"
                for w in list(self.lessons.values()):
                    w.task.cancel()
                self.error("发现课堂", exc)
            except Exception as exc:
                failures += 1
                delay = discovery_delay(cfg.scan_interval, failures)
                self.discovery_state = (
                    f"课堂列表获取失败，{delay:g} 秒后重试；已连接课堂继续监听"
                )
                self.error("发现课堂", exc)
            await self.wait_discovery(discovery_delay(cfg.scan_interval, failures))

    async def wait_discovery(self, delay):
        try:
            await asyncio.wait_for(self.discovery_wake.wait(), delay)
        except TimeoutError:
            pass
        self.discovery_wake.clear()

    async def set_record_state(self, record_id, state):
        domain = self.config.settings.domain
        record = self.store.record_state(domain, record_id, state)
        self.record_errors.pop(record_id, None)
        if not record["listening"]:
            workers = [
                w
                for w in self.lessons.values()
                if getattr(w, "record_id", None) == record_id
            ]
            jobs = [
                task
                for id, task in self.active_jobs.items()
                if self.store.get(id).get("record_id") == record_id
            ]
            for task in [*(w.task for w in workers), *jobs]:
                task.cancel()
            if workers or jobs:
                await asyncio.gather(
                    *(w.task for w in workers), *jobs, return_exceptions=True
                )
        else:
            self.store.enable_course(domain, record["course_id"], True)
            # An explicit resume may attach again to the same remote lesson.
            bound = self.store.db.execute(
                "SELECT lesson FROM record_bindings WHERE record_id=?", (record_id,)
            ).fetchall()
            self.finished.difference_update(r[0] for r in bound)
            if not self.running:
                await self.start()
        self.discovery_wake.set()
        self.bus.publish({"type": "resync"})
        self.emit()
        return self.store.record(domain, record_id)

    async def course_changed(self, course_id):
        course = self.store.get_course(self.config.settings.domain, course_id)
        if not course or not course["enabled"]:
            for record in self.store.records(self.config.settings.domain, course_id):
                if record["listening"]:
                    await self.set_record_state(record["id"], "paused")
            workers = [
                w
                for w in self.lessons.values()
                if getattr(w, "course_id", None) == course_id
            ]
            for w in workers:
                w.task.cancel()
            if workers:
                await asyncio.gather(*(w.task for w in workers), return_exceptions=True)
        self.discovery_wake.set()
        self.bus.publish({"type": "resync"})
        self.emit()

    async def recover_jobs(self):
        while True:
            for id in self.store.pending(self.config.settings.domain):
                self.enqueue(id)
            await asyncio.sleep(1)

    def enqueue(self, id):
        if id in self.enqueued:
            return
        try:
            self.jobs.put_nowait(id)
            self.enqueued.add(id)
        except asyncio.QueueFull:
            pass  # Persisted pending records are recovered on the next scan.

    async def accept(self, lid, problem):
        id, created = self.store.insert(self.config.settings.domain, lid, problem)
        if not created:
            existing = self.store.get(id)
            if existing["status"] in ("failed", "pending"):
                refreshed = dict(existing["payload"])
                for key in ("cover", "body", "options", "type"):
                    refreshed[key] = problem[key]
                if refreshed != existing["payload"]:
                    self.store.update(id, payload=refreshed)
        if created:
            log.info("收到新题：课堂 %s，题目 %s", lid, problem["problem_id"])
            self.bus.publish({"type": "problem", "id": id})
            self.qq.capture(self.store.get(id))
            if self.running:
                self.spawn(
                    self.notifier.deliver(
                        self.store.get(id),
                        [c for c in self.config.settings.channels if c.kind != "qqbot"],
                        self.image_dir,
                        phase="reminder",
                    )
                )
            self.enqueue(id)

    async def solve_loop(self):
        while True:
            id = await self.jobs.get()
            job = asyncio.create_task(self.process(id))
            self.active_jobs[id] = job
            try:
                await job
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                self.store.update(id, status="failed", error=failure(exc))
                self.error("处理习题", exc)
                self.bus.publish({"type": "problem", "id": id})
            finally:
                self.active_jobs.pop(id, None)
                self.enqueued.discard(id)
                self.jobs.task_done()

    async def process(self, id):
        row = self.store.get(id)
        if not row or row["status"] != "pending":
            return
        if row.get("record_id"):
            record = self.store.record(row["domain"], row["record_id"])
            if not record or not record["listening"]:
                return
        cfg = self.config.settings
        p = row["payload"]
        self.store.update(id, status="processing", error="")
        self.bus.publish({"type": "problem", "id": id})
        try:
            # Retain cover images even when AI is disabled/unconfigured, including polls.
            image = mime = None
            if row["image"] and (self.image_dir / row["image"]).is_file():
                image = (self.image_dir / row["image"]).read_bytes()
                mime = image_mime(image)
            elif p.get("cover"):
                image, mime = await image_bytes(
                    p["cover"], referer=f"https://{row['domain']}/"
                )
                filename = (
                    f"{id}."
                    + {
                        "image/jpeg": "jpg",
                        "image/png": "png",
                        "image/gif": "gif",
                        "image/webp": "webp",
                    }[mime]
                )
                (self.image_dir / filename).write_bytes(image)
                self.store.update(id, image=filename)
            self.bus.publish({"type": "problem", "id": id})
            self.qq.enqueue_phase(self.store.get(id), "image")
            if p["type"] == 3:
                self.store.update(
                    id, status="skipped", error="投票题，请按个人意愿选择"
                )
            elif p["type"] not in (1, 2, 4, 5):
                raise ProtocolError("未知题型，请人工查看题面")
            elif not cfg.ai.enabled:
                self.store.update(id, status="skipped", error="AI 已关闭，仅展示题面")
            elif not image:
                raise ProtocolError("缺少题面图片；未向模型发送仅文字作答请求")
            else:
                async with asyncio.timeout(cfg.ai.timeout * 2 + 5):
                    answer = await Solver(cfg.ai).solve(p, image, mime)
                self.store.update(id, status="done", answer=answer)
        except asyncio.CancelledError:
            self.store.update(id, status="pending", error="")
            raise
        except Exception as exc:
            self.store.update(id, status="failed", error=failure(exc))
        self.bus.publish({"type": "problem", "id": id})
        self.qq.enqueue_phase(self.store.get(id), "result")
        await self.notifier.deliver(
            self.store.get(id),
            [c for c in cfg.channels if c.kind != "qqbot"],
            self.image_dir,
        )


class Lesson:
    def __init__(self, runtime, lid, course, cookie):
        self.rt, self.id, self.course, self.cookie = runtime, lid, course, cookie
        self.state = "进入课堂"
        self.record_id = None
        self.missing = 0
        self.task = None
        self.cache = {}
        self.loaded = set()
        self.pending = {}
        self.client = None
        self.ws = None

    def state_changed(self, value):
        self.state = value
        if value == "实时监听中":
            self.rt.record_errors.pop(self.record_id or self.id, None)
        if self.record_id:
            self.rt.store.record_state(
                self.rt.config.settings.domain,
                self.record_id,
                "live" if value == "实时监听中" else "waiting",
            )
        self.rt.emit()

    async def run(self):
        attempt = 0
        try:
            while True:
                try:
                    domain = self.rt.config.settings.domain
                    async with ClientContext(domain, self.cookie) as client:
                        self.client = client
                        self.state_changed("正在进入课堂")
                        ticket = await client.join(self.id)
                        self.state_changed("正在建立 WebSocket 连接")
                        async with socket(domain, self.cookie) as ws:
                            self.ws = ws
                            await send(
                                ws,
                                {
                                    "op": "hello",
                                    "userid": str(ticket["identityId"]),
                                    "role": "student",
                                    "auth": ticket["lessonToken"],
                                    "lessonid": self.id,
                                },
                            )
                            self.state_changed("等待 WebSocket 握手确认")
                            handshake_deadline = time.monotonic() + 15
                            handshake_confirmed = False
                            self.loaded.clear()
                            self.cache.clear()
                            queue = asyncio.Queue(maxsize=512)
                            reader = asyncio.create_task(self.read(ws, queue))
                            try:
                                while True:
                                    if handshake_confirmed:
                                        data = await queue.get()
                                    else:
                                        try:
                                            data = await asyncio.wait_for(
                                                queue.get(),
                                                max(
                                                    0.01,
                                                    handshake_deadline
                                                    - time.monotonic(),
                                                ),
                                            )
                                        except TimeoutError:
                                            raise ProtocolError(
                                                "WebSocket 握手确认超时"
                                            ) from None
                                    if isinstance(data, BaseException):
                                        raise data
                                    if data.get("op") == "lessonfinished":
                                        self.rt.finished.add(self.id)
                                        self.rt.record_errors.pop(
                                            self.record_id or self.id, None
                                        )
                                        if self.record_id:
                                            self.rt.qq.lesson_notice(
                                                domain,
                                                self.id,
                                                self.record_id,
                                                finished=True,
                                            )
                                            self.rt.store.record_state(
                                                domain, self.record_id, "archived"
                                            )
                                            self.rt.bus.publish({"type": "resync"})
                                        return
                                    if data.get("op") == "hello":
                                        handshake_confirmed = True
                                        self.state_changed("实时监听中")
                                        if self.record_id:
                                            self.rt.qq.lesson_notice(
                                                domain, self.id, self.record_id
                                            )
                                        log.info(
                                            "课堂 %s WebSocket 握手已确认", self.id
                                        )
                                    try:
                                        await self.handle(data)
                                    except AuthExpired:
                                        raise
                                    except Exception as exc:
                                        self.rt.error("课堂事件处理", exc)
                                    attempt = 0
                            finally:
                                reader.cancel()
                                await asyncio.gather(reader, return_exceptions=True)
                except AuthExpired as exc:
                    self.rt.store.save_session(self.rt.config.settings.domain, "")
                    self.rt.login_state = "登录已失效，请重新扫码"
                    self.rt.error("课堂鉴权", exc)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = (1, 2, 5)[min(attempt, 2)]
                    attempt += 1
                    detail = failure(exc)
                    self.rt.record_errors[self.record_id or self.id] = detail
                    self.state_changed(f"连接中断，{delay} 秒后重试")
                    log.warning("课堂 %s 连接：%s", self.id, detail)
                    await asyncio.sleep(delay)
        finally:
            if self.record_id:
                record = self.rt.store.record(
                    self.rt.config.settings.domain, self.record_id
                )
                if record and record["listening"]:
                    self.rt.store.record_state(
                        record["domain"], self.record_id, "waiting"
                    )
            self.rt.lessons.pop(self.id, None)
            self.rt.emit()

    async def read(self, ws, queue):
        heartbeat = time.monotonic() + 25
        last_message = time.monotonic()
        seq = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), max(0.01, heartbeat - time.monotonic())
                    )
                    last_message = time.monotonic()
                    data = message(raw)
                    if data:
                        if queue.full():
                            raise ProtocolError("事件队列拥堵，重连以补齐进度")
                        queue.put_nowait(data)
                except TimeoutError:
                    pass
                if time.monotonic() >= heartbeat:
                    if time.monotonic() - last_message > 75:
                        raise ProtocolError("课堂心跳无响应")
                    seq += 1
                    await send(
                        ws, {"op": "fetchtimeline", "lessonid": self.id, "msgid": seq}
                    )
                    heartbeat = time.monotonic() + 25
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(exc)

    async def fetch(self, pres, force=False):
        if not pres or (pres in self.loaded and not force):
            return
        data = await self.client.api(
            "GET",
            "/api/v3/lesson/presentation/fetch",
            params={"presentation_id": pres},
            jwt=True,
        )
        # Replacing a presentation must remove stale slides and problems.
        self.cache = {k: p for k, p in self.cache.items() if p["presentation"] != pres}
        for p in parse_slides(data, pres, self.course):
            self.cache[p["problem_id"]] = p
        self.loaded.add(pres)

    def resolve(self, ref):
        pid = ident(ref.get("problemId") or ref.get("problemid") or ref.get("prob"))
        if pid in self.cache:
            return self.cache[pid]
        sid = ident(ref.get("sid") or ref.get("slideId"))
        return next(
            (p for p in self.cache.values() if sid and p["slide_id"] == sid), None
        )

    async def unlock(self, ref):
        if not isinstance(ref, dict):
            ref = {"prob": ref}
        key = ident(
            ref.get("problemId")
            or ref.get("problemid")
            or ref.get("prob")
            or ref.get("sid")
        )
        if not key:
            return
        self.pending[key] = ref
        p = self.resolve(ref)
        pres = ident(ref.get("pres") or ref.get("presentation"))
        if p is None and pres:
            await self.fetch(pres, force=True)
            p = self.resolve(ref)
        if p is None:
            self.rt.error(
                "题目解析", ProtocolError(f"题目 {key} 尚未匹配到 PPT；保留等待补抓")
            )
            return
        p = copy.deepcopy(p)
        try:
            dt = float(ref.get("dt") or time.time())
            p["unlocked"] = dt / 1000 if dt > 1e11 else dt
            p["limit"] = int(ref.get("limit", -1))
        except (ValueError, TypeError):
            p["unlocked"] = time.time()
            p["limit"] = -1
        await self.rt.accept(self.id, p)
        self.pending.pop(key, None)

    async def handle(self, data):
        op = data.get("op")
        if op in ("hello", "fetchtimeline"):
            presentations = [ident(data.get("presentation"))]
            timeline = data.get("timeline") or []
            if isinstance(timeline, list):
                presentations += [
                    ident(t.get("pres")) for t in timeline if isinstance(t, dict)
                ]
            for pres in dict.fromkeys(presentations):
                await self.fetch(pres)
            unlocked = data.get("unlockedproblem") or []
            if isinstance(unlocked, dict):
                unlocked = list(unlocked)
            for ref in unlocked:
                if isinstance(ref, dict):
                    await self.unlock(ref)
                else:
                    await send(
                        self.ws,
                        {
                            "op": "probleminfo",
                            "lessonid": self.id,
                            "problemid": str(ref),
                            "msgid": int(time.time() * 1000),
                        },
                    )
                    await self.unlock({"prob": ref})
        elif op in ("presentationcreated", "presentationupdated"):
            await self.fetch(ident(data.get("presentation")), force=True)
        elif op == "unlockproblem":
            await self.unlock(data.get("problem") or data)
        elif op == "probleminfo":
            ref = data.get("problem")
            if isinstance(ref, dict):
                ref = {**data, **ref}
            else:
                ref = data
            await self.unlock(ref)
        if op in (
            "hello",
            "fetchtimeline",
            "presentationcreated",
            "presentationupdated",
        ):
            for ref in list(self.pending.values()):
                await self.unlock(ref)
