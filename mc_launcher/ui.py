"""PySide6 desktop UI: launch, instances, downloads, accounts and settings."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # 可选依赖，只用来显示进程内存占用
    psutil = None

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont, QPaintEvent, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import loaders
from .auth import AuthError, offline_account
from .config import APP_NAME, GAME_DIR
from .install import install_version, is_file_status
from .java import JavaError, JavaInstallation, detect_java, inspect_java
from .launch import LaunchError, build_launch_spec, run_game
from .resources.mapper import version_key
from .mirror import (
    abort_downloads,
    enable_mirror,
    mirror_name,
    reset_downloads,
    set_mirror,
)
from .settings import (
    ISOLATION_FORCE_OFF,
    ISOLATION_FORCE_ON,
    ISOLATION_GLOBAL,
    MIRRORS,
    GameDirectory,
    Instance,
    load_settings,
    save_settings,
)
from .version import (
    CATEGORY_LABELS,
    CATEGORY_PRERELEASE,
    CATEGORY_RELEASE_CANDIDATE,
    CATEGORY_SNAPSHOT,
    DOWNLOAD_GROUPS,
    VersionError,
    classify_version,
    download_group,
    instance_version_info,
    list_versions,
    loader_label,
)

TAB_LAUNCH = 0
TAB_INSTANCES = 1
TAB_DOWNLOAD = 2
TAB_ACCOUNTS = 3
TAB_SETTINGS = 4

MEMORY_MIN = 256
MEMORY_MAX = 32768


def nested_tab_position(depth: int) -> QTabWidget.TabPosition:
    """Tag pages alternate: 1 层在上，2 层在左，3 层在上，4 层在左，以此类推。"""
    return QTabWidget.TabPosition.North if depth % 2 else QTabWidget.TabPosition.West


class SideTabBar(QTabBar):
    """左侧标签栏：文字保持正向，放不下就按字换行竖排。"""

    def _lines(self, index: int) -> list[str]:
        text = self.tabText(index)
        return [char for char in text] or [""]

    def tabSizeHint(self, index: int) -> QSize:
        metrics = self.fontMetrics()
        lines = self._lines(index)
        width = max(metrics.horizontalAdvance(line) for line in lines) + 22
        height = metrics.lineSpacing() * len(lines) + 16
        return QSize(width, height)

    def minimumTabSizeHint(self, index: int) -> QSize:
        return self.tabSizeHint(index)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionTab()
        for index in range(self.count()):
            self.initStyleOption(option, index)
            rect = option.rect
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)

            font = self.font()
            font.setBold(selected)
            painter.save()
            painter.setFont(font)
            painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
            metrics = painter.fontMetrics()
            lines = self._lines(index)
            total = metrics.lineSpacing() * len(lines)
            y = rect.top() + (rect.height() - total) // 2
            for line in lines:
                painter.drawText(
                    rect.left(),
                    y,
                    rect.width(),
                    metrics.lineSpacing(),
                    int(Qt.AlignmentFlag.AlignCenter),
                    line,
                )
                y += metrics.lineSpacing()
            painter.restore()


def installed_versions(game_dir: Path) -> list[str]:
    base = game_dir / "versions"
    if not base.is_dir():
        return []

    found: list[str] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        json_path = entry / f"{entry.name}.json"
        if not json_path.is_file():
            continue
        if not (entry / f"{entry.name}.jar").is_file():
            # Forge/Fabric and modpacks only ship a JSON that inheritsFrom a
            # vanilla version, so the client jar comes from the parent.
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not data.get("inheritsFrom"):
                continue
        found.append(entry.name)

    def key(name: str) -> tuple[tuple[int, ...], str]:
        return tuple(int(part) for part in re.findall(r"\d+", name)), name

    found.sort(key=key, reverse=True)
    return found


def split_jvm_args(text: str) -> list[str]:
    if not text.strip():
        return []
    return shlex.split(text, posix=(os.name != "nt"))


class InstallWorker(QThread):
    status_changed = Signal(str)
    progress_changed = Signal(int, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self, version: str, game_dir: Path, java_path: str | None = None, java_paths: list[str] | None = None
    ) -> None:
        super().__init__()
        self._version = version
        self._game_dir = game_dir
        self._java_path = java_path
        self._java_paths = java_paths
        self._maximum = 0
        self._printer = _QtPrinter(self)

    def run(self) -> None:
        try:
            result = install_version(
                self._version,
                game_dir=self._game_dir,
                printer=self._printer,
                java_path=self._java_path,
                java_paths=self._java_paths,
            )
        except Exception as exc:  # surface any failure to the UI
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class _QtPrinter:
    """Adapts install_version's progress callback to Qt signals."""

    def __init__(self, worker: InstallWorker) -> None:
        self._worker = worker

    def callback(self) -> dict[str, object]:
        return {
            "setStatus": self.set_status,
            "setProgress": self.set_progress,
            "setMax": self.set_max,
        }

    def set_status(self, text: str) -> None:
        if is_file_status(text):
            return
        self._worker.status_changed.emit(text)

    def set_max(self, maximum: int) -> None:
        self._worker._maximum = max(0, int(maximum))
        self._worker.progress_changed.emit(0, self._worker._maximum)

    def set_progress(self, value: int) -> None:
        self._worker.progress_changed.emit(int(value), self._worker._maximum)

    def finish(self) -> None:
        pass


class LaunchWorker(QThread):
    log_line = Signal(str)
    status_changed = Signal(str)
    process_started = Signal(object)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        version: str,
        username: str,
        game_dir: Path,
        java_path: str | None,
        max_memory: int,
        min_memory: int,
        extra_args: list[str],
        game_root: Path | None = None,
        java_candidates: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._version = version
        self._username = username
        self._game_dir = game_dir
        self._game_root = game_root or game_dir
        self._java_path = java_path
        self._java_candidates = java_candidates
        self._max_memory = max_memory
        self._min_memory = min_memory
        self._extra_args = extra_args
        self._process = None
        self._stop_requested = False

    def run(self) -> None:
        try:
            spec = build_launch_spec(
                self._version,
                self._username,
                game_dir=self._game_dir,
                game_root=self._game_root,
                java_path=self._java_path,
                memory_mb=self._max_memory,
                min_memory_mb=self._min_memory,
                extra_jvm_args=self._extra_args,
                java_candidates=self._java_candidates,
            )
            self.status_changed.emit(f"正在启动 {self._version}…")
            code = run_game(
                spec, on_line=self.log_line.emit, on_process=self._register_process
            )
        except (LaunchError, JavaError, AuthError, VersionError) as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # unexpected failures should still reach the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished_ok.emit(code)

    def _register_process(self, process) -> None:
        self._process = process
        self.process_started.emit(process)
        if self._stop_requested:
            process.terminate()

    def stop(self) -> None:
        self._stop_requested = True
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class VersionListWorker(QThread):
    versions_ready = Signal(list)
    failed = Signal(str)

    def run(self) -> None:
        try:
            versions = list_versions()
        except VersionError as exc:
            self.failed.emit(str(exc))
            return
        self.versions_ready.emit(versions)


class LoaderVersionsWorker(QThread):
    ready = Signal(list)
    failed = Signal(str)

    def __init__(self, loader_id: str, mc_version: str) -> None:
        super().__init__()
        self._loader_id = loader_id
        self._mc_version = mc_version

    def run(self) -> None:
        try:
            if self._mc_version:
                versions = loaders.loader_versions(self._loader_id, self._mc_version)
            else:
                versions = loaders.supported_minecraft_versions(self._loader_id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
            self.failed.emit(str(exc))
            return
        self.ready.emit(versions)


class LoaderInstallWorker(QThread):
    status_changed = Signal(str)
    progress_changed = Signal(int, int)
    finished_ok = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        loader_id: str,
        mc_version: str,
        game_dir: Path,
        loader_version: str | None = None,
    ) -> None:
        super().__init__()
        self._loader_id = loader_id
        self._mc_version = mc_version
        self._game_dir = game_dir
        self._loader_version = loader_version
        self._cancel = threading.Event()
        self._maximum = 0

    def run(self) -> None:
        def callback() -> dict[str, object]:
            return {
                "setStatus": self.status_changed.emit,
                "setProgress": lambda value: self.progress_changed.emit(
                    int(value), self._maximum
                ),
                "setMax": self._on_max,
            }

        try:
            version_id = loaders.install_loader(
                self._loader_id,
                self._mc_version,
                self._game_dir,
                loader_version=self._loader_version,
                callback=callback(),
                cancel=self._cancel,
            )
        except loaders.LoaderInstallCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(version_id)

    def _on_max(self, value: int) -> None:
        self._maximum = max(0, int(value))
        self.progress_changed.emit(0, self._maximum)

    def cancel(self) -> None:
        self._cancel.set()


