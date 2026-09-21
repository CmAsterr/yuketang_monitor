from __future__ import annotations
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ConfigFile, AI
from .model_catalog import fetch_models
from .lifecycle import AppLifecycle
from .protocol import ProtocolError
from .runtime import Runtime
from .store import Store
from .presentation import present, notification_text, markdown
from .qq_service import result_markdown, reminder_markdown, question_caption


class DataLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.file = self.path.open("a+b")
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            import os

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self.file:
                self.file.close()
            self.file = None
            raise RuntimeError(
                "这个数据目录已有服务运行，请关闭旧服务或使用另一个 --data-dir"
            ) from None

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


def create_app(
    config_path: Path,
    data_dir: Path,
    *,
    autostart=True,
    qq_autostart=True,
    lifecycle: AppLifecycle | None = None,
):
    config = ConfigFile(config_path)
    data_dir = data_dir.resolve()
    static = Path(__file__).parent / "static"
    lifecycle = lifecycle or AppLifecycle(config_path, data_dir)

    @asynccontextmanager
    async def lifespan(app):
        lock = DataLock(data_dir / "service.lock")
        lock.acquire()
        store = None
        watcher = None
        try:
            store = Store(data_dir / "monitor.db")
            rt = Runtime(config, store, data_dir)
            app.state.runtime = rt
            if qq_autostart:
                await rt.qq.start()
            if autostart:
                await rt.start()
            lifecycle.ready = True
            lifecycle.reserve_open()
            watcher = asyncio.create_task(lifecycle.watch())
            yield
        finally:
            lifecycle.ready = False
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            try:
                if store:
                    try:
                        await rt.close()
                    finally:
                        store.close()
            finally:
                lock.release()

    app = FastAPI(title="雨课堂监控", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.lifecycle = lifecycle
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
    )

    @app.middleware("http")
    async def local_write_guard(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("x-yktmon") != "local" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse({"detail": "请从本机看板操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def rt(request):
        return request.app.state.runtime

    async def body(request):
        if int(request.headers.get("content-length") or 0) > 65536:
            raise HTTPException(413, "请求过大")
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, "请求过大")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "需要 JSON 对象") from None
        if not isinstance(result, dict):
            raise HTTPException(400, "需要 JSON 对象")
        return result

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/api/app/status")
    async def app_status():
        return lifecycle.status()

    @app.post("/api/app/open")
    async def app_open():
        if not lifecycle.reserve_open():
            raise HTTPException(409, "服务正在退出，请稍后重新双击桌面快捷方式")
        return lifecycle.status()

    @app.post("/api/app/exit")
    async def app_exit(request: Request):
        data = await body(request)
        if data.get("instance_id") != lifecycle.instance:
            raise HTTPException(409, "服务已重启，请刷新后再退出")
        if lifecycle.on_exit is None:
            raise HTTPException(409, "此启动方式不支持网页退出，请关闭服务启动器")
        lifecycle.request_exit("用户从看板退出程序")
        return {"ok": True}

    @app.websocket("/api/app/presence")
    async def app_presence(ws: WebSocket):
        import re

        origin = ws.headers.get("origin", "")
        page_id = ws.query_params.get("page_id", "")
        if urlsplit(origin).scheme != "http" or urlsplit(
            origin
        ).netloc != ws.headers.get("host"):
            await ws.close(code=1008)
            return
        if (
            not re.fullmatch(r"[A-Za-z0-9-]{16,80}", page_id)
            or lifecycle.exit_requested
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        token = lifecycle.connect(page_id)
        receive = None
        try:
            while not lifecycle.exit_requested:
                await ws.send_json(lifecycle.status())
                receive = asyncio.create_task(ws.receive_text())
                # Do not time out based on JS timers: background tabs may be throttled.
                # Uvicorn's WebSocket ping/pong checks the underlying connection.
                while not receive.done() and not lifecycle.exit_requested:
                    done, _ = await asyncio.wait([receive], timeout=5)
                    if not done:
                        await ws.send_json(lifecycle.status())
                if lifecycle.exit_requested:
                    break
                raw = receive.result()
                if len(raw) > 128:
                    await ws.close(code=1008)
                    return
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            if receive:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            lifecycle.disconnect(token)
            if lifecycle.exit_requested:
                try:
                    await ws.send_json(lifecycle.status())
                    await ws.close(code=1001)
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass

    @app.get("/api/config")
    async def get_config():
        return config.public()

    @app.put("/api/config")
    async def save_config(request: Request):
        patch = await body(request)
        async with rt(request).control:
            try:
                settings = config.prepare(patch)
            except (ValidationError, ValueError, TypeError, AttributeError):
                raise HTTPException(
                    422, "配置无效：检查站点、时间范围、模型名称和机器人必填项"
                ) from None
            await rt(request).apply(settings)
        return config.public()

    @app.get("/api/status")
    async def status(request: Request):
        return rt(request).status()

    @app.post("/api/monitor/{action}")
    async def monitor(action: str, request: Request):
        async with rt(request).control:
            if action == "start":
                await rt(request).start()
            elif action == "stop":
                await rt(request).stop()
            else:
                raise HTTPException(404, "未知操作")
        return rt(request).status()

    @app.post("/api/login")
    async def login(request: Request):
        async with rt(request).control:
            await rt(request).begin_login()
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request):
        async with rt(request).control:
            await rt(request).logout()
        return {"ok": True}

    @app.post("/api/ai/test")
    async def test_ai(request: Request):
        await rt(request).diagnose()
        return {"ok": True}

    @app.post("/api/ai/key")
    async def saved_ai_key():
        # Explicit local-user preference: reveal only this credential through the
        # existing same-origin write guard; regular config reads remain redacted.
        return {"api_key": config.settings.ai.api_key}

    @app.post("/api/ai/models")
    async def models(request: Request):
        data = await body(request)
        try:
            base = AI(
                base_url=data.get("base_url") or config.settings.ai.base_url
            ).base_url
            key = data.get("api_key") or ""
            if not isinstance(key, str):
                raise ValueError()
            if not key:
                if base != config.settings.ai.base_url:
                    raise HTTPException(
                        422,
                        "API 地址已更改，请重新输入 Key；不会把已保存 Key 发送给新的服务",
                    )
                key = config.settings.ai.api_key
            return await fetch_models(base, key)
        except (ValueError, ValidationError):
            raise HTTPException(422, "请填写有效的 HTTPS API 地址和 Key") from None
        except ProtocolError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/records")
    async def records(request: Request, course_id: int | None = None):
        return rt(request).store.records(config.settings.domain, course_id)

    @app.post("/api/records")
    async def create_record(request: Request):
        data = await body(request)
        r = rt(request)
        if type(data.get("course_id")) is not int:
            raise HTTPException(422, "请选择课程")
        title = data.get("title", "")
        listen = data.get("listening", False)
        if not isinstance(title, str) or len(title) > 120 or type(listen) is not bool:
            raise HTTPException(422, "课堂标题或监听状态无效")
        async with r.control:
            try:
                row = r.store.create_record(
                    config.settings.domain, data["course_id"], title.strip(), listen
                )
                if listen:
                    await r.set_record_state(row["id"], "waiting")
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            r.bus.publish({"type": "resync"})
        return row

    @app.patch("/api/records/{id}")
    async def rename_record(id: int, request: Request):
        data = await body(request)
        title = data.get("title")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise HTTPException(422, "请输入课堂标题")
        if not rt(request).store.rename_record(
            config.settings.domain, id, title.strip()
        ):
            raise HTTPException(404, "记录不存在")
        rt(request).bus.publish({"type": "resync"})
        return {"ok": True}

    @app.post("/api/records/{id}/{action}")
    async def record_action(id: int, action: str, request: Request):
        state = {"resume": "waiting", "pause": "paused", "finish": "archived"}.get(
            action
        )
        if not state:
            raise HTTPException(404, "未知操作")
        r = rt(request)
        async with r.control:
            try:
                return await r.set_record_state(id, state)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.get("/api/qq/status")
    async def qq_status(request: Request):
        return rt(request).qq.status()

    @app.post("/api/qq/secret")
    async def qq_secret(request: Request):
        if not config.settings.qq.secret:
            raise HTTPException(404, "尚未保存 QQ AppSecret")
        return {
            "app_id": config.settings.qq.app_id,
            "secret": config.settings.qq.secret,
        }

    @app.post("/api/qq/config")
    async def qq_config(request: Request):
        data = await body(request)
        r = rt(request)
        async with r.control:
            try:
                settings = config.prepare({"qq": data})
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(
                    422, "请输入有效 AppID 和 AppSecret；切换机器人需输入新 Secret"
                ) from None
            async with r.qq.control:
                old = config.settings.qq.app_id
                await r.qq.stop()
                config.save(settings)
                if old != settings.qq.app_id:
                    r.qq.repo.cancel_app(old)
                await r.qq.start()
        return r.qq.status()

    @app.post("/api/qq/{action}")
    async def qq_action(action: str, request: Request):
        q = rt(request).qq
        async with q.control:
            if action == "disconnect":
                await q.stop()
            elif action == "connect":
                await q.start()
            elif action == "clear":
                settings = config.prepare(
                    {"qq": {"enabled": False, "secret": "__CLEAR__"}}
                )
                await q.stop()
                config.save(settings)
            elif action == "binding":
                try:
                    q.new_binding()
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from None
            else:
                raise HTTPException(404, "未知 QQ 操作")
        return q.status()

    @app.post("/api/qq/binding/confirm")
    async def qq_bind(request: Request):
        data = await body(request)
        q = rt(request).qq
        name = data.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise HTTPException(422, "请填写群别名")
        async with q.control:
            try:
                q.confirm_binding(data.get("id"), data.get("candidate"), name.strip())
            except (ValueError, TypeError) as exc:
                raise HTTPException(409, str(exc)) from None
        return q.status()

    @app.put("/api/qq/groups/{id}")
    async def qq_group(id: int, request: Request):
        data = await body(request)
        q = rt(request).qq
        if "name" in data and (
            not isinstance(data["name"], str)
            or not 1 <= len(data["name"].strip()) <= 80
        ):
            raise HTTPException(422, "群名称无效")
        if "enabled" in data and type(data["enabled"]) is not bool:
            raise HTTPException(422, "启用状态无效")
        async with q.control:
            try:
                await q.update_group(
                    id, name=data.get("name"), enabled=data.get("enabled")
                )
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
        return q.status()

    @app.get("/api/courses/{id}/qq-targets")
    async def qq_targets(id: int, request: Request):
        r = rt(request)
        if not r.store.get_course(config.settings.domain, id):
            raise HTTPException(404, "课程不存在")
        return {
            "group_ids": r.qq.repo.mapping(id, r.qq.cfg.app_id),
            "groups": r.qq.repo.groups(r.qq.cfg.app_id),
        }

    @app.put("/api/courses/{id}/qq-targets")
    async def qq_set_targets(id: int, request: Request):
        r = rt(request)
        data = await body(request)
        ids = data.get("group_ids")
        if not r.store.get_course(config.settings.domain, id):
            raise HTTPException(404, "课程不存在")
        if (
            not isinstance(ids, list)
            or len(ids) > 100
            or any(type(x) is not int for x in ids)
        ):
            raise HTTPException(422, "请选择有效通知群")
        async with r.qq.control:
            try:
                r.qq.repo.set_mapping(id, r.qq.cfg.app_id, ids)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        return {"ok": True}

    @app.post("/api/qq/test/send")
    async def qq_test(request: Request):
        import re

        data = await body(request)
        q = rt(request).qq
        if (
            type(data.get("group_id")) is not int
            or not isinstance(data.get("attempt"), str)
            or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", data["attempt"])
        ):
            raise HTTPException(422, "发送目标或请求标识无效")
        async with q.control:
            try:
                id = q.test(data["group_id"], data.get("kind"), data["attempt"])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        return {"id": id, "message": "已加入指定群的发送队列；请查看投递记录"}

    @app.post("/api/qq/outbox/{id}/{action}")
    async def qq_retry(id: int, action: str, request: Request):
        q = rt(request).qq
        data = await body(request)
        async with q.control:
            try:
                if action == "retry":
                    q.retry(id, data.get("confirm_unknown") is True)
                elif action == "cancel":
                    q.cancel(id)
                else:
                    raise ValueError("未知操作")
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        return q.status()

    @app.get("/api/courses")
    async def courses(request: Request):
        return rt(request).store.courses(config.settings.domain)

    @app.put("/api/courses/{id}")
    async def course(id: int, request: Request):
        data = await body(request)
        if "display_name" in data:
            name = data["display_name"]
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                raise HTTPException(422, "请输入课程名称")
            if not rt(request).store.rename_course(
                config.settings.domain, id, name.strip()
            ):
                raise HTTPException(404, "课程不存在")
            rt(request).bus.publish({"type": "resync"})
            return {"ok": True}
        if type(data.get("enabled")) is not bool:
            raise HTTPException(422, "enabled 必须是布尔值")
        r = rt(request)
        async with r.control:
            if not r.store.enable_course(config.settings.domain, id, data["enabled"]):
                raise HTTPException(404, "课程不存在")
            await r.course_changed(id)
        return {"ok": True}

    @app.post("/api/courses")
    async def add_course(request: Request):
        data = await body(request)
        name = data.get("name")
        classroom = data.get("classroom", "")
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 120
            or not isinstance(classroom, str)
            or len(classroom) > 100
        ):
            raise HTTPException(422, "请输入课程名称（1–120 字），课堂 ID 可选")
        r = rt(request)
        async with r.control:
            try:
                row = r.store.add_course(
                    config.settings.domain, classroom.strip(), name.strip()
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            await r.course_changed(row["id"])
        return row

    @app.delete("/api/courses/{id}")
    async def delete_course(id: int, request: Request):
        r = rt(request)
        async with r.control:
            if not r.store.delete_course(config.settings.domain, id):
                raise HTTPException(404, "课程不存在")
            await r.course_changed(id)
        return {"ok": True}

    @app.get("/api/problems")
    async def problems(
        request: Request, limit: int = 50, offset: int = 0, record_id: int | None = None
    ):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise HTTPException(422, "分页参数超出范围")
        store = rt(request).store
        if record_id is not None:
            if not store.record(config.settings.domain, record_id):
                raise HTTPException(404, "课堂记录不存在")
            rows = store.record_problems(
                config.settings.domain, record_id, limit, offset
            )
        else:
            rows = store.recent(config.settings.domain, limit, offset)
        result = []
        for row in rows:
            item = present(row)
            item["qq_deliveries"] = rt(request).qq.repo.history(
                config.settings.qq.app_id, 100, row["id"]
            )
            result.append(item)
        return result

    def problem(r, id):
        row = r.store.get(id)
        if not row or row["domain"] != config.settings.domain:
            raise HTTPException(404, "题目不存在")
        return row

    @app.get("/api/problems/{id}/notification-preview")
    async def notification_preview(id: int, request: Request):
        runtime = rt(request)
        row = problem(runtime, id)
        record = (
            runtime.store.record(row["domain"], row["record_id"])
            if row.get("record_id")
            else None
        )
        number = runtime.qq.display_number(row) if record else 1
        if record:
            reminder = reminder_markdown(row, record, number)
            result = result_markdown(row, record, number)
        else:
            reminder = notification_text(row, "reminder")
            result = notification_text(row)
        return {
            "reminder": reminder,
            "result": result,
            "reminder_html": markdown(reminder),
            "result_html": markdown(result),
            "image_caption": question_caption(row, record, number)
            if record
            else "题面图片",
            "image": f"/api/problems/{id}/image"
            if row["image"]
            else row["payload"].get("cover", ""),
        }

    @app.post("/api/problems/{id}/retry")
    async def retry(id: int, request: Request):
        r = rt(request)
        async with r.control:
            row = problem(r, id)
            if (
                row.get("record_id")
                and not r.store.record(config.settings.domain, row["record_id"])[
                    "listening"
                ]
            ):
                raise HTTPException(409, "历史课堂为只读，请先继续本节监听")
            if not r.running:
                raise HTTPException(409, "请先启动监控")
            if row["status"] in ("processing", "pending"):
                raise HTTPException(409, "题目已在处理中")
            r.store.update(id, status="pending", error="", answer={})
            r.enqueue(id)
        return {"ok": True}

    @app.post("/api/problems/{id}/notify")
    async def retry_notify(id: int, request: Request):
        r = rt(request)
        row = problem(r, id)
        if (
            row.get("record_id")
            and not r.store.record(config.settings.domain, row["record_id"])[
                "listening"
            ]
        ):
            raise HTTPException(409, "历史课堂为只读，请先继续本节监听")
        if not r.running:
            raise HTTPException(409, "请先启动监控")
        if row["status"] in ("pending", "processing"):
            raise HTTPException(409, "题目仍在处理中")

        async def deliver_failed():
            if any(
                d.get("phase") == "reminder" and d["status"] == "failed"
                for d in row["deliveries"]
            ):
                await r.notifier.deliver(
                    row,
                    [c for c in config.settings.channels if c.kind != "qqbot"],
                    r.image_dir,
                    phase="reminder",
                )
            await r.notifier.deliver(
                row,
                [c for c in config.settings.channels if c.kind != "qqbot"],
                r.image_dir,
            )

        r.spawn(deliver_failed())
        return {"ok": True}

    @app.get("/api/problems/{id}/image")
    async def image(id: int, request: Request, download: bool = False):
        r = rt(request)
        row = problem(r, id)
        path = r.image_dir / row["image"]
        if (
            not row["image"]
            or path.resolve().parent != r.image_dir.resolve()
            or not path.is_file()
        ):
            raise HTTPException(404, "本地题面图尚未就绪")
        return FileResponse(path, filename=path.name if download else None)

    @app.post("/api/images/open-folder")
    async def open_image_folder(request: Request):
        import os

        if os.name != "nt":
            raise HTTPException(501, "此系统暂不支持打开文件夹，请使用图片下载")
        os.startfile(str(rt(request).image_dir.resolve()))
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request):
        r = rt(request)

        async def stream():
            q = asyncio.Queue(maxsize=64)
            r.bus.subscribers.add(q)
            try:
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "status", "data": r.status()}, ensure_ascii=False
                    )
                    + "\n\n"
                )
                yield 'data: {"type":"resync"}\n\n'
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), 15)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                r.bus.subscribers.discard(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    return app
