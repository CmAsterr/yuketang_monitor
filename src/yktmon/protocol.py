from __future__ import annotations
import asyncio
import base64
import io
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import qrcode
from PIL import Image
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

UA = "Mozilla/5.0 yktmon/5.0"


class ProtocolError(RuntimeError):
    pass


class AuthExpired(ProtocolError):
    pass


def failure(exc):
    """Do not include response bodies, signed URLs or credentials in user logs."""
    if isinstance(exc, ConnectionClosed):
        code = exc.rcvd.code if exc.rcvd else None
        return (
            f"课堂连接已断开（关闭码 {code}），正在自动重连"
            if code
            else "课堂连接意外断开，正在自动重连"
        )
    if isinstance(exc, (ProtocolError, ValueError)):
        return str(exc)[:300]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "请求超时"
    return type(exc).__name__ + "：请求失败，请检查网络与配置"


class Client:
    def __init__(self, domain, cookie="", *, transport=None):
        self.domain, self.jwt = domain, ""
        self.http = httpx.AsyncClient(
            base_url=f"https://{domain}",
            timeout=15,
            follow_redirects=False,
            headers={"User-Agent": UA, "Referer": f"https://{domain}/web"},
            transport=transport,
        )
        if cookie:
            self.http.cookies.set("sessionid", cookie, domain=domain, path="/")

    async def close(self):
        await self.http.aclose()

    @property
    def cookie(self):
        return next(
            (
                c.value
                for c in self.http.cookies.jar
                if c.name == "sessionid" and c.domain.lstrip(".") == self.domain
            ),
            "",
        )

    async def api(self, method, path, *, jwt=False, **kwargs):
        headers = {"Authorization": f"Bearer {self.jwt}"} if jwt and self.jwt else {}
        try:
            r = await self.http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            phase = {
                httpx.ConnectTimeout: "建立连接",
                httpx.ReadTimeout: "等待响应",
                httpx.WriteTimeout: "发送请求",
                httpx.PoolTimeout: "等待连接池",
            }.get(type(exc), "请求")
            # Only log our fixed API path, never signed URLs or request credentials.
            raise ProtocolError(
                f"{method} {path.split(chr(63))[0]}：{phase}超时"
            ) from None
        if r.headers.get("Set-Auth"):
            self.jwt = r.headers["Set-Auth"].removeprefix("Bearer ")
        if r.status_code in (401, 403):
            raise AuthExpired("登录或课堂鉴权已失效，请重新扫码")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise ProtocolError("雨课堂返回了非 JSON 内容") from None
        if not isinstance(data, dict):
            raise ProtocolError("雨课堂响应结构不正确")
        if (
            data.get("code") in (50000, 40001)
            or str(data.get("msg", "")).upper() == "UNAUTHENTICATED"
        ):
            raise AuthExpired("登录态已失效，请重新扫码")
        if data.get("code") != 0:
            raise ProtocolError(f"雨课堂业务错误：code={data.get('code')}")
        return data.get("data") or {}

    async def join(self, lesson):
        data = await self.api(
            "POST", "/api/v3/lesson/checkin", json={"source": 5, "lessonId": lesson}
        )
        if not all((data.get("identityId"), data.get("lessonToken"), self.jwt)):
            raise ProtocolError("进入课堂缺少 identityId / lessonToken / Set-Auth")
        return data

    async def exchange(self, user, auth):
        r = await self.http.post(
            "/pc/web_login",
            json={"UserID": user, "Auth": auth},
            headers={"Origin": f"https://{self.domain}"},
        )
        if r.status_code >= 400:
            r.raise_for_status()
        if not self.cookie:
            raise AuthExpired("扫码凭据未换得 sessionid，请重新扫码")
        await self.api("GET", "/api/v3/user/basic-info")
        return self.cookie


@asynccontextmanager
async def socket(domain, cookie=""):
    headers = {"User-Agent": UA}
    if cookie:
        headers["Cookie"] = "sessionid=" + cookie
    async with connect(
        f"wss://{domain}/wsapp/",
        origin=f"https://{domain}",
        additional_headers=headers,
        ping_interval=None,
        open_timeout=15,
        close_timeout=1,
        max_size=4 * 1024 * 1024,
    ) as ws:
        yield ws


