"""Production QQ UI QA against a temporary app with a simulated QQ transport only."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect
from yktmon.web import create_app

ROOT = Path(__file__).resolve().parents[1]


class FakeQQ:
    def __init__(self, app_id, secret, event):
        self.app_id = app_id
        self.event = event
        self.state = "已连接"
        self.error = ""
        self.name = "本地模拟 QQ"
        self.last_refresh = time.time()
        self.last_heartbeat = time.time()
        self.last_reconnect = 0

    def start(self):
        pass

    async def close(self):
        self.state = "已断开"

    async def send(self, group, payload, root):
        assert group == "qa-group"
        await asyncio.sleep(0.02)
        return "mock-" + str(time.time_ns())


def serve(config, data, port):
    app = create_app(config, data, autostart=False, qq_autostart=False)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def life(app):
        async with original(app):
            q = app.state.runtime.qq
            q.client_factory = FakeQQ
            q.check_pilot = False
            await q.start()
            if not app.state.runtime.store.courses("changjiang.yuketang.cn"):
                app.state.runtime.store.course(
                    "changjiang.yuketang.cn", "qa", "QQ 模拟课程"
                )
            yield

    app.router.lifespan_context = life

    @app.post("/qa/binding-event")
    async def event():
        q = app.state.runtime.qq
        q.event(
            {
                "t": "GROUP_AT_MESSAGE_CREATE",
                "d": {
                    "content": "绑定 " + q.binding["code"],
                    "group_openid": "qa-group",
                },
            }
        )
        return {"ok": True}

    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


def main():
    qa = ROOT / "qa"
    qa.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ykt-qq-browser-") as td:
        tmp = Path(td)
        cfg = tmp / "config.toml"
        data = tmp / "data"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        log = (qa / "qq-browser-server.log").open("w", encoding="utf-8")
        proc = None

        def start():
            p = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--config",
                    str(cfg),
                    "--data",
                    str(data),
                    "--port",
                    str(port),
                ],
                stdout=log,
                stderr=log,
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
            )
            for _ in range(100):
                try:
                    if (
                        httpx.get(url + "/api/qq/status", timeout=0.2).status_code
                        == 200
                    ):
                        return p
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            p.terminate()
            p.wait()
            raise RuntimeError("QA server startup failed")

        def stop(p):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

        checks = []
        try:
            proc = start()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(url + "/#settings")
                expect(page.locator("#view-settings")).to_be_visible()
                # On a direct hash load, the settings view may be visible but the parent tab state is still settling.
                page.locator("#view-settings").scroll_into_view_if_needed()
                outer = page.locator("#notificationSettings")
                if outer.get_attribute("open") is None:
                    outer.locator(":scope > summary").click()
                qq_details = page.locator("#qqSettings")
                if qq_details.get_attribute("open") is None:
                    qq_details.locator(":scope > summary").click()
                page.locator("#qqAppId").fill("123456")
                page.locator("#qqSecret").fill("mock-secret-never-real")
                page.locator("#qqEnabled").check()
                page.locator("#qqSave").click()
                expect(page.locator("#qqState")).to_contain_text("已连接")
                assert (
                    page.locator("#qqSecret").input_value() == "mock-secret-never-real"
                )
                assert (
                    "mock-secret-never-real"
                    not in httpx.get(url + "/api/qq/status").text
                )
                checks += [
                    "QQ config independent save",
                    "secret redaction",
                    "connected state",
                ]
                page.locator("#qqSettings").scroll_into_view_if_needed()
                if not page.locator("#qqSettings").evaluate("e => e.open"):
                    page.locator("#qqSettings > summary").click()
                groups_panel = page.locator("#qqGroupsPanel")
                if groups_panel.get_attribute("open") is None:
                    groups_panel.locator(":scope > summary").click()
                page.locator("#qqNewBinding").click()
                expect(page.locator("#qqBindCommand")).to_contain_text("绑定 ")
                httpx.post(
                    url + "/qa/binding-event", headers={"X-Yktmon": "local"}
                ).raise_for_status()
                expect(page.locator("#qqConfirm")).to_be_enabled(timeout=8000)
                page.locator("#qqGroupName").fill("本地模拟测试群")
                page.locator("#qqConfirm").click()
                expect(page.locator("#qqGroups")).to_contain_text("本地模拟测试群")
                expect(page.locator("#qqBinding")).not_to_be_visible()
                checks.append("binding code candidate confirm and alias")
                page.locator("[data-view=courses]").click()
                page.get_by_role(
                    "button", name="通知群：QQ 模拟课程", exact=True
                ).click()
                page.locator("#qqTargetChoices input").check()
                page.get_by_role("button", name="保存通知群", exact=True).click()
                expect(page.locator("#qqTargetsDialog")).not_to_be_visible()
                course = httpx.get(url + "/api/courses").json()[0]
                assert (
                    len(
                        httpx.get(
                            url + f"/api/courses/{course['id']}/qq-targets"
                        ).json()["group_ids"]
                    )
                    == 1
                )
                checks.append("course target selection persistence")
                page.locator("#settingsToggle").click()
                page.locator(".qq-test>summary").click()
                page.locator("#qqTestKind").select_option("markdown")
                page.locator("#qqTestSend").click()
                expect(page.locator("#qqConfirmDescription")).to_contain_text(
                    "本地模拟测试群"
                )
                page.locator("#qqConfirmCancel").click()
                assert httpx.get(url + "/api/qq/status").json()["history"] == []
                checks.append("test cancellation sends nothing")
                for kind in ["text", "image", "markdown"]:
                    page.locator("#qqTestKind").select_option(kind)
                    page.locator("#qqTestSend").click()
                    page.locator("#qqConfirmAction").click()
                    expect(page.locator("#qqConfirmDialog")).not_to_be_visible()
                expect(page.locator("#qqHistory")).to_contain_text(
                    "QQ 已返回消息 ID", timeout=8000
                )
                for _ in range(40):
                    h = httpx.get(url + "/api/qq/status").json()["history"]
                    if len(h) == 3 and all(x["state"] == "sent" for x in h):
                        break
                    time.sleep(0.1)
                assert len(h) == 3 and all(x["state"] == "sent" for x in h)
                checks.append(
                    "one target text image markdown tests with persistent delivery IDs"
                )
                page.locator("#qqDisconnect").click()
                expect(page.locator("#qqState")).to_contain_text("已断开")
                assert httpx.get(url + "/api/qq/status").json()["groups"]
                checks.append("disconnect preserves group and config")
                page.locator("#qqConnect").click()
                expect(page.locator("#qqState")).to_contain_text("已连接")
                page.screenshot(
                    path=str(qa / "qq-settings-desktop.png"), full_page=True
                )
                # Restart same temporary config/data, no binding again and no resends.
                stop(proc)
                proc = None
                proc = start()
                page.reload()
                page.locator("#notificationSettings>summary").click()
                expect(page.locator("#qqState")).to_contain_text("已连接")
                expect(page.locator("#qqGroups")).to_contain_text("本地模拟测试群")
                assert len(httpx.get(url + "/api/qq/status").json()["history"]) == 3
                assert httpx.get(
                    url + f"/api/courses/{course['id']}/qq-targets"
                ).json()["group_ids"]
                checks += [
                    "restart automatic connect",
                    "restart binding mapping and history preserved no resend",
                ]
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.screenshot(path=str(qa / "qq-settings-mobile.png"), full_page=True)
                checks.append("mobile no horizontal overflow")
                assert errors == []
                browser.close()
            (qa / "qq-browser-result.json").write_text(
                json.dumps(
                    {"passed": True, "checks": checks, "page_errors": errors},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"QQ production UI: {len(checks)} checks passed, 0 page errors; simulated QQ only"
            )
        finally:
            if proc:
                stop(proc)
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        serve(args.config, args.data, args.port)
    else:
        main()
