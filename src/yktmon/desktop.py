"""Console-free desktop entry. Reuses an identified service or starts one with page leases."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

import httpx
from .lifecycle import workspace_identity
from .web import DataLock


class LaunchError(RuntimeError):
    pass


def probe(client, expected):
    try:
        response = client.get("/api/app/status")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None
    except httpx.HTTPError:
        raise LaunchError("目标端口上的服务响应超时，请稍后重试。") from None
    if response.status_code != 200:
        raise LaunchError(
            "端口已有旧版或其他服务。请先正常退出它，再双击快捷方式；不会自动结束未知进程。"
        )
    try:
        status = response.json()
    except ValueError:
        raise LaunchError("端口返回的不是本程序状态，未启动重复服务。") from None
    if (
        not isinstance(status, dict)
        or status.get("service") != "yktmon"
        or status.get("lifecycle_version") != 1
    ):
        raise LaunchError("端口被其他应用占用，未启动重复服务。")
    if status.get("workspace_id") != expected:
        raise LaunchError(
            "端口上运行的是另一套配置或数据目录，请使用对应入口或更换端口。"
        )
    return status


def spawn_server(config: Path, data: Path, port: int, *, paused=False):
    python = Path(sys.executable)
    if os.name == "nt":
        candidate = python.with_name("pythonw.exe")
        if candidate.exists():
            python = candidate
    args = [
        str(python),
        "-m",
        "yktmon",
        "serve",
        "--desktop",
        "--config",
        str(config),
        "--data-dir",
        str(data),
        "--port",
        str(port),
    ]
    if paused:
        args.append("--paused")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    # Source checkout and installed wheel both work, without relying on editable metadata.
    package_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = package_parent + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (data / "desktop-startup.log").open("a", encoding="utf-8") as output:
        return subprocess.Popen(
            args,
            cwd=str(config.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            creationflags=flags,
        )


def launch(
    config: Path,
    data: Path,
    port=8787,
    *,
    opener=webbrowser.open_new_tab,
    spawn=spawn_server,
    timeout=45.0,
    paused=False,
):
    config = config.resolve()
    data = data.resolve()
    if not 1 <= port <= 65535:
        raise LaunchError("端口必须在 1–65535 之间。")
    data.mkdir(parents=True, exist_ok=True)
    if not config.parent.is_dir():
        raise LaunchError("配置所在文件夹不存在。")
    url = f"http://127.0.0.1:{port}"
    expected = workspace_identity(config, data)
    lock = DataLock(data / "desktop-launcher.lock")
    deadline = time.monotonic() + timeout
    # Serializes double-clicks while the first service is still starting.
    while True:
        try:
            lock.acquire()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise LaunchError("另一个启动器仍在工作，请稍后再试。") from None
            time.sleep(0.15)
    try:
        child = None
        with httpx.Client(
            base_url=url, timeout=2, trust_env=False, follow_redirects=False
        ) as client:
            while time.monotonic() < deadline:
                status = probe(client, expected)
                if status and status.get("shutting_down"):
                    time.sleep(0.2)
                    continue
                if status and status.get("ready"):
                    response = client.post(
                        "/api/app/open", headers={"X-Yktmon": "local"}
                    )
                    if response.status_code == 409:
                        time.sleep(0.2)
                        continue
                    response.raise_for_status()
                    if opener(url + "/#courses") is False:
                        raise LaunchError(
                            "服务已启动，但默认浏览器打开失败。请手动打开 " + url
                        )
                    return {
                        "url": url,
                        "started": child is not None,
                        "mode": status["mode"],
                    }
                if status is None and child is None:
                    child = spawn(config, data, port, paused=paused)
                if child is not None and child.poll() is not None:
                    raise LaunchError(
                        "后台服务启动失败。请查看数据目录中的 desktop-startup.log。"
                    )
                time.sleep(0.2)
        raise LaunchError(
            "服务未在 45 秒内就绪。请查看 desktop-startup.log；不要反复启动新进程。"
        )
    finally:
        lock.release()


def show_error(message):
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, "雨课堂习题看板 · 启动失败", 0x10
        )
    elif sys.stderr:
        print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="雨课堂桌面入口（无终端窗口）")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--paused", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = launch(args.config, args.data_dir, args.port, paused=args.paused)
        # This log has no credentials or database contents.
        with (args.data_dir / "desktop-launcher.log").open("a", encoding="utf-8") as f:
            f.write(
                time.strftime("%Y-%m-%d %H:%M:%S ")
                + json.dumps(result, ensure_ascii=False)
                + "\n"
            )
    except Exception as exc:
        detail = (
            str(exc)
            if isinstance(exc, LaunchError)
            else "启动遇到异常（" + type(exc).__name__ + "），请检查路径、依赖和端口。"
        )
        show_error(detail)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