async def send(ws, payload):
    await ws.send(json.dumps(payload))


def message(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


async def image_bytes(url, *, referer="", transport=None):
    # Separate client: never send classroom JWT/cookies to an image CDN.
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise ProtocolError("题面图地址必须是 HTTPS")
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True, transport=transport
    ) as client:
        async with client.stream("GET", url, headers={"Referer": referer}) as r:
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > 12 * 1024 * 1024:
                    raise ProtocolError("题面图超过 12 MB")
    data = bytes(buf)
    mime = image_mime(data)
    return data, mime


def image_mime(data):
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            if im.width * im.height > 30_000_000:
                raise ProtocolError("图片像素过大")
            im.verify()
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("下载内容不是有效图片") from None
    if fmt not in ("JPEG", "PNG", "GIF", "WEBP"):
        raise ProtocolError("不支持的图片格式")
    return {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "GIF": "image/gif",
        "WEBP": "image/webp",
    }[fmt]


async def qr_login(domain, status, save):
    async with ClientContext(domain) as client:
        async with socket(domain) as ws:
            deadline = time.monotonic() + 300
            refresh = 0.0
            while time.monotonic() < deadline:
                if time.monotonic() >= refresh:
                    await send(
                        ws,
                        {
                            "op": "requestlogin",
                            "role": "web",
                            "version": 1.4,
                            "type": "qrcode",
                            "from": "web",
                        },
                    )
                    refresh = time.monotonic() + 50
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), max(0.1, refresh - time.monotonic())
                    )
                except TimeoutError:
                    continue
                data = message(raw)
                if data.get("qrcode") or data.get("ticket"):
                    if data.get("qrcode"):
                        buf = io.BytesIO()
                        qrcode.make(data["qrcode"]).save(buf, format="PNG")
                        png = buf.getvalue()
                        mime = "image/png"
                    else:
                        png, mime = await image_bytes(data["ticket"])
                    refresh = time.monotonic() + max(
                        5, min(float(data.get("expire_seconds") or 60) - 10, 50)
                    )
                    status(
                        "等待微信扫码",
                        f"data:{mime};base64," + base64.b64encode(png).decode(),
                    )
                if data.get("Auth") and data.get("UserID"):
                    status("正在验证登录态", "")
                    cookie = await client.exchange(data["UserID"], data["Auth"])
                    save(cookie)
                    status("已登录", "")
                    return
            raise ProtocolError("扫码超时，请重新获取二维码")


@asynccontextmanager
async def ClientContext(domain, cookie=""):
    client = Client(domain, cookie)
    try:
        yield client
    finally:
        await client.close()


def ident(value):
    if isinstance(value, dict):
        return str(
            value.get("id") or value.get("presentationId") or value.get("pres") or ""
        )
    return str(value) if value is not None else ""


def parse_slides(data, pres, course):
    result = []
    for slide in data.get("slides") or []:
        if not isinstance(slide, dict):
            continue
        raw = slide.get("problem")
        if not isinstance(raw, dict) or not raw.get("problemId"):
            continue
        try:
            kind = int(raw.get("problemType", 0))
        except (ValueError, TypeError):
            kind = 0
        options = raw.get("options") or []
        if isinstance(options, dict):
            options = [f"{k}. {v}" for k, v in options.items()]
        if not isinstance(options, list):
            options = [str(options)]
        options = [
            str(x.get("body") or x.get("content") or x.get("text") or "")
            if isinstance(x, dict)
            else str(x)
            for x in options
        ]
        cover = next(
            (
                slide[k]
                for k in ("cover", "coverAlt", "coverUrl", "cover_url")
                if isinstance(slide.get(k), str) and slide[k]
            ),
            "",
        )
        result.append(
            {
                "problem_id": str(raw["problemId"]),
                "slide_id": str(slide.get("id", "")),
                "presentation": pres,
                "course": course,
                "type": kind,
                "body": str(raw.get("body") or ""),
                "options": options,
                "cover": cover.replace("&amp;", "&"),
                "limit": -1,
                "unlocked": 0,
            }
        )
    return result
