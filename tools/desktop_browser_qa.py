"""Desktop lifecycle QA. Real local browser and subprocess; temporary config/DB only."""

from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright, expect
from yktmon.desktop import launch, spawn_server

ROOT = Path(__file__).resolve().parents[1]


def main():
    checks = []
    errors = []
    children = []
    qa = ROOT / "qa"
    qa.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yktmon-desktop-") as temp:
        tmp = Path(temp)
        config = tmp / "config.toml"
        data = tmp / "data"
        data.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        opened = []

        def spawn(config, data, port, *, paused=False, persistent=False):
            args = [
                sys.executable,
                "-m",
                "yktmon",
                "serve",
                "--config",
                str(config),
                "--data-dir",
                str(data),
                "--port",
                str(port),
                "--paused",
            ]
            if not persistent:
                args += ["--desktop", "--desktop-grace", "4"]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with (qa / "desktop-server.log").open("a", encoding="utf-8") as log:
                p = subprocess.Popen(
                    args,
                    stdout=log,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    creationflags=flags,
                    env=dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1"),
                )
            children.append(p)
            return p

        def status():
            return httpx.get(url + "/api/app/status", timeout=2, trust_env=False).json()

        def wait_count(n):
            until = time.monotonic() + 4
            while time.monotonic() < until:
                if status()["page_count"] == n:
                    return
                time.sleep(0.05)
            raise AssertionError("Unexpected page count")

        def ready():
            until = time.monotonic() + 10
            while time.monotonic() < until:
                try:
                    if status()["ready"]:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise AssertionError("Not ready")

        try:
            result = launch(
                config,
                data,
                port,
                opener=lambda url: opened.append(url),
                spawn=spawn,
                paused=True,
            )
            assert result["started"] and result["mode"] == "desktop"
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 960})
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(opened[-1])
                expect(page.locator("#appLifecycle")).to_contain_text("桌面模式")
                expect(page.locator("#exitApplication")).to_be_enabled()
                wait_count(1)
                first = status()["instance_id"]
                duplicate = launch(
                    config,
                    data,
                    port,
                    opener=lambda url: opened.append(url),
                    spawn=spawn,
                    paused=True,
                )
                assert (
                    not duplicate["started"]
                    and len(children) == 1
                    and status()["instance_id"] == first
                )
                checks += [
                    "new launch waits for ready",
                    "double launch reuses same process",
                ]
                tab2 = context.new_page()
                tab2.on("pageerror", lambda e: errors.append(str(e)))
                tab2.goto(opened[-1])
                wait_count(2)
                page.reload()
                expect(page.locator("#appLifecycle")).to_contain_text("桌面模式")
                wait_count(2)
                # Longer than the accelerated grace. Neither a background tab nor reload exits.
                time.sleep(4.5)
                assert children[0].poll() is None
                checks += [
                    "multiple tabs tracked independently",
                    "reload retains service",
                    "background page remains alive",
                ]
                tab2.close()
                wait_count(1)
                time.sleep(4.5)
                assert children[0].poll() is None
                checks.append("closing one tab does not stop another")
                page.close()
                wait_count(0)
                time.sleep(0.5)
                reopened = context.new_page()
                reopened.on("pageerror", lambda e: errors.append(str(e)))
                reopened.goto(url)
                wait_count(1)
                time.sleep(4.5)
                assert children[0].poll() is None and status()["instance_id"] == first
                checks.append("reopen within grace cancels idle shutdown")
                reopened.close()
                children[0].wait(timeout=12)
                assert children[0].returncode == 0
                checks.append("last page closes service gracefully after grace")
                launch(
                    config,
                    data,
                    port,
                    opener=lambda url: opened.append(url),
                    spawn=spawn,
                    paused=True,
                )
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(url)
                wait_count(1)
                assert status()["instance_id"] != first
                page.locator("#exitApplication").click()
                page.locator("#cancelExitApplication").click()
                assert children[-1].poll() is None
                page.locator("#exitApplication").click()
                page.locator("#confirmExitApplication").click()
                expect(page.locator("#appLifecycle")).to_contain_text("可以关闭此页面")
                children[-1].wait(timeout=12)
                assert children[-1].returncode == 0
                page.screenshot(path=str(qa / "desktop-exit.png"))
                page.close()
                checks += [
                    "relaunch after full exit",
                    "exit cancel preserves service",
                    "explicit exit stops while page remains open",
                ]
                persistent = spawn(config, data, port, persistent=True)
                ready()
                result = launch(
                    config,
                    data,
                    port,
                    opener=lambda url: opened.append(url),
                    spawn=spawn,
                    paused=True,
                )
                assert result["mode"] == "persistent" and not result["started"]
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(url)
                expect(page.locator("#appLifecycle")).to_contain_text("长期后台模式")
                page.close()
                wait_count(0)
                time.sleep(4.5)
                assert persistent.poll() is None
                response = httpx.post(
                    url + "/api/app/exit",
                    headers={"X-Yktmon": "local"},
                    json={"instance_id": status()["instance_id"]},
                    trust_env=False,
                )
                response.raise_for_status()
                persistent.wait(timeout=12)
                assert persistent.returncode == 0
                checks.append(
                    "persistent CLI mode unaffected by page closure or launcher reuse"
                )

                def spawn_hidden(config, data, port, *, paused=False):
                    child = spawn_server(config, data, port, paused=paused)
                    children.append(child)
                    return child

                launch(
                    config,
                    data,
                    port,
                    opener=lambda url: opened.append(url),
                    spawn=spawn_hidden,
                    paused=True,
                )
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(url)
                wait_count(1)
                assert (
                    status()["mode"] == "desktop" and status()["grace_seconds"] == 180
                )
                response = httpx.post(
                    url + "/api/app/exit",
                    headers={"X-Yktmon": "local"},
                    json={"instance_id": status()["instance_id"]},
                    trust_env=False,
                )
                response.raise_for_status()
                children[-1].wait(timeout=12)
                assert children[-1].returncode == 0
                page.close()
                checks.append(
                    "actual hidden pythonw launcher uses production 180-second grace"
                )
                assert errors == [], errors
                browser.close()
            import sqlite3

            db = sqlite3.connect(data / "monitor.db")
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            db.close()
            checks.append("database remains valid after all shutdown paths")
            (qa / "desktop-result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "grace_seconds_in_test": 4,
                        "production_grace_seconds": 180,
                        "checks": checks,
                        "page_errors": errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"Desktop QA: {len(checks)} checks passed, 0 page errors; only temporary local service used"
            )
        finally:
            for p in children:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()


if __name__ == "__main__":
    main()
