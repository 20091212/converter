"""utils.py —— 通用辅助工具（不依赖 Qt，可单独测试 / 复用）

包含：
1. 外部程序检测：ffmpeg / ffprobe / soffice（LibreOffice），含版本号
2. 音频、办公文件格式集合与「文件类型」判断
3. LibreOffice「目标格式 → 导出过滤器(filter)」映射
4. 文件遍历（支持递归）、大小格式化、输出路径规划
5. 跨平台细节：Windows 隐藏子进程控制台、打开目录、用户数据目录
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

LOG = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# 一、跨平台子进程 / 系统相关小工具
# ---------------------------------------------------------------------------
def subprocess_kwargs() -> dict:
    """返回 Popen 的跨平台附加参数：Windows 下隐藏控制台黑窗。"""
    kw: dict = {}
    if IS_WINDOWS:
        # CREATE_NO_WINDOW：不弹出 cmd 窗口（打包成 windowed exe 后尤其重要）
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kw


def app_dir() -> Path:
    """程序所在目录（兼容 PyInstaller 冻结后的 exe）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """跨平台的用户数据目录（放日志、LibreOffice 临时配置）。"""
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / "UnifiedConverter"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_folder(path: Path | str) -> None:
    """用系统文件管理器打开目录（跨平台）。"""
    target = str(path)
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    elif IS_MACOS:
        subprocess.Popen(["open", target], **subprocess_kwargs())
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise RuntimeError("系统中找不到 xdg-open，无法打开目录")
        subprocess.Popen([opener, target], **subprocess_kwargs())


def human_size(num: float) -> str:
    """把字节数格式化成易读字符串。"""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def shorten_path(path: Path | str, limit: int = 60) -> str:
    """过长的路径中间用 ... 省略，便于在表格 / 日志里显示。"""
    text = str(path)
    if len(text) <= limit:
        return text
    head = max(6, (limit - 4) // 2)
    tail = max(6, limit - 4 - head)
    return f"{text[:head]}...{text[-tail:]}"


def display_log_path(path: Path | str | None = None) -> str:
    """返回便于显示 / 复制的日志路径（默认日志文件）。"""
    target = Path(path) if path else (log_dir() / "converter.log")
    return str(target)


# ---------------------------------------------------------------------------
# 二、外部程序检测
# ---------------------------------------------------------------------------
@dataclass
class ToolInfo:
    """一个外部依赖（ffmpeg / soffice）的检测结果。"""

    key: str
    label: str
    path: str | None = None
    version: str | None = None
    error: str | None = None
    hint: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.path)

    def describe(self) -> str:
        if not self.ok:
            reason = self.error or "未安装或不在 PATH 中"
            return f"{self.label}：未检测到 —— {reason}"
        return f"{self.label}：{self.version or '已找到'}  [{self.path}]"


def _find_program(names: Sequence[str], extra: Sequence[Path], env_var: str | None = None) -> str | None:
    """按 环境变量 → PATH → 常见安装目录 的顺序查找可执行文件。"""
    if env_var:
        override = os.environ.get(env_var)
        if override:
            candidate = Path(override)
            if candidate.is_file():
                return str(candidate)
            found = shutil.which(override)
            if found:
                return found
            LOG.warning("%s 指向的路径不存在：%s", env_var, override)

    for name in names:
        found = shutil.which(name)
        if found:
            return found

    for candidate in extra:
        try:
            if candidate and candidate.is_file():
                return str(candidate)
        except OSError:  # 权限 / 路径异常直接忽略
            continue
    return None


def _ffmpeg_candidates() -> list[Path]:
    """ffmpeg 的常见安装路径（含 winget / scoop / chocolatey / 便携版）。"""
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    here = app_dir()
    # 随程序一起分发的便携版优先（打包后可做到「解压即用」）
    result: list[Path] = [here / exe, here / "bin" / exe, here / "ffmpeg" / exe]

    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        profile = Path(os.environ.get("USERPROFILE", ""))
        bases = [
            Path("C:/ffmpeg/bin"),
            Path("C:/Program Files/ffmpeg/bin"),
            Path("C:/Program Files (x86)/ffmpeg/bin"),
            local / "Microsoft" / "WinGet" / "Links",
            local / "Microsoft" / "WinGet" / "Packages",
            profile / "scoop" / "shims",
            Path("C:/ProgramData/chocolatey/bin"),
        ]
        for base in bases:
            if not base.is_dir():
                continue
            if base.name == "Packages":
                # winget 包目录结构：<包名>_<源>_<hash>/<软件版本目录>/bin/ffmpeg.exe
                for pattern in ("Gyan.FFmpeg*/ffmpeg-*/bin", "*/ffmpeg-*/bin", "Gyan.FFmpeg*/**/bin"):
                    try:
                        result.extend(p / exe for p in sorted(base.glob(pattern)))
                    except OSError:
                        continue
            else:
                result.append(base / exe)
    elif IS_MACOS:
        result += [
            Path("/opt/homebrew/bin") / exe,
            Path("/usr/local/bin") / exe,
            Path("/usr/bin") / exe,
        ]
    else:
        result += [
            Path("/usr/bin") / exe,
            Path("/usr/local/bin") / exe,
            Path("/snap/bin") / exe,
        ]
    return result


