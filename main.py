"""main.py —— 统一格式转换器 程序入口

职责：
* 初始化日志（文件 + 控制台 + 界面三路输出）
* 创建 QApplication、加载主题、创建主窗口
* 启动时做一次依赖检测（FFmpeg / LibreOffice）
* 捕获未处理异常，避免界面直接崩溃

运行：python main.py
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

# 保证以「脚本所在目录」为基准导入本地模块（打包成 exe 后同样成立）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import IS_MACOS, IS_WINDOWS, detect_environment, display_log_path  # noqa: E402

LOG_FORMAT = "%(asctime)s  %(levelname)-7s [%(name)s] %(message)s"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def configure_logging(level: int = logging.INFO) -> Path:
    """配置根日志：滚动文件 + 控制台（界面日志由 GUI 自行挂接）。"""
    from utils import log_dir

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):  # 避免重复初始化（例如在 IDE 里二次运行）
        root.removeHandler(handler)

    log_file = log_dir() / "converter.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    # 打包成 --windowed 的 exe 时没有控制台，此时 stdout 可能为 None
    if sys.stdout is not None and sys.stderr is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(console)

    logging.getLogger("PySide6").setLevel(logging.WARNING)
    logging.info("日志文件：%s", display_log_path(log_file))
    return log_file


# ---------------------------------------------------------------------------
# 未处理异常
# ---------------------------------------------------------------------------
def install_exception_hook(app) -> None:
    """把未捕获异常写进日志，并用对话框提示用户，而不是让程序静默退出。"""

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.critical("未处理的异常：\n%s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None, "程序发生错误",
                f"发生了未预期的错误：\n\n{exc_type.__name__}: {exc_value}\n\n"
                "详细信息已写入日志文件（帮助 → 环境检测 中可查看路径）。",
            )
        except Exception:  # noqa: BLE001 - 弹窗失败时不再抛出
            pass

    sys.excepthook = hook


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
def choose_font() -> str:
    """各平台常见的中文界面字体，缺失时回退到系统默认。"""
    if IS_WINDOWS:
        return "Microsoft YaHei UI"
    if IS_MACOS:
        return "PingFang SC"
    return "Noto Sans CJK SC"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    log_file = configure_logging()

    # Qt 必须在创建 QApplication 之前设置高 DPI 相关策略
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("UnifiedConverter")
    app.setApplicationDisplayName("统一格式转换器")
    app.setOrganizationName("UnifiedConverter")
    app.setApplicationVersion("1.0.0")

    from gui import APP_NAME, MainWindow, apply_style

    apply_style(app)
    app.setFont(QFont(choose_font(), 10))

    install_exception_hook(app)

    # 启动时的依赖检测（缺失只提示，不影响程序运行）
    env = detect_environment()
    logging.info("启动检测：%s", env.summary())
    for tool in (env.ffmpeg, env.soffice):
        if not tool.ok:
            logging.warning("%s 不可用：%s", tool.label, tool.hint)

    window = MainWindow(env)
    window.show()

    logging.info("%s 界面已就绪（日志：%s）", APP_NAME, log_file)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
