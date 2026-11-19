"""Per-instance management for mods, resource packs, shaders and data packs."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

DISABLED_SUFFIX = ".disabled"

# (key, label, folder name; None means <saves>/<save>/datapacks)
CATEGORIES = (
    ("mod", "模组", "mods"),
    ("resourcepack", "资源包", "resourcepacks"),
    ("shader", "光影", "shaderpacks"),
    ("datapack", "数据包", None),
)


@dataclass
class ResourceEntry:
    path: Path
    name: str
    enabled: bool
    size: int
    modified: float


def category_directory(root: Path, category: str) -> Path | None:
    for key, _label, folder in CATEGORIES:
        if key == category:
            return root / folder if folder else None
    return None


def list_saves(root: Path) -> list[Path]:
    saves_dir = root / "saves"
    if not saves_dir.is_dir():
        return []
    return sorted(
        (entry for entry in saves_dir.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name.lower(),
    )


def datapack_directory(root: Path, save_name: str | None) -> Path | None:
    if not save_name:
        return None
    return root / "saves" / save_name / "datapacks"


def list_entries(directory: Path | None) -> list[ResourceEntry]:
    if directory is None or not directory.is_dir():
        return []
    entries: list[ResourceEntry] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(".") or name.endswith(".part"):
            continue
        enabled = not name.lower().endswith(DISABLED_SUFFIX)
        display = name[: -len(DISABLED_SUFFIX)] if not enabled else name
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            ResourceEntry(
                path=path,
                name=display,
                enabled=enabled,
                size=stat.st_size,
                modified=stat.st_mtime,
            )
        )
    entries.sort(key=lambda entry: entry.name.lower())
    return entries


def toggle_entry(entry: ResourceEntry) -> Path:
    """Rename between ``name`` and ``name.disabled``; returns the new path."""
    if entry.enabled:
        target = entry.path.with_name(entry.path.name + DISABLED_SUFFIX)
    else:
        if not entry.path.name.lower().endswith(DISABLED_SUFFIX):
            return entry.path
        target = entry.path.with_name(entry.path.name[: -len(DISABLED_SUFFIX)])
    if target.exists():
        raise FileExistsError(f"目标文件已存在：{target.name}")
    entry.path.rename(target)
    return target


def delete_entry(entry: ResourceEntry) -> None:
    entry.path.unlink(missing_ok=True)


def unique_destination(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    index = 2
    while True:
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def import_file(source: str | Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source_path = Path(source)
    target = unique_destination(directory, source_path.name)
    shutil.copy2(source_path, target)
    return target


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_time(timestamp: float) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


class CategoryPage(QWidget):
    """One tab: table of resource files plus toggle/delete/import actions."""

    def __init__(self, category: str, root: Path, parent=None) -> None:
        super().__init__(parent)
        self.category = category
        self.root = Path(root)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        top = QHBoxLayout()
        self.save_combo = None
        self.hint = QLabel("")
        self.hint.setStyleSheet("color: palette(mid);")
        if category == "datapack":
            top.addWidget(QLabel("存档"))
            self.save_combo = QComboBox()
            self.save_combo.currentIndexChanged.connect(self.refresh)
            top.addWidget(self.save_combo, 1)
        else:
            top.addWidget(self.hint, 1)
        layout.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "大小", "修改时间", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda _item: self._toggle())
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.toggle_button = QPushButton("启用 / 禁用")
        self.toggle_button.clicked.connect(self._toggle)
        buttons.addWidget(self.toggle_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)
        self.import_button = QPushButton("导入文件…")
        self.import_button.clicked.connect(self._import)
        buttons.addWidget(self.import_button)
        self.open_button = QPushButton("打开文件夹")
        self.open_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)

        self._entries: list[ResourceEntry] = []
        self.refresh()

    # ------------------------------------------------------------------ paths
    def current_directory(self) -> Path | None:
        if self.category == "datapack":
            save = self.save_combo.currentData() if self.save_combo is not None else None
            return datapack_directory(self.root, save)
        return category_directory(self.root, self.category)

    def _reload_saves(self) -> None:
        if self.save_combo is None:
            return
        previous = self.save_combo.currentData()
        self.save_combo.blockSignals(True)
        self.save_combo.clear()
        for save in list_saves(self.root):
            self.save_combo.addItem(save.name, save.name)
        if previous:
            index = self.save_combo.findData(previous)
            if index >= 0:
                self.save_combo.setCurrentIndex(index)
        self.save_combo.blockSignals(False)

    # ------------------------------------------------------------------ table
    def refresh(self) -> None:
        if self.category == "datapack":
            self._reload_saves()
            if self.save_combo is not None and self.save_combo.count() == 0:
                self.hint.setText("未找到存档，请先在游戏中创建存档。")
                self.save_combo.setVisible(False)
            else:
                self.hint.setText("")
                if self.save_combo is not None:
                    self.save_combo.setVisible(True)

        directory = self.current_directory()
        self._entries = list_entries(directory)
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            name_item = QTableWidgetItem(entry.name)
            name_item.setData(Qt.ItemDataRole.UserRole, row)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(format_size(entry.size)))
            self.table.setItem(row, 2, QTableWidgetItem(format_time(entry.modified)))
            status = QTableWidgetItem("已启用" if entry.enabled else "已禁用")
            if not entry.enabled:
                status.setForeground(Qt.GlobalColor.gray)
            self.table.setItem(row, 3, status)

        has_files = bool(self._entries)
        for button in (self.toggle_button, self.delete_button):
            button.setEnabled(has_files)
        self.import_button.setEnabled(directory is not None)
        self.open_button.setEnabled(directory is not None)
        if directory is not None and not has_files:
            self.hint.setText("此目录为空，可点击「导入文件…」添加。")
        elif directory is not None and self.category != "datapack":
            self.hint.setText(str(directory))

    def selected_entries(self) -> list[ResourceEntry]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [
            self._entries[row]
            for row in sorted(rows)
            if 0 <= row < len(self._entries)
        ]

    # ---------------------------------------------------------------- actions
    def _toggle(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        failures: list[str] = []
        for entry in entries:
            try:
                toggle_entry(entry)
            except OSError as exc:
                failures.append(f"{entry.name}：{exc}")
        self.refresh()
        if failures:
            QMessageBox.warning(self, "切换失败", "\n".join(failures))

    def _delete(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        names = "\n".join(entry.name for entry in entries[:8])
        if len(entries) > 8:
            names += f"\n…其余 {len(entries) - 8} 个"
        confirm = QMessageBox.question(
            self,
            "删除文件",
            f"确定要永久删除以下文件吗？\n\n{names}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        failures: list[str] = []
        for entry in entries:
            try:
                delete_entry(entry)
            except OSError as exc:
                failures.append(f"{entry.name}：{exc}")
        self.refresh()
        if failures:
            QMessageBox.warning(self, "删除失败", "\n".join(failures))

    def _import(self) -> None:
        directory = self.current_directory()
        if directory is None:
            QMessageBox.information(self, "导入", "请先选择存档。")
            return
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "选择要导入的文件",
            str(Path.home()),
            "资源文件 (*.jar *.zip *.mcpack *.mcaddon);;所有文件 (*)",
        )
        if not paths:
            return
        directory.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []
        for path in paths:
            try:
                import_file(path, directory)
            except OSError as exc:
                failures.append(f"{Path(path).name}：{exc}")
        self.refresh()
        if failures:
            QMessageBox.warning(self, "导入失败", "\n".join(failures))

    def _open_folder(self) -> None:
        directory = self.current_directory()
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))


class ResourceManagerDialog(QDialog):
    """Resource management for one instance, one tab per resource type."""

    def __init__(self, parent, instance, game_root: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"资源管理 · {instance.name}")
        self.resize(760, 520)
        self.instance = instance
        self.game_root = Path(game_root)

        layout = QVBoxLayout(self)
        self.summary = QLabel(
            f"实例「{instance.name}」 · 版本 {instance.version}"
            f"\n游戏目录：{self.game_root}"
        )
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.summary)

        self.tabs = QTabWidget()
        self.pages: dict[str, CategoryPage] = {}
        for key, label, _folder in CATEGORIES:
            page = CategoryPage(key, self.game_root)
            self.pages[key] = page
            self.tabs.addTab(page, label)
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        refresh_button = QPushButton("全部刷新")
        refresh_button.clicked.connect(self.refresh_all)
        buttons.addWidget(refresh_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self) -> None:
        page = self.tabs.currentWidget()
        if isinstance(page, CategoryPage):
            page.refresh()

    def refresh_all(self) -> None:
        for page in self.pages.values():
            page.refresh()