def _soffice_candidates() -> list[Path]:
    """LibreOffice 的常见安装路径 / 应用包路径。"""
    exe = "soffice.exe" if IS_WINDOWS else "soffice"
    here = app_dir()
    result: list[Path] = [
        here / exe,
        here / "LibreOffice" / "program" / exe,
        here / "libreoffice" / "program" / exe,
    ]

    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        result += [
            program_files / "LibreOffice" / "program" / exe,
            Path("C:/Program Files (x86)/LibreOffice/program") / exe,
            local / "Programs" / "LibreOffice" / "program" / exe,
        ]
    elif IS_MACOS:
        result += [
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
            Path.home() / "Applications" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice",
        ]
    else:
        result += [
            Path("/usr/bin/soffice"),
            Path("/usr/local/bin/soffice"),
            Path("/usr/bin/libreoffice"),
            Path("/opt/libreoffice/program/soffice"),
            Path("/snap/bin/libreoffice"),
        ]
    return result


def _read_version(cmd: list[str], timeout: float = 15.0) -> str | None:
    """执行 `xxx --version` 取第一行有效输出作为版本号。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("读取版本失败 %s: %s", cmd, exc)
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _windows_file_version(path: str) -> str | None:
    """读取 Windows 可执行文件的版本资源（如 26.8.0.3）。

    比执行 ``soffice --version`` 更可靠：某些安装（例如免安装的管理员映像）
    启动 soffice 后会挂住不返回，直接读文件版本可以避免检测阶段卡死。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(length)
        ):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        parts = (
            info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF,
        )
        return ".".join(str(p) for p in parts) or None
    except (OSError, ValueError):
        return None


def _soffice_version(path: str) -> str | None:
    """获取 LibreOffice 版本号。

    Windows 优先读文件版本资源 / version.ini（避免 `soffice --version` 挂住），
    其它平台直接执行 ``soffice --version``。
    """
    if IS_WINDOWS:
        file_version = _windows_file_version(path)
        if file_version:
            return f"LibreOffice {file_version}"
        ini = Path(path).parent / "version.ini"
        try:
            if ini.is_file():
                for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("UpdateID=LibreOffice_"):
                        major = line.split("_")[1]
                        return f"LibreOffice {major}.x"
        except OSError:
            pass
        return None
    return _read_version([path, "--version"], timeout=20)


@dataclass
class Environment:
    """本机依赖检测结果汇总。"""

    ffmpeg: ToolInfo
    ffprobe: ToolInfo
    soffice: ToolInfo

    @property
    def audio_ok(self) -> bool:
        return self.ffmpeg.ok

    @property
    def office_ok(self) -> bool:
        return self.soffice.ok

    def summary(self) -> str:
        """状态栏用的一行摘要。"""
        mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
        return (
            f"FFmpeg {mark(self.ffmpeg.ok)}   "
            f"LibreOffice {mark(self.soffice.ok)}"
        )

    def details(self) -> list[str]:
        """关于 / 环境检测对话框用的多行详情。"""
        lines = [self.ffmpeg.describe(), self.ffprobe.describe(), self.soffice.describe()]
        if not self.ffmpeg.ok:
            lines.append("")
            lines.append("音频转换需要 FFmpeg：" + FFMPEG_HINT)
        if not self.soffice.ok:
            lines.append("")
            lines.append("办公文档转换需要 LibreOffice：" + SOFFICE_HINT)
        return lines


FFMPEG_HINT = (
    "Windows: winget install Gyan.FFmpeg    "
    "macOS: brew install ffmpeg    "
    "Linux: sudo apt install ffmpeg"
)
SOFFICE_HINT = (
    "Windows: winget install TheDocumentFoundation.LibreOffice    "
    "macOS: brew install --cask libreoffice    "
    "Linux: sudo apt install libreoffice"
)

_ENV_CACHE: Environment | None = None


