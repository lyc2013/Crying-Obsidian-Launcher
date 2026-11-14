"""Resource browsing pages: search, pagination, cards, download queue."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import GAME_DIR
from ..settings import save_settings
from . import cache, mapper, modpack
from .download import download_file, unique_target
from .http import CancelToken
from .models import (
    KIND_LABELS,
    RESOURCE_KINDS,
    SOURCE_NAMES,
    ResourceFile,
    ResourceItem,
    ResourceVersion,
)
from .providers import check_mcim, provider_for
from .widgets import ImageLoader, ResourceCard, SkeletonCard
from .workers import DownloadWorker, TaskThread

PAGE_SIZES = (20, 40, 60)
SORT_OPTIONS = (
    ("热度", "popularity"),
    ("最近更新", "updated"),
    ("关注数", "follows"),
    ("名称", "name"),
)
SOURCE_OPTIONS = (
    ("Modrinth（MCIM 镜像）", "modrinth"),
    ("Modrinth 官方", "modrinth_official"),
    ("CurseForge（MCIM 镜像）", "curseforge"),
)


def _folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:64] or "instance"


class ResourcePage(QWidget):
    instances_changed = Signal()

    def __init__(self, kind: str, get_settings, images: ImageLoader, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.kind_label = KIND_LABELS[kind]
        self.get_settings = get_settings
        self.images = images

        settings = get_settings()
        self.page_size = settings.resource_page_size
        self.mode = settings.resource_page_mode
        self.prefer_chinese = settings.resource_prefer_chinese

        self.search_text = ""
        self.sort = "popularity"
        self.page_index = 0
        self.total = 0
        self.has_more = False
        self._loading = False
        self._token: CancelToken | None = None
        self._gen = 0
        self._threads: set[TaskThread] = set()
        self.cards: list[ResourceCard] = []
        self._open_card: ResourceCard | None = None
        self._dependency_card: ResourceCard | None = None
        self._dependency_banner: QWidget | None = None
        self._workers: set[DownloadWorker] = set()
        self._queue_rows: dict[int, dict] = {}
        self._modpack_running = False
        self._mirror_down = False
        self._started = False

        self.provider = self._build_provider()
        self._build_ui()
        self._refresh_source_hint()

    def ensure_loaded(self) -> None:
        """Load the first page only when the tab is actually shown."""
        if not self._started:
            self._started = True
            self._check_mirror()
            self.load(0)

    # ------------------------------------------------------------------ setup
    def _build_provider(self):
        settings = self.get_settings()
        return provider_for(
            settings.resource_source,
            api_key=settings.curseforge_api_key,
            proxy=settings.resource_proxy,
            concurrency=settings.resource_concurrency,
        )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(f"搜索{KIND_LABELS[self.kind]}…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._debounce_search)
        self.search_edit.returnPressed.connect(self._apply_search)
        top.addWidget(self.search_edit, 1)

        self.sort_combo = QComboBox()
        for label, value in SORT_OPTIONS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        top.addWidget(self.sort_combo)

        self.source_combo = QComboBox()
        for label, value in SOURCE_OPTIONS:
            self.source_combo.addItem(label, value)
        current = self.get_settings().resource_source
        index = max(0, self.source_combo.findData(current))
        self.source_combo.setCurrentIndex(index)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        top.addWidget(self.source_combo)

        self.target_combo = QComboBox()
        self.target_combo.setToolTip("选择一个本地实例后，详情页会标记适配 / 不兼容的资源版本")
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        top.addWidget(self.target_combo)
        layout.addLayout(top)

        self.hint_label = QLabel()
        self.hint_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.hint_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(2, 2, 2, 2)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        self.scroll.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        layout.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.prev_button = QPushButton("上一页")
        self.prev_button.clicked.connect(lambda: self._go_page(self.page_index - 1))
        footer.addWidget(self.prev_button)

        self.page_label = QLabel("第 1 页")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.addWidget(self.page_label)

        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(lambda: self._go_page(self.page_index + 1))
        footer.addWidget(self.next_button)

        footer.addWidget(QLabel("每页"))
        self.page_size_combo = QComboBox()
        for size in PAGE_SIZES:
            self.page_size_combo.addItem(str(size), size)
        index = self.page_size_combo.findData(self.page_size)
        self.page_size_combo.setCurrentIndex(index if index >= 0 else 0)
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        footer.addWidget(self.page_size_combo)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: palette(mid);")
        footer.addWidget(self.status_label, 1)

        self.retry_button = QPushButton("重试")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(lambda: self.load(self.page_index))
        footer.addWidget(self.retry_button)
        layout.addLayout(footer)

        self.queue_host = QWidget()
        self.queue_layout = QVBoxLayout(self.queue_host)
        self.queue_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_layout.setSpacing(4)
        self.queue_host.setVisible(False)
        layout.addWidget(self.queue_host)

        self._apply_mode()
        self.refresh_targets()

    # ------------------------------------------------------------------ targets
    def refresh_targets(self) -> None:
        if not hasattr(self, "target_combo"):
            return
        settings = self.get_settings()
        previous = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem("目标版本：不指定", None)
        from ..version import instance_version_info, loader_label

        selected = 0
        for position, instance in enumerate(settings.instances[:20], start=1):
            game_dir = settings.effective_game_root(instance)
            base, _category = instance_version_info(instance.version, game_dir)
            label = loader_label(instance.version, game_dir)
            key = mapper.normalize_loader(label)
            target = (base, key) if base and base != "—" else None
            self.target_combo.addItem(
                f"目标：{instance.name}（{base} / {label}）", target
            )
            if target is not None and target == previous:
                selected = position
        self.target_combo.setCurrentIndex(selected)
        self.target_combo.blockSignals(False)

    def current_target(self) -> tuple[str, str] | None:
        data = self.target_combo.currentData()
        if isinstance(data, (tuple, list)) and len(data) == 2 and data[0]:
            return (str(data[0]), data[1] or "")
        return None

    def _on_target_changed(self) -> None:
        target = self.current_target()
        card = self._open_card
        if card is not None and card.detail is not None:
            card.detail.set_target(target)

    # --------------------------------------------------------------- settings
    def apply_settings(self) -> None:
        settings = self.get_settings()
        old_key = (
            self.provider.source,
            settings.curseforge_api_key,
            settings.resource_proxy,
            settings.resource_concurrency,
        )
        self.prefer_chinese = settings.resource_prefer_chinese
        self.mode = settings.resource_page_mode
        new_provider = self._build_provider()
        new_key = (
            new_provider.source,
            settings.curseforge_api_key,
            settings.resource_proxy,
            settings.resource_concurrency,
        )
        if new_key != old_key:
            self.provider = new_provider
            index = self.source_combo.findData(settings.resource_source)
            if index >= 0 and index != self.source_combo.currentIndex():
                self.source_combo.blockSignals(True)
                self.source_combo.setCurrentIndex(index)
                self.source_combo.blockSignals(False)
            self._refresh_source_hint()
            self._check_mirror()
            if self._started:
                self.load(0)
        if self.page_size != settings.resource_page_size:
            self.page_size = settings.resource_page_size
            index = self.page_size_combo.findData(self.page_size)
            if index >= 0:
                self.page_size_combo.setCurrentIndex(index)
        self._apply_mode()

    def _apply_mode(self) -> None:
        paginated = self.mode == "pagination"
        for widget in (self.prev_button, self.page_label, self.next_button):
            widget.setVisible(paginated)

    def _refresh_source_hint(self) -> None:
        name = SOURCE_NAMES.get(self.provider.source, self.provider.source)
        hint = f"当前使用：{name} · {KIND_LABELS[self.kind]}"
        if self._mirror_down:
            hint += "（镜像不可用，已回退官方源）"
        self.hint_label.setText(hint)

    def _check_mirror(self) -> None:
        if self.provider.source != "modrinth":
            return
        gen = self._gen

        def done(ok) -> None:
            if gen != self._gen:
                return
            self._mirror_down = not ok
            self._refresh_source_hint()

        thread = TaskThread(lambda: check_mcim(8))
        thread.done.connect(done)
        thread.finished.connect(thread.deleteLater)
        self._threads.add(thread)
        thread.start()

    # ---------------------------------------------------------------- searching
    def _debounce_search(self) -> None:
        if not hasattr(self, "_search_timer"):
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(300)
            self._search_timer.timeout.connect(self._apply_search)
        self._search_timer.start()

    def _apply_search(self) -> None:
        if hasattr(self, "_search_timer"):
            self._search_timer.stop()
        text = self.search_edit.text().strip()
        if text == self.search_text and self.page_index == 0 and self.cards:
            return
        self.search_text = text
        self.load(0)

    def _on_sort_changed(self) -> None:
        self.sort = self.sort_combo.currentData() or "popularity"
        self.load(0)

    def _on_source_changed(self) -> None:
        source = self.source_combo.currentData()
        settings = self.get_settings()
        settings.resource_source = source
        try:
            save_settings(settings)
        except OSError:
            pass
        self.provider = self._build_provider()
        self._mirror_down = False
        self._refresh_source_hint()
        self._check_mirror()
        self.load(0)

    def _on_page_size_changed(self) -> None:
        self.page_size = int(self.page_size_combo.currentData() or 20)
        settings = self.get_settings()
        settings.resource_page_size = self.page_size
        try:
            save_settings(settings)
        except OSError:
            pass
        self.load(0)

    def _go_page(self, page: int) -> None:
        if page < 0:
            return
        if page > 0 and not (self.cards):
            return
        self.load(page)

    def load(self, page: int, append: bool = False) -> None:
        if self._token is not None:
            self._token.cancel()
        self._gen += 1
        gen = self._gen
        token = CancelToken()
        self._token = token
        self._loading = True
        self.retry_button.setVisible(False)
        self.status_label.setText("正在加载…")

        if not append:
            self._clear_list()
            for _ in range(5):
                self.list_layout.insertWidget(self.list_layout.count() - 1, SkeletonCard())
            self.page_index = page
            self.scroll.verticalScrollBar().setValue(0)
        else:
            skeleton = SkeletonCard()
            skeleton.setObjectName("loadMoreSkeleton")
            self.list_layout.insertWidget(self.list_layout.count() - 1, skeleton)

        provider = self.provider
        query = self.search_text
        sort = self.sort
        page_size = self.page_size

        def task():
            return provider.search(self.kind, query, page, page_size, sort, token)

        thread = TaskThread(task, token)
        thread.done.connect(lambda result: self._show_results(result, gen, append))
        thread.failed.connect(lambda message: self._show_error(message, gen, append))
        thread.finished.connect(lambda: self._finish(gen))
        thread.finished.connect(thread.deleteLater)
        self._threads.add(thread)
        thread.start()

    def _finish(self, gen: int) -> None:
        if gen == self._gen:
            self._loading = False

    def _clear_list(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards = []
        self._open_card = None
        self._dependency_card = None
        self._dependency_banner = None

    def _show_results(self, result, gen: int, append: bool) -> None:
        if gen != self._gen:
            return
        if not append:
            self._clear_list()
        else:
            for index in range(self.list_layout.count()):
                widget = self.list_layout.itemAt(index).widget()
                if widget is not None and widget.objectName() == "loadMoreSkeleton":
                    widget.deleteLater()
                    break
        self.total = result.total
        self.page_index = result.offset // max(1, self.page_size)
        if not result.items and not self.cards:
            self._show_empty()
            self._update_footer(0)
            return
        for item in result.items:
            card = ResourceCard(self, item)
            card.activated.connect(self._toggle_card)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.cards.append(card)
        self._update_footer(len(result.items))
        pages = mapper.total_pages(self.total, self.page_size)
        if self.provider.source == "curseforge":
            pages = mapper.cf_page_limit(self.total, self.page_size)
        self.has_more = (self.page_index + 1) < pages
        self.status_label.setText(
            f"共 {mapper.format_count(self.total)} 个{KIND_LABELS[self.kind]}"
        )
        self._loading = False

    def _show_error(self, message: str, gen: int, append: bool) -> None:
        if gen != self._gen:
            return
        self._loading = False
        if append:
            self.status_label.setText(f"加载更多失败：{message}")
            return
        self._clear_list()
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel(f"加载失败\n{message}")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet("color: #b42318;")
        layout.addWidget(label)
        button = QPushButton("重试")
        button.clicked.connect(lambda: self.load(self.page_index))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        self.list_layout.insertWidget(self.list_layout.count() - 1, box)
        self.status_label.setText("加载失败")
        self.retry_button.setVisible(False)

    def _show_empty(self) -> None:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("没有找到相关资源")
        label.setStyleSheet("color: palette(mid); font-size: 14px;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        self.list_layout.insertWidget(self.list_layout.count() - 1, box)
        self.status_label.setText("0 个结果")

    def _update_footer(self, shown: int) -> None:
        pages = mapper.total_pages(self.total, self.page_size)
        if self.provider.source == "curseforge":
            pages = mapper.cf_page_limit(self.total, self.page_size)
        self.page_label.setText(f"第 {self.page_index + 1} / {max(1, pages)} 页")
        self.prev_button.setEnabled(self.page_index > 0)
        self.next_button.setEnabled(self.page_index + 1 < pages)
        self._apply_mode()

    def _maybe_load_more(self, value: int) -> None:
        if self.mode != "scroll" or self._loading or not self.has_more:
            return
        bar = self.scroll.verticalScrollBar()
        if value >= bar.maximum() - 160:
            self.load(self.page_index + 1, append=True)

    # ------------------------------------------------------------------ cards
    def _toggle_card(self, card: ResourceCard) -> None:
        from .detail import DetailPanel

        if card is self._open_card and card.detail is not None:
            self._collapse_open_card()
            return
        self._collapse_open_card()
        panel = DetailPanel(self, card.item, target=self.current_target())
        card.set_expanded(True, panel)
        self._open_card = card
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(card))

    def _collapse_open_card(self) -> None:
        card = self._open_card
        if card is None:
            return
        if card.detail is not None:
            panel = card.detail
            if hasattr(panel, "shutdown"):
                panel.shutdown()
            card.set_expanded(False)
        self._open_card = None

    def open_dependency(self, project_id: str, title: str) -> None:
        from .detail import DetailPanel

        self._remove_dependency()
        banner = QFrame()
        banner.setStyleSheet(
            "QFrame { background: palette(highlight); border-radius: 6px; }"
        )
        row = QHBoxLayout(banner)
        row.setContentsMargins(8, 4, 8, 4)
        label = QLabel(f"依赖：{title or project_id}")
        row.addWidget(label, 1)
        back = QPushButton("返回列表")
        back.clicked.connect(self._remove_dependency)
        row.addWidget(back)
        self.list_layout.insertWidget(0, banner)
        self._dependency_banner = banner

        placeholder = SkeletonCard()
        self.list_layout.insertWidget(1, placeholder)

        gen = self._gen
        token = CancelToken()

        def task():
            return self.provider.get_project(self.kind, project_id, token=token)

        thread = TaskThread(task, token)

        def done(item) -> None:
            if gen != self._gen:
                return
            placeholder.deleteLater()
            card = ResourceCard(self, item)
            card.activated.connect(self._toggle_card)
            self.list_layout.insertWidget(1, card)
            self._dependency_card = card
            panel = DetailPanel(self, item, target=self.current_target())
            card.set_expanded(True, panel)
            self._open_card = card
            self.scroll.verticalScrollBar().setValue(0)

        def failed(message: str) -> None:
            placeholder.deleteLater()
            label.setText(f"依赖加载失败：{message}")

        thread.done.connect(done)
        thread.failed.connect(failed)
        thread.finished.connect(thread.deleteLater)
        self._threads.add(thread)
        thread.start()

    def _remove_dependency(self) -> None:
        if self._dependency_card is not None:
            if self._open_card is self._dependency_card and self._dependency_card.detail:
                self._dependency_card.detail.shutdown()
            self._dependency_card.deleteLater()
            self._dependency_card = None
            self._open_card = None
        if self._dependency_banner is not None:
            self._dependency_banner.deleteLater()
            self._dependency_banner = None

    # --------------------------------------------------------------- downloads
    def _base_dir(self) -> Path:
        settings = self.get_settings()
        if settings.resource_download_dir:
            return Path(settings.resource_download_dir).expanduser()
        instance = settings.default_instance()
        if instance is not None:
            return settings.effective_game_root(instance)
        return Path(GAME_DIR).expanduser()

    def resource_dir(self, kind: str) -> Path | None:
        base = self._base_dir()
        return {
            "mod": base / "mods",
            "resourcepack": base / "resourcepacks",
            "shader": base / "shaderpacks",
        }.get(kind)

    def _pick_save(self) -> Path | None:
        saves = self._base_dir() / "saves"
        names = []
        try:
            names = sorted(entry.name for entry in saves.iterdir() if entry.is_dir())
        except OSError:
            pass
        if not names:
            QMessageBox.information(
                self, "数据包", "当前游戏目录下没有存档，请先创建存档后再下载数据包。"
            )
            return None
        chosen, ok = QInputDialog.getItem(
            self, "选择存档", "数据包将放入哪个存档：", names, 0, False
        )
        if not ok or not chosen:
            return None
        return saves / chosen / "datapacks"

    def start_download(self, version: ResourceVersion, item: ResourceItem) -> None:
        file = version.primary_file()
        if file is None:
            QMessageBox.warning(self, "无法下载", "该版本没有可用文件。")
            return
        if self.kind == "modpack":
            self._start_modpack(file, item)
            return
        if self.kind == "datapack":
            target_dir = self._pick_save()
            if target_dir is None:
                return
        else:
            target_dir = self.resource_dir(self.kind)
            if target_dir is None:
                QMessageBox.warning(self, "无法下载", "未找到下载目录。")
                return
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / file.filename
        file = replace(file)
        if target.exists():
            choice = self._ask_existing(target)
            if choice == "skip":
                return
            if choice == "rename":
                target = unique_target(target)
                file.filename = target.name
        self._enqueue_download(
            f"{item.title} · {version.version_number}",
            lambda token, progress, status: download_file(
                self.provider.client,
                self.provider,
                file,
                target_dir,
                token=token,
                on_progress=progress,
                on_status=status,
            ),
        )

    def _ask_existing(self, target: Path) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("文件已存在")
        box.setText(f"{target.name} 已存在，如何处理？")
        overwrite = box.addButton("覆盖", QMessageBox.ButtonRole.AcceptRole)
        rename = box.addButton("重命名", QMessageBox.ButtonRole.ActionRole)
        box.addButton("跳过", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite:
            return "overwrite"
        if clicked is rename:
            return "rename"
        return "skip"

    def _unique_instance_name(self, base_name: str) -> str:
        settings = self.get_settings()
        used = {item.name for item in settings.instances}
        name = base_name.strip() or "整合包"
        suffix = 2
        while name in used:
            name = f"{base_name} ({suffix})"
            suffix += 1
        return name

    def _start_modpack(self, file: ResourceFile, item: ResourceItem) -> None:
        if self._modpack_running:
            QMessageBox.information(self, "整合包", "已有整合包正在安装，请稍候。")
            return
        self._modpack_running = True
        base = self._base_dir()
        base.mkdir(parents=True, exist_ok=True)

        def task(token, progress, status):
            status("正在下载整合包…")
            archive = modpack.download_archive(
                self.provider.client, self.provider, file, token
            )
            info = modpack.inspect_modpack(archive)
            name = self._unique_instance_name(str(info.get("name") or item.title))
            root = base / "instances" / _folder_name(name)
            root.mkdir(parents=True, exist_ok=True)
            status("正在安装整合包…")
            state = {"max": 0}

            def set_max(value: int) -> None:
                state["max"] = max(1, int(value))

            def set_progress(value: int) -> None:
                progress(int(value), max(state["max"], int(value) + 1))

            callback = {
                "setStatus": status,
                "setProgress": set_progress,
                "setMax": set_max,
            }
            result = modpack.install_archive(
                archive,
                base,
                root,
                self.provider,
                self.provider.client,
                callback=callback,
                cancel=token,
            )
            return {
                "name": name,
                "version": result.get("launch_version") or result.get("mc_version") or "",
                "mc_version": result.get("mc_version") or "",
            }

        row = self._enqueue_download(item.title, task)
        worker = row["worker"]

        def reset_flag() -> None:
            self._modpack_running = False

        worker.failed.connect(lambda _message: reset_flag())
        worker.cancelled.connect(reset_flag)
        worker.finished.connect(lambda: self._apply_modpack_result(worker, base, item))
        worker.start()

    def _apply_modpack_result(self, worker: DownloadWorker, base: Path, item: ResourceItem) -> None:
        result = worker.result if isinstance(worker.result, dict) else None
        self._workers.discard(worker)
        self._modpack_running = False
        if not result:
            return
        try:
            settings = self.get_settings()
            instance = settings.add_instance(
                result.get("name") or item.title,
                result.get("version") or result.get("mc_version") or "",
                str(base),
            )
            save_settings(settings)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            QMessageBox.warning(self, "整合包", f"创建实例失败：{exc}")
            return
        self.instances_changed.emit()
        QMessageBox.information(
            self, "整合包已安装", f"已创建实例「{instance.name}」，版本 {instance.version}。"
        )

    def _enqueue_download(self, title: str, task) -> dict:
        row = self._create_queue_row(title)
        worker = DownloadWorker(task, self)
        row["worker"] = worker
        self._workers.add(worker)

        def on_progress(done: int, total: int, speed: float) -> None:
            if total > 0:
                row["bar"].setRange(0, 100)
                row["bar"].setValue(min(100, int(done * 100 / total)))
            else:
                row["bar"].setRange(0, 0)
            speed_text = f"{mapper.format_size(speed)}/s" if speed else "—"
            row["info"].setText(f"{mapper.format_size(done)} / {mapper.format_size(total)} · {speed_text}")

        def on_status(text: str) -> None:
            row["label"].setText(text)

        def on_done(path: str) -> None:
            row["bar"].setRange(0, 100)
            row["bar"].setValue(100)
            row["label"].setText(f"{title} · 已保存")
            row["info"].setText(Path(path).name if path else "")
            row["cancel"].setVisible(False)
            row["close"].setVisible(True)

        def on_failed(message: str) -> None:
            row["label"].setText(f"{title} · 下载失败")
            row["info"].setText(message)
            row["cancel"].setVisible(False)
            row["close"].setVisible(True)

        def on_cancelled() -> None:
            row["label"].setText(f"{title} · 已取消")
            row["cancel"].setVisible(False)
            row["close"].setVisible(True)

        worker.progress.connect(on_progress)
        worker.status.connect(on_status)
        worker.succeeded.connect(on_done)
        worker.failed.connect(on_failed)
        worker.cancelled.connect(on_cancelled)
        worker.finished.connect(worker.deleteLater)
        row["cancel"].clicked.connect(worker.cancel)
        row["close"].clicked.connect(lambda: self._remove_queue_row(row))
        worker.start()
        return row

    def _create_queue_row(self, title: str) -> dict:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 6px; }"
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setMinimumWidth(140)
        layout.addWidget(label, 1)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFixedWidth(160)
        layout.addWidget(bar)
        info = QLabel("准备中…")
        info.setStyleSheet("color: palette(mid); font-size: 11px;")
        info.setMinimumWidth(150)
        layout.addWidget(info)
        cancel = QPushButton("取消")
        layout.addWidget(cancel)
        close = QPushButton("✕")
        close.setFixedWidth(28)
        close.setVisible(False)
        layout.addWidget(close)
        self.queue_layout.addWidget(frame)
        self.queue_host.setVisible(True)
        row = {
            "frame": frame,
            "label": label,
            "bar": bar,
            "info": info,
            "cancel": cancel,
            "close": close,
        }
        self._queue_rows[id(frame)] = row
        return row

    def _remove_queue_row(self, row: dict) -> None:
        frame = row.get("frame")
        if frame is not None:
            frame.deleteLater()
        if not any(entry.get("frame") is not None for entry in self._queue_rows.values()):
            self.queue_host.setVisible(False)

    def shutdown(self) -> None:
        self._gen += 1
        if self._token is not None:
            self._token.cancel()
        for thread in list(self._threads):
            thread.cancel()
        for worker in list(self._workers):
            worker.cancel()


class ResourceBrowser(QTabWidget):
    instances_changed = Signal()

    def __init__(self, get_settings, parent=None) -> None:
        super().__init__(parent)
        self.get_settings = get_settings
        self.images = ImageLoader(self)
        self.pages: dict[str, ResourcePage] = {}
        for kind in RESOURCE_KINDS:
            page = ResourcePage(kind, get_settings, self.images)
            page.instances_changed.connect(self.instances_changed)
            page.instances_changed.connect(self._refresh_targets)
            self.addTab(page, KIND_LABELS[kind])
            self.pages[kind] = page
        self.currentChanged.connect(self._on_tab_changed)
        self.pages[RESOURCE_KINDS[0]].ensure_loaded()

    def _on_tab_changed(self, index: int) -> None:
        page = self.widget(index)
        if isinstance(page, ResourcePage):
            page.ensure_loaded()

    def _refresh_targets(self) -> None:
        for page in self.pages.values():
            page.refresh_targets()

    def apply_settings(self) -> None:
        for page in self.pages.values():
            page.apply_settings()
            page.refresh_targets()

    def shutdown(self) -> None:
        for page in self.pages.values():
            page.shutdown()
