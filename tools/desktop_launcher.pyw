"""Shortcut bootstrap; relocatable with the checkout and silent under pythonw."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
try:
    from yktmon.desktop import main
except Exception as exc:
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None,
        "启动依赖未就绪（"
        + type(exc).__name__
        + "）。\n请在项目目录执行：\npython -m pip install -e .\n然后重新创建桌面快捷方式。",
        "雨课堂习题看板 · 启动失败",
        0x10,
    )
else:
    raise SystemExit(main())
