from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SERVERS = {
    "长江雨课堂": "changjiang.yuketang.cn",
    "雨课堂": "www.yuketang.cn",
    "荷塘雨课堂": "pro.yuketang.cn",
    "黄河雨课堂": "huanghe.yuketang.cn",
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AI(Model):
    enabled: bool = True
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = ""
    max_tokens: int = Field(4096, ge=256, le=65536)
    timeout: float = Field(60, ge=5, le=180)
    image_detail: Literal["auto", "low", "high", "original"] = "high"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        u = urlsplit(value)
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("AI 地址必须是无凭据和查询参数的 HTTPS URL")
        from .model_catalog import api_base

        return api_base(value)

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        if not value.strip():
            raise ValueError("模型名称不能为空")
        return value.strip()


class Channel(Model):
    kind: Literal["wecom", "dingtalk", "feishu", "qqbot"]
    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    app_id: str = ""
    client_secret: str = ""
    group_openid: str = ""

    @field_validator("webhook_url")
    @classmethod
    def valid_url(cls, value):
        if value and (
            urlsplit(value).scheme != "https" or not urlsplit(value).hostname
        ):
            raise ValueError("机器人地址必须使用 HTTPS")
        return value


class QQConfig(Model):
    enabled: bool = False
    app_id: str = ""
    secret: str = ""

    @model_validator(mode="after")
    def enabled_credentials(self):
        if self.enabled and (not self.app_id or not self.secret):
            raise ValueError("启用 QQ 需要 AppID 和 AppSecret")
        return self

    @field_validator("app_id")
    @classmethod
    def valid_app(cls, value):
        import re

        if value and not re.fullmatch(r"\d{4,30}", value):
            raise ValueError("AppID 应为数字")
        return value

    @field_validator("secret")
    @classmethod
    def valid_secret(cls, value):
        if len(value) > 512:
            raise ValueError("AppSecret 过长")
        return value.strip()


class Settings(Model):
    domain: str = "changjiang.yuketang.cn"
    scan_interval: float = Field(30, ge=5, le=600)
    course_filter: list[str] = Field(default_factory=list)
    ai: AI = Field(default_factory=AI)
    channels: list[Channel] = Field(default_factory=list)
    qq: QQConfig = Field(default_factory=QQConfig)

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value):
        if value not in SERVERS.values():
            raise ValueError("请选择受支持的雨课堂站点")
        return value

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value):
        if len({c.kind for c in value}) != len(value):
            raise ValueError("每种渠道只能配置一次")
        for c in value:
            if (
                c.enabled
                and c.kind == "qqbot"
                and not all((c.app_id, c.client_secret, c.group_openid))
            ):
                raise ValueError("QQ 需要 app_id、client_secret 和 group_openid")
            if c.enabled and c.kind != "qqbot" and not c.webhook_url:
                raise ValueError("启用机器人前请填写 webhook_url")
        return value


SECRETS = ("webhook_url", "secret", "client_secret")


class ConfigFile:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.doc = (
            tomlkit.parse(path.read_text("utf-8-sig"))
            if path.exists()
            else tomlkit.document()
        )
        self.settings = Settings.model_validate(self.doc.unwrap())

    def public(self):
        data = self.settings.model_dump()
        data["ai"]["api_key_configured"] = bool(data["ai"]["api_key"])
        data["ai"]["api_key"] = ""
        data["qq"]["secret_configured"] = bool(data["qq"]["secret"])
        data["qq"]["secret"] = ""
        for channel in data["channels"]:
            for key in SECRETS:
                channel[key + "_configured"] = bool(channel[key])
                channel[key] = ""
        return {**data, "servers": SERVERS}

    def prepare(self, patch: dict):
        data = self.settings.model_dump()
        patch = copy.deepcopy(patch)
        if "qq" in patch:
            qq = patch.pop("qq")
            qq.pop("secret_configured", None)
            if qq.get("app_id", data["qq"]["app_id"]) != data["qq"][
                "app_id"
            ] and not qq.get("secret"):
                raise ValueError("切换机器人必须填写新 AppSecret")
            if qq.get("secret") == "":
                qq.pop("secret")
            if qq.get("secret") == "__CLEAR__":
                qq["secret"] = ""
            data["qq"].update(qq)
        if "ai" in patch:
            ai = patch.pop("ai")
            ai.pop("api_key_configured", None)
            if ai.get("api_key") == "":
                ai.pop("api_key")
            if ai.get("api_key") == "__CLEAR__":
                ai["api_key"] = ""
            data["ai"].update(ai)
        if "channels" in patch:
            old = {c["kind"]: c for c in data["channels"]}
            for channel in patch["channels"]:
                for key in SECRETS:
                    channel.pop(key + "_configured", None)
                    if channel.get(key, "") == "":
                        channel[key] = old.get(channel.get("kind"), {}).get(key, "")
                    if channel[key] == "__CLEAR__":
                        channel[key] = ""
        data.update(patch)
        return Settings.model_validate(data)

    def save(self, settings: Settings):
        doc = copy.deepcopy(self.doc)

        def merge(target, values):
            for key, value in values.items():
                if (
                    isinstance(value, dict)
                    and key in target
                    and hasattr(target[key], "items")
                ):
                    merge(target[key], value)
                else:
                    target[key] = value

        merge(doc, settings.model_dump())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(tomlkit.dumps(doc))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)
        self.doc, self.settings = doc, settings
