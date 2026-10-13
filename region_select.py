from __future__ import annotations

from math import ceil

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from screenshot import capture_virtual_pixmap, qimage_to_jpeg

LEARNING_ACTIONS: list[tuple[str, str]] = [
    ("概括要点", "请简洁概括框选区域的主题和核心要点，篇幅按内容需要调整。"),
    ("翻译", "请翻译框选区域的文字；若已是中文则提炼要点。"),
    ("给我解释", "请解释框选区域里的关键概念或内容，讲清楚为什么。"),
    ("找考试重点", "请从框选区域中找出可能的考试重点，并简要说明。"),
    ("帮我做这道题", "请解答框选区域里的题目，根据题目要求和正文语言选择作答语言（如韩语、英语），给出完整答案与必要步骤；外语作答下方附对应的完整中文翻译。篇幅以讲清题目为准。"),
    ("我没看懂这里", "请用更简单的话解释框选区域，并指出我最该先看哪一句。"),
]


def virtual_geometry() -> QRect:
    screens = QGuiApplication.screens()
    if not screens:
        return QRect(0, 0, 800, 600)
    geo = screens[0].geometry()
    for screen in screens[1:]:
        geo = geo.united(screen.geometry())
    return geo


class RegionSelectOverlay(QWidget):
    """Fullscreen snip overlay: freeze the screen, dim it, drag to select."""

    region_selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._bg = QPixmap()
        self._live = False
        self._hint = QLabel("拖动鼠标框选，右键或 Esc 取消", self)
        self._hint.setStyleSheet(
            "background: rgba(0,0,0,200); color: white; padding: 8px 14px; border-radius: 8px;"
        )
        self._hint.adjustSize()

    def begin(self, *, live=False) -> None:
        geo = virtual_geometry()
        self._live=live;self.setAttribute(Qt.WA_TranslucentBackground,live)
        self._bg = QPixmap() if live else capture_virtual_pixmap()
        self.setGeometry(geo)
        self._origin = None
        self._current = None
        self._hint.move(24, 24)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _cancel(self) -> None:
        self._origin = None
        self._current = None
        self.hide()
        self.release_capture()
        self.cancelled.emit()

    def release_capture(self):
        self._bg=QPixmap();self._origin=None;self._current=None

    def selected_jpeg(self, rect: QRect) -> bytes:
        """Crop the exact frozen frame the user selected, including HiDPI scaling."""
        if self._bg.isNull():
            raise ValueError("没有可用的框选截图")
        selected = rect.intersected(self.geometry())
        if selected.isEmpty():
            raise ValueError("选区不在截图内")
        sx = self._bg.width() / self.width()
        sy = self._bg.height() / self.height()
        left = int((selected.x() - self.x()) * sx)
        top = int((selected.y() - self.y()) * sy)
        right = ceil((selected.x() + selected.width() - self.x()) * sx)
        bottom = ceil((selected.y() + selected.height() - self.y()) * sy)
        try:return qimage_to_jpeg(self._bg.copy(left, top, right-left, bottom-top).toImage())
        finally:self.release_capture()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.RightButton:
            self._cancel()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.globalPosition().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._origin is not None:
            self._current = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        end = event.globalPosition().toPoint()
        rect = QRect(self._origin, end).normalized()
        self._origin = None
        self._current = None
        self.hide()
        if rect.width() < 8 or rect.height() < 8:
            self.release_capture()
            self.cancelled.emit()
            return
        self.region_selected.emit(rect)

    def _selection_rect(self) -> QRect | None:
        if self._origin is None or self._current is None:
            return None
        local_origin = self._origin - self.pos()
        local_current = self._current - self.pos()
        return QRect(local_origin, local_current).normalized()

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._bg.isNull():
            painter.drawPixmap(self.rect(), self._bg)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        rect = self._selection_rect()
        if rect is None or rect.width() < 2 or rect.height() < 2:
            return
        if self._live:
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            painter.fillRect(rect,QColor(0,0,0,1))
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(255,255,255),2));painter.drawRect(rect.adjusted(0,0,-1,-1))
            return
        src = QRect(
            int(rect.x() * self._bg.width() / max(1, self.width())),
            int(rect.y() * self._bg.height() / max(1, self.height())),
            int(rect.width() * self._bg.width() / max(1, self.width())),
            int(rect.height() * self._bg.height() / max(1, self.height())),
        )
        painter.drawPixmap(rect, self._bg, src)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.setPen(QPen(QColor(0, 120, 215), 1))
        painter.drawRect(rect.adjusted(1, 1, -2, -2))


class LearningActionMenu(QWidget):
    """Popup action list after a region is selected."""

    action_chosen = Signal(str)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Ativia · 框选学习")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        title = QLabel("想怎么理解这部分？")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)
        layout.addWidget(title)

        for label, command in LEARNING_ACTIONS:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, c=command: self._choose(c))
            layout.addWidget(button)

        cancel = QPushButton("取消")
        cancel.clicked.connect(self._cancel)
        layout.addWidget(cancel)

        self.setStyleSheet(
            """
            QWidget {
                background: #f5f5ef;
                color: #2d4b3a;
                border-radius: 10px;
                font-family: 'Microsoft YaHei', 'Segoe UI';
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #dce4d7;
                border-radius: 10px;
                padding: 11px 16px;
                font-size: 13px;
                text-align: left;
            }
            QPushButton:hover {
                background: #e7efdf;
                border-color: #aac2a6;
            }
            QLabel {
                font-size: 13px;
                color: #536d59;
                font-weight: 600;
                padding-bottom: 8px;
            }
            """
        )
        self.setMinimumWidth(280)
        self.adjustSize()

    def popup_near(self, global_pos: QPoint) -> None:
        self.adjustSize()
        geo = virtual_geometry()
        x = min(max(geo.left(), global_pos.x()), geo.right() - self.width())
        y = min(max(geo.top(), global_pos.y()), geo.bottom() - self.height())
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def _choose(self, command: str) -> None:
        self.hide()
        self.action_chosen.emit(command)

    def _cancel(self) -> None:
        self.hide()
        self.cancelled.emit()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)
