"""App-owned page leases and graceful shutdown coordination; no globals or I/O on import."""

from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("yktmon.lifecycle")


def workspace_identity(config_path: Path, data_dir: Path) -> str:
    parts = [os.path.normcase(str(p.resolve())) for p in (config_path, data_dir)]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


class AppLifecycle:
    def __init__(
        self,
        config_path: Path,
        data_dir: Path,
        *,
        desktop=False,
        grace_seconds=180.0,
        on_exit: Callable[[], None] | None = None,
        clock=time.monotonic,
    ):
        self.desktop = desktop
        self.grace_seconds = float(grace_seconds)
        if self.grace_seconds <= 0:
            raise ValueError("Grace period must be positive")
        self.identity = workspace_identity(config_path, data_dir)
        self.instance = secrets.token_urlsafe(18)
        self.clock = clock
        self.on_exit = on_exit
        self.connections: dict[str, str] = {}
        self.ever_connected = False
        self.empty_since = self.clock()
        self.last_tick = self.empty_since
        self.exit_requested = False
        self.exit_reason = ""
        self.ready = False

    def connect(self, page_id: str) -> str:
        if self.exit_requested:
            raise RuntimeError("Service is shutting down")
        token = secrets.token_urlsafe(18)
        self.connections[token] = page_id
        self.ever_connected = True
        self.empty_since = None
        return token

    def disconnect(self, token: str):
        removed = self.connections.pop(token, None)
        if removed is not None and not self.connections:
            self.empty_since = self.clock()

    def reserve_open(self):
        """Opening a new browser tab cancels a pending idle exit, never revives shutdown."""
        if self.exit_requested:
            return False
        if not self.connections:
            self.empty_since = self.clock()
        return True

    def request_exit(self, reason: str):
        if self.exit_requested:
            return
        self.exit_requested = True
        self.exit_reason = reason
        log.info("准备正常退出：%s", reason)
        if self.on_exit:
            self.on_exit()

    @property
    def idle_limit(self):
        # First browser startup needs time even when a test uses a shortened close grace.
        return (
            self.grace_seconds if self.ever_connected else max(30, self.grace_seconds)
        )

    def tick(self):
        now = self.clock()
        gap = now - self.last_tick
        self.last_tick = now
        # A suspended machine/event loop gets a fresh grace window on resume.
        if gap > 30 and not self.connections:
            self.empty_since = now
        if (
            self.desktop
            and self.ready
            and not self.connections
            and self.empty_since is not None
        ):
            if now - self.empty_since >= self.idle_limit:
                self.request_exit("最后一个看板页面已关闭，宽限期结束")

    async def watch(self):
        while not self.exit_requested:
            self.tick()
            await asyncio.sleep(0.5)

    def status(self):
        remaining = None
        if self.desktop and not self.connections and self.empty_since is not None:
            remaining = max(
                0, round(self.idle_limit - (self.clock() - self.empty_since))
            )
        return {
            "service": "yktmon",
            "lifecycle_version": 1,
            "workspace_id": self.identity,
            "instance_id": self.instance,
            "mode": "desktop" if self.desktop else "persistent",
            "grace_seconds": self.grace_seconds,
            "page_count": len(set(self.connections.values())),
            "shutdown_in_seconds": remaining,
            "shutting_down": self.exit_requested,
            "shutdown_reason": self.exit_reason,
            "ready": self.ready,
            "can_exit": self.on_exit is not None,
        }
