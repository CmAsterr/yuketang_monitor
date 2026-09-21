"""Local-only interaction QA with temporary accounts, config, images and DB."""

from __future__ import annotations
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright, expect
from yktmon.config import ConfigFile, Settings
from yktmon.store import Store

ROOT = Path(__file__).resolve().parents[1]


def ensure_course_open(page):
    detail = page.locator("#courses details.course-block").first
    if detail.get_attribute("open") is None:
        detail.locator("summary").click()


def sample_image(path):
    # A synthetic problem fixture, never user course data.
    im = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(im)
    fonts = [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/arial.ttf")]
    font_path = next((p for p in fonts if p.is_file()), None)
    font = ImageFont.truetype(str(font_path), 30) if font_path else ImageFont.load_default(size=30)
    small = ImageFont.truetype(str(font_path), 20) if font_path else ImageFont.load_default(size=20)
    draw.rectangle((0, 0, 1000, 60), fill="#f4f6f8")
    draw.text((30, 16), "单选题 · 本地模拟题", font=small, fill="#4b6158")
    draw.text((42, 95), "以下哪一项描述了栈的特点？", font=font, fill="#263d34")
    for i, line in enumerate(
        ["A. 先进先出", "B. 后进先出", "C. 随机访问", "D. 按优先级访问"]
    ):
        draw.text((48, 185 + i * 66), line, font=font, fill="#35483f")
    im.save(path)


def main():
    qa = ROOT / "qa"
    qa.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yktmon-browser-") as temp:
        tmp = Path(temp)
        cfg = tmp / "config.toml"
        ConfigFile(cfg).save(Settings())
        store = Store(tmp / "data" / "monitor.db")
        course = store.course("changjiang.yuketang.cn", "example", "示例 · 数据结构")
        record = store.create_record(
            "changjiang.yuketang.cn", course["id"], "第 1 次课 · 栈与队列"
        )
        store.bind_record("changjiang.yuketang.cn", "test", record["id"])
        store.create_record(
            "changjiang.yuketang.cn", course["id"], "第 2 次课 · 待上课"
        )
        p = {
            "problem_id": "mock-1",
            "slide_id": "s",
            "presentation": "ppt",
            "course": "示例 · 数据结构",
            "type": 1,
            "body": "以下哪一项描述了栈的特点？",
            "options": ["", "", "", ""],
            "cover": "",
            "limit": -1,
            "unlocked": time.time(),
        }
        malicious = dict(
            p, problem_id="mock-2", body='<img src=x onerror="window.injected=true">'
        )
        id2, _ = store.insert("changjiang.yuketang.cn", "test", malicious)
        store.update(id2, status="failed", error="示例：图片下载失败，支持重新作答")
        id, _ = store.insert("changjiang.yuketang.cn", "test", p)
        images = tmp / "data" / "images"
        images.mkdir(exist_ok=True)
        sample_image(images / "mock.png")
        malformed = '{"answer":["B"],"reasoning":"**后进先出（LIFO）** 是栈的特点。\\n\\n- 入栈和出栈都发生在栈顶。\\n- 最后入栈的元素最先出栈。"，"confidence":0.95}'
        store.update(
            id,
            status="done",
            image="mock.png",
            answer={
                "answer": [malformed],
                "structured": False,
                "model": "模拟模型 · 本地测试",
                "elapsed": 1.2,
            },
        )
        store.close()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        out = (qa / "browser-server.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "yktmon",
                "serve",
                "--config",
                str(cfg),
                "--data-dir",
                str(tmp / "data"),
                "--port",
                str(port),
                "--paused",
            ],
            stdout=out,
            stderr=out,
            env=dict(os.environ, PYTHONUNBUFFERED="1"),
        )
        checks = []
        try:
            for _ in range(100):
                try:
                    if httpx.get(url + "/api/status", timeout=0.2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("QA server did not start")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1100},
                    device_scale_factor=1,
                    accept_downloads=True,
                )
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                # Only this temporary page gets a mock model endpoint; never real credentials.
                model_calls = []

                def mock_models(route):
                    model_calls.append(route.request.post_data_json)
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {"models": ["vision-a", "vision-b"], "message": "模拟列表"}
                        ),
                    )

                page.route("**/api/ai/models", mock_models)
                page.goto(url)
                expect(page.locator("#connection")).to_have_text("看板实时连接正常")
                page.locator("#courses details.course-block>summary").first.click()
                page.locator("#courses").get_by_role(
                    "button", name="第 1 次课 · 栈与队列", exact=True
                ).wait_for()
                assert page.locator("#view-courses").is_visible()
                assert not page.locator("#view-problems").is_visible()
                assert page.locator("[data-view]").count() == 2
                assert page.locator("#login").is_hidden()
                checks += [
                    "only course and settings pinned pages",
                    "login moved into settings",
                ]
                page.locator("#courses").get_by_role(
                    "button", name="第 1 次课 · 栈与队列", exact=True
                ).click()
                page.get_by_text("本页 2 道题").wait_for()
                assert page.locator("article.problem").count() == 2
                assert (
                    page.get_by_role("button", name="重新作答", exact=True).count() == 0
                )
                assert page.evaluate("window.injected") is None
                assert page.locator(".options").count() == 0
                assert (
                    page.locator(".answer-markdown").first.inner_text().strip() == "B"
                )
                assert (
                    page.locator(".reason-markdown strong").inner_text()
                    == "后进先出（LIFO）"
                )
                assert page.locator(".reason-markdown li").count() == 2
                assert "95%" in page.locator(".confidence").first.inner_text()
                checks += [
                    "read-only archived record",
                    "Markdown and repaired answer",
                    "no duplicate blank choices",
                    "untrusted content escaped",
                ]
                page.get_by_role("button", name="查看原题大图").click()
                expect(page.locator("#imageDialog")).to_be_visible()
                page.locator("#closeImage").click()
                with page.expect_download() as dl:
                    page.get_by_role("link", name="下载原图").click()
                assert dl.value.suggested_filename == "mock.png"
                checks.append("image preview and download")
                page.get_by_role("button", name="群消息预览").first.click()
                page.locator("#notificationDialog").wait_for(state="visible")
                assert (
                    "答案" in page.locator("#previewResult").inner_text()
                    and "B" in page.locator("#previewResult").inner_text()
                )
                expect(page.locator("#previewReminder h1")).to_have_text("新题提醒！")
                expect(page.locator("#previewReminder em")).to_have_text(
                    "大肥鱼正在做题喵～"
                )
                expect(page.locator("#previewReminder p").last).to_have_text(
                    "题型：单选（大肥鱼正在做题喵～）"
                )
                caption = page.locator("#previewImageCaption").inner_text()
                assert caption.endswith("题目 2") and "原题图片" not in caption
                expect(page.locator("#previewReminder strong").first).to_have_text(
                    caption
                )
                expect(page.locator("#previewResult strong").first).to_have_text(
                    caption
                )
                caption_styles = [
                    page.locator(selector).first.evaluate(
                        "e => {const s=getComputedStyle(e);return [s.fontFamily,s.fontSize,s.fontWeight,s.lineHeight,s.color,s.letterSpacing]}"
                    )
                    for selector in [
                        "#previewReminder .preview-question-caption",
                        "#previewImageCaption",
                        "#previewResult .preview-question-caption",
                    ]
                ]
                assert caption_styles[0] == caption_styles[1] == caption_styles[2]
                assert page.locator("#previewResult h1").count() == 0
                page.screenshot(path=str(qa / "qq-message-preview.png"))
                page.locator("#closeNotification").click()
                checks.append("notification preview read only")
                checks.append(
                    "three QQ message templates share matching caption and formatting"
                )
                page.get_by_role("button", name="修改标题", exact=True).click()
                page.locator("#renameInput").fill("栈的课堂笔记")
                page.get_by_role("button", name="保存名称", exact=True).click()
                expect(page.locator("#problemsTitle")).to_have_text("栈的课堂笔记")
                page.locator("[data-view=courses]").click()
                ensure_course_open(page)
                page.locator(
                    "#courses .record-link", has_text="第 2 次课 · 待上课"
                ).click()
                expect(page.locator("#problemsTitle")).to_have_text(
                    "第 2 次课 · 待上课"
                )
                assert page.locator(".class-tab").count() == 2
                expect(page.get_by_text("本节还没有习题", exact=True)).to_be_visible()
                page.get_by_role(
                    "button", name="关闭标签：第 2 次课 · 待上课", exact=True
                ).click()
                assert page.locator("#view-courses").is_visible()
                assert page.locator(".class-tab").count() == 1
                ensure_course_open(page)
                page.locator(
                    "#courses .record-link", has_text="第 2 次课 · 待上课"
                ).click()
                # No login session in this test: starting a record cannot call classroom APIs.
                page.get_by_role("button", name="开始监听", exact=True).click()
                page.get_by_role("button", name="暂停监听", exact=True).wait_for()
                page.get_by_role(
                    "button", name="关闭标签：第 2 次课 · 待上课", exact=True
                ).click()
                page.locator("#closeTabDialog").wait_for(state="visible")
                page.locator("#closeKeep").click()
                expect(page.locator("#view-courses")).to_be_visible()
                assert httpx.get(url + "/api/records").json()[0]["listening"] == 1
                ensure_course_open(page)
                page.locator(
                    "#courses .record-link", has_text="第 2 次课 · 待上课"
                ).click()
                page.get_by_role(
                    "button", name="关闭标签：第 2 次课 · 待上课", exact=True
                ).click()
                page.locator("#closePause").click()
                expect(page.locator("#closeTabDialog")).not_to_be_visible()
                assert httpx.get(url + "/api/records").json()[0]["listening"] == 0
                checks += [
                    "rename record",
                    "multiple independent tabs",
                    "close keeps record",
                    "close listening tab background or pause choice",
                ]
                page.get_by_role(
                    "button", name="修改课程名称：示例 · 数据结构", exact=True
                ).click()
                page.locator("#renameInput").fill("数据结构 · 自定义名称")
                page.get_by_role("button", name="保存名称", exact=True).click()
                page.get_by_role(
                    "button", name="修改课程名称：数据结构 · 自定义名称", exact=True
                ).wait_for()
                assert (
                    httpx.get(url + "/api/courses").json()[0]["name"]
                    == "示例 · 数据结构"
                )
                page.locator("#newTab").click()
                page.locator("#newRecordTitle").fill("第三次课")
                page.get_by_role("button", name="创建课堂", exact=True).click()
                expect(page.locator("#problemsTitle")).to_have_text("第三次课")
                assert not httpx.get(url + "/api/records").json()[0]["listening"]
                page.reload()
                expect(page.locator("#problemsTitle")).to_have_text("第三次课")
                assert page.locator(".class-tab").count() == 2
                checks += [
                    "course alias preserves original identity",
                    "new local record tab",
                    "tab restore on reload",
                ]
                page.locator("[data-view=courses]").click()
                page.locator("#addCourseToggle").click()
                page.locator("#courseName").fill("临时课程")
                page.locator("#classroomId").fill("temporary-id")
                page.locator("#courseForm button[type=submit]").click()
                page.get_by_text("课程已添加，可以新开一节课。").wait_for()
                page.get_by_role(
                    "button", name="删除课程：临时课程", exact=True
                ).click()
                page.locator("#cancelDelete").click()
                page.get_by_role(
                    "button", name="删除课程：临时课程", exact=True
                ).click()
                page.locator("#confirmDelete").click()
                page.get_by_text("课程规则已删除，历史习题和图片已保留。").wait_for()
                expect(
                    page.get_by_role("button", name="删除课程：临时课程", exact=True)
                ).to_have_count(0)
                checks.append("course add and delete preserves history")
                page.screenshot(path=str(qa / "courses-desktop.png"), full_page=True)
                page.locator("#settingsToggle").click()
                assert page.locator("#login").is_visible()
                page.locator("#scan").fill("45")
                page.get_by_role("button", name="返回课程", exact=True).first.click()
                page.locator("#settingsToggle").click()
                assert page.locator("#scan").input_value() == "45"
                page.get_by_role("button", name="保存并应用").click()
                page.get_by_text("配置已保存并应用。").wait_for()
                assert ConfigFile(cfg).settings.scan_interval == 45
                page.locator("#apiKey").fill("temporary-fake-key")
                page.locator("#toggleKey").click()
                assert page.locator("#apiKey").get_attribute("type") == "text"
                page.locator("#toggleKey").click()
                assert page.locator("#apiKey").get_attribute("type") == "password"
                expect(page.locator("#modelsState")).to_contain_text("已获取 2 个模型")
                assert page.locator("#modelSelect option").count() >= 3
                page.locator("#modelSelect").select_option("vision-b")
                assert page.locator("#model").input_value() == "vision-b"
                assert model_calls[-1]["api_key"] == "temporary-fake-key"
                assert (
                    page.locator("#saveState").evaluate(
                        "(e)=>getComputedStyle(e).color"
                    )
                    == "rgb(192, 54, 44)"
                )
                page.get_by_role("button", name="保存并应用").click()
                expect(page.locator("#saveState")).to_have_text("已保存")
                page.reload()
                page.locator("#settingsToggle").click()
                expect(page.locator("#apiKey")).to_have_value("temporary-fake-key")
                page.locator("#toggleKey").click()
                assert page.locator("#apiKey").get_attribute("type") == "text"
                page.locator("#toggleKey").click()
                checks += [
                    "saved API key restored and revealable",
                    "dirty state red",
                    "native model dropdown selection",
                ]
                # Clear dummy credential before diagnostics; never call external provider.
                page.locator("#apiKey").fill("__CLEAR__")
                page.get_by_role("button", name="保存并应用").click()
                expect(page.locator("#saveState")).to_have_text("已保存")
                page.locator("#testAI").click()
                page.get_by_role("heading", name="AI 自检未通过").wait_for()
                checks += [
                    "settings draft survives navigation",
                    "API key show hide",
                    "automatic searchable model list",
                    "no-key diagnostics",
                ]
                page.screenshot(path=str(qa / "settings-desktop.png"), full_page=True)
                page.locator("[data-view=courses]").click()
                ensure_course_open(page)
                page.locator("#courses .record-link", has_text="栈的课堂笔记").click()
                page.get_by_text("本页 2 道题").wait_for()
                page.locator("#problemSearch").fill("栈")
                assert page.locator("article.problem").count() == 1
                page.locator("#problemsTitle").click()
                page.screenshot(path=str(qa / "dashboard-desktop.png"), full_page=True)
                checks.append("record scoped search")
                page.route(
                    "**/api/config",
                    lambda route: route.fulfill(
                        status=200, content_type="application/json", body='{"ai":{}}'
                    ),
                )
                page.reload()
                expect(page.locator("#connection")).to_have_text("看板实时连接正常")
                page.get_by_text("本页 2 道题").wait_for()
                assert page.locator("#domain option").count() >= 1
                page.unroute("**/api/config")
                checks.append("missing config fields fallback")
                page.set_viewport_size({"width": 390, "height": 844})
                page.reload()
                page.get_by_text("本页 2 道题").wait_for()
                page.locator("#problemSearch").fill("栈")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(path=str(qa / "dashboard-mobile.png"), full_page=True)
                page.locator("[data-view=courses]").click()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.locator("#settingsToggle").click()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                checks.append("mobile tabs and all views no horizontal overflow")
                assert not errors, errors
                browser.close()
            (qa / "browser-result.json").write_text(
                json.dumps(
                    {"passed": True, "page_errors": errors, "checks": checks},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"Browser QA passed: {len(checks)} checks, 0 page errors")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            out.close()


if __name__ == "__main__":
    main()
