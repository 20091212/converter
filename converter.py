"""converter.py —— 转换核心逻辑（纯 Python，不依赖 Qt，可单独测试）

设计要点
--------
* ``AudioConverter``：调用 FFmpeg，用 ``-progress pipe:1`` 读取实时进度（配合 ffprobe
  取时长，可给出百分比）；无损格式不写码率。
* ``OfficeConverter``：调用 LibreOffice ``soffice --headless --convert-to``，为每种
  目标格式提供正确的过滤器，并通过「转换前后扫描输出目录」定位实际生成的文件
  （LibreOffice 有时会改变文件名 / 扩展名）。
* ``plan_tasks``：在真正开始前把「类型不匹配、缺少依赖、目标已存在」等情况算清楚，
  以「跳过」形式呈现，绝不半路崩溃。
* 所有子进程都支持取消（先 terminate，必要时 kill；Windows 下用 taskkill /T 结束进程树）
  与超时，输出通过后台线程逐行读取，避免管道塞满导致死锁。
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from utils import (
    AUDIO_ENCODER_ARGS,
    Environment,
    FileItem,
    STATUS_SKIPPED,
    build_output_path,
    detect_environment,
    family_compatible,
    is_lossy_audio_target,
    normalize_ext,
    soffice_filters,
    subprocess_kwargs,
    target_kind,
    unique_path,
    user_data_dir,
    validate_bitrate,
)

LOG = logging.getLogger(__name__)

#: 进度 / 状态回调签名
ProgressCallback = Callable[[int], None]  # 0~100；-1 表示「不确定进度」
StatusCallback = Callable[[str], None]


class ConversionError(RuntimeError):
    """转换失败（消息面向最终用户，会显示在表格与日志中）。"""


class CancelledError(RuntimeError):
    """用户主动取消。"""


# ---------------------------------------------------------------------------
# 子进程执行辅助
# ---------------------------------------------------------------------------
def terminate_process(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """尽可能安全地终止子进程（含 Windows 进程树）。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "nt":
        # soffice.exe 会拉起子进程，用 taskkill /T 结束整棵树
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                **subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        LOG.warning("子进程 %s 未能终止", proc.pid)