def detect_environment(force: bool = False) -> Environment:
    """检测 ffmpeg / ffprobe / soffice，结果在进程内缓存（force=True 强制重测）。"""
    global _ENV_CACHE
    if _ENV_CACHE is not None and not force:
        return _ENV_CACHE

    ffmpeg_path = _find_program(["ffmpeg"], _ffmpeg_candidates(), "FFMPEG_BINARY")
    ffprobe_path = _find_program(["ffprobe"], _ffmpeg_candidates(), "FFPROBE_BINARY")
    if ffmpeg_path and not ffprobe_path:
        # ffprobe 一般与 ffmpeg 同目录，兜底推导
        guess = Path(ffmpeg_path).with_name("ffprobe.exe" if IS_WINDOWS else "ffprobe")
        ffprobe_path = str(guess) if guess.is_file() else None
    soffice_path = _find_program(["soffice", "libreoffice"], _soffice_candidates(), "SOFFICE_BINARY")

    env = Environment(
        ffmpeg=ToolInfo(
            key="ffmpeg",
            label="FFmpeg",
            path=ffmpeg_path,
            version=_read_version([ffmpeg_path, "-version"]) if ffmpeg_path else None,
            error=None if ffmpeg_path else "已安装但未找到可执行文件",
            hint=FFMPEG_HINT,
        ),
        ffprobe=ToolInfo(
            key="ffprobe",
            label="FFprobe",
            path=ffprobe_path,
            version=_read_version([ffprobe_path, "-version"]) if ffprobe_path else None,
            error=None if ffprobe_path else "随 FFmpeg 一起提供，缺失时无法显示转换百分比",
            hint=FFMPEG_HINT,
        ),
        soffice=ToolInfo(
            key="soffice",
            label="LibreOffice",
            path=soffice_path,
            version=_soffice_version(soffice_path) if soffice_path else None,
            error=None if soffice_path else "已安装但未找到 soffice 可执行文件",
            hint=SOFFICE_HINT,
        ),
    )
    _ENV_CACHE = env
    LOG.info("环境检测：%s", env.summary())
    for line in env.details():
        LOG.debug("  %s", line)
    return env


# ---------------------------------------------------------------------------
# 三、格式集合与判断
# ---------------------------------------------------------------------------
#: 可识别的音频源格式（尽量宽松，实际能不能解由 FFmpeg 决定）
AUDIO_SOURCE_EXTS: set[str] = {
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".aiff",
    ".aif", ".alac", ".ape", ".amr", ".ac3", ".m4b", ".wv", ".mp2", ".oga",
    ".mka", ".caf", ".au", ".ra", ".dts", ".m4r", ".3gp", ".aifc",
}

#: 可识别的办公文档源格式
OFFICE_SOURCE_EXTS: set[str] = {
    ".docx", ".doc", ".docm", ".dot", ".dotx",
    ".xlsx", ".xls", ".xlsm", ".xltx",
    ".pptx", ".ppt", ".pptm",
    ".odt", ".ods", ".odp", ".odg", ".ott", ".ots", ".otp",
    ".rtf", ".txt", ".csv", ".tsv", ".html", ".htm", ".xml",
    ".fodt", ".fods", ".fodp", ".pages", ".key", ".numbers",
}

#: 音频目标格式（按需求固定顺序）
AUDIO_TARGETS: list[str] = [".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus"]

#: 办公目标格式（按需求固定顺序）
OFFICE_TARGETS: list[str] = [
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".html", ".txt", ".csv", ".rtf",
]

#: 无损音频目标：这些格式不设置码率
LOSSLESS_AUDIO_TARGETS: set[str] = {".wav", ".flac"}

#: 支持码率的常用取值（下拉框，可直接手输）
BITRATE_PRESETS: list[str] = [
    "64k", "96k", "128k", "160k", "192k", "256k", "320k",
]

