"""Explicitly editable local memories; no DB work on the Qt thread."""
from datetime import datetime
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QTabWidget,QListWidget,QWidget,
    QPlainTextEdit,QLabel,QPushButton,QCheckBox,QSpinBox,QFormLayout,QComboBox)
from .gate import Candidate,sensitive,fact_key,domain

class MemoryDialog(QDialog):
    def __init__(self,runtime,parent=None):
        super().__init__(parent);self.runtime=runtime;self.service=runtime.memory
        self.setWindowTitle('Ativia · Memory');self.resize(660,530)
        self.identity=None;self.signature=None;self.pending=None
        layout=QVBoxLayout(self);self.tabs=QTabWidget();self.lists=[]
        for title in ('关于我','最近记忆','固定记忆'):
            items=QListWidget();items.currentItemChanged.connect(self.selected);self.lists.append(items);self.tabs.addTab(items,title)
        options=QWidget();form=QFormLayout(options)
        self.enabled=QCheckBox('保存明确偏好和重要事件');self.enabled.setChecked(self.service.enabled)
        self.recall=QCheckBox('回复时使用相关记忆');self.recall.setChecked(self.service.recall_enabled)
        self.budget=QSpinBox();self.budget.setRange(200,4000);self.budget.setValue(self.service.budget)
        form.addRow(self.enabled);form.addRow(self.recall);form.addRow('记忆上下文上限（保守 token 上界）',self.budget)
        note=QLabel('普通聊天不作为长期记忆保存。临时兴趣默认 14 天过期。\n关闭保存不会删除已有记忆；可在列表中逐条删除。')
        note.setWordWrap(True);form.addRow(note)
        save_options=QPushButton('保存记忆设置');save_options.clicked.connect(lambda:runtime.memory_options(
            self.enabled.isChecked(),self.recall.isChecked(),self.budget.value()));form.addRow(save_options)
        self.tabs.addTab(options,'记忆设置');layout.addWidget(self.tabs)
        self.metadata=QLabel();self.metadata.setWordWrap(True);self.metadata.setTextFormat(Qt.PlainText);layout.addWidget(self.metadata)
        scopes=QHBoxLayout();self.visibility=QComboBox()
        for label,value in (('私有（仅桌面）','private'),('当前群','group'),('公开（跨渠道）','public')):self.visibility.addItem(label,value)
        self.scope_button=QPushButton('修改这条记忆的可见范围');self.scope_button.clicked.connect(self.change_visibility)
        scopes.addWidget(self.visibility);scopes.addWidget(self.scope_button);layout.addLayout(scopes)
        self.editor=QPlainTextEdit();self.editor.setPlaceholderText('选择一条记忆进行修改，或添加你希望 Ativia 记住的偏好。');self.editor.setMaximumHeight(120);layout.addWidget(self.editor)
        buttons=QHBoxLayout()
        self.new=QPushButton('新记忆');self.save=QPushButton('保存');self.remove=QPushButton('删除')
        self.pin=QPushButton('Pin / Unpin');self.close_button=QPushButton('关闭')
        for w in (self.new,self.save,self.remove,self.pin,self.close_button):buttons.addWidget(w)
        layout.addLayout(buttons);self.status=QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        self.new.clicked.connect(self.new_memory);self.save.clicked.connect(self.save_memory)
        self.remove.clicked.connect(lambda:self.change('delete'));self.pin.clicked.connect(self.toggle_pin);self.close_button.clicked.connect(self.close)
        self.timer=QTimer(self);self.timer.setInterval(150);self.timer.timeout.connect(self.refresh)
        self.tabs.currentChanged.connect(self.tab_changed)
        self.refresh()

    def showEvent(self,event):self.timer.start();self.refresh();super().showEvent(event)
    def hideEvent(self,event):self.timer.stop();super().hideEvent(event)
    def refresh(self):
        if self.pending and self.pending.done():
            self.pending=None;self.status.setText('已更新。' if not self.service.error else '记忆操作暂不可用：'+self.service.error)
        rows=self.service.snapshot
        sig=tuple((r['id'],r['content'],r['pinned'],r.get('updated_at')) for r in rows)
        if sig!=self.signature:
            self.signature=sig
            for index,items in enumerate(self.lists):
                items.blockSignals(True);items.clear()
                chosen=[r for r in rows if (r['layer']=='semantic' if index==0 else r['layer']=='episodic' if index==1 else r['pinned'])]
                for r in chosen:
                    scope=(r.get('subject_name') or r.get('subject_id',''))+' · '+r.get('visibility','private')
                    prefix='['+scope+'] ' if r.get('conversation_type')=='group' else ''
                    items.addItem(('★ ' if r['pinned'] else '')+prefix+r['content'])
                    item=items.item(items.count()-1);item.setData(Qt.UserRole,r['id'])
                    if r['id']==self.identity:items.setCurrentItem(item)
                items.blockSignals(False)
            row=next((r for r in rows if r['id']==self.identity),None)
            if row:self.show_metadata(row)
            elif self.identity:self.new_memory()
        if self.service.error:self.status.setText('记忆暂不可用：'+self.service.error+'；聊天和字幕可继续使用。')
        elif not self.service.ready.is_set():self.status.setText('正在读取本地记忆…')
        self.scope_button.setEnabled(bool(self.identity));self.remove.setEnabled(bool(self.identity));self.pin.setEnabled(bool(self.identity))
    def selected(self,item,old=None):
        if not item:return
        self.identity=item.data(Qt.UserRole)
        row=next((r for r in self.service.snapshot if r['id']==self.identity),None)
        if not row:return
        self.editor.setPlainText(row['content'])
        self.remove.setEnabled(True);self.pin.setEnabled(True)
        self.show_metadata(row)
    def show_metadata(self,row):
        self.visibility.setCurrentIndex(self.visibility.findData(row.get('visibility','private')))
        created=datetime.fromtimestamp(row['created_at']).strftime('%Y-%m-%d %H:%M')
        scope=f"对象：{row.get('subject_name') or row.get('subject_id','owner:local')} · 范围：{row.get('visibility','private')} · 群：{row.get('group_id') or '无'}\n"
        self.metadata.setText(scope+f"{row['layer']} · {row['classification']} · 来源 {row['source']} · {created}\n使用次数 {row['usage_count']}；固定："+('是' if row['pinned'] else '否'))
    def tab_changed(self,index):
        item=self.lists[index].currentItem() if index<len(self.lists) else None
        if item:self.selected(item)
        else:self.new_memory()
        self.editor.setEnabled(index<len(self.lists));self.save.setEnabled(index<len(self.lists));self.new.setEnabled(index<len(self.lists))
    def new_memory(self):
        self.identity=None;self.editor.clear();self.metadata.setText('只保存你明确填写的这条记忆。');self.refresh()
    def save_memory(self):
        text=self.editor.toPlainText().strip()
        if not text or len(text)>400 or sensitive(text):
            self.status.setText('内容应为 1–400 字，不能包含密钥、密码或音频数据。');return
        if self.identity:self.pending=self.service.change('edit',self.identity,text)
        else:self.pending=self.service.change('save',Candidate(text,'PERMANENT','semantic','preference',.9,
            source='explicit_user_edit',mode=domain(text),fact_key=fact_key(text)))
        self.status.setText('正在保存…')
    def change(self,action,*args):
        if not self.identity:return
        self.pending=self.service.change(action,self.identity,*args)
        if action=='delete':self.identity=None;self.editor.clear();self.metadata.clear()
    def change_visibility(self):
        row=next((r for r in self.service.snapshot if r['id']==self.identity),None)
        if not row:return
        value=self.visibility.currentData();group=row.get('group_id','')
        if value=='group' and not group:self.status.setText('这条记忆没有所属群；请在微信群记忆中选择。');return
        self.change('set_visibility',value,group)
        self.status.setText('正在修改可见范围…')
    def toggle_pin(self):
        row=next((r for r in self.service.snapshot if r['id']==self.identity),None)
        if row:self.change('pin',not row['pinned'])