def run_streaming(
    cmd: Sequence[str],
    cancel: threading.Event | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """运行子进程，逐行回调输出，支持取消与超时。

    返回 ``(returncode, 完整输出文本)``；被取消时抛出 :class:`CancelledError`，
    超时抛出 :class:`ConversionError`。

    输出通过独立线程读入队列，主线程以 0.2s 为粒度轮询 —— 既能及时响应取消，
    也不会因为管道缓冲区写满而卡死。
    """
    LOG.debug("执行：%s", " ".join(str(x) for x in cmd))
    proc = subprocess.Popen(
        [str(x) for x in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(cwd) if cwd else None,
        **subprocess_kwargs(),
    )

    lines: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                lines.put(raw)
        except (OSError, ValueError):  # 进程被强杀时管道可能已关闭
            pass
        finally:
            lines.put(None)

    threading.Thread(target=_reader, name="proc-reader", daemon=True).start()

    collected: list[str] = []
    started = time.monotonic()
    cancelled = False

    while True:
        if cancel is not None and cancel.is_set():
            cancelled = True
            terminate_process(proc)
            break
        if timeout and time.monotonic() - started > timeout:
            terminate_process(proc)
            raise ConversionError(f"转换超时（超过 {int(timeout)} 秒），已终止")

        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            if proc.poll() is not None:
                # 进程已退出：把队列里剩余输出读完
                while True:
                    try:
                        rest = lines.get_nowait()
                    except queue.Empty:
                        break
                    if rest is None:
                        break
                    collected.append(rest)
                    if on_line:
                        on_line(rest)
                break
            continue

        if line is None:  # 输出结束（EOF）
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_process(proc)
            break

        collected.append(line)
        if on_line:
            on_line(line)

    returncode = proc.wait()
    if cancelled:
        raise CancelledError("已取消")
    return returncode, "".join(collected)


def _tail(text: str, limit: int = 700) -> str:
    """截取错误输出末尾若干字符，用于生成简洁的失败提示。"""
    clean = "\n".join(line.rstrip() for line in (text or "").splitlines() if line.strip())
    if len(clean) <= limit:
        return clean
    return "..." + clean[-limit:]


# ---------------------------------------------------------------------------
# 音频转换（FFmpeg）
# ---------------------------------------------------------------------------
class AudioConverter:
    """基于 FFmpeg 的音频格式转换。"""

    def __init__(self, ffmpeg: str | None, ffprobe: str | None = None, timeout: float = 1800.0):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.timeout = timeout
        self._duration_cache: dict[str, float | None] = {}

    # --- 能力检测 -----------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    # --- 时长探测（用于换算百分比） ------------------------------------
    def probe_duration(self, source: Path) -> float | None:
        key = str(source)
        if key in self._duration_cache:
            return self._duration_cache[key]

        duration: float | None = None
        if self.ffprobe:
            try:
                proc = subprocess.run(
                    [
                        self.ffprobe, "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        str(source),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    timeout=30,
                    **subprocess_kwargs(),
                )
                value = (proc.stdout or "").strip()
                if value and value.upper() != "N/A":
                    duration = float(value)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                LOG.debug("ffprobe 读取时长失败 %s：%s", source, exc)

        self._duration_cache[key] = duration
        return duration

    # --- 命令构造 -----------------------------------------------------
    def build_command(
        self,
        source: Path,
        target: Path,
        bitrate: str | None = None,
        overwrite: bool = True,
    ) -> list[str]:
        """构造 ffmpeg 命令。

        形如：``ffmpeg -y -hide_banner -loglevel error -progress pipe:1 -i src -vn
        [-b:a 192k] <编码参数> dst``
        """
        if not self.ffmpeg:
            raise ConversionError("未检测到 FFmpeg，无法转换音频文件")

        ext = normalize_ext(target.suffix)
        cmd: list[str] = [
            self.ffmpeg,
            "-y" if overwrite else "-n",
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-progress", "pipe:1",   # 机器可读的进度输出到 stdout
            "-i", str(source),
            "-vn",                   # 只保留音频，忽略封面 / 视频流
        ]

        encoder_args = AUDIO_ENCODER_ARGS.get(ext, [])
        # 无损格式（wav / flac）不设置码率
        if bitrate and is_lossy_audio_target(ext):
            cmd += ["-b:a", bitrate]
        cmd += list(encoder_args)
        cmd += [str(target)]
        return cmd

    # --- 实际执行 -----------------------------------------------------
    def convert(
        self,
        source: Path,
        target: Path,
        bitrate: str | None = "192k",
        overwrite: bool = True,
        cancel: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> Path:
        if not self.available:
            raise ConversionError("未检测到 FFmpeg，请先安装后重试")

        source, target = Path(source), Path(target)
        if not source.is_file():
            raise ConversionError(f"源文件不存在：{source.name}")
        target.parent.mkdir(parents=True, exist_ok=True)

        duration = self.probe_duration(source)
        total_us = duration * 1_000_000 if duration else None
        last_percent = -1

        def handle_line(line: str) -> None:
            """解析 ffmpeg -progress 输出：out_time_us / out_time / progress。"""
            nonlocal last_percent
            text = line.strip()
            if "=" not in text or not on_progress:
                return
            key, _, value = text.partition("=")
            value = value.strip()
            microseconds: int | None = None
            if key == "out_time_us" and value.isdigit():
                microseconds = int(value)
            elif key == "out_time_ms" and value.isdigit():
                # 历史遗留：ffmpeg 的 out_time_ms 实际单位仍是微秒
                microseconds = int(value)
            elif key == "out_time" and ":" in value:
                parts = value.split(":")
                try:
                    if len(parts) == 3:
                        h, m, s = parts
                        microseconds = int((int(h) * 3600 + int(m) * 60 + float(s)) * 1_000_000)
                except ValueError:
                    microseconds = None

            if microseconds is not None and total_us:
                percent = int(min(99.0, max(0.0, microseconds / total_us * 100)))
                if percent != last_percent:
                    last_percent = percent
                    on_progress(percent)
            elif key == "progress" and value == "end":
                on_progress(100)

        if on_status:
            on_status("调用 FFmpeg 解码 → 编码…")

        cmd = self.build_command(source, target, bitrate=bitrate, overwrite=overwrite)
        returncode, output = run_streaming(
            cmd, cancel=cancel, timeout=self.timeout, on_line=handle_line
        )

        if returncode != 0:
            raise ConversionError(f"FFmpeg 返回错误码 {returncode}：{_tail(output)}")
        if not target.is_file() or target.stat().st_size == 0:
            raise ConversionError(f"FFmpeg 未生成有效文件：{_tail(output) or '输出文件为空'}")

        if on_progress:
            on_progress(100)
        return target

    # --- 附加能力：读取媒体信息（供「关于」或未来扩展使用） -------------
    def probe_summary(self, source: Path) -> str:
        if not self.ffprobe:
            return ""
        try:
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-show_entries",
                 "format=format_name,duration,bit_rate", "-of", "default=nw=1", str(source)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                text=True, timeout=30, **subprocess_kwargs(),
            )
            return " ".join((proc.stdout or "").split())
        except (OSError, subprocess.SubprocessError):
            return ""


# ---------------------------------------------------------------------------
# 办公文档转换（LibreOffice headless）
# ---------------------------------------------------------------------------
class OfficeConverter:
    """基于 LibreOffice ``soffice --headless --convert-to`` 的文档转换。"""

    def __init__(self, soffice: str | None, timeout: float = 600.0):
        self.soffice = soffice
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.soffice)

    def _user_installation(self) -> str:
        """独立的用户配置目录。

        作用有两个：
        1. 避免与用户正在使用的 LibreOffice 抢占配置目录导致转换失败；
        2. 避免多次运行之间的 profile 锁冲突。目录复用可加快启动速度。
        """
        profile = user_data_dir() / "lo_profile"
        profile.mkdir(parents=True, exist_ok=True)
        return profile.as_uri()

    def build_command(self, source: Path, out_dir: Path, filter_spec: str) -> list[str]:
        """构造命令：``soffice --headless --norestore --convert-to <filter> --outdir <dir> <src>``。"""
        if not self.soffice:
            raise ConversionError("未检测到 LibreOffice，无法转换办公文档")
        return [
            self.soffice,
            f"-env:UserInstallation={self._user_installation()}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to", filter_spec,
            "--outdir", str(out_dir),
            str(source),
        ]

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, float]:
        """记录目录内现有文件的修改时间，用于识别新增产物。"""
        snapshot: dict[str, float] = {}
        try:
            for entry in directory.iterdir():
                if entry.is_file():
                    try:
                        snapshot[entry.name] = entry.stat().st_mtime
                    except OSError:
                        snapshot[entry.name] = 0.0
        except OSError:
            pass
        return snapshot

    @staticmethod
    def _locate_output(
        out_dir: Path,
        before: dict[str, float],
        target_ext: str,
        started_at: float,
        source_stem: str = "",
    ) -> Path | None:
        """扫描输出目录，找出本次转换真正生成的文件。

        LibreOffice 输出的文件名通常等于源文件主名 + 目标扩展名，但偶尔会有差异
        （例如 html 生成 .htm、编码变体等），所以这里以「新出现或刚刚被改写、且扩展名
        匹配」为准，并在多个候选中优先选择主名与源文件一致的。
        """
        wanted = normalize_ext(target_ext)
        allowed = {wanted}
        if wanted == ".html":
            allowed.add(".htm")
        if wanted == ".txt":
            allowed.add(".text")

        candidates: list[Path] = []
        for entry in sorted(out_dir.iterdir()):
            if not entry.is_file() or entry.suffix.lower() not in allowed:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            changed = before.get(entry.name) != stat.st_mtime
            fresh = stat.st_mtime >= started_at - 1.0
            if changed or fresh:
                candidates.append(entry)

        if not candidates:
            return None

        def sort_key(path: Path) -> tuple[int, float]:
            # 主名与源文件一致的优先，其次取最新的
            same_stem = 0 if (source_stem and path.stem == source_stem) else 1
            try:
                return same_stem, -path.stat().st_mtime
            except OSError:
                return same_stem, 0.0

        return sorted(candidates, key=sort_key)[0]

    def convert(
        self,
        source: Path,
        target: Path,
        source_ext: str = "",
        overwrite: bool = True,
        cancel: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> Path:
        if not self.available:
            raise ConversionError("未检测到 LibreOffice（soffice），请先安装后重试")

        source, target = Path(source), Path(target)
        if not source.is_file():
            raise ConversionError(f"源文件不存在：{source.name}")

        target_ext = normalize_ext(target.suffix)
        out_dir = target.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        source_ext = normalize_ext(source_ext) or normalize_ext(source.suffix)

        if on_progress:
            on_progress(-1)  # LibreOffice 不提供进度，界面显示「不确定进度」
        if on_status:
            on_status("调用 LibreOffice 无界面模式转换…")

        last_output = ""
        produced: Path | None = None
        for index, filter_spec in enumerate(soffice_filters(target_ext, source_ext)):
            if cancel is not None and cancel.is_set():
                raise CancelledError("已取消")

            before = self._snapshot(out_dir)
            started_at = time.time()
            cmd = self.build_command(source, out_dir, filter_spec)
            if index:
                LOG.info("改用备用过滤器重试：%s", filter_spec)

            returncode, output = run_streaming(
                cmd, cancel=cancel, timeout=self.timeout, cwd=out_dir
            )
            last_output = output or last_output
            produced = self._locate_output(
                out_dir, before, target_ext, started_at, source.stem
            )

            if produced is None:
                LOG.warning(
                    "过滤器 %s 未生成 %s 文件（返回码 %s）：%s",
                    filter_spec, target_ext, returncode, _tail(output, 300),
                )
                continue
            break

        if produced is None:
            detail = _tail(last_output) or "LibreOffice 未生成输出文件"
            raise ConversionError(f"转换失败：{detail}")

        # 输出名可能与期望不同（LibreOffice 会自行决定文件名）
        if produced.resolve() != target.resolve():
            if target.exists() and not overwrite:
                LOG.info("目标已存在且未勾选覆盖，保留 LibreOffice 生成的：%s", produced.name)
            else:
                try:
                    if target.exists():
                        target.unlink()
                    produced.replace(target)
                except OSError as exc:
                    LOG.warning("重命名为 %s 失败（保留原文件 %s）：%s", target.name, produced.name, exc)
                    target = produced

        if on_progress:
            on_progress(100)
        return target


# ---------------------------------------------------------------------------
# 任务规划 + 统一入口
# ---------------------------------------------------------------------------
@dataclass
class ConversionTask:
    """一个待转换文件（含跳过原因与输出路径）。"""

    index: int
    item: FileItem
    category: str
    source_ext: str
    target_ext: str
    output: Path
    skip_reason: str = ""
    renamed: bool = False

    @property
    def will_skip(self) -> bool:
        return bool(self.skip_reason)


def plan_tasks(
    items: Iterable[FileItem],
    target_ext: str,
    out_dir: Path | None,
    overwrite: bool = True,
    env: Environment | None = None,
) -> list[ConversionTask]:
    """把文件列表 + 目标格式 规划成可执行任务列表。

    所有「类型不匹配 / 缺少依赖 / 输出与源相同」的情况都在这里判定为「跳过」，
    并附带面向用户的原因说明，保证批量转换不会因为个别文件而崩溃。
    """
    env = env or detect_environment()
    target_ext = normalize_ext(target_ext)
    kind = target_kind(target_ext)
    tasks: list[ConversionTask] = []

    for index, item in enumerate(items):
        source = Path(item.path)
        source_ext = normalize_ext(source.suffix)
        output = build_output_path(source, out_dir, target_ext)
        task = ConversionTask(
            index=index,
            item=item,
            category=item.category,
            source_ext=source_ext,
            target_ext=target_ext,
            output=output,
        )

        # 1) 源文件是否还在
        if not source.is_file():
            task.skip_reason = "源文件不存在或已被移动"
        # 2) 文件类型是否受支持
        elif item.category == "other":
            task.skip_reason = f"不支持的文件类型：{source_ext or '无扩展名'}"
        # 3) 音视频 / 办公 必须与目标类型一致
        elif kind is None:
            task.skip_reason = f"未知的目标格式：{target_ext}"
        elif item.category != kind:
            source_kind = "音频" if item.category == "audio" else "办公文档"
            target_label = "音频" if kind == "audio" else "办公文档"
            task.skip_reason = f"类型不匹配：{source_kind}文件不能转换为{target_label}格式"
        # 4) 依赖是否齐备
        elif kind == "audio" and not env.audio_ok:
            task.skip_reason = "未检测到 FFmpeg，无法转换音频"
        elif kind == "office" and not env.office_ok:
            task.skip_reason = "未检测到 LibreOffice，无法转换办公文档"
        # 5) 办公文档的跨族转换（如 表格 → 文字文档）不可靠
        elif kind == "office":
            compatible, reason = family_compatible(source_ext, target_ext)
            if not compatible:
                task.skip_reason = reason
        # 6) 目标与源是同一个文件（同目录同扩展名）
        if not task.skip_reason:
            try:
                if output.resolve() == source.resolve():
                    task.skip_reason = "输出与源文件相同（格式未变化）"
            except OSError:
                pass
        # 7) 重名处理：未勾选「覆盖」时自动加序号，避免误删
        if not task.skip_reason and output.exists() and not overwrite:
            new_output = unique_path(output)
            task.output = new_output
            task.renamed = True

        tasks.append(task)

    return tasks


class Converter:
    """统一转换入口：按任务类型分派到 FFmpeg / LibreOffice。"""

    def __init__(
        self,
        env: Environment | None = None,
        bitrate: str = "192k",
        overwrite: bool = True,
        audio_timeout: float = 1800.0,
        office_timeout: float = 600.0,
    ):
        self.env = env or detect_environment()
        self.audio = AudioConverter(
            self.env.ffmpeg.path, self.env.ffprobe.path, timeout=audio_timeout
        )
        self.office = OfficeConverter(self.env.soffice.path, timeout=office_timeout)
        self.bitrate = validate_bitrate(bitrate) or "192k"
        self.overwrite = overwrite

    def convert(
        self,
        task: ConversionTask,
        cancel: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> Path:
        """执行单个任务，返回实际输出路径。"""
        if task.skip_reason:
            raise ConversionError(task.skip_reason)

        source, target = Path(task.item.path), Path(task.output)
        if task.category == "audio":
            return self.audio.convert(
                source, target,
                bitrate=self.bitrate, overwrite=self.overwrite,
                cancel=cancel, on_progress=on_progress, on_status=on_status,
            )
        return self.office.convert(
            source, target,
            source_ext=task.source_ext, overwrite=self.overwrite,
            cancel=cancel, on_progress=on_progress, on_status=on_status,
        )
