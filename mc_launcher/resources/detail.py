"""Inline resource detail panel with the 4-level drill-down."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import mapper
from .http import CancelToken
from .models import (
    NO_LOADER_KINDS,
    SOURCE_NAMES,
    ResourceItem,
    ResourceVersion,
)
from .providers import game_version_counts
from .widgets import Chip, IconWidget, SkeletonRow
from .workers import TaskThread


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child is not None:
            _clear_layout(child)


class DetailPanel(QFrame):
    def __init__(self, page, item: ResourceItem, parent=None, target=None) -> None:
        super().__init__(parent)
        self.page = page
        self.item = item
        self.provider = page.provider
        self.token = CancelToken()
        self._gen = 0
        self._level_gen = 0
        self._threads: set[TaskThread] = set()

        self.all_versions: list[ResourceVersion] = []
        self.current_files: list[ResourceVersion] = []
        self.target_mc, self.target_loader = target or (None, None)
        self.game_version: str | None = None
        self.loader: str | None = None
        self.file_version: str | None = None
        self._body_original = ""
        self._body_translated = ""
        self._body_format = "markdown"
        self._showing_translated = False

        self.setStyleSheet(
            "DetailPanel { background: palette(alternate-base); "
            "border-top: 1px solid palette(mid); }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(8)

        self.breadcrumb = QHBoxLayout()
        self.breadcrumb.setSpacing(2)
        viewport = QWidget()
        viewport.setLayout(self.breadcrumb)
        layout.addWidget(viewport)
        self._render_breadcrumb()

        # ---------- level 1: project introduction ----------
        header = QHBoxLayout()
        header.setSpacing(12)
        self.big_icon = IconWidget(72)
        header.addWidget(self.big_icon, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(3)
        self.title_label = QLabel(item.title)
        title_font = self.title_label.font()
        title_font.setPointSize(title_font.pointSize() + 3)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setWordWrap(True)
        info.addWidget(self.title_label)

        self.byline_label = QLabel()
        self.byline_label.setStyleSheet("color: palette(mid);")
        info.addWidget(self.byline_label)

        self.meta_label = QLabel()
        self.meta_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        info.addWidget(self.meta_label)

        self.links_label = QLabel()
        self.links_label.setTextFormat(Qt.TextFormat.RichText)
        self.links_label.setOpenExternalLinks(True)
        self.links_label.setWordWrap(True)
        info.addWidget(self.links_label)
        info.addStretch(1)
        header.addLayout(info, 1)
        layout.addLayout(header)

        self.category_row = QHBoxLayout()
        self.category_row.setSpacing(4)
        layout.addLayout(self.category_row)

        self.description = QTextBrowser()
        self.description.setOpenExternalLinks(True)
        self.description.setMinimumHeight(90)
        self.description.setMaximumHeight(220)
        self.description.setVisible(False)
        layout.addWidget(self.description)

        self.body_toggle = QPushButton("查看原文")
        self.body_toggle.setVisible(False)
        self.body_toggle.setFlat(True)
        self.body_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.body_toggle.clicked.connect(self._toggle_body_language)
        layout.addWidget(self.body_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setFixedHeight(124)
        self.gallery_scroll.setVisible(False)
        self.gallery_scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.gallery_scroll)

        self.dependency_title = QLabel("依赖")
        self.dependency_title.setStyleSheet("font-weight: 600;")
        self.dependency_title.setVisible(False)
        layout.addWidget(self.dependency_title)
        self.dependency_box = QVBoxLayout()
        self.dependency_box.setSpacing(4)
        layout.addLayout(self.dependency_box)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: palette(mid);")
        layout.addWidget(line)

        # ---------- level 2: game versions ----------
        self.versions_title = QLabel("支持的 Minecraft 版本")
        self.versions_title.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.versions_title)
        self.versions_host = QWidget()
        self.versions_layout = QVBoxLayout(self.versions_host)
        self.versions_layout.setContentsMargins(0, 0, 0, 0)
        self.versions_layout.setSpacing(4)
        layout.addWidget(self.versions_host)

        # ---------- level 3: loaders ----------
        self.loaders_host = QWidget()
        self.loaders_layout = QVBoxLayout(self.loaders_host)
        self.loaders_layout.setContentsMargins(0, 0, 0, 0)
        self.loaders_layout.setSpacing(4)
        layout.addWidget(self.loaders_host)
        self.loaders_host.setVisible(False)

        # ---------- level 4: files ----------
        self.files_host = QWidget()
        self.files_layout = QVBoxLayout(self.files_host)
        self.files_layout.setContentsMargins(0, 0, 0, 0)
        self.files_layout.setSpacing(4)
        layout.addWidget(self.files_host)
        self.files_host.setVisible(False)

        self._fill_header(item)
        self._seed_versions()
        self._start_requests()

    # ------------------------------------------------------------------ utils
    def _run(self, fn, on_done, on_error, gen: int, level: bool = False) -> None:
        thread = TaskThread(lambda: fn(self.token), self.token)

        def done(result) -> None:
            current = self._level_gen if level else self._gen
            if gen == current:
                on_done(result)

        def failed(message: str) -> None:
            current = self._level_gen if level else self._gen
            if gen == current:
                on_error(message)

        thread.done.connect(done)
        thread.failed.connect(failed)
        thread.finished.connect(lambda: self._threads.discard(thread))
        self._threads.add(thread)
        thread.start()

    def shutdown(self) -> None:
        self._gen += 1
        self._level_gen += 1
        self.token.cancel()
        for thread in list(self._threads):
            thread.cancel()

    @staticmethod
    def _error_box(message: str, retry) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(f"加载失败：{message}")
        label.setStyleSheet("color: #b42318;")
        label.setWordWrap(True)
        layout.addWidget(label, 1)
        button = QPushButton("重试")
        button.clicked.connect(retry)
        layout.addWidget(button, 0)
        return box

    @staticmethod
    def _loading(layout, rows: int = 3) -> None:
        for _ in range(rows):
            layout.addWidget(SkeletonRow(22))

    def _render_breadcrumb(self) -> None:
        _clear_layout(self.breadcrumb)
        parts: list[tuple[str, object]] = [("资源", self._reset_levels)]
        if self.game_version:
            parts.append((self.game_version, lambda: self._select_game_version(self.game_version)))
        if self.loader and self.item.kind not in NO_LOADER_KINDS:
            parts.append((self.loader, lambda: self._select_loader(self.loader)))
        if self.file_version:
            parts.append((self.file_version, lambda: None))
        for index, (text, handler) in enumerate(parts):
            if index:
                separator = QLabel("›")
                separator.setStyleSheet("color: palette(mid);")
                self.breadcrumb.addWidget(separator)
            button = QPushButton(text)
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet("QPushButton { border: none; padding: 2px 4px; }")
            button.clicked.connect(handler)
            self.breadcrumb.addWidget(button)
        self.breadcrumb.addStretch(1)

    # --------------------------------------------------------------- level 1
    def _start_requests(self) -> None:
        self._run(
            lambda token: self.provider.get_project(
                self.item.kind, self.item.id, seed=self.item, token=token
            ),
            self._apply_project,
            self._project_failed,
            self._gen,
        )
        self._run_versions()
        self._run_dependencies()

    def _apply_project(self, item: ResourceItem) -> None:
        self._fill_header(item)
        self._render_gallery(item.gallery)

    def _project_failed(self, message: str) -> None:
        if not self.links_label.text():
            self.links_label.setText(f"<span style='color:#b42318'>简介加载失败：{message}</span>")

    def _fill_header(self, item: ResourceItem) -> None:
        self.item = item
        self.title_label.setText(item.title)
        self.byline_label.setText(
            f"{item.author or '未知作者'} · {SOURCE_NAMES.get(item.source, item.source)}"
        )
        details = [f"{mapper.format_count(item.downloads)} 下载"]
        if item.follows:
            details.append(f"{mapper.format_count(item.follows)} 关注")
        if item.created:
            details.append(f"创建于 {mapper.format_date(item.created)}")
        if item.updated:
            details.append(f"更新于 {mapper.format_date(item.updated)}")
        self.meta_label.setText(" · ".join(details))

        links = []
        if item.website:
            links.append(f"<a href='{item.website}'>官网</a>")
        if item.source_url:
            links.append(f"<a href='{item.source_url}'>源码</a>")
        if item.license:
            links.append(f"协议 {item.license}")
        self.links_label.setText(" · ".join(links))
        self.big_icon.set_url(item.icon_url, self.page.images)

        _clear_layout(self.category_row)
        categories = item.categories[:8]
        for name in categories:
            self.category_row.addWidget(Chip(name, "neutral"))
        self.category_row.addStretch(1)

        body = item.body or item.summary
        self._body_original = body or ""
        self._body_translated = item.translated_body or ""
        self._body_format = item.body_format
        self._showing_translated = bool(
            self.page.prefer_chinese and self._body_translated
        )
        self._render_body()
        self.body_toggle.setVisible(bool(self._body_translated))
        self.body_toggle.setText("查看原文" if self._showing_translated else "查看中文")

    def _render_body(self) -> None:
        body = self._body_translated if self._showing_translated else self._body_original
        if not body:
            self.description.setVisible(False)
            return
        self.description.setVisible(True)
        if self._body_format == "html":
            self.description.setHtml(mapper.sanitize_html(body))
        else:
            self.description.setMarkdown(body)

    def _toggle_body_language(self) -> None:
        self._showing_translated = not self._showing_translated
        self.body_toggle.setText("查看原文" if self._showing_translated else "查看中文")
        self._render_body()

    def _render_gallery(self, urls: list[str]) -> None:
        self.gallery_scroll.setVisible(bool(urls))
        if not urls:
            return
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for url in urls[:12]:
            icon = IconWidget(112)
            icon.set_url(url, self.page.images)
            icon.setCursor(Qt.CursorShape.PointingHandCursor)
            icon.mousePressEvent = (  # type: ignore[method-assign]
                lambda event, target=url: QDesktopServices.openUrl(QUrl(target))
            )
            row.addWidget(icon)
        row.addStretch(1)
        self.gallery_scroll.setWidget(host)

    # ------------------------------------------------------------ dependencies
    def _run_dependencies(self) -> None:
        _clear_layout(self.dependency_box)
        self.dependency_title.setVisible(True)
        self.dependency_box.addWidget(SkeletonRow(20))
        self._run(
            lambda token: self.provider.get_dependencies(self.item, token=token),
            self._render_dependencies,
            self._dependencies_failed,
            self._gen,
        )

    def _dependencies_failed(self, message: str) -> None:
        _clear_layout(self.dependency_box)
        self.dependency_box.addWidget(self._error_box(message, self._run_dependencies))

    def _render_dependencies(self, dependencies: list[dict]) -> None:
        _clear_layout(self.dependency_box)
        if not dependencies:
            self.dependency_title.setVisible(False)
            return
        labels = {"required": "必选", "optional": "可选", "incompatible": "冲突"}
        rows: dict[str, list[dict]] = {}
        for entry in dependencies:
            rows.setdefault(entry.get("relation", "required"), []).append(entry)
        for relation in ("required", "optional", "incompatible"):
            entries = rows.get(relation) or []
            if not entries:
                continue
            box = QWidget()
            layout = QHBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            layout.addWidget(QLabel(f"{labels.get(relation, relation)}："))
            for entry in entries[:8]:
                button = QPushButton(entry.get("title") or entry.get("project_id") or "依赖")
                button.setFlat(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setStyleSheet("QPushButton { color: #2f5fd0; border: none; }")
                project_id = entry.get("project_id")
                title = entry.get("title") or ""
                if project_id:
                    button.clicked.connect(
                        lambda _checked=False, pid=project_id, name=title: self.page.open_dependency(
                            str(pid), name
                        )
                    )
                layout.addWidget(button)
            layout.addStretch(1)
            self.dependency_box.addWidget(box)

    # --------------------------------------------------------------- level 2
    def _seed_versions(self) -> None:
        _clear_layout(self.versions_layout)
        if self.item.game_versions:
            self._render_version_grid(
                [(name, 0) for name in mapper.sort_versions_desc(self.item.game_versions)]
            )
            self.versions_layout.addWidget(SkeletonRow(18))
        else:
            self._loading(self.versions_layout, 3)
            self.versions_title.setText("支持的 Minecraft 版本（加载中…）")

    def _run_versions(self) -> None:
        self._run(
            lambda token: self.provider.get_versions(self.item.kind, self.item.id, token=token),
            self._versions_ready,
            self._versions_failed,
            self._gen,
        )

    def _versions_failed(self, message: str) -> None:
        _clear_layout(self.versions_layout)
        self.versions_layout.addWidget(self._error_box(message, self._run_versions))
        self.versions_title.setText("支持的 Minecraft 版本")

    def _versions_ready(self, versions: list[ResourceVersion]) -> None:
        self.all_versions = versions
        counts = game_version_counts(versions)
        installed = self._installed_game_versions(versions)
        self.versions_title.setText(
            f"支持的 Minecraft 版本（{len(counts)} 个）"
        )
        _clear_layout(self.versions_layout)
        self._render_version_grid(list(counts.items()), installed)

    def _installed_game_versions(self, versions: list[ResourceVersion]) -> set[str]:
        folder = self.page.resource_dir(self.item.kind)
        if folder is None:
            return set()
        try:
            existing = {entry.name for entry in Path(folder).iterdir() if entry.is_file()}
        except OSError:
            return set()
        result = set()
        for version in versions:
            names = {file.filename for file in version.files}
            if names & existing:
                result.update(version.game_versions)
        return result

    def _render_version_grid(self, entries, installed: set[str] | None = None) -> None:
        installed = installed or set()
        selected = self.game_version
        grid = QGridLayout()
        grid.setSpacing(4)
        for index, (name, count) in enumerate(entries):
            label = name
            if count:
                label += f"  ({count})"
            if name in installed:
                label = "✓ " + label
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(name == selected)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, value=name: self._select_game_version(value)
            )
            grid.addWidget(button, index // 4, index % 4)
        self.versions_layout.addLayout(grid)

    # --------------------------------------------------------------- level 3
    def _reset_levels(self) -> None:
        self.game_version = None
        self.loader = None
        self.file_version = None
        self._level_gen += 1
        self.loaders_host.setVisible(False)
        self.files_host.setVisible(False)
        _clear_layout(self.loaders_layout)
        _clear_layout(self.files_layout)
        _clear_layout(self.breadcrumb)
        self._render_breadcrumb()

    def _select_game_version(self, game_version: str) -> None:
        self.game_version = game_version
        self.loader = None
        self.file_version = None
        self._level_gen += 1
        _clear_layout(self.breadcrumb)
        self._render_breadcrumb()
        self.files_host.setVisible(False)
        _clear_layout(self.files_layout)

        self.loaders_host.setVisible(True)
        _clear_layout(self.loaders_layout)
        title = QLabel(f"{game_version} 支持的加载器")
        title.setStyleSheet("font-weight: 600;")
        self.loaders_layout.addWidget(title)
        self._loading(self.loaders_layout, 2)
        gen = self._level_gen
        self._run(
            lambda token: self.provider.get_versions(
                self.item.kind, self.item.id, game_version=game_version, token=token
            ),
            self._loaders_ready,
            lambda message: self._loaders_failed(message, game_version),
            gen,
            level=True,
        )

    def _loaders_failed(self, message: str, game_version: str) -> None:
        _clear_layout(self.loaders_layout)
        self.loaders_layout.addWidget(
            self._error_box(message, lambda: self._select_game_version(game_version))
        )

    def _loaders_ready(self, versions: list[ResourceVersion]) -> None:
        _clear_layout(self.loaders_layout)
        title = QLabel(f"{self.game_version} 支持的加载器")
        title.setStyleSheet("font-weight: 600;")
        self.loaders_layout.addWidget(title)

        loaders = mapper.dedupe_loaders(
            loader for version in versions for loader in version.loaders
        )
        if self.item.kind in NO_LOADER_KINDS or not loaders:
            if self.item.kind not in NO_LOADER_KINDS:
                hint = QLabel("该资源在此版本未标注加载器，直接显示文件列表。")
                hint.setStyleSheet("color: palette(mid);")
                self.loaders_layout.addWidget(hint)
            self._show_files(versions)
            return

        if self.target_loader:
            wanted = mapper.normalize_loader(self.target_loader)
            if wanted in loaders:
                loaders = [wanted] + [name for name in loaders if name != wanted]

        row = QHBoxLayout()
        row.setSpacing(4)
        for loader in loaders:
            matches_target = bool(self.target_loader) and loader == mapper.normalize_loader(
                self.target_loader
            )
            button = QPushButton(("✓ " if matches_target else "") + loader)
            if matches_target:
                button.setStyleSheet("font-weight: 600;")
                button.setToolTip("适配当前实例的加载器")
            button.setCheckable(True)
            button.setChecked(loader == self.loader)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, value=loader: self._select_loader(value)
            )
            row.addWidget(button)
        row.addStretch(1)
        self.loaders_layout.addLayout(row)

    def _select_loader(self, loader: str | None) -> None:
        self.loader = loader
        self._level_gen += 1
        _clear_layout(self.breadcrumb)
        self._render_breadcrumb()
        self.files_host.setVisible(True)
        _clear_layout(self.files_layout)
        self.files_layout.addWidget(QLabel(f"{self.game_version} · {loader or '全部文件'} 的版本"))
        self._loading(self.files_layout, 3)
        gen = self._level_gen
        self._run(
            lambda token: self.provider.get_versions(
                self.item.kind,
                self.item.id,
                game_version=self.game_version,
                loader=loader,
                token=token,
            ),
            self._show_files,
            lambda message: self._files_failed(message, loader),
            gen,
            level=True,
        )

    def _files_failed(self, message: str, loader: str | None) -> None:
        _clear_layout(self.files_layout)
        self.files_layout.addWidget(
            self._error_box(message, lambda: self._select_loader(loader))
        )

    # --------------------------------------------------------------- level 4
    def _show_files(self, versions: list[ResourceVersion]) -> None:
        self.current_files = list(versions)
        self.files_host.setVisible(True)
        _clear_layout(self.files_layout)
        header = QLabel(
            f"{self.game_version} · {self.loader or '全部文件'} · {len(versions)} 个版本"
        )
        header.setStyleSheet("font-weight: 600;")
        self.files_layout.addWidget(header)
        if not versions:
            empty = QLabel("该组合下没有可用文件。")
            empty.setStyleSheet("color: palette(mid);")
            self.files_layout.addWidget(empty)
            return
        if self.game_version and versions[0].version_number != self.file_version:
            self.file_version = versions[0].version_number
            _clear_layout(self.breadcrumb)
            self._render_breadcrumb()
        for version in versions:
            self.files_layout.addWidget(self._file_row(version))

    def set_target(self, target) -> None:
        self.target_mc, self.target_loader = target or (None, None)
        if self.game_version:
            self._select_game_version(self.game_version)
        elif self.current_files:
            self._show_files(self.current_files)

    def _file_compatible(self, version: ResourceVersion) -> bool:
        if not self.target_mc:
            return True
        if version.game_versions and self.target_mc not in version.game_versions:
            return False
        if self.target_loader and version.loaders:
            wanted = mapper.normalize_loader(self.target_loader)
            if wanted and wanted not in {
                mapper.normalize_loader(name) for name in version.loaders
            }:
                return False
        return True

    def _file_row(self, version: ResourceVersion) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 6px; }"
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        column = QVBoxLayout()
        column.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        name = QLabel(version.name or version.version_number)
        name.setStyleSheet("font-weight: 600;")
        top.addWidget(name)
        release_text = {"release": "Release", "beta": "Beta", "alpha": "Alpha"}.get(
            version.release_type, version.release_type
        )
        top.addWidget(Chip(release_text, version.release_type))
        if version.featured:
            top.addWidget(Chip("特色版本", "featured"))
        if version.dependencies:
            top.addWidget(Chip(f"{len(version.dependencies)} 个依赖", "neutral"))
        if self.target_mc:
            compatible = self._file_compatible(version)
            top.addWidget(
                Chip(
                    "适配当前版本" if compatible else "不兼容当前版本",
                    "release" if compatible else "neutral",
                )
            )
        top.addStretch(1)
        column.addLayout(top)

        primary = version.primary_file()
        meta_parts = []
        if version.published:
            meta_parts.append(mapper.format_date(version.published))
        if primary is not None:
            meta_parts.append(mapper.format_size(primary.size))
        if version.downloads:
            meta_parts.append(f"{mapper.format_count(version.downloads)} 下载")
        if version.game_versions:
            meta_parts.append("MC " + ", ".join(version.game_versions[:4]))
        meta = QLabel(" · ".join(meta_parts))
        meta.setStyleSheet("color: palette(mid); font-size: 11px;")
        meta.setWordWrap(True)
        column.addWidget(meta)

        if version.changelog:
            toggle = QPushButton("变更日志 ▸")
            toggle.setFlat(True)
            toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            toggle.setStyleSheet("QPushButton { border: none; color: #2f5fd0; }")
            changelog = QLabel(mapper.sanitize_html(version.changelog))
            changelog.setWordWrap(True)
            changelog.setVisible(False)
            changelog.setStyleSheet("color: palette(text); font-size: 11px;")
            toggle.clicked.connect(
                lambda _checked=False, label=changelog, button=toggle: (
                    label.setVisible(not label.isVisible()),
                    button.setText("变更日志 ▾" if label.isVisible() else "变更日志 ▸"),
                )
            )
            column.addWidget(toggle)
            column.addWidget(changelog)
        layout.addLayout(column, 1)

        download = QPushButton("下载")
        download.setEnabled(primary is not None)
        if primary is None:
            download.setToolTip("该版本没有可用文件")
        else:
            download.clicked.connect(
                lambda _checked=False, v=version: self.page.start_download(v, self.item)
            )
        layout.addWidget(download, 0, Qt.AlignmentFlag.AlignTop)
        return frame
