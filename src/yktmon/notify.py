from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
import httpx
from .protocol import ProtocolError, failure
from .presentation import notification_text


class Notifier:
    def __init__(self, store, publish, *, transport=None):
        self.store, self.publish, self.transport = store, publish, transport
        self.locks = {}

    async def deliver(self, row, channels, image_root, phase="result"):
        async with self.locks.setdefault(row["id"], asyncio.Lock()):
            signature = hashlib.sha256(
                json.dumps(
                    ["reminder"]
                    if phase == "reminder"
                    else [row["status"], row["answer"], row["error"]],
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            sent = {
                d["channel"]
                for d in self.store.get(row["id"])["deliveries"]
                if d["status"] in ("sent", "sending")
                and d["signature"] == signature
                and d.get("phase", "result") == phase
            }

            async def one(channel):
                if not channel.enabled or channel.kind in sent:
                    return
                self.store.delivery(
                    row["id"], channel.kind, "sending", signature=signature, phase=phase
                )
                try:
                    await self.send(channel, row, image_root, phase=phase)
                    self.store.delivery(
                        row["id"],
                        channel.kind,
                        "sent",
                        signature=signature,
                        phase=phase,
                    )
                except asyncio.CancelledError:
                    self.store.delivery(
                        row["id"],
                        channel.kind,
                        "failed",
                        "发送中断；结果未知，请检查群消息后再重试",
                        signature=signature,
                        phase=phase,
                    )
                    raise
                except Exception as exc:
                    self.store.delivery(
                        row["id"],
                        channel.kind,
                        "failed",
                        failure(exc),
                        signature=signature,
                        phase=phase,
                    )
                self.publish({"type": "problem", "id": row["id"]})

            await asyncio.gather(*(one(c) for c in channels))

    async def send(self, c, row, image_root, phase="result"):
        p = row["payload"]
        text = notification_text(row, phase)
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as http:

            async def post(url, payload, headers=None):
                r = await http.post(url, json=payload, headers=headers)
                r.raise_for_status()
                try:
                    result = r.json()
                except ValueError:
                    raise ProtocolError("机器人返回非 JSON，无法确认送达") from None
                if not isinstance(result, dict):
                    raise ProtocolError("机器人响应结构不正确")
                return result

            def check(result, *keys):
                code = next((result[k] for k in keys if k in result), None)
                if code not in (0, "0"):
                    raise ProtocolError(f"机器人未确认成功，错误码 {code}")

            if c.kind == "wecom":
                for part in split_utf8(text, 2048):
                    result = await post(
                        c.webhook_url, {"msgtype": "text", "text": {"content": part}}
                    )
                    check(result, "errcode")
                if row["image"]:
                    data = (image_root / row["image"]).read_bytes()
                    if len(data) > 2 * 1024 * 1024:
                        raise ProtocolError(
                            "文字已送达；图片超过企微 2 MB 限制，请从看板查看"
                        )
                    result = await post(
                        c.webhook_url,
                        {
                            "msgtype": "image",
                            "image": {
                                "base64": base64.b64encode(data).decode(),
                                "md5": hashlib.md5(data).hexdigest(),
                            },
                        },
                    )
                    check(result, "errcode")
            elif c.kind == "dingtalk":
                url = c.webhook_url
                if c.secret:
                    ts = str(int(time.time() * 1000))
                    sign = base64.b64encode(
                        hmac.new(
                            c.secret.encode(),
                            f"{ts}\n{c.secret}".encode(),
                            hashlib.sha256,
                        ).digest()
                    ).decode()
                    url += ("&" if "?" in url else "?") + urlencode(
                        {"timestamp": ts, "sign": sign}
                    )
                check(
                    await post(
                        url,
                        {
                            "msgtype": "markdown",
                            "markdown": {
                                "title": (
                                    "新习题提醒" if phase == "reminder" else "习题解答"
                                )
                                + " · "
                                + p.get("course", "课堂"),
                                "text": text
                                + (
                                    "\n\n![题面](" + p["cover"] + ")"
                                    if p.get("cover", "").startswith("https://")
                                    else ""
                                ),
                            },
                        },
                    ),
                    "errcode",
                )
            elif c.kind == "feishu":
                payload = {
                    "msg_type": "post",
                    "content": {
                        "post": {
                            "zh_cn": {
                                "title": (
                                    "新习题提醒" if phase == "reminder" else "习题解答"
                                )
                                + " · "
                                + p.get("course", "课堂"),
                                "content": [
                                    [{"tag": "text", "text": line}]
                                    for line in text.split("\n\n")
                                ],
                            }
                        }
                    },
                }
                if p.get("cover", "").startswith("https://"):
                    payload["content"]["post"]["zh_cn"]["content"].append(
                        [{"tag": "a", "text": "查看原题图片", "href": p["cover"]}]
                    )
                if c.secret:
                    ts = str(int(time.time()))
                    payload["timestamp"] = ts
                    payload["sign"] = base64.b64encode(
                        hmac.new(
                            f"{ts}\n{c.secret}".encode(), b"", hashlib.sha256
                        ).digest()
                    ).decode()
                check(await post(c.webhook_url, payload), "code", "StatusCode")
            elif c.kind == "qqbot":
                token = await post(
                    "https://bots.qq.com/app/getAppAccessToken",
                    {"appId": c.app_id, "clientSecret": c.client_secret},
                )
                if not token.get("access_token"):
                    raise ProtocolError("QQ 未返回 access_token")
                for part in split_utf8(text, 1800):
                    result = await post(
                        f"https://api.sgroup.qq.com/v2/groups/{c.group_openid}/messages",
                        {"content": part, "msg_type": 0},
                        headers={"Authorization": "QQBot " + token["access_token"]},
                    )
                    if not result.get("id"):
                        raise ProtocolError(
                            "QQ 未确认消息 ID；请检查主动消息权限和额度"
                        )


def split_utf8(text, limit):
    chunks = []
    current = []
    size = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(ch)
        size += n
    if current:
        chunks.append("".join(current))
    return chunks
