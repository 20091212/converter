"""worker.py —— 后台转换线程

* ``ConversionWorker`` 继承 ``QThread``，在独立线程里顺序处理任务，
  通过信号把「状态 / 进度 / 日志」回传主线程，保证界面永不卡死。
* 取消：``cancel()`` 置位取消事件 —— 当前文件会被安全终止（FFmpeg 先 terminate
  再 kill；LibreOffice 用 taskkill /T 结束进程树），后续任务不再启动。
* 单个文件失败不会影响其它文件，失败原因写入表格「状态」列与日志。
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass
from typing import Sequence

from PySide6.QtCore import QThread, Signal

from converter import CancelledError, ConversionTask, Converter
from utils import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)

LOG = logging.getLogger(__name__)


@dataclass
class ConversionStats:
    """一轮转换的统计结果。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: bool = False
    elapsed: float = 0.0

    def summary(self) -> str:
        text = f"成功 {self.success} · 失败 {self.failed} · 跳过 {self.skipped}"
        if self.cancelled:
            text += " · 已取消"
        return f"{text}（耗时 {self.elapsed:.1f} 秒）"


class ConversionWorker(QThread):
    """把任务列表跑完的后台线程。"""

    #: (行号, 状态, 提示信息, 输出路径)
    file_changed = Signal(int, str, str, str)
    #: (行号, 当前文件说明文字)
    current_file = Signal(int, str)
    #: (行号, 单文件百分比；-1 表示不确定进度)
    file_progress = Signal(int, int)
    #: 总体百分比 0~100
    overall_progress = Signal(int)
    #: 一轮结束：(统计信息, 是否用户取消)
    completed = Signal(object)

    def __init__(self, tasks: Sequence[ConversionTask], converter: Converter, parent=None):
        super().__init__(parent)
        self._tasks = list(tasks)
        self._converter = converter
        self._cancel = threading.Event()

    # ------------------------------------------------------------------
    # 对外控制
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """请求取消：立即终止正在转换的文件，并停止后续任务。"""
        if not self._cancel.is_set():
            LOG.info("收到取消请求，正在停止…")
            self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def run(self) -> None:  # noqa: C901 - 主循环，逻辑线性直白
        import time

        started = time.monotonic()
        stats = ConversionStats(total=len(self._tasks))
        total = max(1, len(self._tasks))
        completed_units = 0

        LOG.info("开始转换：共 %d 个文件", len(self._tasks))

        for task in self._tasks:
            if self._cancel.is_set():
                stats.cancelled = True
                completed_units += 1
                continue

            item = task.item
            index = task.index
            self.current_file.emit(index, item.name)
            self.file_progress.emit(index, -1)
            self.file_changed.emit(index, STATUS_RUNNING, "正在转换…", str(task.output))
            LOG.info("[%d/%d] %s → %s", index + 1, len(self._tasks), item.name,
                     task.target_ext.lstrip("."))

            # 规划阶段已判定需要跳过的情况
            if task.will_skip:
                stats.skipped += 1
                self.file_changed.emit(index, STATUS_SKIPPED, task.skip_reason, "")
                LOG.warning("跳过 %s：%s", item.name, task.skip_reason)
                completed_units += 1
                self.overall_progress.emit(int(completed_units / total * 100))
                continue

            try:
                output = self._converter.convert(
                    task,
                    cancel=self._cancel,
                    on_progress=lambda percent, i=index: self.file_progress.emit(i, percent),
                    on_status=lambda text, i=index: self.current_file.emit(i, f"{item.name} — {text}"),
                )
            except CancelledError:
                stats.cancelled = True
                self.file_changed.emit(index, STATUS_CANCELLED, "用户取消", "")
                LOG.warning("已取消：%s", item.name)
                completed_units += 1
                self.overall_progress.emit(int(completed_units / total * 100))
                break
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不应影响其它文件
                stats.failed += 1
                message = str(exc) or exc.__class__.__name__
                self.file_changed.emit(index, STATUS_FAILED, message, "")
                LOG.error("失败 %s：%s", item.name, message)
                LOG.debug("异常详情：\n%s", traceback.format_exc())
            else:
                stats.success += 1
                self.file_progress.emit(index, 100)
                self.file_changed.emit(index, STATUS_SUCCESS, "转换完成", str(output))
                LOG.info("完成 %s → %s", item.name, output.name)

            completed_units += 1
            self.overall_progress.emit(int(completed_units / total * 100))

        if self._cancel.is_set():
            stats.cancelled = True

        stats.elapsed = time.monotonic() - started
        self.overall_progress.emit(100)
        self.current_file.emit(-1, "已结束")
        LOG.info("转换结束：%s", stats.summary())
        self.completed.emit(stats)