class LoaderInstallDialog(QDialog):
    """Pick a loader + version for one MC version and install it with progress."""

    def __init__(
        self,
        parent,
        mc_version: str,
        game_dir: Path,
        current_loader: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("安装加载器")
        self.resize(480, 320)
        self.mc_version = mc_version
        self.game_dir = game_dir
        self.installed_version_id: str | None = None
        self._worker: LoaderInstallWorker | None = None
        self._versions_worker: LoaderVersionsWorker | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Minecraft 版本", QLabel(mc_version))

        self.loader_combo = QComboBox()
        for key, label in loaders.LOADERS:
            self.loader_combo.addItem(label, key)
        if current_loader:
            index = self.loader_combo.findData(current_loader)
            if index >= 0:
                self.loader_combo.setCurrentIndex(index)
        form.addRow("加载器", self.loader_combo)

        self.version_combo = QComboBox()
        form.addRow("加载器版本", self.version_combo)
        layout.addLayout(form)

        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: palette(mid);")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        layout.addWidget(self.log, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.install_button = QPushButton("安装")
        self.install_button.clicked.connect(self._install)
        buttons.addWidget(self.install_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.loader_combo.currentIndexChanged.connect(self._load_versions)
        self._load_versions()

    def _load_versions(self) -> None:
        loader_id = self.loader_combo.currentData()
        if not loader_id:
            return
        self.version_combo.clear()
        self.version_combo.addItem("正在获取版本…", None)
        self.version_combo.setEnabled(False)
        installed = loaders.installed_loader_version(loader_id, self.mc_version, self.game_dir)
        if installed:
            self.hint_label.setText(f"该版本已安装 {loader_id}：{installed}（可为其他实例重新安装）")
        else:
            self.hint_label.setText("正在读取可用加载器版本…")
        worker = LoaderVersionsWorker(loader_id, self.mc_version)
        worker.ready.connect(self._versions_ready)
        worker.failed.connect(self._versions_failed)
        self._versions_worker = worker
        worker.start()

    def _versions_ready(self, versions: list[str]) -> None:
        self.version_combo.clear()
        if not versions:
            self.version_combo.addItem("该游戏版本没有可用加载器版本", None)
            self.version_combo.setEnabled(False)
            self.install_button.setEnabled(False)
            self.hint_label.setText("该加载器未发布此 Minecraft 版本。")
            return
        for index, version in enumerate(versions):
            label = version
            if index == 0:
                label += "  （最新 / 推荐）"
            self.version_combo.addItem(label, version)
        self.version_combo.setCurrentIndex(0)
        self.version_combo.setEnabled(True)
        self.install_button.setEnabled(True)
        self.hint_label.setText(f"共 {len(versions)} 个可用版本，默认选择最新推荐版。")

    def _versions_failed(self, message: str) -> None:
        self.version_combo.clear()
        self.version_combo.addItem("加载失败", None)
        self.version_combo.setEnabled(False)
        self.install_button.setEnabled(False)
        self.hint_label.setText(f"版本列表获取失败：{message}")

    def _install(self) -> None:
        loader_version = self.version_combo.currentData()
        loader_id = self.loader_combo.currentData()
        if not loader_id or not loader_version:
            return
        self.install_button.setEnabled(False)
        self.loader_combo.setEnabled(False)
        self.version_combo.setEnabled(False)
        self.close_button.setEnabled(False)
        self.progress.setRange(0, 0)
        self.log.clear()
        self._append(f"开始安装 {loader_id} {loader_version}（MC {self.mc_version}）")

        worker = LoaderInstallWorker(
            loader_id, self.mc_version, self.game_dir, loader_version
        )
        worker.status_changed.connect(self._append)
        worker.progress_changed.connect(self._on_progress)
        worker.finished_ok.connect(self._installed)
        worker.failed.connect(self._install_failed)
        worker.cancelled.connect(self._install_cancelled)
        self._worker = worker
        worker.start()

    def _append(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _on_progress(self, value: int, maximum: int) -> None:
        if maximum > 0:
            self.progress.setRange(0, maximum)
            self.progress.setValue(value)

    def _installed(self, version_id: str) -> None:
        self.installed_version_id = version_id
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._append(f"安装完成：{version_id}")
        self.close_button.setEnabled(True)
        self.close_button.setText("完成")
        self.accept()

    def _install_failed(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._append(f"安装失败：{message}")
        self._reset_controls()

    def _install_cancelled(self) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._append("已取消安装")
        self._reset_controls()

    def _reset_controls(self) -> None:
        self.install_button.setEnabled(True)
        self.loader_combo.setEnabled(True)
        self.version_combo.setEnabled(True)
        self.close_button.setEnabled(True)

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(1500)
        if self._versions_worker is not None and self._versions_worker.isRunning():
            self._versions_worker.wait(3000)
        super().reject()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()

        # An explicit MC_MIRROR (for example from `--mirror`) wins on startup.
        env_mirror = os.environ.get("MC_MIRROR")
        if env_mirror:
            if env_mirror in MIRRORS:
                self.settings.mirror = env_mirror
        else:
            os.environ["MC_MIRROR"] = self.settings.mirror
        enable_mirror()

        self._install_worker: InstallWorker | None = None
        self._launch_worker: LaunchWorker | None = None
        self._version_worker: VersionListWorker | None = None
        self._java_installations: list[JavaInstallation] = []
        self._pending_download: tuple[str, Path] | None = None
        self._pending_loader: str | None = None
        self._loader_worker: LoaderInstallWorker | None = None
        self._download_loader_versions: set[str] | None = None
        self._download_loader_worker: LoaderVersionsWorker | None = None
        self._install_version = ""
        self._launching = False
        self._install_seconds = 0
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(1000)
        self._install_timer.timeout.connect(self._tick_install_timer)

        self._game_process = None
        self._game_started_at = 0.0
        self._process_expanded = False
        self._process_status = "未运行"
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(1000)
        self._process_timer.timeout.connect(self._refresh_process_panel)

        self._ensure_first_instance()
        self.setWindowTitle(f"{APP_NAME}  ·  镜像 {mirror_name()}")
        self.resize(980, 720)

        self._build_ui()
        self._detect_java()
        self._refresh_all()
        self._fetch_online_versions()

    # ------------------------------------------------------------------ pages

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(nested_tab_position(1))
        self.setCentralWidget(self.tabs)

        self.tabs.addTab(self._build_launch_page(), "启动")
        self.tabs.addTab(self._build_instances_page(), "版本管理")
        self.tabs.addTab(self._build_download_page(), "下载")
        self.tabs.addTab(self._build_accounts_page(), "账号管理")
        self.tabs.addTab(self._build_settings_page(), "设置")

    def _build_process_bar(self) -> QHBoxLayout:
        """启动页右上角的进程小窗。"""
        row = QHBoxLayout()
        row.addStretch(1)

        self.process_frame = QFrame()
        self.process_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.process_frame.setMaximumWidth(460)
        process_layout = QVBoxLayout(self.process_frame)
        process_layout.setContentsMargins(8, 4, 8, 4)
        process_layout.setSpacing(2)

        self.process_header = QPushButton("▸ 进程 · 空闲")
        self.process_header.setFlat(True)
        self.process_header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.process_header.setStyleSheet("text-align: left;")
        self.process_header.clicked.connect(self._toggle_process_details)
        process_layout.addWidget(self.process_header)

        self.process_details = QLabel("")
        self.process_details.setWordWrap(True)
        self.process_details.setTextFormat(Qt.TextFormat.RichText)
        self.process_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.process_details.setVisible(False)
        process_layout.addWidget(self.process_details)

        row.addWidget(self.process_frame)
        return row

    def _build_launch_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(14)
        layout.addLayout(self._build_process_bar())

        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setMinimumHeight(96)
        font = self.launch_button.font()
        font.setPointSize(22)
        self.launch_button.setFont(font)
        self.launch_button.clicked.connect(self._toggle_launch)
        layout.addWidget(self.launch_button)

        self.launch_info = QLabel("")
        self.launch_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.launch_info.setWordWrap(True)
        layout.addWidget(self.launch_info)

        self.launch_status = QLabel("就绪")
        self.launch_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.launch_status)

        self.launch_log = QPlainTextEdit()
        self.launch_log.setReadOnly(True)
        self.launch_log.setPlaceholderText("游戏日志会显示在这里")
        self.launch_log.setMaximumBlockCount(10000)
        log_font = QFont("Consolas")
        log_font.setStyleHint(QFont.StyleHint.Monospace)
        log_font.setPointSize(10)
        self.launch_log.setFont(log_font)
        layout.addWidget(self.launch_log, 2)
        layout.addStretch(1)
        return page

    def _build_instances_page(self) -> QWidget:
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(14)

        left = QVBoxLayout()
        self.instance_tree = QTreeWidget()
        self.instance_tree.setHeaderHidden(True)
        self.instance_tree.setColumnCount(1)
        self.instance_tree.currentItemChanged.connect(self._instance_selected)
        left.addWidget(self.instance_tree, 1)

        self.game_dir_add_button = QPushButton("添加游戏目录…")
        self.game_dir_add_button.clicked.connect(self._add_game_directory)
        left.addWidget(self.game_dir_add_button)

        self.instance_add_button = QPushButton("新建实例…")
        self.instance_add_button.clicked.connect(self._add_instance)
        left.addWidget(self.instance_add_button)

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        left_wrap.setMaximumWidth(380)
        outer.addWidget(left_wrap, 1)

        box = QGroupBox("实例设置")
        form = QFormLayout()
        form.setSpacing(8)

        self.inst_name_edit = QLineEdit()
        form.addRow("名称", self.inst_name_edit)

        self.inst_version_combo = QComboBox()
        self.inst_version_combo.setEditable(True)
        self.inst_version_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.inst_version_combo.currentTextChanged.connect(self._update_instance_loader)
        form.addRow("游戏版本", self.inst_version_combo)

        self.inst_loader_label = QLabel("—")
        form.addRow("加载器", self.inst_loader_label)

        self.inst_base_version_label = QLabel("—")
        form.addRow("实际版本", self.inst_base_version_label)

        self.inst_category_label = QLabel("—")
        form.addRow("分类", self.inst_category_label)

        dir_row = QHBoxLayout()
        self.inst_dir_edit = QLineEdit()
        self.inst_dir_edit.editingFinished.connect(self._reload_instance_versions)
        dir_row.addWidget(self.inst_dir_edit, 1)
        self.inst_dir_button = QPushButton("选择…")
        self.inst_dir_button.clicked.connect(self._browse_instance_dir)
        dir_row.addWidget(self.inst_dir_button)
        form.addRow("游戏目录", dir_row)

        self.inst_java_combo = QComboBox()
        form.addRow("Java", self.inst_java_combo)

        self.inst_isolation_label = QLabel("")
        self.inst_isolation_label.setWordWrap(True)
        form.addRow("实例隔离", self.inst_isolation_label)

        self.inst_memory_check = QCheckBox("单独设置内存（不跟随全局）")
        self.inst_memory_check.toggled.connect(self._toggle_instance_memory)
        form.addRow("", self.inst_memory_check)

        self.inst_max_spin = QSpinBox()
        self.inst_max_spin.setRange(MEMORY_MIN, MEMORY_MAX)
        self.inst_max_spin.setSingleStep(512)
        self.inst_max_spin.setSuffix(" MB")
        form.addRow("最大内存", self.inst_max_spin)

        self.inst_min_spin = QSpinBox()
        self.inst_min_spin.setRange(MEMORY_MIN, MEMORY_MAX)
        self.inst_min_spin.setSingleStep(512)
        self.inst_min_spin.setSuffix(" MB")
        form.addRow("最小内存", self.inst_min_spin)

        self.inst_args_edit = QLineEdit()
        self.inst_args_edit.setPlaceholderText("额外的 JVM 参数，例如 -XX:+UseG1GC")
        form.addRow("附加参数", self.inst_args_edit)

        box.setLayout(form)
        outer.addWidget(box, 2)

        right_actions = QVBoxLayout()
        outer.addLayout(right_actions)
        self.inst_save_button = QPushButton("保存修改")
        self.inst_save_button.clicked.connect(self._save_instance)
        right_actions.addWidget(self.inst_save_button)
        self.inst_default_button = QPushButton("设为默认")
        self.inst_default_button.clicked.connect(self._set_default_instance)
        right_actions.addWidget(self.inst_default_button)
        self.inst_launch_button = QPushButton("启动此实例")
        self.inst_launch_button.clicked.connect(self._launch_selected_instance)
        right_actions.addWidget(self.inst_launch_button)
        self.inst_loader_button = QPushButton("安装加载器…")
        self.inst_loader_button.clicked.connect(self._open_loader_dialog)
        right_actions.addWidget(self.inst_loader_button)
        self.inst_delete_button = QPushButton("删除实例")
        self.inst_delete_button.clicked.connect(self._delete_instance)
        right_actions.addWidget(self.inst_delete_button)
        right_actions.addStretch(1)
        self.inst_default_label = QLabel("")
        self.inst_default_label.setWordWrap(True)
        right_actions.addWidget(self.inst_default_label)
        return page

    def _build_download_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.download_top_tabs = QTabWidget()
        # 第 2 层（页面内层）标签放在左侧，文字不旋转、按字换行
        self.download_top_tabs.setTabBar(SideTabBar())
        self.download_top_tabs.setTabPosition(nested_tab_position(2))

        # ---- 原版游戏：四个分类的子标签 ----
        vanilla = QWidget()
        vanilla_layout = QVBoxLayout(vanilla)
        vanilla_layout.setContentsMargins(0, 0, 0, 0)
        vanilla_layout.setSpacing(8)

        top = QHBoxLayout()
        self.download_sub_tabs = QTabWidget()
        # 第 3 层（再内层）标签回到上方
        self.download_sub_tabs.setTabPosition(nested_tab_position(3))
        self.download_lists: dict[str, QListWidget] = {}
        self.snapshot_filter: QComboBox | None = None
        for group_label, _categories in DOWNLOAD_GROUPS:
            group_page = QWidget()
            group_layout = QVBoxLayout(group_page)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(6)

            if group_label == "预览版":
                self.snapshot_filter = QComboBox()
                self.snapshot_filter.addItem("全部", "")
                self.snapshot_filter.addItem("快照 Snapshot", CATEGORY_SNAPSHOT)
                self.snapshot_filter.addItem("预发布 PreRelease", CATEGORY_PRERELEASE)
                self.snapshot_filter.addItem(
                    "候选 ReleaseCandidate", CATEGORY_RELEASE_CANDIDATE
                )
                self.snapshot_filter.currentIndexChanged.connect(
                    self._apply_snapshot_filter
                )
                group_layout.addWidget(self.snapshot_filter)

            lst = QListWidget()
            lst.currentItemChanged.connect(self._download_selected)
            group_layout.addWidget(lst, 1)
            self.download_sub_tabs.addTab(group_page, group_label)
            self.download_lists[group_label] = lst
        top.addWidget(self.download_sub_tabs, 1)

        side = QVBoxLayout()
        self.download_refresh_button = QPushButton("刷新版本列表")
        self.download_refresh_button.clicked.connect(self._fetch_online_versions)
        side.addWidget(self.download_refresh_button)
        self.download_count_label = QLabel("正在获取版本列表…")
        self.download_count_label.setWordWrap(True)
        side.addWidget(self.download_count_label)
        side.addStretch(1)
        top.addLayout(side)
        vanilla_layout.addLayout(top, 1)

        loader_row = QHBoxLayout()
        loader_row.setSpacing(8)
        loader_row.addWidget(QLabel("加载器"))
        self.download_loader_combo = QComboBox()
        self.download_loader_combo.addItem("原版（无加载器）", "")
        for key, label in loaders.LOADERS:
            self.download_loader_combo.addItem(label, key)
        self.download_loader_combo.currentIndexChanged.connect(
            self._on_download_loader_changed
        )
        loader_row.addWidget(self.download_loader_combo, 1)
        self.download_loader_hint = QLabel("选择加载器后只显示其支持的游戏版本")
        self.download_loader_hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        loader_row.addWidget(self.download_loader_hint, 2)
        vanilla_layout.addLayout(loader_row)

        form = QFormLayout()
        form.setSpacing(8)
        self.download_name_edit = QLineEdit()
        self.download_name_edit.setPlaceholderText("下载完成后创建的实例名称")
        form.addRow("实例名称", self.download_name_edit)

        dir_row = QHBoxLayout()
        self.download_dir_edit = QLineEdit(str(GAME_DIR))
        dir_row.addWidget(self.download_dir_edit, 1)
        self.download_dir_button = QPushButton("选择…")
        self.download_dir_button.clicked.connect(self._browse_download_dir)
        dir_row.addWidget(self.download_dir_button)
        form.addRow("游戏目录", dir_row)
        vanilla_layout.addLayout(form)

        self.download_button = QPushButton("下载并创建实例")
        self.download_button.clicked.connect(self._download)
        vanilla_layout.addWidget(self.download_button)

        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 1)
        self.download_progress.setValue(0)
        vanilla_layout.addWidget(self.download_progress)

        self.download_status = QLabel("就绪")
        self.download_status.setWordWrap(True)
        vanilla_layout.addWidget(self.download_status)

        self.download_top_tabs.addTab(vanilla, "原版游戏")

        # ---- 资源下载：模组 / 整合包 / 光影 / 资源包 / 数据包 ----
        from .resources.browser import ResourceBrowser

        self.resource_browser = ResourceBrowser(lambda: self.settings)
        self.resource_browser.instances_changed.connect(self._refresh_all)
        self.download_top_tabs.addTab(self.resource_browser, "资源下载")

        layout.addWidget(self.download_top_tabs, 1)
        return page

    def _build_accounts_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.account_list = QListWidget()
        layout.addWidget(self.account_list, 1)

        add_row = QHBoxLayout()
        self.account_name_edit = QLineEdit()
        self.account_name_edit.setPlaceholderText("3-16 位字母、数字或下划线")
        add_row.addWidget(self.account_name_edit, 1)
        self.account_add_button = QPushButton("添加账号")
        self.account_add_button.clicked.connect(self._add_account)
        add_row.addWidget(self.account_add_button)
        layout.addLayout(add_row)

        action_row = QHBoxLayout()
        self.account_default_button = QPushButton("设为默认")
        self.account_default_button.clicked.connect(self._set_default_account)
        action_row.addWidget(self.account_default_button)
        self.account_delete_button = QPushButton("删除账号")
        self.account_delete_button.clicked.connect(self._delete_account)
        action_row.addWidget(self.account_delete_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.account_default_label = QLabel("")
        layout.addWidget(self.account_default_label)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        java_box = QGroupBox("Java")
        java_layout = QVBoxLayout(java_box)
        self.java_list = QListWidget()
        self.java_list.setMaximumHeight(160)
        java_layout.addWidget(self.java_list)

        java_buttons = QHBoxLayout()
        self.java_add_button = QPushButton("添加 Java…")
        self.java_add_button.clicked.connect(self._add_java)
        java_buttons.addWidget(self.java_add_button)
        self.java_refresh_button = QPushButton("重新检测")
        self.java_refresh_button.clicked.connect(self._detect_java_and_refresh)
        java_buttons.addWidget(self.java_refresh_button)
        java_buttons.addStretch(1)
        java_layout.addLayout(java_buttons)

        java_form = QFormLayout()
        self.default_java_combo = QComboBox()
        java_form.addRow("全局 Java", self.default_java_combo)
        java_layout.addLayout(java_form)
        layout.addWidget(java_box)

        mirror_box = QGroupBox("下载源")
        mirror_layout = QVBoxLayout(mirror_box)
        self.mirror_combo = QComboBox()
        self.mirror_combo.addItem("Mojang 官方源", "mojang")
        self.mirror_combo.addItem("BMCLAPI 镜像（国内推荐）", "bmclapi")
        mirror_layout.addWidget(self.mirror_combo)
        layout.addWidget(mirror_box)

        resource_box = QGroupBox("资源下载")
        resource_form = QFormLayout(resource_box)

        self.resource_source_combo = QComboBox()
        self.resource_source_combo.addItem("Modrinth（MCIM 镜像）", "modrinth")
        self.resource_source_combo.addItem("Modrinth 官方", "modrinth_official")
        self.resource_source_combo.addItem("CurseForge（MCIM 镜像）", "curseforge")
        resource_form.addRow("下载源", self.resource_source_combo)

        self.curseforge_key_edit = QLineEdit()
        self.curseforge_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.curseforge_key_edit.setPlaceholderText("可留空；填写后优先使用官方 API")
        resource_form.addRow("CurseForge API Key", self.curseforge_key_edit)

        self.resource_concurrency_spin = QSpinBox()
        self.resource_concurrency_spin.setRange(1, 6)
        self.resource_concurrency_spin.setSuffix(" 个")
        resource_form.addRow("请求并发数", self.resource_concurrency_spin)

        self.resource_page_mode_combo = QComboBox()
        self.resource_page_mode_combo.addItem("底部分页器", "pagination")
        self.resource_page_mode_combo.addItem("滚动到底自动加载", "scroll")
        resource_form.addRow("分页模式", self.resource_page_mode_combo)

        self.resource_chinese_check = QCheckBox("优先展示中文简介（镜像提供翻译时）")
        resource_form.addRow("", self.resource_chinese_check)

        resource_dir_row = QHBoxLayout()
        self.resource_dir_edit = QLineEdit()
        self.resource_dir_edit.setPlaceholderText("留空则使用默认实例的游戏目录")
        resource_dir_row.addWidget(self.resource_dir_edit, 1)
        self.resource_dir_button = QPushButton("选择…")
        self.resource_dir_button.clicked.connect(self._browse_resource_dir)
        resource_dir_row.addWidget(self.resource_dir_button)
        resource_form.addRow("下载目录", resource_dir_row)

        self.resource_proxy_edit = QLineEdit()
        self.resource_proxy_edit.setPlaceholderText("例如 http://127.0.0.1:7890，留空表示不使用代理")
        resource_form.addRow("代理", self.resource_proxy_edit)
        layout.addWidget(resource_box)

        isolation_box = QGroupBox("实例隔离")
        isolation_layout = QVBoxLayout(isolation_box)

        self.isolation_check = QCheckBox("启用实例隔离（全局）")
        self.isolation_check.setToolTip(
            "开启后每个实例使用 游戏目录/instances/实例名/ 作为自己的游戏根目录"
        )
        self.isolation_check.toggled.connect(self._global_isolation_toggled)
        isolation_layout.addWidget(self.isolation_check)

        isolation_layout.addWidget(QLabel("游戏目录（目录级设置优先于全局）："))
        self.game_dir_list = QListWidget()
        self.game_dir_list.setMaximumHeight(130)
        self.game_dir_list.currentItemChanged.connect(self._game_dir_selected)
        isolation_layout.addWidget(self.game_dir_list)

        self.iso_override_combo = QComboBox()
        self.iso_override_combo.addItem("跟随全局", ISOLATION_GLOBAL)
        self.iso_override_combo.addItem("强制开启", ISOLATION_FORCE_ON)
        self.iso_override_combo.addItem("强制关闭", ISOLATION_FORCE_OFF)
        self.iso_override_combo.currentIndexChanged.connect(self._override_changed)
        isolation_layout.addWidget(self.iso_override_combo)

        self.iso_effective_label = QLabel("")
        self.iso_effective_label.setWordWrap(True)
        isolation_layout.addWidget(self.iso_effective_label)
        layout.addWidget(isolation_box)

        memory_box = QGroupBox("全局内存")
        memory_form = QFormLayout(memory_box)
        self.global_max_spin = QSpinBox()
        self.global_max_spin.setRange(MEMORY_MIN, MEMORY_MAX)
        self.global_max_spin.setSingleStep(512)
        self.global_max_spin.setSuffix(" MB")
        memory_form.addRow("最大内存", self.global_max_spin)

        self.global_min_spin = QSpinBox()
        self.global_min_spin.setRange(MEMORY_MIN, MEMORY_MAX)
        self.global_min_spin.setSingleStep(512)
        self.global_min_spin.setSuffix(" MB")
        memory_form.addRow("最小内存", self.global_min_spin)
        layout.addWidget(memory_box)

        self.show_log_check = QCheckBox("在启动页面显示游戏日志")
        layout.addWidget(self.show_log_check)

        self.settings_save_button = QPushButton("保存设置")
        self.settings_save_button.clicked.connect(self._save_settings)
        layout.addWidget(self.settings_save_button)
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)
        return page

    # ----------------------------------------------------------- startup data

    def _ensure_first_instance(self) -> None:
        if self.settings.instances:
            return
        versions = installed_versions(GAME_DIR)
        if not versions:
            return
        self.settings.add_instance("默认实例", versions[0], GAME_DIR)
        save_settings(self.settings)

    def _detect_java(self) -> None:
        try:
            self._java_installations = detect_java(extra_paths=self.settings.java_paths)
        except JavaError:
            self._java_installations = []

    def _detect_java_and_refresh(self) -> None:
        self._detect_java()
        self._refresh_java_widgets()
        if self._java_installations:
            self.download_status.setText(f"检测到 {len(self._java_installations)} 个 Java")
        else:
            self.download_status.setText("未检测到 Java，可手动添加")

    def _refresh_all(self) -> None:
        self._refresh_java_widgets()
        self._refresh_instance_list()
        self._refresh_account_list()
        self._refresh_settings_widgets()
        self._refresh_launch_info()

    def _refresh_settings_widgets(self) -> None:
        index = self.mirror_combo.findData(self.settings.mirror)
        self.mirror_combo.setCurrentIndex(index if index >= 0 else 0)

        self.global_max_spin.setValue(self.settings.max_memory)
        self.global_min_spin.setValue(self.settings.min_memory)
        self.show_log_check.setChecked(self.settings.show_log)
        self.isolation_check.setChecked(self.settings.instance_isolation)
        self.launch_log.setVisible(self.settings.show_log)

        index = self.resource_source_combo.findData(self.settings.resource_source)
        self.resource_source_combo.setCurrentIndex(index if index >= 0 else 0)
        self.curseforge_key_edit.setText(self.settings.curseforge_api_key)
        self.resource_concurrency_spin.setValue(self.settings.resource_concurrency)
        index = self.resource_page_mode_combo.findData(self.settings.resource_page_mode)
        self.resource_page_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        self.resource_chinese_check.setChecked(self.settings.resource_prefer_chinese)
        self.resource_dir_edit.setText(self.settings.resource_download_dir or "")
        self.resource_proxy_edit.setText(self.settings.resource_proxy)
        self._refresh_game_dir_widgets()

    def _refresh_game_dir_widgets(self, select_id: str | None = None) -> None:
        if select_id is None:
            item = self.game_dir_list.currentItem()
            select_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if select_id is None:
            default_instance = self.settings.default_instance()
            if default_instance is not None:
                select_id = self.settings.game_dir_for_instance(default_instance).id

        labels = {
            ISOLATION_GLOBAL: "跟随全局",
            ISOLATION_FORCE_ON: "强制开启",
            ISOLATION_FORCE_OFF: "强制关闭",
        }
        self.game_dir_list.blockSignals(True)
        self.game_dir_list.clear()
        for entry in self.settings.game_dirs:
            item = QListWidgetItem(f"{entry.path}  ·  {labels[entry.isolation_override]}")
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            self.game_dir_list.addItem(item)
        self.game_dir_list.blockSignals(False)

        if self.settings.game_dirs:
            target_row = 0
            if select_id:
                for row in range(self.game_dir_list.count()):
                    if self.game_dir_list.item(row).data(Qt.ItemDataRole.UserRole) == select_id:
                        target_row = row
                        break
            self.game_dir_list.setCurrentRow(target_row)
        else:
            self.iso_override_combo.setCurrentIndex(0)
            self.iso_effective_label.setText("尚未添加游戏目录")

    def _selected_game_dir(self) -> GameDirectory | None:
        item = self.game_dir_list.currentItem()
        if item is None:
            return None
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        return next(
            (entry for entry in self.settings.game_dirs if entry.id == entry_id), None
        )

    def _game_dir_selected(self, *_args) -> None:
        entry = self._selected_game_dir()
        if entry is None:
            return
        self.iso_override_combo.blockSignals(True)
        index = self.iso_override_combo.findData(entry.isolation_override)
        self.iso_override_combo.setCurrentIndex(index if index >= 0 else 0)
        self.iso_override_combo.blockSignals(False)
        self._update_isolation_preview()

    def _override_changed(self, *_args) -> None:
        entry = self._selected_game_dir()
        if entry is None:
            return
        override = self.iso_override_combo.currentData() or ISOLATION_GLOBAL
        if entry.isolation_override != override:
            entry.isolation_override = override
            self._persist()
            self._refresh_game_dir_list_text(entry)
            self._refresh_instance_isolation_label()
        self._update_isolation_preview()

    def _refresh_game_dir_list_text(self, entry: GameDirectory) -> None:
        labels = {
            ISOLATION_GLOBAL: "跟随全局",
            ISOLATION_FORCE_ON: "强制开启",
            ISOLATION_FORCE_OFF: "强制关闭",
        }
        item = self.game_dir_list.currentItem()
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == entry.id:
            item.setText(f"{entry.path}  ·  {labels[entry.isolation_override]}")

    def _global_isolation_toggled(self, enabled: bool) -> None:
        # Update in memory so the preview reflects the toggle before saving.
        self.settings.instance_isolation = enabled
        self._update_isolation_preview()
        self._refresh_instance_isolation_label()

    def _update_isolation_preview(self) -> None:
        entry = self._selected_game_dir()
        if entry is None:
            self.iso_effective_label.setText("尚未添加游戏目录")
            return

        labels = {
            ISOLATION_GLOBAL: "跟随全局",
            ISOLATION_FORCE_ON: "强制开启",
            ISOLATION_FORCE_OFF: "强制关闭",
        }
        effective = self.settings.get_effective_isolation(entry.id)
        state = "开启" if effective else "关闭"
        self.iso_effective_label.setText(
            f"当前生效：{state}（目录设置：{labels[entry.isolation_override]}）"
        )

    def _refresh_java_widgets(self) -> None:
        manual = set(self.settings.java_paths)
        self.java_list.clear()
        if not self._java_installations:
            placeholder = QListWidgetItem("未检测到 Java")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.java_list.addItem(placeholder)
        for info in self._java_installations:
            mark = "（手动添加）" if str(info.home) in manual or str(info.executable) in manual else ""
            self.java_list.addItem(f"Java {info.version}{mark} · {info.executable}")

        self._fill_java_combo(self.default_java_combo, self.settings.default_java, follow_global=False)
        # Keep whatever the instance form currently shows instead of resetting it.
        self._fill_java_combo(self.inst_java_combo, self.inst_java_combo.currentData(), follow_global=True)

    def _fill_java_combo(
        self, combo: QComboBox, selected: str | None, follow_global: bool
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        if follow_global:
            combo.addItem("跟随全局设置", None)
        else:
            combo.addItem("自动（按版本选择）", None)

        for info in self._java_installations:
            combo.addItem(f"Java {info.version} · {info.executable}", str(info.executable))

        index = combo.findData(selected) if selected else 0
        if index < 0:
            combo.addItem(f"手动指定 · {selected}", selected)
            index = combo.count() - 1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _refresh_instance_list(
        self, select_id: str | None = None, select_dir_id: str | None = None
    ) -> None:
        """Rebuild the 游戏目录 → 实例 tree."""
        self.settings.sync_game_dirs()

        if select_id is None and select_dir_id is None:
            current = self.instance_tree.currentItem()
            data = current.data(0, Qt.ItemDataRole.UserRole) if current else None
            if data and data[0] == "instance":
                select_id = data[1]
            elif data and data[0] == "dir":
                select_dir_id = data[1]

        grouped: dict[str, list[Instance]] = {}
        for instance in self.settings.instances:
            directory = self.settings.game_dir_for_instance(instance)
            grouped.setdefault(directory.id, []).append(instance)

        self.instance_tree.blockSignals(True)
        self.instance_tree.clear()
        for entry in self.settings.game_dirs:
            children = grouped.get(entry.id, [])
            root = QTreeWidgetItem([f"{entry.path}（{len(children)} 个实例）"])
            root.setData(0, Qt.ItemDataRole.UserRole, ("dir", entry.id))
            root.setToolTip(0, f"{entry.path}（{len(children)} 个实例）")
            self.instance_tree.addTopLevelItem(root)

            for instance in children:
                suffix = "  [默认]" if instance.id == self.settings.default_instance_id else ""
                game_dir_path = Path(instance.game_dir).expanduser()
                loader = loader_label(instance.version, game_dir_path)
                base_version, category = instance_version_info(
                    instance.version, game_dir_path
                )
                text = (
                    f"{instance.name}  ·  {base_version}  ·  {category}  "
                    f"·  {loader}{suffix}"
                )
                child = QTreeWidgetItem([text])
                child.setData(0, Qt.ItemDataRole.UserRole, ("instance", instance.id))
                child.setToolTip(
                    0,
                    (
                        f"{instance.name}\n实际版本 {base_version}\n分类 {category}\n"
                        f"加载器 {loader}\n{instance.game_dir}"
                    ),
                )
                root.addChild(child)
            root.setExpanded(True)
        self.instance_tree.blockSignals(False)

        if not self._select_tree_node(select_id, select_dir_id):
            default = self.settings.default_instance()
            if default is not None:
                self._select_tree_node(default.id, None)
            elif self.settings.game_dirs:
                self._select_tree_node(None, self.settings.game_dirs[0].id)
            else:
                self._clear_instance_form("尚未添加游戏目录")
                self._set_instance_buttons_enabled(False)

        default = self.settings.default_instance()
        self.inst_default_label.setText(
            f"默认实例：{default.name}" if default else "尚未添加实例"
        )

    def _select_tree_node(
        self, instance_id: str | None, game_dir_id: str | None
    ) -> bool:
        for row in range(self.instance_tree.topLevelItemCount()):
            root = self.instance_tree.topLevelItem(row)
            data = root.data(0, Qt.ItemDataRole.UserRole)
            if game_dir_id and data[1] == game_dir_id:
                self.instance_tree.setCurrentItem(root)
                return True
            if instance_id is None:
                continue
            for index in range(root.childCount()):
                child = root.child(index)
                child_data = child.data(0, Qt.ItemDataRole.UserRole)
                if child_data[1] == instance_id:
                    self.instance_tree.setCurrentItem(child)
                    return True
        return False

    def _refresh_account_list(self) -> None:
        self.account_list.clear()
        for name in self.settings.accounts:
            try:
                uuid = offline_account(name).uuid
            except AuthError:
                uuid = "无效用户名"
            suffix = "  [默认]" if name == self.settings.default_account else ""
            self.account_list.addItem(f"{name}  ·  {uuid}{suffix}")

        current = self.settings.default_account
        self.account_default_label.setText(
            f"默认账号：{current}" if current else "尚未添加账号"
        )

    def _refresh_launch_info(self) -> None:
        instance = self.settings.default_instance()
        account = self.settings.default_account
        if instance:
            extra = "（独立内存）" if instance.max_memory else "（全局内存）"
            game_dir_path = Path(instance.game_dir).expanduser()
            loader = loader_label(instance.version, game_dir_path)
            base_version, category = instance_version_info(instance.version, game_dir_path)
            base = (
                f"默认实例：{instance.name}（{base_version} · {category} · {loader}）{extra}"
            )
        else:
            base = "尚未添加实例，请到「版本管理」或「下载」页面添加"
        account_text = f"账号：{account}" if account else "账号：未设置"
        self.launch_info.setText(f"{base}\n{account_text}")

    # -------------------------------------------------------------- instances

    def _instance_selected(self, *_args) -> None:
        item = self.instance_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data:
            self._clear_instance_form("尚未添加游戏目录")
            return

        if data[0] == "instance":
            instance = next(
                (entry for entry in self.settings.instances if entry.id == data[1]), None
            )
            if instance is not None:
                self._load_instance(instance)
                self._set_instance_buttons_enabled(True)
            else:
                self._clear_instance_form("实例已不存在")
                self._set_instance_buttons_enabled(False)
            return

        # A game directory is selected: it has no settings of its own here.
        count = sum(
            1
            for entry in self.settings.instances
            if self.settings.game_dir_for_instance(entry).id == data[1]
        )
        hint = (
            f"该游戏目录下有 {count} 个实例，请在左侧选择实例进行编辑"
            if count
            else "该游戏目录下还没有实例，点「新建实例…」创建"
        )
        self._clear_instance_form(hint)
        self._set_instance_buttons_enabled(False)

    def _clear_instance_form(self, hint: str) -> None:
        self.inst_name_edit.clear()
        self.inst_dir_edit.clear()
        self.inst_version_combo.blockSignals(True)
        self.inst_version_combo.clear()
        self.inst_version_combo.blockSignals(False)
        self.inst_java_combo.blockSignals(True)
        self.inst_java_combo.clear()
        self.inst_java_combo.blockSignals(False)
        self.inst_memory_check.setChecked(False)
        self.inst_max_spin.setValue(self.settings.max_memory)
        self.inst_min_spin.setValue(self.settings.min_memory)
        self.inst_args_edit.clear()
        self.inst_loader_label.setText("—")
        self.inst_base_version_label.setText("—")
        self.inst_category_label.setText("—")
        self.inst_isolation_label.setText(hint)

    def _set_instance_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.inst_save_button,
            self.inst_default_button,
            self.inst_launch_button,
            self.inst_loader_button,
            self.inst_delete_button,
        ):
            button.setEnabled(enabled)

    def _load_instance(self, instance: Instance) -> None:
        self.inst_name_edit.setText(instance.name)
        self.inst_dir_edit.setText(instance.game_dir)

        self.inst_version_combo.blockSignals(True)
        self.inst_version_combo.clear()
        versions = installed_versions(Path(instance.game_dir).expanduser())
        if instance.version and instance.version not in versions:
            versions.insert(0, instance.version)
        self.inst_version_combo.addItems(versions)
        self.inst_version_combo.setEditText(instance.version)
        self.inst_version_combo.blockSignals(False)

        self._fill_java_combo(
            self.inst_java_combo, instance.java_path, follow_global=True
        )

        override = instance.max_memory is not None or instance.min_memory is not None
        self.inst_memory_check.setChecked(override)
        self.inst_max_spin.setEnabled(override)
        self.inst_min_spin.setEnabled(override)
        self.inst_max_spin.setValue(instance.max_memory or self.settings.max_memory)
        self.inst_min_spin.setValue(instance.min_memory or self.settings.min_memory)
        self.inst_args_edit.setText(instance.jvm_args)
        self._update_instance_isolation_label(instance)
        self._update_instance_loader()

    def _update_instance_loader(self, *_args) -> None:
        version = self.inst_version_combo.currentText().strip()
        directory = self.inst_dir_edit.text().strip()
        if not version or not directory:
            self.inst_loader_label.setText("—")
            self.inst_base_version_label.setText("—")
            self.inst_category_label.setText("—")
            return
        game_dir = Path(directory).expanduser()
        self.inst_loader_label.setText(loader_label(version, game_dir))
        base_version, category = instance_version_info(version, game_dir)
        self.inst_base_version_label.setText(base_version)
        self.inst_category_label.setText(category)

    def _open_loader_dialog(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            QMessageBox.warning(self, "未选择实例", "请先在左侧选择一个实例")
            return
        record = self.settings.game_dir_for_instance(instance)
        if self.settings.get_effective_isolation(record.id):
            game_dir = self.settings.effective_game_root(instance)
        else:
            game_dir = Path(instance.game_dir).expanduser()
        base_version, _category = instance_version_info(
            instance.version, Path(instance.game_dir).expanduser()
        )
        mc_version = base_version if base_version and base_version != "—" else instance.version
        current_label = loader_label(instance.version, game_dir)
        current_loader = loaders.LOADER_IDS.get(current_label)

        dialog = LoaderInstallDialog(self, mc_version, game_dir, current_loader)
        if (
            dialog.exec() == QDialog.DialogCode.Accepted
            and dialog.installed_version_id
        ):
            instance.version = dialog.installed_version_id
            self._persist()
            self._refresh_instance_list(select_id=instance.id)
            self._refresh_launch_info()
            self.download_status.setText(
                f"实例「{instance.name}」已切换为 {dialog.installed_version_id}"
            )

    def _update_instance_isolation_label(self, instance: Instance) -> None:
        game_dir = self.settings.game_dir_for_instance(instance)
        effective = self.settings.get_effective_isolation(game_dir.id)
        override_labels = {
            ISOLATION_GLOBAL: "跟随全局",
            ISOLATION_FORCE_ON: "强制开启",
            ISOLATION_FORCE_OFF: "强制关闭",
        }
        if effective:
            root = self.settings.effective_game_root(instance)
            text = f"开启（{override_labels[game_dir.isolation_override]}）→ {root}"
        else:
            text = (
                f"关闭（{override_labels[game_dir.isolation_override]}）"
                f"→ 共享 {instance.game_dir}"
            )
        self.inst_isolation_label.setText(text)

    def _refresh_instance_isolation_label(self) -> None:
        instance = self._selected_instance()
        if instance is not None:
            self._update_instance_isolation_label(instance)

    def _toggle_instance_memory(self, enabled: bool) -> None:
        self.inst_max_spin.setEnabled(enabled)
        self.inst_min_spin.setEnabled(enabled)

    def _reload_instance_versions(self) -> None:
        directory = Path(self.inst_dir_edit.text().strip()).expanduser()
        current = self.inst_version_combo.currentText().strip()
        versions = installed_versions(directory)
        if current and current not in versions:
            versions.insert(0, current)
        self.inst_version_combo.clear()
        self.inst_version_combo.addItems(versions)
        if current:
            self.inst_version_combo.setEditText(current)

    def _selected_game_dir_entry(self) -> GameDirectory | None:
        item = self.instance_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data:
            return None
        if data[0] == "dir":
            return next(
                (entry for entry in self.settings.game_dirs if entry.id == data[1]), None
            )
        instance = self._selected_instance()
        return self.settings.game_dir_for_instance(instance) if instance else None

    def _instance_name_taken(
        self, game_dir: str, name: str, exclude_id: str | None = None
    ) -> bool:
        game_dir_id = self.settings.register_game_dir(game_dir).id
        for entry in self.settings.instances:
            if entry.id == exclude_id or entry.name != name:
                continue
            directory = self.settings.game_dir_for_instance(entry)
            if directory.id == game_dir_id:
                return True
        return False

    def _selected_instance(self) -> Instance | None:
        item = self.instance_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data or data[0] != "instance":
            return None
        instance_id = data[1]
        return next(
            (entry for entry in self.settings.instances if entry.id == instance_id), None
        )

    def _save_instance(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            QMessageBox.warning(self, "未选择实例", "请先在左侧选择一个实例")
            return

        name = self.inst_name_edit.text().strip()
        version = self.inst_version_combo.currentText().strip()
        game_dir = self.inst_dir_edit.text().strip()
        if not name or not version or not game_dir:
            QMessageBox.warning(self, "填写不完整", "名称、版本和游戏目录都不能为空")
            return

        if self._instance_name_taken(game_dir, name, exclude_id=instance.id):
            QMessageBox.warning(
                self,
                "名称重复",
                "同一游戏目录下的实例名必须唯一（隔离目录按实例名创建）。\n请换一个名称。",
            )
            return

        max_memory = self.inst_max_spin.value() if self.inst_memory_check.isChecked() else None
        min_memory = self.inst_min_spin.value() if self.inst_memory_check.isChecked() else None
        if max_memory is not None and min_memory is not None and min_memory > max_memory:
            QMessageBox.warning(self, "内存设置错误", "最小内存不能大于最大内存")
            return

        instance.name = name
        instance.version = version
        instance.game_dir = game_dir
        instance.max_memory = max_memory
        instance.min_memory = min_memory
        instance.jvm_args = self.inst_args_edit.text().strip()
        instance.java_path = self.inst_java_combo.currentData()
        self.settings.register_game_dir(instance.game_dir)

        self._persist()
        self._refresh_instance_list(select_id=instance.id)
        self._refresh_game_dir_widgets()
        self._refresh_launch_info()
        self.download_status.setText(f"已保存实例「{name}」")

    def _add_game_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择游戏目录（.minecraft 根目录）"
        )
        if not directory:
            return

        path = Path(directory).expanduser()
        entry = self.settings.register_game_dir(path)

        versions = installed_versions(path)
        created = self.settings.import_instances(path, versions)

        self._persist()
        select_id = created[0].id if created else None
        self._refresh_instance_list(select_id=select_id, select_dir_id=entry.id)
        self._refresh_game_dir_widgets(select_id=entry.id)
        self._refresh_launch_info()

        if not versions:
            self.download_status.setText(
                "已添加游戏目录，但还没有已安装版本，请先到「下载」页面下载"
            )
            return

        mode = "隔离" if self.settings.get_effective_isolation(entry.id) else "共享"
        parts = [f"已添加游戏目录：发现 {len(versions)} 个版本，{mode}模式"]
        if created:
            parts.append(f"新建 {len(created)} 个实例")
        else:
            parts.append("实例已存在，未重复创建")
        self.download_status.setText("，".join(parts))

    def _add_instance(self) -> None:
        entry = self._selected_game_dir_entry()
        if entry is None:
            if not self.settings.game_dirs:
                QMessageBox.warning(
                    self, "没有游戏目录", "请先点「添加游戏目录…」选择一个 .minecraft 目录"
                )
                return
            labels = [item.path for item in self.settings.game_dirs]
            choice, ok = QInputDialog.getItem(
                self, "选择游戏目录", "在哪个游戏目录下创建实例", labels, 0, False
            )
            if not ok or not choice:
                return
            entry = self.settings.game_dirs[labels.index(choice)]

        directory = Path(entry.path).expanduser()
        versions = installed_versions(directory)
        if not versions:
            QMessageBox.warning(
                self,
                "没有可用版本",
                f"{entry.path} 下没有已安装的版本。\n请先在「下载」页面下载一个版本。",
            )
            return

        version, ok = QInputDialog.getItem(self, "选择版本", "游戏版本", versions, 0, False)
        if not ok or not version:
            return

        default_name = version
        suffix = 2
        while self._instance_name_taken(entry.path, default_name):
            default_name = f"{version} ({suffix})"
            suffix += 1

        name, ok = QInputDialog.getText(self, "实例名称", "实例名称", text=default_name)
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        if self._instance_name_taken(entry.path, name):
            QMessageBox.warning(
                self, "名称重复", "同一游戏目录下的实例名必须唯一，请换一个名称"
            )
            return

        instance = self.settings.add_instance(name, version, entry.path)
        self._persist()
        self._refresh_instance_list(select_id=instance.id)
        self._refresh_game_dir_widgets()
        self._refresh_launch_info()
        self.download_status.setText(f"已新建实例「{instance.name}」")

    def _browse_instance_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择游戏目录", self.inst_dir_edit.text()
        )
        if directory:
            self.inst_dir_edit.setText(directory)
            self._reload_instance_versions()

    def _set_default_instance(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            return
        self.settings.set_default_instance(instance.id)
        self._persist()
        self._refresh_instance_list(select_id=instance.id)
        self._refresh_launch_info()
        self.download_status.setText(f"默认实例设为「{instance.name}」")

    def _delete_instance(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            return
        answer = QMessageBox.question(
            self,
            "删除实例",
            f"删除实例「{instance.name}」？\n只会移除配置，不会删除游戏文件。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.remove_instance(instance.id)
        self._persist()
        self._refresh_instance_list()
        self._refresh_launch_info()
        self.download_status.setText("已删除实例")

    # -------------------------------------------------------------- downloads

    def _current_download_list(self) -> QListWidget | None:
        index = self.download_sub_tabs.currentIndex()
        if index < 0:
            return None
        return self.download_lists.get(self.download_sub_tabs.tabText(index))

    def _apply_snapshot_filter(self, *_args) -> None:
        if self.snapshot_filter is None:
            return
        self._apply_download_filters()

    def _on_download_loader_changed(self) -> None:
        loader_id = self.download_loader_combo.currentData() or ""
        self._download_loader_versions = None
        if not loader_id:
            self.download_loader_hint.setText("原版模式：显示全部官方版本")
            self._apply_download_filters()
            return
        self.download_loader_hint.setText("正在获取该加载器支持的游戏版本…")
        worker = LoaderVersionsWorker(loader_id, "")
        worker.ready.connect(
            lambda versions: self._loader_versions_ready(loader_id, versions)
        )
        worker.failed.connect(
            lambda message: self.download_loader_hint.setText(
                f"加载器版本获取失败：{message}"
            )
        )
        self._download_loader_worker = worker
        worker.start()

    def _loader_versions_ready(self, loader_id: str, versions: list[str]) -> None:
        if (self.download_loader_combo.currentData() or "") != loader_id:
            return
        self._download_loader_versions = set(versions)
        self.download_loader_hint.setText(
            f"{self.download_loader_combo.currentText()} 支持 {len(versions)} 个游戏版本"
        )
        self._apply_download_filters()

    def _apply_download_filters(self) -> None:
        wanted = self.snapshot_filter.currentData() if self.snapshot_filter else ""
        allowed = self._download_loader_versions
        for group, lst in self.download_lists.items():
            visible_without_loader: list[QListWidgetItem] = []
            for row in range(lst.count()):
                item = lst.item(row)
                base = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
                category = item.data(Qt.ItemDataRole.UserRole)
                visible = not wanted or category == wanted
                if allowed is not None and base not in allowed:
                    visible = False
                item.setHidden(not visible)
                if visible and group == "正式版":
                    visible_without_loader.append(item)

            # 标记最新 / 推荐（加载器模式下）
            if allowed is not None and visible_without_loader:
                bases = [
                    entry.data(Qt.ItemDataRole.UserRole + 1) or entry.text()
                    for entry in visible_without_loader
                ]
                newest = max(bases, key=version_key)
                stable = [
                    name
                    for name in bases
                    if not re.search(r"(?:snapshot|pre|rc)", name, re.IGNORECASE)
                ]
                recommended = max(stable, key=version_key) if stable else newest
                for entry in visible_without_loader:
                    base = entry.data(Qt.ItemDataRole.UserRole + 1) or entry.text()
                    if base == newest == recommended:
                        entry.setText(f"{base}  ·  推荐 / 最新")
                    elif base == newest:
                        entry.setText(f"{base}  ·  最新")
                    elif base == recommended:
                        entry.setText(f"{base}  ·  推荐")
                    else:
                        entry.setText(base)
            else:
                for entry in visible_without_loader:
                    base = entry.data(Qt.ItemDataRole.UserRole + 1)
                    if base:
                        entry.setText(base)


    def _download_selected(self, *_args) -> None:
        lst = self._current_download_list()
        item = lst.currentItem() if lst is not None else None
        if item is None:
            return
        version = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        self.download_name_edit.setText(version)

    def _browse_download_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择实例目录", self.download_dir_edit.text()
        )
        if directory:
            self.download_dir_edit.setText(directory)

    def _browse_resource_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择资源下载目录", self.resource_dir_edit.text() or str(GAME_DIR)
        )
        if directory:
            self.resource_dir_edit.setText(directory)

    def _download(self) -> None:
        if self._loader_worker is not None and self._loader_worker.isRunning():
            self._loader_worker.cancel()
            self.download_button.setEnabled(False)
            self.download_button.setText("正在取消…")
            self.download_status.setText("正在取消加载器安装…")
            return

        if self._install_worker is not None and self._install_worker.isRunning():
            abort_downloads()
            self.download_button.setEnabled(False)
            self.download_button.setText("正在取消…")
            self.download_status.setText("正在取消安装，已下载的文件会保留…")
            return

        lst = self._current_download_list()
        item = lst.currentItem() if lst is not None else None
        if item is None:
            QMessageBox.warning(self, "未选择版本", "请先在列表中选择要下载的版本")
            return

        version = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        name = self.download_name_edit.text().strip() or version
        directory = Path(self.download_dir_edit.text().strip() or str(GAME_DIR)).expanduser()

        self._install_version = version
        self._pending_download = (name, directory)
        self._pending_loader = self.download_loader_combo.currentData() or None
        self._set_install_running(True)
        self.download_status.setText(f"正在安装 {version} 到 {directory}…")
        self.download_progress.setRange(0, 0)
        self._refresh_process_panel()

        worker = InstallWorker(
            version, directory, self.settings.default_java, self.settings.java_paths
        )
        worker.status_changed.connect(self._install_status)
        worker.progress_changed.connect(self._install_progress)
        worker.finished_ok.connect(self._install_done)
        worker.failed.connect(self._install_failed)
        self._install_worker = worker
        worker.start()
        self._refresh_process_panel()

    def _install_status(self, text: str) -> None:
        self.download_status.setText(text)

    def _install_progress(self, value: int, maximum: int) -> None:
        if maximum > 0:
            self.download_progress.setRange(0, maximum)
            self.download_progress.setValue(value)

    def _install_done(self, result) -> None:
        reset_downloads()
        self._refresh_process_panel()
        pending = self._pending_download
        loader_id = self._pending_loader
        self._pending_loader = None
        if loader_id and pending is not None:
            self._run_loader_after_install(loader_id, result.version, pending, result)
            return
        self._set_install_running(False)
        self._pending_download = None
        self._install_version = ""
        self._create_instance_after_install(result.version, pending, result)

    def _run_loader_after_install(
        self, loader_id: str, mc_version: str, pending, result
    ) -> None:
        name, directory = pending
        label = loaders.LOADER_LABELS.get(loader_id, loader_id)
        self.download_status.setText(f"原版已安装，正在安装 {label}…")
        self.download_progress.setRange(0, 0)
        worker = LoaderInstallWorker(loader_id, mc_version, directory)
        worker.status_changed.connect(self._install_status)
        worker.progress_changed.connect(self._install_progress)
        worker.failed.connect(self._install_failed)
        worker.cancelled.connect(lambda: self._install_failed("安装已取消"))
        worker.finished_ok.connect(
            lambda version_id: self._loader_after_install_done(version_id, pending, result)
        )
        self._loader_worker = worker
        worker.start()
        self._refresh_process_panel()

    def _loader_after_install_done(self, version_id: str, pending, result) -> None:
        self._loader_worker = None
        self._set_install_running(False)
        self._pending_download = None
        self._install_version = ""
        self._create_instance_after_install(version_id, pending, result)

    def _create_instance_after_install(self, version_id: str, pending, result) -> None:
        instance = None
        if pending is not None:
            name, directory = pending
            instance = self.settings.add_instance(name, version_id, directory)
            self._persist()

        java_text = str(result.java_executable) if result.java_executable else "跳过（使用已有 Java）"
        self.download_status.setText(f"安装完成：{version_id}")
        self._append_launch_log(
            f"[install] {version_id} 完成，client.jar 与 assets 已校验；JRE：{java_text}"
        )

        self._refresh_instance_list(select_id=instance.id if instance else None)
        self._refresh_game_dir_widgets()
        self._refresh_launch_info()
        if instance is not None:
            QMessageBox.information(
                self, "安装完成", f"已创建实例「{instance.name}」（{version_id}）"
            )
            self.tabs.setCurrentIndex(TAB_INSTANCES)

    def _install_failed(self, message: str) -> None:
        self._pending_download = None
        self._pending_loader = None
        self._loader_worker = None
        self._install_version = ""
        reset_downloads()
        self._set_install_running(False)
        self._refresh_process_panel()
        if message == "安装已取消":
            self.download_status.setText("已取消安装（已下载的文件会保留，可再次点击继续）")
            self._append_launch_log("[install] 用户取消了安装")
            return
        self.download_status.setText("安装失败")
        self._append_launch_log(f"[error] {message}")
        QMessageBox.critical(self, "安装失败", message)

    def _set_install_running(self, running: bool) -> None:
        self.download_button.setEnabled(True)
        self.download_button.setText("取消安装" if running else "下载并创建实例")
        self.download_refresh_button.setEnabled(not running)
        self.launch_button.setEnabled(not running)
        self.inst_launch_button.setEnabled(
            not running and self._selected_instance() is not None
        )
        self.instance_add_button.setEnabled(not running)
        self.game_dir_add_button.setEnabled(not running)
        if running:
            self.download_progress.setRange(0, 0)
            self._start_install_timer()
        else:
            self._stop_install_timer()
            self._reset_progress()

    def _start_install_timer(self) -> None:
        self._install_seconds = 0
        self.download_progress.setFormat("已用 0 秒  %p%")
        self._install_timer.start()

    def _stop_install_timer(self) -> None:
        self._install_timer.stop()
        self.download_progress.setFormat("%p%")

    def _tick_install_timer(self) -> None:
        self._install_seconds += 1
        self.download_progress.setFormat(f"已用 {self._install_seconds} 秒  %p%")
        self._refresh_process_panel()

    def _reset_progress(self) -> None:
        self.download_progress.setRange(0, 1)
        self.download_progress.setValue(0)

    def _fetch_online_versions(self) -> None:
        if self._version_worker is not None and self._version_worker.isRunning():
            return
        self.download_refresh_button.setEnabled(False)
        self.download_count_label.setText("正在获取版本列表…")
        worker = VersionListWorker()
        worker.versions_ready.connect(self._online_versions_ready)
        worker.failed.connect(self._online_versions_failed)
        self._version_worker = worker
        worker.start()
        self._refresh_process_panel()

    def _online_versions_ready(self, versions: list[dict]) -> None:
        self.download_refresh_button.setEnabled(True)
        self._refresh_process_panel()

        grouped: dict[str, list[tuple[str, str]]] = {
            label: [] for label, _categories in DOWNLOAD_GROUPS
        }
        for entry in versions:
            version_id = str(entry.get("id", ""))
            if not version_id:
                continue
            category = classify_version(version_id, entry.get("type"))
            group = download_group(category)
            if group is None:
                self._append_launch_log(f"[warn] 无法分类的版本：{version_id}（{category}）")
                continue
            grouped[group].append((version_id, category))

        total = 0
        for group, entries in grouped.items():
            entries.sort(
                key=lambda pair: (
                    tuple(int(part) for part in re.findall(r"\d+", pair[0])),
                    pair[0],
                ),
                reverse=True,
            )
            lst = self.download_lists[group]
            lst.clear()
            for version_id, category in entries:
                item = QListWidgetItem(version_id)
                item.setData(Qt.ItemDataRole.UserRole, category)
                item.setData(Qt.ItemDataRole.UserRole + 1, version_id)
                item.setToolTip(CATEGORY_LABELS.get(category, category))
                lst.addItem(item)
            total += len(entries)

        self._apply_snapshot_filter()
        self.download_count_label.setText(f"共 {total} 个版本")

        for index, (group, _categories) in enumerate(DOWNLOAD_GROUPS):
            lst = self.download_lists[group]
            if lst.count():
                self.download_sub_tabs.setCurrentIndex(index)
                lst.setCurrentRow(0)
                break

    def _online_versions_failed(self, message: str) -> None:
        self.download_refresh_button.setEnabled(True)
        self.download_count_label.setText("版本列表获取失败，可点「刷新版本列表」重试")
        self._append_launch_log(f"[error] {message}")
        self._refresh_process_panel()

    # --------------------------------------------------------------- accounts

    def _add_account(self) -> None:
        raw = self.account_name_edit.text().strip()
        try:
            account = offline_account(raw)
        except AuthError as exc:
            QMessageBox.warning(self, "用户名无效", str(exc))
            return

        if account.username in self.settings.accounts:
            QMessageBox.information(self, "账号已存在", f"账号「{account.username}」已在列表中")
            return

        self.settings.accounts.append(account.username)
        if not self.settings.default_account:
            self.settings.default_account = account.username
        self.settings.resolve_defaults()
        self._persist()
        self.account_name_edit.clear()
        self._refresh_account_list()
        self._refresh_launch_info()
        self.download_status.setText(f"已添加账号「{account.username}」")

    def _set_default_account(self) -> None:
        item = self.account_list.currentItem()
        if item is None:
            return
        name = item.text().split("  ·  ", 1)[0].replace("  [默认]", "")
        if name in self.settings.accounts:
            self.settings.default_account = name
            self._persist()
            self._refresh_account_list()
            self._refresh_launch_info()

    def _delete_account(self) -> None:
        item = self.account_list.currentItem()
        if item is None:
            return
        name = item.text().split("  ·  ", 1)[0].replace("  [默认]", "")
        answer = QMessageBox.question(self, "删除账号", f"删除离线账号「{name}」？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.accounts = [entry for entry in self.settings.accounts if entry != name]
        self.settings.resolve_defaults()
        self._persist()
        self._refresh_account_list()
        self._refresh_launch_info()

    # --------------------------------------------------------------- settings

    def _add_java(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择 Java 主目录（包含 bin/java）")
        if not directory:
            return
        try:
            info = inspect_java(directory)
        except JavaError as exc:
            QMessageBox.critical(self, "Java 无效", str(exc))
            return

        home = str(info.home)
        if info.executable == Path(directory):
            home = directory
        if home not in self.settings.java_paths:
            self.settings.java_paths.append(home)
            self._persist()

        self._detect_java()
        self._refresh_java_widgets()
        self.download_status.setText(f"已添加 Java {info.version}")

    def _save_settings(self) -> None:
        max_memory = self.global_max_spin.value()
        min_memory = self.global_min_spin.value()
        if min_memory > max_memory:
            QMessageBox.warning(self, "内存设置错误", "最小内存不能大于最大内存")
            return

        self.settings.max_memory = max_memory
        self.settings.min_memory = min_memory
        self.settings.show_log = self.show_log_check.isChecked()
        self.settings.instance_isolation = self.isolation_check.isChecked()
        self.settings.default_java = self.default_java_combo.currentData()
        self.settings.mirror = self.mirror_combo.currentData() or "mojang"
        self.settings.resource_source = self.resource_source_combo.currentData() or "modrinth"
        self.settings.curseforge_api_key = self.curseforge_key_edit.text().strip()
        self.settings.resource_concurrency = self.resource_concurrency_spin.value()
        self.settings.resource_page_mode = (
            self.resource_page_mode_combo.currentData() or "pagination"
        )
        self.settings.resource_prefer_chinese = self.resource_chinese_check.isChecked()
        directory = self.resource_dir_edit.text().strip()
        self.settings.resource_download_dir = directory or None
        self.settings.resource_proxy = self.resource_proxy_edit.text().strip()
        set_mirror(self.settings.mirror)
        enable_mirror()
        self._persist()
        if hasattr(self, "resource_browser"):
            self.resource_browser.apply_settings()

        self.launch_log.setVisible(self.settings.show_log)
        self.setWindowTitle(f"{APP_NAME}  ·  镜像 {mirror_name()}")
        self._refresh_launch_info()
        self._update_isolation_preview()
        self._refresh_instance_isolation_label()
        self.download_status.setText(f"设置已保存（下载源：{mirror_name()}）")

    def _persist(self) -> None:
        try:
            save_settings(self.settings)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"无法写入配置：{exc}")

    # ----------------------------------------------------------------- launch

    def _toggle_launch(self) -> None:
        if self._launching:
            self._stop_launch()
            return
        self._start_launch(self.settings.default_instance())

    def _launch_selected_instance(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            QMessageBox.warning(self, "未选择实例", "请先在左侧选择一个实例")
            return
        self._start_launch(instance)

    def _resolve_game_root(self, instance: Instance) -> tuple[bool, Path]:
        """Apply the isolation priority and create the root when isolated."""
        record = self.settings.game_dir_for_instance(instance)
        isolated = self.settings.get_effective_isolation(record.id)
        root = self.settings.effective_game_root(instance)
        if isolated:
            root.mkdir(parents=True, exist_ok=True)
        return isolated, root

    def _start_launch(self, instance: Instance | None) -> None:
        if self._launching:
            QMessageBox.information(self, "正在运行", "游戏已经在运行")
            return
        if instance is None:
            QMessageBox.warning(
                self, "没有实例", "请先到「下载」页面下载一个版本，或在「版本管理」添加实例"
            )
            return

        username = self.settings.default_account
        if not username:
            QMessageBox.warning(
                self,
                "没有账号",
                "请先在「账号管理」页面添加一个离线账号并设为默认",
            )
            self.tabs.setCurrentIndex(TAB_ACCOUNTS)
            return

        max_memory = instance.max_memory or self.settings.max_memory
        min_memory = instance.min_memory or self.settings.min_memory
        java_path = instance.java_path or self.settings.default_java
        extra_args = split_jvm_args(instance.jvm_args)
        install_dir = Path(instance.game_dir).expanduser()

        # Isolation: the install directory keeps versions/libraries/assets,
        # only the actual --gameDir moves into instances/<name>/.
        isolated, game_root = self._resolve_game_root(instance)
        self._persist()

        isolation_text = "开启 → " + str(game_root) if isolated else "关闭 → 共享游戏目录"
        self._append_launch_log(
            f"[launcher] 启动实例「{instance.name}」：{instance.version}，"
            f"用户 {username}，内存 {min_memory}-{max_memory} MB，实例隔离：{isolation_text}"
        )
        self.launch_status.setText(f"正在启动 {instance.version}…")

        worker = LaunchWorker(
            instance.version,
            username,
            install_dir,
            java_path,
            max_memory,
            min_memory,
            extra_args,
            game_root=game_root,
            java_candidates=None if java_path else self.settings.java_paths,
        )
        worker.log_line.connect(self._append_launch_log)
        worker.status_changed.connect(self.launch_status.setText)
        worker.process_started.connect(self._game_process_started)
        worker.finished_ok.connect(self._launch_done)
        worker.failed.connect(self._launch_failed)
        self._launch_worker = worker
        self._set_launching(True)
        self._game_process = None
        self._game_started_at = 0.0
        self._process_status = "正在启动…"
        self._refresh_process_panel()
        worker.start()

    def _set_launching(self, launching: bool) -> None:
        self._launching = launching
        self.launch_button.setText("结束游戏" if launching else "启动游戏")
        self.inst_launch_button.setEnabled(
            not launching and self._selected_instance() is not None
        )
        if launching:
            self.launch_button.setEnabled(True)

    def _launch_done(self, code: int) -> None:
        self._set_launching(False)
        self.launch_status.setText(f"游戏已退出（代码 {code}）")
        self._append_launch_log(f"[launcher] Minecraft 退出，代码 {code}")
        self._game_process = None
        self._process_status = f"已退出（代码 {code}）"
        self._process_timer.stop()
        self._refresh_process_panel()

    def _launch_failed(self, message: str) -> None:
        self._set_launching(False)
        self.launch_status.setText("启动失败")
        self._append_launch_log(f"[error] {message}")
        self._game_process = None
        self._process_status = "启动失败"
        self._process_timer.stop()
        self._refresh_process_panel()
        QMessageBox.critical(self, "启动失败", message)

    def _stop_launch(self) -> None:
        if self._launch_worker is not None and self._launch_worker.isRunning():
            self._launch_worker.stop()
            self.launch_status.setText("正在停止…")

    # ------------------------------------------------------------ process panel

    def _game_process_started(self, process) -> None:
        self._game_process = process
        self._game_started_at = time.time()
        self._process_timer.start()
        self._refresh_process_panel()

    def _toggle_process_details(self) -> None:
        self._process_expanded = not self._process_expanded
        self._refresh_process_panel()

    def _refresh_process_panel(self) -> None:
        arrow = "▾" if self._process_expanded else "▸"
        active: list[str] = []

        process = self._game_process
        if process is not None and process.poll() is None:
            active.append(f"游戏 PID {process.pid}")
        elif self._launching:
            active.append("游戏启动中")
        if self._install_running():
            active.append("下载中")
        if self._version_running():
            active.append("获取版本列表")

        summary = " · ".join(active) if active else "空闲"
        self.process_header.setText(f"{arrow} 进程 · {summary}")
        self._sync_process_details(self._process_detail_html())

    def _sync_process_details(self, text: str) -> None:
        self.process_details.setText(text)
        self.process_details.setVisible(self._process_expanded and bool(text))

    def _install_running(self) -> bool:
        return self._install_worker is not None and self._install_worker.isRunning()

    def _version_running(self) -> bool:
        return self._version_worker is not None and self._version_worker.isRunning()

    def _process_detail_html(self) -> str:
        blocks: list[str] = []

        process = self._game_process
        if process is not None and process.poll() is None:
            elapsed = time.time() - self._game_started_at
            parts = [f"PID {process.pid}", f"运行 {self._format_elapsed(elapsed)}"]
            if psutil is not None:
                try:
                    rss = psutil.Process(process.pid).memory_info().rss / (1024 * 1024)
                    parts.append(f"内存 {rss:.0f} MB")
                except Exception:  # 进程刚好退出或没有权限
                    pass
            blocks.append("<b>游戏进程</b>：" + " · ".join(parts))
            args = process.args
            command = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
            if len(command) > 200:
                command = command[:200] + "…"
            blocks.append(f"命令行：{command}")
        elif self._launching:
            blocks.append("<b>游戏进程</b>：正在启动…")
        else:
            blocks.append(f"<b>游戏进程</b>：{self._process_status}")

        if self._install_running():
            maximum = self.download_progress.maximum()
            if maximum > 0:
                progress = f"{self.download_progress.value() * 100 // maximum}%"
            else:
                progress = "准备中"
            name = self._install_version or "未知版本"
            directory = self._pending_download[1] if self._pending_download else ""
            block = f"<b>下载/安装</b>：{name} · {progress} · 已用 {self._install_seconds} 秒"
            if directory:
                block += f"<br>目录：{directory}"
            blocks.append(block)

        if self._version_running():
            blocks.append("<b>版本列表</b>：正在从服务器获取…")

        return "<br>".join(blocks)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _append_launch_log(self, line: str) -> None:
        self.launch_log.appendPlainText(line)
        bar = self.launch_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def closeEvent(self, event: QCloseEvent) -> None:
        install_running = self._install_worker is not None and self._install_worker.isRunning()
        if install_running:
            QMessageBox.information(
                self,
                "请稍候",
                "安装尚未完成。可以先点「取消安装」，结束后再关闭窗口。",
            )
            event.ignore()
            return
        if self._launch_worker is not None and self._launch_worker.isRunning():
            self._launch_worker.stop()
            self._launch_worker.wait(3000)
        if hasattr(self, "resource_browser"):
            self.resource_browser.shutdown()
        self._process_timer.stop()
        event.accept()


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()