#: 音频编码参数：尽量使用 FFmpeg 内置/常见编码器，避免依赖外部库缺失
AUDIO_ENCODER_ARGS: dict[str, list[str]] = {
    ".mp3": ["-c:a", "libmp3lame"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".aac": ["-c:a", "aac", "-f", "adts"],
    ".m4a": ["-c:a", "aac", "-movflags", "+faststart"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    ".opus": ["-c:a", "libopus"],
}

#: 办公文档的「文档族」——LibreOffice 只在同族内互转才是可靠的
DOC_FAMILY_BY_EXT: dict[str, str] = {
    # Writer（文字文档）
    ".docx": "writer", ".doc": "writer", ".docm": "writer", ".dot": "writer",
    ".dotx": "writer", ".odt": "writer", ".ott": "writer", ".rtf": "writer",
    ".txt": "writer", ".fodt": "writer", ".pages": "writer",
    # Calc（表格）
    ".xlsx": "calc", ".xls": "calc", ".xlsm": "calc", ".xltx": "calc",
    ".ods": "calc", ".ots": "calc", ".csv": "calc", ".tsv": "calc",
    ".fods": "calc", ".numbers": "calc",
    # Impress（演示文稿）
    ".pptx": "ppt", ".ppt": "ppt", ".pptm": "ppt", ".odp": "ppt",
    ".otp": "ppt", ".fodp": "ppt", ".key": "ppt",
    # Draw / 网页
    ".odg": "draw",
    ".html": "writer", ".htm": "writer", ".xml": "writer",
}

FAMILY_LABELS = {"writer": "文字文档", "calc": "表格", "ppt": "演示文稿", "draw": "图形"}

#: 目标格式 → 所属文档族（pdf 为 None，表示按源类型自动选择导出过滤器）
TARGET_FAMILY: dict[str, str | None] = {
    ".pdf": None,
    ".docx": "writer", ".odt": "writer", ".rtf": "writer", ".html": "writer", ".txt": "writer",
    ".xlsx": "calc", ".ods": "calc", ".csv": "calc",
    ".pptx": "ppt", ".odp": "ppt",
}


def normalize_ext(value: str) -> str:
    """把 'mp3' / '.MP3' 统一成 '.mp3'。"""
    ext = (value or "").strip().lower()
    if not ext:
        return ""
    return ext if ext.startswith(".") else "." + ext


def file_category(path: Path | str) -> str:
    """判断文件类型：audio / office / other。"""
    ext = normalize_ext(Path(path).suffix)
    if ext in AUDIO_SOURCE_EXTS:
        return "audio"
    if ext in OFFICE_SOURCE_EXTS:
        return "office"
    return "other"


def category_label(category: str, ext: str) -> str:
    """表格「类型」列显示用的文字。"""
    ext = normalize_ext(ext)
    if category == "audio":
        return f"音频 · {ext.lstrip('.')}"
    if category == "office":
        family = DOC_FAMILY_BY_EXT.get(ext)
        suffix = f"（{FAMILY_LABELS[family]}）" if family in FAMILY_LABELS else ""
        return f"办公 · {ext.lstrip('.')}{suffix}"
    return "不支持"


def target_kind(ext: str) -> str | None:
    """目标格式属于哪一类：audio / office / None（未知）。"""
    ext = normalize_ext(ext)
    if ext in AUDIO_TARGETS:
        return "audio"
    if ext in OFFICE_TARGETS:
        return "office"
    return None


def is_lossy_audio_target(ext: str) -> bool:
    """是否为有损音频目标（需要/支持设置码率）。"""
    ext = normalize_ext(ext)
    return ext in AUDIO_TARGETS and ext not in LOSSLESS_AUDIO_TARGETS


def doc_family(ext: str) -> str | None:
    return DOC_FAMILY_BY_EXT.get(normalize_ext(ext))


def family_compatible(source_ext: str, target_ext: str) -> tuple[bool, str]:
    """判断办公文档的跨族转换是否可靠。

    返回 (是否兼容, 说明文字)。pdf 目标对所有族都兼容。
    """
    src_family = doc_family(source_ext)
    tgt_family = TARGET_FAMILY.get(normalize_ext(target_ext))
    if tgt_family is None:  # pdf：LibreOffice 按源文档类型选择导出过滤器
        return True, ""
    if src_family is None:
        return True, ""  # 源族未知时交给 LibreOffice 自己判断
    if src_family == tgt_family:
        return True, ""
    return False, (
        f"LibreOffice 不支持 {FAMILY_LABELS.get(src_family, src_family)} → "
        f"{FAMILY_LABELS.get(tgt_family, tgt_family)} 这类跨类型转换"
    )


#: 目标格式 → LibreOffice 过滤器列表（第一个为首选，其余为失败时的回退）
SOFFICE_FILTER_MAP: dict[str, list[str]] = {
    ".docx": ["docx:MS Word 2007 XML"],
    ".xlsx": ["xlsx:Calc MS Excel 2007 XML"],
    ".pptx": ["pptx:Impress MS PowerPoint 2007 XML"],
    ".odt": ["odt:writer8", "odt:OpenDocument Text"],
    ".ods": ["ods:calc8", "ods:OpenDocument Spreadsheet"],
    ".odp": ["odp:impress8", "odp:OpenDocument Presentation"],
    ".html": ["html:HTML (StarWriter)", "html:HTML"],
    ".txt": ["txt:Text (encoded):UTF8", "txt:Text"],
    ".csv": ["csv:Text - txt - csv (StarCalc):44,34,76,1", "csv:Text - txt - csv (StarCalc)"],
    ".rtf": ["rtf:Rich Text Format"],
    ".pdf": ["pdf:writer_pdf_Export"],  # 仅作兜底，实际按源族选择
}

#: PDF 导出过滤器按文档族选择，效果比统一的 writer_pdf_Export 更正确
PDF_FILTER_BY_FAMILY: dict[str, str] = {
    "writer": "writer_pdf_Export",
    "calc": "calc_pdf_Export",
    "ppt": "impress_pdf_Export",
    "draw": "draw_pdf_Export",
}


def soffice_filters(target_ext: str, source_ext: str = "") -> list[str]:
    """构造 `--convert-to` 用的过滤器字符串列表（含回退项）。"""
    target = normalize_ext(target_ext)
    if target == ".pdf":
        family = doc_family(source_ext)
        filter_name = PDF_FILTER_BY_FAMILY.get(family or "")
        # 只写 `pdf` 时 LibreOffice 也会按文档类型自动选择，作为最后兜底
        return [f"pdf:{filter_name}"] if filter_name else ["pdf"]
    return list(SOFFICE_FILTER_MAP.get(target, [target.lstrip(".")]))


def validate_bitrate(value: str) -> str | None:
    """校验码率字符串，合法返回规范化结果，非法返回 None。"""
    text = (value or "").strip().lower().replace(" ", "")
    if not text:
        return None
    digits = text[:-1] if text.endswith("k") else text
    if not digits.isdigit():
        return None
    number = int(digits)
    if not 8 <= number <= 512:  # 8k ~ 512k 之外的码率基本没有意义
        return None
    return f"{number}k"


# ---------------------------------------------------------------------------
# 四、文件遍历与输出路径规划
# ---------------------------------------------------------------------------
#: 需要忽略的系统 / 临时文件（Office 锁文件、macOS 元数据等）
_IGNORED_PREFIXES = ("~$", ".~", "._")
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}


def is_ignored_file(path: Path) -> bool:
    name = path.name
    return name in _IGNORED_NAMES or name.startswith(_IGNORED_PREFIXES)


def iter_files(paths: Iterable[Path | str], recursive: bool = False) -> Iterator[Path]:
    """把「文件 + 文件夹」混合的输入展开成文件列表。"""
    for raw in paths:
        path = Path(raw)
        try:
            if path.is_dir():
                pattern = "**/*" if recursive else "*"
                for child in sorted(path.glob(pattern)):
                    if child.is_file() and not is_ignored_file(child):
                        yield child
            elif path.is_file() and not is_ignored_file(path):
                yield path
        except OSError as exc:
            LOG.warning("遍历失败，已跳过 %s：%s", path, exc)


def build_output_path(source: Path, out_dir: Path | None, target_ext: str) -> Path:
    """按目标格式计算输出路径（不含重名处理）。"""
    directory = Path(out_dir) if out_dir else source.parent
    return directory / f"{source.stem}{normalize_ext(target_ext)}"


def unique_path(path: Path) -> Path:
    """目标已存在时自动加序号：xxx.mp3 → xxx_1.mp3。"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成唯一输出文件名：{path}")


# ---------------------------------------------------------------------------
# 五、任务条目模型
# ---------------------------------------------------------------------------
STATUS_WAITING = "等待"
STATUS_RUNNING = "转换中"
STATUS_SUCCESS = "成功"
STATUS_FAILED = "失败"
STATUS_SKIPPED = "跳过"
STATUS_CANCELLED = "已取消"

ALL_STATUSES = (
    STATUS_WAITING, STATUS_RUNNING, STATUS_SUCCESS,
    STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED,
)


@dataclass
class FileItem:
    """文件列表中的一行。"""

    path: Path
    size: int = 0
    category: str = "other"
    status: str = STATUS_WAITING
    message: str = ""
    output: str = ""
    kind: str = ""  # 目标类型（audio / office），用于刷新预览

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.size:
            try:
                self.size = self.path.stat().st_size
            except OSError:
                self.size = 0
        if self.category == "other":
            self.category = file_category(self.path)

    # --- 便捷属性 -----------------------------------------------------
    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ext(self) -> str:
        return normalize_ext(self.path.suffix)

    @property
    def size_label(self) -> str:
        return human_size(self.size)

    @property
    def type_label(self) -> str:
        return category_label(self.category, self.ext)

    def reset(self) -> None:
        """开始新一轮转换前恢复初始状态。"""
        self.status = STATUS_WAITING
        self.message = ""
        self.output = ""
