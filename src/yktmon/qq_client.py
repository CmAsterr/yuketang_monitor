"""QQ official API and resumable outbound gateway; no pilot module dependency."""

from __future__ import annotations
import asyncio
import base64
import io
import json
import math
import time
from urllib.parse import quote, urlsplit
import httpx
from PIL import Image
from websockets.asyncio.client import connect


class QQError(Exception):
    def __init__(
        self,
        message,
        *,
        code="",
        unknown=False,
        retry=False,
        permanent=False,
        http_status=None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.unknown = unknown
        self.retry = retry
        self.permanent = permanent


def image_upload(data):
    """Conservative application limit, not a claim about platform maximums."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > 20_000_000:
                raise ValueError()
            im.load()
            if im.format in ("JPEG", "PNG") and len(data) <= 4 * 1024 * 1024:
                return data
            im.thumbnail((2400, 2400))
            out = io.BytesIO()
            im.convert("RGB").save(out, format="JPEG", quality=88)
            data = out.getvalue()
            if len(data) > 4 * 1024 * 1024:
                raise ValueError()
            return data
    except Exception:
        raise QQError("图片无效或超过本程序大小限制；原图保留在本地") from None


class DeliveryID(str):
    """A message ID plus an explicit rendering downgrade, safe for concurrent sends."""

    def __new__(cls, value, note=""):
        obj = super().__new__(cls, value)
        obj.note = note
        return obj


def image_markdown_body(url, caption, width, height):
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        return None
    # Percent-escape Markdown delimiters without changing existing signed URL escapes.
    safe_url = quote(url, safe=":/?#%=&+-_.~@")
    title = str(caption).replace("\n", " ").replace("\r", " ")
    for char in ("\\", "*", "_", "[", "]", "`"):
        title = title.replace(char, "\\" + char)
    return {
        "msg_type": 2,
        "markdown": {
            "content": f"![题面 #{width}px #{height}px]({safe_url})\n\n**{title}**",
            "force_verify_image_resource": True,
        },
    }


class QQClient:
    def __init__(self, app_id, secret, event, *, transport=None, connector=connect):
        self.app_id, self.secret, self.event = app_id, secret, event
        self.http = httpx.AsyncClient(
            timeout=15, transport=transport, follow_redirects=False
        )
        self.connector = connector
        self.token_lock = asyncio.Lock()
        self.token = ""
        self.expiry = 0
        self.session = ""
        self.seq = None
        self.state = "未连接"
        self.error = ""
        self.task = None
        self.acked = True
        self.last_refresh = 0
        self.last_heartbeat = 0
        self.last_reconnect = 0
        self.name = ""

    async def auth(self):
        async with self.token_lock:
            if self.token and time.monotonic() < self.expiry:
                return
            try:
                r = await self.http.post(
                    "https://bots.qq.com/app/getAppAccessToken",
                    json={"appId": self.app_id, "clientSecret": self.secret},
                )
                d = r.json()
            except (httpx.HTTPError, ValueError):
                raise QQError("QQ Token 请求失败", retry=True) from None
            if r.status_code in (429, 500, 502, 503, 504):
                raise QQError("QQ 鉴权服务暂不可用", code=r.status_code, retry=True)
            if (
                r.status_code != 200
                or not isinstance(d, dict)
                or not isinstance(d.get("access_token"), str)
            ):
                raise QQError(
                    "QQ 凭据被拒绝，请检查 AppID / AppSecret",
                    code=r.status_code,
                    permanent=True,
                )
            try:
                ttl = float(d.get("expires_in", 0))
            except (ValueError, TypeError):
                ttl = 0
            if not math.isfinite(ttl) or ttl <= 2:
                raise QQError("QQ Token 有效期异常", permanent=True)
            self.token = d["access_token"]
            self.expiry = time.monotonic() + ttl - min(60, ttl * 0.2)
            self.last_refresh = time.time()

    async def request(self, method, path, *, sending=False, **kwargs):
        for attempt in range(2):
            await self.auth()
            try:
                r = await self.http.request(
                    method,
                    "https://api.sgroup.qq.com" + path,
                    headers={"Authorization": "QQBot " + self.token},
                    **kwargs,
                )
            except httpx.HTTPError:
                raise QQError(
                    "QQ 发送响应丢失，结果未知，请核对群消息"
                    if sending
                    else "QQ 网络请求失败",
                    unknown=sending,
                    retry=not sending,
                ) from None
            if r.status_code == 401:
                self.expiry = 0
                if attempt == 0:
                    continue  # explicit authentication rejection, no message accepted
            try:
                d = r.json()
            except ValueError:
                raise QQError(
                    f"QQ 响应格式异常 HTTP {r.status_code}", unknown=sending
                ) from None
            code = d.get("code") if isinstance(d, dict) else None
            if code is not None and (
                isinstance(code, bool)
                or not isinstance(code, (int, str))
                or not str(code).isdigit()
            ):
                code = "未识别"
            elif isinstance(code, str):
                code = int(code)
            if r.status_code >= 300 or not isinstance(d, dict) or code not in (None, 0):
                category = (
                    "权限或凭据被拒绝"
                    if r.status_code in (401, 403)
                    else "限流，请稍后重试"
                    if r.status_code == 429
                    else "接口拒绝请求，请核查额度、权限或内容"
                )
                raise QQError(
                    f"QQ {category}（HTTP {r.status_code} / code {code}）",
                    code=code or r.status_code,
                    http_status=r.status_code,
                    retry=r.status_code == 429
                    or (not sending and r.status_code >= 500),
                    unknown=sending and r.status_code >= 500,
                    permanent=r.status_code == 401,
                )
            return d
        raise QQError("QQ 鉴权失败", permanent=True)

    async def send(self, openid, payload, image_root):
        group = quote(openid, safe="")
        kind = payload["kind"]
        note = ""
        if kind == "image":
            path = (image_root / payload["image"]).resolve()
            if path.parent != image_root.resolve() or not path.is_file():
                raise QQError("本地题面图片不存在")
            data = image_upload(path.read_bytes())
            url = payload.get("markdown_url")
            if url:
                with Image.open(path) as im:
                    width, height = im.size
                body = image_markdown_body(url, payload["caption"], width, height)
                if body:
                    try:
                        result = await self.request(
                            "POST",
                            f"/v2/groups/{group}/messages",
                            json=body,
                            sending=True,
                        )
                        if not result.get("id"):
                            raise QQError(
                                "QQ 未返回消息 ID，发送结果未知", unknown=True
                            )
                        return DeliveryID(str(result["id"]))
                    except QQError as exc:
                        # Only explicit payload rejection permits a second send in another format.
                        # No fallback after a timeout, 5xx or missing ID: it may already be delivered.
                        if exc.unknown or exc.http_status not in (400, 422):
                            raise
                        note = "Markdown 图片被拒绝，已降级为本地原生图片；附文为普通文字，未加粗"
                else:
                    note = "原图地址不支持 Markdown，已发送本地原生图片；附文未加粗"
            elif payload.get("prefer_bold"):
                note = "没有可用原图链接，已发送本地原生图片；附文未加粗"
            media = await self.request(
                "POST",
                f"/v2/groups/{group}/files",
                json={
                    "file_type": 1,
                    "file_data": base64.b64encode(data).decode(),
                    "srv_send_msg": False,
                },
            )
            if not media.get("file_info"):
                raise QQError("图片上传未返回 file_info")
            body = {
                "msg_type": 7,
                "media": {"file_info": media["file_info"]},
                "content": payload["caption"],
            }
        elif kind == "markdown":
            if len(payload["text"].encode("utf-8")) > 12000:
                raise QQError(
                    "解答超过本程序单条 Markdown 长度预算，未截断公式；请在网页查看完整结果"
                )
            body = {"msg_type": 2, "markdown": {"content": payload["text"]}}
        else:
            body = {"msg_type": 0, "content": payload["text"]}
        result = await self.request(
            "POST", f"/v2/groups/{group}/messages", json=body, sending=True
        )
        if not result.get("id"):
            raise QQError("QQ 未返回消息 ID，发送结果未知", unknown=True)
        return DeliveryID(str(result["id"]), note)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self.http.aclose()
        self.state = "已断开"
        self.token = ""

    async def run(self):
        attempt = 0
        while True:
            try:
                self.state = "连接中"
                gateway = await self.request("GET", "/gateway/bot")
                url = gateway.get("url", "")
                if urlsplit(url).scheme != "wss":
                    raise QQError("QQ 网关地址无效", permanent=True)
                async with self.connector(
                    url,
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=1,
                    max_size=2**22,
                ) as ws:
                    hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if hello.get("op") != 10:
                        raise QQError("QQ 网关握手异常")
                    interval = max(
                        1, min(60, float(hello["d"]["heartbeat_interval"]) / 1000)
                    )
                    await self.auth()
                    self.acked = True
                    await ws.send(
                        json.dumps(
                            {
                                "op": 6,
                                "d": {
                                    "token": "QQBot " + self.token,
                                    "session_id": self.session,
                                    "seq": self.seq,
                                },
                            }
                            if self.session
                            else {
                                "op": 2,
                                "d": {
                                    "token": "QQBot " + self.token,
                                    "intents": 1 << 25,
                                    "shard": [0, 1],
                                },
                            }
                        )
                    )

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(interval)
                            if not self.acked:
                                raise QQError("QQ 心跳未确认")
                            await self.auth()
                            self.acked = False
                            await ws.send(json.dumps({"op": 1, "d": self.seq}))

                    beat = asyncio.create_task(heartbeat())
                    recv = None
                    try:
                        while True:
                            recv = asyncio.create_task(ws.recv())
                            done, _ = await asyncio.wait(
                                [recv, beat],
                                timeout=interval * 2 + 15,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if beat in done:
                                await beat
                            if recv not in done:
                                raise QQError("QQ 网关无响应")
                            e = json.loads(recv.result())
                            op = e.get("op")
                            d = e.get("d") or {}
                            if e.get("s") is not None:
                                self.seq = e["s"]
                            if op == 11:
                                self.acked = True
                                self.last_heartbeat = time.time()
                            elif op == 1:
                                await ws.send(json.dumps({"op": 1, "d": self.seq}))
                            elif op == 7:
                                raise QQError("QQ 请求重新连接")
                            elif op == 9:
                                self.session = ""
                                self.seq = None
                                raise QQError("QQ 会话失效，重新连接")
                            elif op == 0:
                                kind = e.get("t")
                                if kind in ("READY", "RESUMED"):
                                    if kind == "READY":
                                        self.session = d.get("session_id", "")
                                        self.name = str(
                                            (d.get("user") or {}).get("username")
                                            or "QQ机器人"
                                        )
                                    self.state = "已连接"
                                    self.error = ""
                                    attempt = 0
                                self.event(e)
                    finally:
                        for task in (beat, recv):
                            if task:
                                task.cancel()
                        await asyncio.gather(
                            *(t for t in (beat, recv) if t), return_exceptions=True
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None)
                if code in (4006, 4007, 4009):
                    self.session = ""
                    self.seq = None
                    self.expiry = 0
                permanent = (
                    isinstance(exc, QQError)
                    and exc.permanent
                    or code in (4004, 4010, 4011, 4012, 4013, 4014)
                )
                self.error = str(exc) if isinstance(exc, QQError) else "QQ 网关连接中断"
                if permanent:
                    self.state = "凭据或网关配置错误"
                    return
                self.last_reconnect = time.time()
                delay = (1, 2, 5)[min(attempt, 2)]
                attempt += 1
                self.state = f"自动重连中（{delay} 秒）"
                await asyncio.sleep(delay)
