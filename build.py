"""build.py —— 一键打包脚本（PyInstaller 包装）

用法：
    python build.py                # 目录模式（onedir，推荐：启动快、便于放便携版 ffmpeg）
    python build.py --onefile      # 单文件模式（体积大、启动慢几秒）
    python build.py --icon app.ico # 指定图标（Windows .ico / macOS .icns）
    python build.py --console      # 保留控制台窗口（排查启动问题用）

说明：FFmpeg 与 LibreOffice 不会被打进包里，目标机器需要自行安装，
      或者把便携版放在 exe 同目录（bin/ffmpeg.exe、LibreOffice/program/soffice.exe）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "统一格式转换器"

#: 用不到的 Qt 模块，排除后可显著减小体积
EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtNetworkAuth",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtBluetooth", "PySide6.QtPositioning",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSpatialAudio",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtWebSockets",
    "tkinter", "matplotlib", "numpy", "PIL", "pytest",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 统一格式转换器")
    parser.add_argument("--onefile", action="store_true", help="打包成单个 exe")
    parser.add_argument("--icon", default=None, help="图标文件路径")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（便于排错）")
    parser.add_argument("--name", default=APP_NAME, help="输出名称")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先执行：pip install pyinstaller")
        print("国内可加：-i https://mirrors.aliyun.com/pypi/simple/")
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--console" if args.console else "--windowed",
        "--name", args.name,
        "--paths", str(ROOT),
    ]
    if args.icon:
        icon = Path(args.icon)
        if not icon.is_file():
            print(f"图标文件不存在：{icon}")
            return 1
        cmd += ["--icon", str(icon)]
    for module in EXCLUDES:
        cmd += ["--exclude-module", module]
    cmd.append("main.py")

    print("=" * 70)
    print("执行：", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("=" * 70)

    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n打包失败，请检查上方输出。")
        return result.returncode

    target = ROOT / "dist" / (f"{args.name}.exe" if args.onefile else args.name)
    print("\n打包完成：", target)
    print("提示：FFmpeg 与 LibreOffice 未包含在内，目标机器需自行安装，")
    print("      或把便携版放到 exe 同目录（bin/ffmpeg.exe、LibreOffice/program/soffice.exe）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
