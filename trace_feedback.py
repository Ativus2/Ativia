"""Human labels on immutable trace IDs; no conversation or model callbacks."""
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from character_trace import REASONS

STATUSES = {"displayed": "已发言", "silent": "保持安静", "cancelled": "已取消",
            "skipped": "跳过观察", "error": "处理错误"}


def readable_trace(row):
    trace = row["trace"]
    delivery = trace["delivery"]
    context = trace.get("context", {})
    observation = trace.get("accepted_observation") or trace.get("candidate")
    parts = ["实际结果 · " + STATUSES.get(delivery["status"], delivery["status"]),
             delivery.get("response") or delivery.get("error") or "这次没有展示角色发言。",
             "", "本次输入", context.get("user_command") or context.get("user_spoken") or "观察屏幕（没有明确提问）"]
    if context.get("transcript"):
        parts.extend(["听觉文字线索：" + context["transcript"]])
    if context.get("previous_summary"):
        parts.extend(["", "本次学习的原回答", context["previous_summary"]])
    if isinstance(observation, dict):
        parts.extend(["", "程序对画面的理解（可能有误）", observation.get("subject", ""), observation.get("evidence", "")])
    if trace.get("reason") == "unchanged_screen_without_user_speech":
        parts.extend(["", "画面与上次相近且没有新用户语音，未请求模型分析。"])
    if context.get("recent_context"):
        parts.extend(["", "请求时的近期交流", context["recent_context"]])
    memory = trace.get("state_before", {}).get("request_memory", trace.get("state_before", {}).get("memory", []))
    if memory:
        parts.extend(["", "请求时的近期观察"])
        for entry in memory:
            old = entry["observation"]
            parts.append(f"• {old['subject']}：{old['evidence']}（{'曾发言' if entry['announced'] else '未发言'}）")
    if trace.get("truncated_fields"):
        parts.extend(["", "此记录有字段被截短；完整 Trace 中列出了字段名称。"])
    return "\n".join(parts)


