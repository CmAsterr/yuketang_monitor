from __future__ import annotations
import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
import uvicorn
from .config import ConfigFile, Settings
from .store import Store
from .web import DataLock, create_app
from .lifecycle import AppLifecycle


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(
        description="雨课堂习题实时看板（网页，不是桌面弹窗）"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "init", "migrate-courses"],
        default="serve",
    )
    parser.add_argument("--config", type=Path, default=Path.cwd() / "config.toml")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--desktop", action="store_true", help="桌面模式：最后一个看板关闭 3 分钟后退出"
    )
    parser.add_argument(
        "--desktop-grace", type=float, default=180, help=argparse.SUPPRESS
    )
    parser.add_argument("--paused", action="store_true", help="启动看板但暂停监控")
    parser.add_argument(
        "--no-browser", action="store_true", help="兼容旧命令；新版始终只输出看板地址"
    )
    parser.add_argument("--legacy-db", type=Path)
    args = parser.parse_args()
    config = args.config.resolve()
    data = (args.data_dir or config.parent / "data").resolve()
    if args.command == "init":
        if config.exists():
            parser.error("配置文件已存在，不覆盖")
        ConfigFile(config).save(Settings())
        print(f"配置已创建：{config}", flush=True)
        return
    if args.command == "migrate-courses":
        if not args.legacy_db or not args.legacy_db.is_file():
            parser.error("请提供 --legacy-db 数据库快照")
        lock = DataLock(data / "service.lock")
        lock.acquire()
        try:
            store = Store(data / "monitor.db")
            try:
                print(
                    f"已迁入 {store.migrate_courses(args.legacy_db)} 门课程（历史原库保留）",
                    flush=True,
                )
            finally:
                store.close()
        finally:
            lock.release()
        return
    if not 1 <= args.port <= 65535:
        parser.error("port 应在 1~65535 之间")
    if not 1 <= args.desktop_grace <= 3600:
        parser.error("desktop-grace 应在 1~3600 秒之间")
    data.mkdir(parents=True, exist_ok=True)
    # pythonw has no attached console. Give libraries real streams and retain startup errors.
    if sys.stdout is None:
        sys.stdout = (data / "desktop-console.log").open(
            "a", encoding="utf-8", buffering=1
        )
    if sys.stderr is None:
        sys.stderr = sys.stdout
    handler = RotatingFileHandler(
        data / "yktmon.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    print(
        f"雨课堂监控 v5 · 这是网页看板，请在浏览器打开 http://127.0.0.1:{args.port}\n配置：{config}\n数据：{data}\n按 Ctrl+C 停止服务。",
        flush=True,
    )
    lifecycle = AppLifecycle(
        config, data, desktop=args.desktop, grace_seconds=args.desktop_grace
    )
    app = create_app(config, data, autostart=not args.paused, lifecycle=lifecycle)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=5,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
    )

    def request_exit():
        server.should_exit = True

    lifecycle.on_exit = request_exit
    print(
        "运行方式："
        + (
            "桌面模式；最后一个页面关闭后 3 分钟退出。"
            if args.desktop
            else "长期后台模式；关闭网页不退出。"
        ),
        flush=True,
    )
    server.run()


if __name__ == "__main__":
    main()
