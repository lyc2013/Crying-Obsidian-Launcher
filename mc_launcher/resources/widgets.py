"""Reusable widgets for the resource browser."""

from __future__ import annotations

import urllib.request

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..resources import mapper
from . import cache
from .http import user_agent
from .models import ResourceItem


def _fetch_image(url: str) -> bytes | None:
    path = cache.cached_image(url)
    if path is not None:
        try:
            return path.read_bytes()
        except OSError:
            pass
    request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
    except Exception:
        return None
    if data:
        cache.store_image(url, data)
        return data
    return None


class _ImageSignals(QObject):
    loaded = Signal(str, bytes)
    failed = Signal(str)


class _ImageTask(QRunnable):
    def __init__(self, url: str, signals: _ImageSignals) -> None:
        super().__init__()
        self.url = url
        self.signals = signals

    def run(self) -> None:
        data = _fetch_image(self.url)
        if data:
            self.signals.loaded.emit(self.url, data)
        else:
            self.signals.failed.emit(self.url)


class ImageLoader(QObject):
    loaded = Signal(str, bytes)
    failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self._signals = _ImageSignals(self)
        self._signals.loaded.connect(self.loaded)
        self._signals.failed.connect(self.failed)
        self.loaded.connect(self._forget)
        self.failed.connect(self._forget)
        self._pending: set[str] = set()

    def request(self, url: str | None) -> None:
        if not url:
            return
        path = cache.cached_image(url)
        if path is not None:
            try:
                self.loaded.emit(url, path.read_bytes())
                return
            except OSError:
                pass
        if url in self._pending:
            return
        self._pending.add(url)
        self.pool.start(_ImageTask(url, self._signals))

    def _forget(self, url: str, *_rest) -> None:
        self._pending.discard(url)


def placeholder_pixmap(size: int = 64) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#dfe3ea"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, size, size, 8, 8)
    painter.setPen(QColor("#98a2b3"))
    font = painter.font()
    font.setPointSize(max(10, size // 4))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "?")
    painter.end()
    return pixmap


class IconWidget(QLabel):
    def __init__(self, size: int = 56, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self._url = ""
        self.setFixedSize(size, size)
        self.setScaledContents(False)
        self.setPixmap(placeholder_pixmap(size))

    def set_url(self, url: str | None, loader: ImageLoader) -> None:
        if not url or url == self._url:
            return
        self._url = url
        loader.loaded.connect(self._on_loaded)
        loader.request(url)

    def _on_loaded(self, url: str, data: bytes) -> None:
        if url != self._url:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        scaled = pixmap.scaled(
            QSize(self._size, self._size),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


class Chip(QLabel):
    def __init__(self, text: str, color: str | None = None, parent=None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        palette = {
            "source": ("#fff4e5", "#a85400"),
            "loader": ("#eef2ff", "#3b4bb3"),
            "neutral": ("#eef0f4", "#4c566a"),
            "release": ("#e8f7ee", "#177245"),
            "beta": ("#fff6df", "#8a5a00"),
            "alpha": ("#fdecec", "#a12a2a"),
            "featured": ("#f4ecff", "#6b3fa0"),
        }.get(color or "neutral", ("#eef0f4", "#4c566a"))
        self.setStyleSheet(
            "QLabel {"
            f"background: {palette[0]}; color: {palette[1]};"
            "border-radius: 8px; padding: 1px 7px; font-size: 11px;"
            "}"
        )


class SkeletonCard(QFrame):
    def __init__(self, height: int = 92, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; }"
            "QLabel { background: palette(alternate-base); border-radius: 6px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        icon = QLabel()
        icon.setFixedSize(56, 56)
        layout.addWidget(icon)
        column = QVBoxLayout()
        column.setSpacing(6)
        title = QLabel()
        title.setFixedHeight(14)
        title.setMinimumWidth(180)
        summary = QLabel()
        summary.setFixedHeight(12)
        summary.setMinimumWidth(260)
        column.addWidget(title)
        column.addWidget(summary)
        column.addStretch(1)
        layout.addLayout(column, 1)
        side = QLabel()
        side.setFixedSize(90, 18)
        layout.addWidget(side)


class SkeletonRow(QFrame):
    def __init__(self, height: int = 24, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setStyleSheet(
            "QFrame { background: palette(alternate-base); border-radius: 5px; }"
        )


class ResourceCard(QWidget):
    activated = Signal(object)

    def __init__(self, page, item: ResourceItem, parent=None) -> None:
        super().__init__(parent)
        self.page = page
        self.item = item
        self.detail: QWidget | None = None
        self._expanded = False

        self.frame = QFrame()
        self.frame.setObjectName("resourceCard")
        self.frame.setStyleSheet(
            "#resourceCard { border: 1px solid palette(mid); border-radius: 8px; }"
            "QPushButton#cardTitle { border: none; text-align: left; padding: 0; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)

        layout = QHBoxLayout(self.frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self.icon = IconWidget(56)
        self.icon.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon.mousePressEvent = lambda event: self.activated.emit(self)  # type: ignore[method-assign]
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        self.title_button = QPushButton(item.title)
        self.title_button.setObjectName("cardTitle")
        self.title_button.setCursor(Qt.CursorShape.PointingHandCursor)
        font = self.title_button.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        self.title_button.setFont(font)
        self.title_button.clicked.connect(lambda: self.activated.emit(self))
        text_box.addWidget(self.title_button)

        meta = " · ".join(
            part
            for part in (
                item.author or "未知作者",
                f"{mapper.format_count(item.downloads)} 下载",
                f"{mapper.format_date(item.updated)} 更新" if item.updated else "",
            )
            if part
        )
        self.meta_label = QLabel(meta)
        self.meta_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        text_box.addWidget(self.meta_label)

        self.summary_label = QLabel(item.summary or "（暂无简介）")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: palette(text); font-size: 12px;")
        text_box.addWidget(self.summary_label)
        layout.addLayout(text_box, 1)

        side = QVBoxLayout()
        side.setSpacing(4)
        chips = QHBoxLayout()
        chips.setSpacing(4)
        for label in mapper.display_loaders(item.kind, item.loaders):
            chips.addWidget(Chip(label, "loader"))
        chips.addStretch(1)
        side.addLayout(chips)
        self.badge_row = QHBoxLayout()
        self.badge_row.addStretch(1)
        self.source_badge = Chip("CF" if item.source == "curseforge" else "MR", "source")
        self.badge_row.addWidget(self.source_badge)
        self.chevron = QLabel("▸")
        self.chevron.setStyleSheet("color: palette(mid);")
        self.badge_row.addWidget(self.chevron)
        side.addLayout(self.badge_row)
        layout.addLayout(side, 0)

        self.icon.set_url(item.icon_url, page.images)

    def set_expanded(self, expanded: bool, panel: QWidget | None = None) -> None:
        self._expanded = expanded
        self.chevron.setText("▾" if expanded else "▸")
        if expanded and panel is not None:
            self.detail = panel
            outer = self.layout()
            outer.addWidget(panel)
        elif not expanded and self.detail is not None:
            self.detail.setParent(None)
            self.detail.deleteLater()
            self.detail = None