class TraceFeedbackDialog(QDialog):
    collection_changed = Signal()

    def __init__(self, collector, parent=None):
        super().__init__(parent)
        self.collector = collector
        self.setWindowTitle("Character Trace · 本地记录与反馈")
        self.resize(820, 740)
        self._selected_id = None
        self._selected_row = None
        self._refreshing = False
        layout = QVBoxLayout(self)
        notice = QLabel("开启后，本地保存文字上下文、观察、候选回复、决策与反馈。可能含屏幕或对话中的私人内容。\n"
                        "不保存截图或音频文件；不额外上传、不用于自动学习。最多保留 1000 条，未反馈不代表失败。\n"
                        "关闭采集会丢弃当前未完成的记录；每次启动默认关闭。导出前请检查敏感内容。")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.recording = QCheckBox("开启本次会话的本地采集")
        self.recording.setChecked(collector.enabled)
        self.recording.toggled.connect(self._toggle)
        layout.addWidget(self.recording)
        self.records = QListWidget()
        self.records.setAccessibleName("最近的 Character Trace，包含沉默与跳过的观察")
        self.records.currentItemChanged.connect(self._select)
        layout.addWidget(self.records, 2)
        self.raw_view = QCheckBox("查看完整 Trace（调试字段）")
        self.raw_view.toggled.connect(self._render_details)
        layout.addWidget(self.raw_view)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("选择一条记录，核对上下文和实际回应后再反馈。")
        self.details.setAccessibleName("所选记录的完整文字轨迹")
        layout.addWidget(self.details, 2)
        row = QHBoxLayout()
        self.rating = QComboBox()
        for title, value in (("未标注／撤回反馈", "unrated"), ("成功：这次表现合适", "success"),
                             ("失败：这次表现不合适", "failure")):
            self.rating.addItem(title, value)
        self.reason = QComboBox()
        for key, title in REASONS.items():
            self.reason.addItem(title, key)
        row.addWidget(self.rating)
        row.addWidget(self.reason)
        self.save_button = QPushButton("保存所选记录的反馈")
        self.save_button.clicked.connect(self._save)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.comment = QPlainTextEdit()
        self.comment.setMaximumHeight(80)
        self.comment.setPlaceholderText("可选：哪里好／不好，或你期待的反应（最多 1000 字）。反馈不会发给角色。")
        layout.addWidget(self.comment)
        actions = QHBoxLayout()
        self.export_button = QPushButton("导出 traces / success / failure")
        self.export_button.clicked.connect(self._export)
        self.clear_button = QPushButton("清空本地记录与反馈")
        self.clear_button.clicked.connect(self._clear)
        actions.addWidget(self.export_button)
        actions.addWidget(self.clear_button)
        layout.addLayout(actions)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh()

    def _toggle(self, enabled):
        self.collector.set_enabled(enabled)
        self.collection_changed.emit()
        self.refresh()

    def refresh(self):
        # Never move feedback to a newer response or erase an in-progress comment.
        selected = self._selected_id
        try:
            rows = self.collector.store.recent()
        except Exception:
            self.status.setText("无法读取本地记录；桌宠仍可正常使用。")
            return
        self._refreshing = True
        self.records.clear()
        selected_item = None
        labels = {"unrated": "未标注", "success": "成功", "failure": "失败"}
        for row in rows:
            item = QListWidgetItem(f"{row['created']}  ·  {STATUSES.get(row['status'], row['status'])}  ·  {labels[row['label']]}\n{row['preview']}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.records.addItem(item)
            if row["id"] == selected:
                selected_item = item
        if selected_item is not None:
            self.records.setCurrentItem(selected_item)
        self._refreshing = False
        if selected is not None and selected_item is None:
            self._select(None, None)
        self.save_button.setEnabled(self._selected_id is not None)
        self.status.setText(self.collector.last_error or f"本地记录 {len(rows)} 条 · {self.collector.store.path}")

    def _select(self, current, previous):
        if self._refreshing:
            return
        self._selected_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.save_button.setEnabled(self._selected_id is not None)
        self.details.clear()
        self._selected_row = None
        self.comment.clear()
        if self._selected_id is None:
            return
        try:
            row = self.collector.store.get(self._selected_id)
        except Exception:
            self.status.setText("这条记录不可用，请刷新后重试。")
            self.save_button.setEnabled(False)
            return
        self._selected_row = row
        self._render_details()
        feedback = row["feedback"] or dict(label="unrated", reason="other", comment="")
        self.rating.setCurrentIndex(self.rating.findData(feedback["label"]))
        self.reason.setCurrentIndex(self.reason.findData(feedback["reason"]))
        self.comment.setPlainText(feedback["comment"])

    def _render_details(self):
        if self._selected_row is not None:
            self.details.setPlainText(json.dumps(self._selected_row, ensure_ascii=False, indent=2)
                                      if self.raw_view.isChecked() else readable_trace(self._selected_row))

    def _save(self):
        if self._selected_id is None:
            return
        try:
            self.collector.store.label(self._selected_id, self.rating.currentData(),
                                       self.reason.currentData(), self.comment.toPlainText())
        except Exception:
            self.status.setText("反馈未保存：检查说明是否超过 1000 字，以及记录文件是否可写。")
            return
        self.refresh()
        self._select(self.records.currentItem(), None)
        self.status.setText("已保存；只更新这条记录的标签，不会影响角色行为。")

    def _export(self):
        parent = QFileDialog.getExistingDirectory(self, "选择本地导出目录（包含所有文字轨迹和反馈，请检查隐私）")
        if not parent:
            return
        try:
            destination = self.collector.store.export(parent)
            self.status.setText(f"已导出到 {destination}；未标注记录只在 traces.jsonl 中。")
        except Exception:
            self.status.setText("导出失败。没有 manifest.json 的导出目录是不完整的，请勿使用。")

    def _clear(self):
        if QMessageBox.question(self, "清空本地采集数据", "删除本机采集库里的所有记录和反馈？已导出的文件需要另行删除。") != QMessageBox.StandardButton.Yes:
            return
        self.recording.setChecked(False)
        # Also invalidate pending drafts when the checkbox was already off.
        self.collector.set_enabled(False)
        self.collection_changed.emit()
        try:
            self.collector.store.clear()
            self.refresh()
            self.status.setText("本地记录和反馈已清空；采集已关闭。")
        except Exception:
            self.status.setText("清理未完成，请检查本地记录文件是否被占用。")
