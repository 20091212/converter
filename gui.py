"""gui.py —— 主窗口与界面逻辑（PySide6）

界面结构（从上到下）：
    ① 文件列表        —— 添加文件 / 添加文件夹 / 移除选中 / 清空列表，支持拖拽
    ② 转换设置        —— 目标格式（分组下拉）、输出目录、音频码率、递归、覆盖
    ③ 执行区          —— 开始转换 / 取消转换 / 打开输出目录 + 总体与单文件进度
    ④ 日志窗口        —— 分级着色（INFO / WARNING / ERROR），可清空、可保存
    ⑤ 状态栏          —— 成功 / 失败 / 跳过 计数 + 依赖检测结果

设计约定：
    * 所有耗时的转换都在 :class:`worker.ConversionWorker` 线程里执行，主线程只更新 UI；
    * 日志通过 ``QtLogHandler`` 以信号方式投递到界面，跨线程安全；
    * 依赖缺失（FFmpeg / LibreOffice）只提示、不崩溃：相关文件会被标记为「跳过」。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QKeySequence,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from converter import ConversionTask, Converter, plan_tasks
from utils import (
    AUDIO_TARGETS,
    BITRATE_PRESETS,
    Environment,
    FileItem,
    OFFICE_TARGETS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_WAITING,
    detect_environment,
    human_size,
    is_lossy_audio_target,
    iter_files,
    normalize_ext,
    open_folder,
    shorten_path,
    target_kind,
)
from worker import ConversionStats, ConversionWorker

LOG = logging.getLogger(__name__)

APP_NAME = "统一格式转换器"
APP_VERSION = "1.0.0"

#: 状态 → 颜色
STATUS_COLORS: dict[str, str] = {
    STATUS_WAITING: "#6b7280",
    STATUS_RUNNING: "#1d4ed8",
    STATUS_SUCCESS: "#15803d",
    STATUS_FAILED: "#dc2626",
    STATUS_SKIPPED: "#b45309",
    STATUS_CANCELLED: "#7c3aed",
}

#: 日志级别 → 颜色
LOG_COLORS: dict[int, str] = {
    logging.DEBUG: "#6b7280",
    logging.INFO: "#1f2937",
    logging.WARNING: "#b45309",
    logging.ERROR: "#dc2626",
    logging.CRITICAL: "#991b1b",
}

TABLE_HEADERS = ["文件名", "类型", "大小", "状态", "输出路径"]


# ---------------------------------------------------------------------------
# 日志 → 界面 的桥接
# ---------------------------------------------------------------------------
class QtLogHandler(QObject, logging.Handler):
    """把 logging 记录以 Qt 信号发到界面（跨线程安全）。"""

    record_emitted = Signal(str, int)

    def __init__(self, level: int = logging.INFO, parent: QObject | None = None):
        QObject.__init__(self, parent)
        logging.Handler.__init__(self, level=level)
        self.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - logging 接口
        try:
            self.record_emitted.emit(self.format(record), record.levelno)
        except RuntimeError:
            # 界面已销毁（程序退出中）时静默忽略
            pass


# ---------------------------------------------------------------------------
# 环境检测对话框
# ---------------------------------------------------------------------------
class EnvironmentDialog(QDialog):
    """显示 FFmpeg / LibreOffice 的检测结果，并提供重新检测按钮。"""

    def __init__(self, env: Environment, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("环境检测")
        self.resize(660, 360)

        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(QLabel("本工具完全在本地运行，仅依赖以下两个外部程序：", self))
        layout.addWidget(self.text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.redetect_button = QPushButton("重新检测", self)
        buttons.addButton(self.redetect_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._env = env
        self._render()

    def _render(self) -> None:
        env = self._env
        lines = [
            f"{APP_NAME}  v{APP_VERSION}",
            "=" * 62,
            "",
            "【依赖检测结果】",
        ]
        lines += ["  " + line for line in env.details()]
        lines += [
            "",
            "【说明】",
            "  · 音频转换（mp3 / wav / flac / aac / m4a / ogg / opus …）由 FFmpeg 完成；",
            "  · 办公文档转换（docx / xlsx / pptx / odt / pdf / html …）由 LibreOffice 完成；",
            "  · 两者都是本地程序，转换过程不会上传任何文件；",
            "  · 缺失的依赖只会让对应的转换被标记为「跳过」，不会导致程序崩溃。",
        ]
        self.text.setPlainText("\n".join(lines))

    def update_environment(self, env: Environment) -> None:
        self._env = env
        self._render()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, env: Environment, parent: QWidget | None = None):
        super().__init__(parent)
        self.env = env
        self.items: list[FileItem] = []
        self.tasks: list[ConversionTask] = []
        self.worker: ConversionWorker | None = None
        self.converter: Converter | None = None
        self.stats = ConversionStats()
        self.last_out_dir: Path | None = None

        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.resize(1080, 760)
        self.setAcceptDrops(True)
        self.setMinimumSize(880, 600)

        self._build_menu()
        self._build_ui()
        self._build_statusbar()
        self._install_log_handler()

        self._refresh_env_labels()
        self._update_buttons()
        self._refresh_output_preview()
        LOG.info("%s 已启动", APP_NAME)
        if not self.env.audio_ok:
            LOG.warning("未检测到 FFmpeg —— 音频转换将被跳过。%s", self.env.ffmpeg.hint)
        if not self.env.office_ok:
            LOG.warning("未检测到 LibreOffice —— 办公文档转换将被跳过。%s", self.env.soffice.hint)

    # ==================================================================
    # 一、界面构建
    # ==================================================================
    def _build_menu(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("文件(&F)")
        self._add_action(file_menu, "添加文件…", QKeySequence.StandardKey.Open, self.on_add_files)
        self._add_action(file_menu, "添加文件夹…", "Ctrl+Shift+O", self.on_add_folder)
        file_menu.addSeparator()
        self._add_action(file_menu, "移除选中", "Delete", self.on_remove_selected)
        self._add_action(file_menu, "清空列表", "Ctrl+L", self.on_clear)
        file_menu.addSeparator()
        self._add_action(file_menu, "退出", QKeySequence.StandardKey.Quit, self.close)

        run_menu = bar.addMenu("转换(&R)")
        self._add_action(run_menu, "开始转换", "Ctrl+R", self.on_start)
        self._add_action(run_menu, "取消转换", "Ctrl+.", self.on_cancel)
        run_menu.addSeparator()
        self._add_action(run_menu, "打开输出目录", "Ctrl+E", self.on_open_output)

        help_menu = bar.addMenu("帮助(&H)")
        self._add_action(help_menu, "环境检测…", "F2", self.on_show_environment)
        self._add_action(help_menu, "使用说明…", "F1", self.on_show_help)
        self._add_action(help_menu, "关于…", None, self.on_about)

    def _add_action(self, menu: QMenu, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut if isinstance(shortcut, str) else shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self._build_top_panel())
        splitter.addWidget(self._build_log_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([480, 220])
        self.setCentralWidget(splitter)

    # --- 上半部分：文件列表 + 设置 + 执行 --------------------------------
    def _build_top_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 6)
        layout.setSpacing(8)

        # ① 文件列表
        file_box = QGroupBox("① 文件列表（可直接把文件或文件夹拖到窗口中）", panel)
        file_layout = QVBoxLayout(file_box)

        button_row = QHBoxLayout()
        self.add_files_button = QPushButton("添加文件")
        self.add_folder_button = QPushButton("添加文件夹")
        self.remove_button = QPushButton("移除选中")
        self.clear_button = QPushButton("清空列表")
        for button, slot in (
            (self.add_files_button, self.on_add_files),
            (self.add_folder_button, self.on_add_folder),
            (self.remove_button, self.on_remove_selected),
            (self.clear_button, self.on_clear),
        ):
            button.clicked.connect(slot)
            button_row.addWidget(button)
        button_row.addStretch(1)
        self.count_label = QLabel("共 0 个文件")
        self.count_label.setStyleSheet("color:#4b5563;")
        button_row.addWidget(self.count_label)
        file_layout.addLayout(button_row)

        self.table = QTableWidget(0, len(TABLE_HEADERS), file_box)
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_menu)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(3, 130)
        file_layout.addWidget(self.table, 1)
        layout.addWidget(file_box, 1)

        # ② 转换设置
        settings_box = QGroupBox("② 转换设置", panel)
        grid = QGridLayout(settings_box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("目标格式："), 0, 0, Qt.AlignmentFlag.AlignRight)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumWidth(200)
        self._fill_target_combo()
        self.target_combo.currentIndexChanged.connect(self.on_target_changed)
        grid.addWidget(self.target_combo, 0, 1)

        grid.addWidget(QLabel("输出目录："), 0, 2, Qt.AlignmentFlag.AlignRight)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("留空 = 输出到每个源文件所在目录")
        self.output_edit.setClearButtonEnabled(True)
        grid.addWidget(self.output_edit, 0, 3)
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self.on_choose_output_dir)
        grid.addWidget(self.browse_button, 0, 4)

        grid.addWidget(QLabel("音频码率："), 1, 0, Qt.AlignmentFlag.AlignRight)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.setEditable(True)
        self.bitrate_combo.addItems(BITRATE_PRESETS)
        self.bitrate_combo.setCurrentText("192k")
        self.bitrate_combo.setToolTip("仅对有损音频格式有效；无损格式（wav / flac）会自动忽略该值")
        self.bitrate_combo.setMinimumWidth(120)
        grid.addWidget(self.bitrate_combo, 1, 1)
        self.bitrate_hint = QLabel()
        self.bitrate_hint.setStyleSheet("color:#6b7280;")
        grid.addWidget(self.bitrate_hint, 1, 3, 1, 2)
        # 码率被改动时同步刷新提示文字（连接放在 hint 创建之后，避免构造期触发）
        self.bitrate_combo.currentTextChanged.connect(self.on_target_changed)

        self.recursive_check = QCheckBox("递归子目录（添加文件夹时包含所有子文件夹）")
        self.overwrite_check = QCheckBox("覆盖已有文件")
        self.overwrite_check.setChecked(True)
        options_row = QHBoxLayout()
        options_row.addWidget(self.recursive_check)
        options_row.addWidget(self.overwrite_check)
        options_row.addStretch(1)
        grid.addLayout(options_row, 2, 0, 1, 5)

        self.target_hint = QLabel()
        self.target_hint.setWordWrap(True)
        self.target_hint.setStyleSheet("color:#6b7280;")
        grid.addWidget(self.target_hint, 3, 0, 1, 5)

        grid.setColumnStretch(3, 1)
        layout.addWidget(settings_box)

        # ③ 执行 + 进度
        run_box = QGroupBox("③ 执行", panel)
        run_layout = QVBoxLayout(run_box)

        run_row = QHBoxLayout()
        self.start_button = QPushButton("开始转换")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumHeight(36)
        self.start_button.clicked.connect(self.on_start)
        self.cancel_button = QPushButton("取消转换")
        self.cancel_button.setMinimumHeight(36)
        self.cancel_button.clicked.connect(self.on_cancel)
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.setMinimumHeight(36)
        self.open_output_button.clicked.connect(self.on_open_output)
        run_row.addWidget(self.start_button, 2)
        run_row.addWidget(self.cancel_button, 1)
        run_row.addWidget(self.open_output_button, 1)
        run_layout.addLayout(run_row)

        self.overall_label = QLabel("总体进度")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(0)
        overall_row = QHBoxLayout()
        overall_row.addWidget(self.overall_label)
        overall_row.addWidget(self.overall_bar, 1)
        run_layout.addLayout(overall_row)

        current_row = QHBoxLayout()
        self.current_label = QLabel("当前文件：—")
        self.current_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.current_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 100)
        self.file_bar.setValue(0)
        self.file_bar.setFixedWidth(220)
        current_row.addWidget(self.current_label, 1)
        current_row.addWidget(self.file_bar)
        run_layout.addLayout(current_row)
        layout.addWidget(run_box)

        return panel

    # --- 下半部分：日志 ------------------------------------------------
    def _build_log_panel(self) -> QWidget:
        box = QGroupBox("④ 转换日志", self)
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self.autoscroll_check = QCheckBox("自动滚动")
        self.autoscroll_check.setChecked(True)
        clear_log_button = QPushButton("清空日志")
        clear_log_button.clicked.connect(self.on_clear_log)
        save_log_button = QPushButton("保存日志…")
        save_log_button.clicked.connect(self.on_save_log)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["全部", "INFO 及以上", "WARNING 及以上", "仅 ERROR"])
        self.level_combo.setCurrentIndex(1)
        self.level_combo.currentIndexChanged.connect(self.on_log_level_changed)
        row.addWidget(QLabel("显示级别："))
        row.addWidget(self.level_combo)
        row.addStretch(1)
        row.addWidget(self.autoscroll_check)
        row.addWidget(save_log_button)
        row.addWidget(clear_log_button)
        layout.addLayout(row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)  # 防止长时间运行后内存膨胀
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setStyleSheet("background:#fbfbfd; border:1px solid #d8dbe2;")
        layout.addWidget(self.log_view, 1)
        return box

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        self.env_label = QLabel()
        self.counts_label = QLabel("成功 0 · 失败 0 · 跳过 0")
        bar.addPermanentWidget(self.env_label)
        bar.addPermanentWidget(self.counts_label)
        bar.showMessage("就绪")

    def _fill_target_combo(self) -> None:
        """目标格式下拉框：音频 / 办公 两组，组标题不可选。"""
        combo = self.target_combo
        combo.clear()
        for title, extensions in (("音频格式", AUDIO_TARGETS), ("办公文档格式", OFFICE_TARGETS)):
            header = QStandardItem(title)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            header.setForeground(QColor("#374151"))
            combo.model().appendRow(header)
            for ext in extensions:
                name = ext.lstrip(".")
                label = f"    {name}"
                if name == "mp3":
                    label += "   （默认）"
                combo.addItem(label, {"ext": ext, "kind": target_kind(ext), "name": name})
        # 默认选中 mp3
        for row in range(combo.count()):
            data = combo.itemData(row)
            if isinstance(data, dict) and data.get("ext") == ".mp3":
                combo.setCurrentIndex(row)
                break

    # ==================================================================
    # 二、日志
    # ==================================================================
    def _install_log_handler(self) -> None:
        self.log_handler = QtLogHandler(logging.INFO, self)
        self.log_handler.record_emitted.connect(self.on_log_record)
        logging.getLogger().addHandler(self.log_handler)

    def on_log_record(self, text: str, level: int) -> None:
        visible_level = {
            0: logging.DEBUG,
            1: logging.INFO,
            2: logging.WARNING,
            3: logging.ERROR,
        }.get(self.level_combo.currentIndex(), logging.INFO)
        if level < visible_level:
            return
        color = LOG_COLORS.get(level, "#1f2937")
        safe = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        self.log_view.appendHtml(f'<span style="color:{color};white-space:pre;">{safe}</span>')
        if self.autoscroll_check.isChecked():
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def on_log_level_changed(self) -> None:
        self.log_view.clear()
        self.log_view.appendHtml(
            '<span style="color:#6b7280;">（切换显示级别后仅显示新产生的日志）</span>'
        )

    def on_clear_log(self) -> None:
        self.log_view.clear()

    def on_save_log(self) -> None:
        default = str(Path.home() / "conversion.log")
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", default, "日志文件 (*.log *.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"无法写入文件：\n{exc}")
            return
        LOG.info("日志已保存到 %s", path)

    # ==================================================================
    # 三、文件列表操作
    # ==================================================================
    def on_add_files(self) -> None:
        filters = (
            "所有支持的文件 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma *.aiff "
            "*.docx *.doc *.xlsx *.xls *.pptx *.ppt *.odt *.ods *.odp *.rtf *.txt *.csv *.html);;"
            "音频文件 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma *.aiff *.alac *.ape);;"
            "办公文档 (*.docx *.doc *.xlsx *.xls *.pptx *.ppt *.odt *.ods *.odp *.rtf *.txt *.csv *.html);;"
            "所有文件 (*)"
        )
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要转换的文件", str(Path.home()), filters)
        if paths:
            self.add_paths(paths)

    def on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", str(Path.home()))
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths: list[str]) -> None:
        """把文件 / 文件夹展开后加入列表（自动去重）。"""
        recursive = self.recursive_check.isChecked()
        existing = {str(item.path).lower() for item in self.items}
        added = 0
        duplicates = 0
        unsupported = 0

        for path in iter_files(paths, recursive=recursive):
            key = str(path.resolve()).lower() if path.exists() else str(path).lower()
            if key in existing:
                duplicates += 1
                continue
            item = FileItem(path=path)
            if item.category == "other":
                unsupported += 1
            existing.add(key)
            self.items.append(item)
            added += 1

        if added:
            self._rebuild_table()
            LOG.info("已添加 %d 个文件（递归子目录：%s）", added, "是" if recursive else "否")
            if unsupported:
                LOG.warning("其中 %d 个文件的类型不受支持，转换时会被标记为「跳过」", unsupported)
        if duplicates:
            LOG.info("已忽略 %d 个重复文件", duplicates)
        if not added and not duplicates:
            LOG.warning("所选位置没有找到可处理的文件")
        self._update_buttons()
        self._refresh_output_preview()

    def on_remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选择要移除的文件。")
            return
        for row in rows:
            if 0 <= row < len(self.items):
                del self.items[row]
        self._rebuild_table()
        self._update_buttons()
        LOG.info("已移除 %d 个文件", len(rows))

    def on_clear(self) -> None:
        if not self.items:
            return
        self.items.clear()
        self._rebuild_table()
        self._update_buttons()
        self.counts_label.setText("成功 0 · 失败 0 · 跳过 0")
        self.overall_bar.setValue(0)
        self.file_bar.setValue(0)
        self.current_label.setText("当前文件：—")
        LOG.info("已清空文件列表")

    def on_table_menu(self, position) -> None:
        menu = QMenu(self)
        if self.table.indexAt(position).isValid():
            menu.addAction("移除选中", self.on_remove_selected)
            menu.addAction("打开文件所在目录", self.on_open_source_dir)
            menu.addSeparator()
        menu.addAction("添加文件…", self.on_add_files)
        menu.addAction("添加文件夹…", self.on_add_folder)
        menu.addAction("清空列表", self.on_clear)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def on_open_source_dir(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows or rows[0] >= len(self.items):
            return
        folder = self.items[rows[0]].path.parent
        self._safe_open_folder(folder)

    # ==================================================================
    # 四、表格渲染
    # ==================================================================
    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            self._fill_row(row, item)
        self.count_label.setText(f"共 {len(self.items)} 个文件")

    def _fill_row(self, row: int, item: FileItem) -> None:
        name_cell = QTableWidgetItem(item.name)
        name_cell.setToolTip(str(item.path))
        self.table.setItem(row, 0, name_cell)

        type_cell = QTableWidgetItem(item.type_label)
        if item.category == "other":
            type_cell.setForeground(QColor(STATUS_COLORS[STATUS_SKIPPED]))
        self.table.setItem(row, 1, type_cell)

        size_cell = QTableWidgetItem(item.size_label)
        size_cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 2, size_cell)

        self.table.setItem(row, 3, QTableWidgetItem(item.status))
        self._update_status_cell(row, item)

        output_cell = QTableWidgetItem(shorten_path(item.output) if item.output else "")
        output_cell.setToolTip(item.output)
        self.table.setItem(row, 4, output_cell)

    def _update_row(self, row: int, item: FileItem) -> None:
        """转换过程中只需刷新「状态」「输出路径」两列。"""
        status_cell = self.table.item(row, 3)
        if status_cell is None:
            self._fill_row(row, item)
            return
        status_cell.setText(item.status)
        self._update_status_cell(row, item)

        output_cell = self.table.item(row, 4)
        if output_cell is not None:
            output_cell.setText(shorten_path(item.output) if item.output else "")
            output_cell.setToolTip(item.output)

    def _update_status_cell(self, row: int, item: FileItem) -> None:
        cell = self.table.item(row, 3)
        if cell is None:
            return
        cell.setForeground(QColor(STATUS_COLORS.get(item.status, "#1f2937")))
        cell.setToolTip(item.message)
        if item.status in (STATUS_FAILED, STATUS_SKIPPED, STATUS_SUCCESS) and item.message:
            cell.setText(f"{item.status} - {item.message[:40]}")

    # ==================================================================
    # 五、设置交互
    # ==================================================================
    def current_target(self) -> dict | None:
        data = self.target_combo.currentData()
        return data if isinstance(data, dict) else None

    def on_target_changed(self) -> None:
        data = self.current_target()
        if not data:
            return
        lossy = is_lossy_audio_target(data["ext"])
        self.bitrate_combo.setEnabled(lossy)
        if lossy:
            self.bitrate_hint.setText(f"将使用 {self.bitrate_combo.currentText().strip() or '192k'} 码率编码")
        elif data["kind"] == "audio":
            self.bitrate_hint.setText("无损格式，不设置码率")
        else:
            self.bitrate_hint.setText("办公文档转换不涉及码率")

        if data["kind"] == "audio":
            self.target_hint.setText(
                "当前目标为音频格式：办公文档（docx / xlsx / pptx …）将被标记为「跳过」。"
                "依赖：FFmpeg。"
            )
        else:
            self.target_hint.setText(
                "当前为办公文档格式：音频文件将被标记为「跳过」。依赖：LibreOffice（soffice）。"
            )
        self._refresh_output_preview()

    def on_choose_output_dir(self) -> None:
        start = self.output_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if folder:
            self.output_edit.setText(folder)
            self._refresh_output_preview()

    def output_dir(self) -> Path | None:
        text = self.output_edit.text().strip().strip('"')
        return Path(text) if text else None

    def _refresh_output_preview(self) -> None:
        """根据当前设置刷新每行「输出路径」的预览。"""
        data = self.current_target()
        if not data:
            return
        out_dir = self.output_dir()
        target_ext = data["ext"]
        for row, item in enumerate(self.items):
            item.kind = data["kind"] or ""
            try:
                preview = (
                    (out_dir if out_dir else item.path.parent) / f"{item.path.stem}{target_ext}"
                )
                item.output = str(preview)
            except (OSError, ValueError):
                item.output = ""
            cell = self.table.item(row, 4)
            if cell is not None:
                cell.setText(shorten_path(item.output) if item.output else "")
                cell.setToolTip(item.output)

    # ==================================================================
    # 六、执行转换
    # ==================================================================
    def on_start(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.items:
            QMessageBox.information(self, "提示", "请先添加需要转换的文件。")
            return

        data = self.current_target()
        if not data:
            QMessageBox.warning(self, "提示", "请先选择目标格式。")
            return

        kind = data["kind"]
        if kind == "audio" and not self.env.audio_ok:
            QMessageBox.warning(
                self, "缺少 FFmpeg",
                "未检测到 FFmpeg，无法进行音频转换。\n\n请先安装：\n" + self.env.ffmpeg.hint +
                "\n\n安装完成后可在「帮助 → 环境检测」中重新检测。",
            )
            return
        if kind == "office" and not self.env.office_ok:
            QMessageBox.warning(
                self, "缺少 LibreOffice",
                "未检测到 LibreOffice，无法进行办公文档转换。\n\n请先安装：\n" + self.env.soffice.hint +
                "\n\n安装完成后可在「帮助 → 环境检测」中重新检测。",
            )
            return

        out_dir = self.output_dir()
        if out_dir is not None:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(self, "输出目录不可用", f"无法创建输出目录：\n{out_dir}\n\n{exc}")
                return
            self.last_out_dir = out_dir

        overwrite = self.overwrite_check.isChecked()
        bitrate = self.bitrate_combo.currentText().strip() or "192k"
        self.tasks = plan_tasks(self.items, data["ext"], out_dir, overwrite, self.env)

        # 恢复所有条目为「等待」，并刷新预览
        for item in self.items:
            item.reset()
        self._rebuild_table()
        self._refresh_output_preview()

        self.stats = ConversionStats(total=len(self.tasks))
        self.counts_label.setText("成功 0 · 失败 0 · 跳过 0")
        self.overall_bar.setValue(0)
        self.file_bar.setValue(0)

        self.converter = Converter(self.env, bitrate=bitrate, overwrite=overwrite)
        self.worker = ConversionWorker(self.tasks, self.converter, self)
        self.worker.file_changed.connect(self.on_file_changed)
        self.worker.file_progress.connect(self.on_file_progress)
        self.worker.current_file.connect(self.on_current_file)
        self.worker.overall_progress.connect(self.overall_bar.setValue)
        self.worker.completed.connect(self.on_completed)

        self._set_running(True)
        LOG.info(
            "开始批量转换：目标 %s，输出目录 %s，覆盖已有文件 %s，码率 %s",
            data["name"], out_dir or "（与源文件同目录）", "是" if overwrite else "否",
            bitrate if is_lossy_audio_target(data["ext"]) else "（不适用）",
        )
        self.worker.start()

    def on_cancel(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        self.statusBar().showMessage("正在取消…")
        self.cancel_button.setEnabled(False)
        self.worker.cancel()

    def on_file_changed(self, index: int, status: str, message: str, output: str) -> None:
        if not (0 <= index < len(self.items)):
            return
        item = self.items[index]
        item.status = status
        item.message = message
        if output:
            item.output = output
        self._update_row(index, item)
        if status in (STATUS_FAILED, STATUS_SKIPPED):
            self.table.scrollToItem(self.table.item(index, 0))

    def on_file_progress(self, index: int, percent: int) -> None:
        if percent < 0:
            # 无法获取具体进度（如 LibreOffice）：显示不确定进度条
            self.file_bar.setRange(0, 0)
            return
        self.file_bar.setRange(0, 100)
        self.file_bar.setValue(percent)

    def on_current_file(self, index: int, text: str) -> None:
        if index < 0:
            self.current_label.setText("当前文件：—")
            self.file_bar.setRange(0, 100)
            self.file_bar.setValue(0)
            return
        self.current_label.setText(f"当前文件：{text}")
        self.current_label.setToolTip(text)

    def on_completed(self, stats: ConversionStats) -> None:
        self.stats = stats
        self._set_running(False)
        self.counts_label.setText(f"成功 {stats.success} · 失败 {stats.failed} · 跳过 {stats.skipped}")
        self.statusBar().showMessage(stats.summary(), 15000)
        self.file_bar.setRange(0, 100)
        self.file_bar.setValue(100 if not stats.cancelled else 0)

        if stats.cancelled:
            LOG.warning("转换已取消：%s", stats.summary())
        else:
            LOG.info("全部完成：%s", stats.summary())

        if stats.failed or stats.cancelled:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("转换结束")
            box.setText(stats.summary())
            detail = [
                f"成功：{stats.success}", f"失败：{stats.failed}", f"跳过：{stats.skipped}",
            ]
            if stats.cancelled:
                detail.append("已按请求取消剩余任务。")
            failed_items = [i for i in self.items if i.status == STATUS_FAILED]
            if failed_items:
                detail.append("")
                detail.append("失败明细（最多显示 10 条）：")
                for item in failed_items[:10]:
                    detail.append(f"  · {item.name}：{item.message[:120]}")
            box.setDetailedText("\n".join(detail))
            open_button = box.addButton("打开输出目录", QMessageBox.ButtonRole.ActionRole)
            box.addButton("关闭", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() is open_button:
                self.on_open_output()

    def _set_running(self, running: bool) -> None:
        for widget in (
            self.start_button, self.add_files_button, self.add_folder_button,
            self.remove_button, self.clear_button, self.target_combo,
            self.output_edit, self.browse_button, self.bitrate_combo,
            self.recursive_check, self.overwrite_check,
        ):
            widget.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.table.setEnabled(True)
        self.statusBar().showMessage("转换中…" if running else "就绪")

    # ==================================================================
    # 七、其它交互
    # ==================================================================
    def on_open_output(self) -> None:
        folder = self._resolve_output_folder()
        if folder is None:
            QMessageBox.information(self, "提示", "还没有可打开的输出目录，请先设置输出目录或完成一次转换。")
            return
        self._safe_open_folder(folder)

    def _resolve_output_folder(self) -> Path | None:
        out_dir = self.output_dir()
        if out_dir and out_dir.exists():
            return out_dir
        if self.last_out_dir and self.last_out_dir.exists():
            return self.last_out_dir
        for item in self.items:
            if item.output:
                parent = Path(item.output).parent
                if parent.exists():
                    return parent
            if item.path.parent.exists():
                return item.path.parent
        return None

    def _safe_open_folder(self, folder: Path) -> None:
        try:
            open_folder(folder)
            LOG.info("已打开目录：%s", folder)
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "无法打开目录", f"{folder}\n\n{exc}")

    def on_show_environment(self) -> None:
        self.env = detect_environment(force=True)
        self._refresh_env_labels()
        dialog = EnvironmentDialog(self.env, self)
        dialog.redetect_button.clicked.connect(lambda: self._redetect(dialog))
        dialog.exec()

    def _redetect(self, dialog: EnvironmentDialog) -> None:
        self.env = detect_environment(force=True)
        self._refresh_env_labels()
        dialog.update_environment(self.env)
        QMessageBox.information(dialog, "检测完成", self.env.summary())

    def on_show_help(self) -> None:
        QMessageBox.information(
            self,
            "使用说明",
            "1. 点击「添加文件」或「添加文件夹」，也可以直接把文件 / 文件夹拖进窗口；\n"
            "2. 在「目标格式」中选择要输出的格式（音频格式与办公文档格式分两组）；\n"
            "3. 需要时选择输出目录；留空则输出到每个源文件所在目录；\n"
            "4. 音频目标可设置码率（默认 192k，无损格式自动忽略）；\n"
            "5. 点击「开始转换」，进度与日志会实时显示；转换中可随时「取消转换」；\n"
            "6. 类型不匹配、缺少依赖、目标与源相同的文件会被标记为「跳过」，不影响其它文件。\n\n"
            "提示：所有转换都在本机完成，文件不会上传到任何服务器。",
        )

    def on_about(self) -> None:
        env = self.env
        QMessageBox.about(
            self,
            f"关于 {APP_NAME}",
            f"<h3>{APP_NAME} v{APP_VERSION}</h3>"
            "<p>完全本地运行的批量格式转换工具：音频交给 <b>FFmpeg</b>，"
            "办公文档交给 <b>LibreOffice</b>，不上传任何文件。</p>"
            f"<p><b>依赖状态</b><br>FFmpeg：{'已就绪' if env.audio_ok else '未检测到'}<br>"
            f"LibreOffice：{'已就绪' if env.office_ok else '未检测到'}</p>"
            "<p>技术栈：Python + PySide6</p>",
        )

    def _refresh_env_labels(self) -> None:
        self.env_label.setText(self.env.summary())
        color = "#15803d" if (self.env.audio_ok and self.env.office_ok) else "#b45309"
        self.env_label.setStyleSheet(f"color:{color}; padding-right:12px;")
        self.env_label.setToolTip("\n".join(self.env.details()))

    def _update_buttons(self) -> None:
        has_items = bool(self.items)
        running = self.worker is not None and self.worker.isRunning()
        self.start_button.setEnabled(has_items and not running)
        self.remove_button.setEnabled(has_items and not running)
        self.clear_button.setEnabled(has_items and not running)
        self.open_output_button.setEnabled(not running)

    # ==================================================================
    # 八、拖拽支持
    # ==================================================================
    def _dropped_paths(self, event) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        return paths

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "提示", "正在转换中，请先取消或等待完成后再添加文件。")
            event.ignore()
            return
        LOG.info("拖入 %d 个路径", len(paths))
        self.add_paths(paths)
        event.acceptProposedAction()

    # ==================================================================
    # 九、退出清理
    # ==================================================================
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "正在转换",
                "仍有文件在转换中，确定要退出吗？\n未完成的转换将被终止。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(8000)

        try:
            logging.getLogger().removeHandler(self.log_handler)
        except (ValueError, AttributeError):
            pass
        LOG.info("%s 已退出", APP_NAME)
        event.accept()


def apply_style(app: QApplication) -> None:
    """应用统一的浅色主题。"""
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QWidget { font-size: 13px; color: #111827; }
        QGroupBox {
            border: 1px solid #d8dbe2; border-radius: 6px;
            margin-top: 10px; padding: 10px 8px 8px 8px; background: #ffffff;
        }
        QGroupBox::title {
            subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #374151;
        }
        QPushButton {
            background: #f3f4f6; border: 1px solid #cbd2dc; border-radius: 5px;
            padding: 5px 14px;
        }
        QPushButton:hover { background: #e8ebf0; }
        QPushButton:pressed { background: #dde1e8; }
        QPushButton:disabled { color: #9ca3af; background: #f7f8fa; }
        QPushButton#primaryButton {
            background: #2563eb; border: 1px solid #1d4ed8; color: #ffffff; font-weight: 600;
        }
        QPushButton#primaryButton:hover { background: #1d4ed8; }
        QPushButton#primaryButton:disabled { background: #bfdbfe; border-color: #bfdbfe; color: #eff6ff; }
        QTableWidget, QPlainTextEdit, QLineEdit, QComboBox {
            background: #ffffff; border: 1px solid #d8dbe2; border-radius: 4px;
            selection-background-color: #dbeafe; selection-color: #111827;
        }
        QLineEdit, QComboBox { padding: 4px 6px; }
        QHeaderView::section {
            background: #f3f4f6; border: none; border-right: 1px solid #e5e7eb;
            border-bottom: 1px solid #e5e7eb; padding: 5px 6px; font-weight: 600;
        }
        QProgressBar {
            border: 1px solid #d8dbe2; border-radius: 4px; background: #f3f4f6;
            text-align: center; height: 18px;
        }
        QProgressBar::chunk { background: #2563eb; border-radius: 3px; }
        QStatusBar { background: #f3f4f6; }
        """
    )
