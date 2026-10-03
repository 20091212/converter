"""selftest.py —— 自检脚本：验证 FFmpeg / LibreOffice 链路与核心逻辑是否真正可用

用法：``python selftest.py``（可加 ``--gui`` 额外做一次界面离屏冒烟测试）

做的事情：
1. 打印依赖检测结果（ffmpeg / ffprobe / soffice 的路径与版本）
2. 生成 1 秒正弦波 WAV，测试  wav → mp3（192k）、wav → flac（无损不带码率）
3. 生成一个 txt，测试 txt → pdf / docx（需要 LibreOffice）
4. 校验 plan_tasks 的「跳过」判定（类型不匹配、源=目标）
5. 可选：用 offscreen 平台实例化主窗口，确认界面能正常构建

全部通过时退出码为 0；任何一项失败都会打印 [FAIL] 并以 1 退出。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from converter import Converter, plan_tasks  # noqa: E402
from utils import (  # noqa: E402
    FileItem,
    detect_environment,
    human_size,
    soffice_filters,
    subprocess_kwargs,
    target_kind,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
    return condition


def _head_bytes(path: Path, size: int = 8) -> bytes:
    """只读取文件头部若干字节，用于校验文件签名（避免把大文件整个读进内存）。"""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def make_sine_wav(ffmpeg: str, path: Path, seconds: int = 1) -> bool:
    """用 FFmpeg 合成一段正弦波 WAV 作为测试素材。"""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", "44100", "-ac", "2", str(path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          timeout=60, **subprocess_kwargs())
    if proc.returncode != 0:
        print("      ffmpeg 生成测试音频失败：", proc.stderr.decode("utf-8", "replace")[:300])
        return False
    return path.is_file() and path.stat().st_size > 0


def main() -> int:
    parser = argparse.ArgumentParser(description="统一格式转换器 自检")
    parser.add_argument("--gui", action="store_true", help="额外执行界面离屏冒烟测试")
    args = parser.parse_args()

    print("=" * 72)
    print("统一格式转换器 —— 自检")
    print("=" * 72)

    env = detect_environment(force=True)
    print("\n[1] 依赖检测")
    for line in env.details():
        print("   " + line)

    work = Path(tempfile.mkdtemp(prefix="ucv-selftest-"))
    print(f"\n临时工作目录：{work}")

    converter = Converter(env, bitrate="192k", overwrite=True)

    # ---------------------------------------------------------------- 音频
    print("\n[2] 音频转换（FFmpeg）")
    if not env.audio_ok:
        check("音频链路", False, "未检测到 FFmpeg，跳过音频测试")
    else:
        source_wav = work / "tone.wav"
        if check("生成测试 WAV", make_sine_wav(env.ffmpeg.path, source_wav),
                 human_size(source_wav.stat().st_size) if source_wav.exists() else ""):
            duration = converter.audio.probe_duration(source_wav)
            check("ffprobe 读取时长", duration is not None and 0.5 < duration < 3,
                  f"{duration:.2f}s" if duration else "无")

            mp3 = work / "tone.mp3"
            try:
                out = converter.audio.convert(source_wav, mp3, bitrate="192k")
                check("wav → mp3 (192k)", out.is_file() and out.stat().st_size > 0,
                      f"产物 {human_size(out.stat().st_size)}")
                info = converter.audio.probe_summary(out)
                check("mp3 可被 ffprobe 识别", "mp3" in info, info[:60])
            except Exception as exc:  # noqa: BLE001
                check("wav → mp3 (192k)", False, str(exc)[:200])

            flac = work / "tone.flac"
            try:
                out = converter.audio.convert(source_wav, flac, bitrate=None)
                check("wav → flac（无损不回码率）", out.is_file() and out.stat().st_size > 0,
                      human_size(out.stat().st_size))
            except Exception as exc:  # noqa: BLE001
                check("wav → flac（无损不回码率）", False, str(exc)[:200])

            m4a = work / "tone.m4a"
            try:
                out = converter.audio.convert(source_wav, m4a, bitrate="128k")
                check("wav → m4a (128k)", out.is_file() and out.stat().st_size > 0,
                      human_size(out.stat().st_size))
            except Exception as exc:  # noqa: BLE001
                check("wav → m4a (128k)", False, str(exc)[:200])

            # 取消功能：置位取消事件后应立即抛出 CancelledError
            from converter import CancelledError

            cancel = threading.Event()
            cancel.set()
            try:
                converter.audio.convert(source_wav, work / "cancel.mp3", cancel=cancel)
                check("取消机制（音频）", False, "取消后仍然完成了转换")
            except CancelledError:
                check("取消机制（音频）", True, "已按请求中止")
            except Exception as exc:  # noqa: BLE001
                check("取消机制（音频）", False, f"抛出了非取消异常：{exc}")

    # ---------------------------------------------------------------- 办公
    print("\n[3] 办公文档转换（LibreOffice）")
    if not env.office_ok:
        check("办公链路", False, "未检测到 LibreOffice，跳过办公测试")
    else:
        note = work / "note.txt"
        note.write_text("统一格式转换器 自检\n第二行：中文与 ASCII mixed text.\n", encoding="utf-8")
        print(f"   过滤器示例：{soffice_filters('.pdf', '.txt')} / {soffice_filters('.csv', '.xlsx')}")

        pdf = work / "note.pdf"
        try:
            out = converter.office.convert(note, pdf, source_ext=".txt")
            ok = out.is_file() and _head_bytes(out).startswith(b"%PDF")
            check("txt → pdf", ok,
                  f"产物 {out.name} {human_size(out.stat().st_size)}" if ok else "未生成有效 PDF")
        except Exception as exc:  # noqa: BLE001
            check("txt → pdf", False, f"{type(exc).__name__}: {exc}"[:250])

        docx = work / "note.docx"
        try:
            out = converter.office.convert(note, docx, source_ext=".txt")
            ok = out.is_file() and _head_bytes(out, 2) == b"PK"
            check("txt → docx", ok,
                  f"产物 {out.name} {human_size(out.stat().st_size)}" if ok else "未生成有效 docx")
        except Exception as exc:  # noqa: BLE001
            check("txt → docx", False, f"{type(exc).__name__}: {exc}"[:250])

        # 表格族：csv → xlsx → pdf（验证 calc 过滤器与按族选择的 PDF 导出过滤器）
        csv_file = work / "table.csv"
        csv_file.write_text("名称,数量,单价\n沥青混凝土,120,480\n水泥稳定土,80,255\n", encoding="utf-8")
        xlsx = work / "table.xlsx"
        try:
            out = converter.office.convert(csv_file, xlsx, source_ext=".csv")
            ok = out.is_file() and _head_bytes(out, 2) == b"PK"
            check("csv → xlsx", ok,
                  f"产物 {out.name} {human_size(out.stat().st_size)}" if ok else "未生成有效 xlsx")
        except Exception as exc:  # noqa: BLE001
            check("csv → xlsx", False, f"{type(exc).__name__}: {exc}"[:250])

        if xlsx.exists():
            xlsx_pdf = work / "table.pdf"
            try:
                out = converter.office.convert(xlsx, xlsx_pdf, source_ext=".xlsx")
                head = _head_bytes(out)
                ok = out.is_file() and head.startswith(b"%PDF")
                check("xlsx → pdf（calc 族过滤器）", ok,
                      human_size(out.stat().st_size) if out.is_file() else f"未生成 {out.name}")
            except Exception as exc:  # noqa: BLE001
                check("xlsx → pdf（calc 族过滤器）", False, f"{type(exc).__name__}: {exc}"[:250])

        # docx → html / txt：验证 writer 族过滤器与「输出名可能变化」的定位逻辑
        if docx.exists():
            try:
                out = converter.office.convert(docx, work / "note.html", source_ext=".docx")
                text = out.read_text(encoding="utf-8", errors="replace")
                check("docx → html", out.is_file() and "自检" in text, f"产物 {out.name}")
            except Exception as exc:  # noqa: BLE001
                check("docx → html", False, str(exc)[:250])

        # docx → xlsx 属于跨族转换，应被提前判定为「跳过」而不是抛错
        docx_item = FileItem(path=docx if docx.exists() else note)
        tasks = plan_tasks([docx_item], ".xlsx", work / "out", True, env)
        print(f"   plan_tasks 跳过原因：{tasks[0].skip_reason!r}")
        check("跨族转换被标记为跳过", bool(tasks[0].skip_reason))

    # ---------------------------------------------------------------- 规划逻辑
    print("\n[4] 任务规划（类型匹配 / 跳过判定）")
    sample_audio = work / "tone.wav"
    if sample_audio.exists():
        item = FileItem(path=sample_audio)
        task = plan_tasks([item], ".pdf", work / "out", True, env)[0]
        check("音频 → 办公目标 判定为跳过",
              task.will_skip and "类型不匹配" in task.skip_reason, task.skip_reason)

        same_dir_task = plan_tasks([item], ".wav", sample_audio.parent, True, env)[0]
        check("输出与源相同 判定为跳过",
              same_dir_task.will_skip and "相同" in same_dir_task.skip_reason,
              same_dir_task.skip_reason)

        preview = plan_tasks([item], ".flac", work / "out", True, env)[0]
        check("输出路径预览正确",
              preview.output.name == "tone.flac" and not preview.will_skip,
              str(preview.output))

    # ---------------------------------------------------------------- 界面
    print("\n[5] 界面冒烟测试")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication

        from gui import MainWindow, apply_style

        app = QApplication.instance() or QApplication([])
        apply_style(app)
        window = MainWindow(env)
        window.add_paths([str(work)])
        # 选中 flac，验证「目标格式 → 输出路径预览」联动
        for row in range(window.target_combo.count()):
            data = window.target_combo.itemData(row)
            if isinstance(data, dict) and data.get("ext") == ".flac":
                window.target_combo.setCurrentIndex(row)
                break
        app.processEvents()
        preview_ok = any(str(item.output).endswith(".flac") for item in window.items)
        window.show()
        app.processEvents()
        check("主窗口构建成功", window.table.rowCount() > 0, f"表格 {window.table.rowCount()} 行")
        check("输出路径预览随目标格式联动", preview_ok)
        window.close()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("界面冒烟测试", False, str(exc)[:200])

    # ---------------------------------------------------------------- 汇总
    print("\n" + "=" * 72)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print("   ✗ " + name)
    print("=" * 72)
    print(f"（临时文件保留在 {work}，可自行删除）")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
