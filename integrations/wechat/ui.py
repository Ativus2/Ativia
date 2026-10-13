"""Small optional entry in the existing Settings menu; no credentials of its own."""
from dataclasses import replace
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QDialog,QFormLayout,QCheckBox,QLineEdit,QPlainTextEdit,QLabel,QPushButton

STATUS={'disabled':'未启用','paused':'随 Ativia 完全暂停','connecting':'正在连接微信','listening':'正在监听白名单群',
'logged_in_window_missing':'wxauto 未识别已登录窗口；请运行微信诊断检查登录状态和控件兼容性',
'unsupported_client_version':'当前微信版本不受已安装 wxauto 支持','bridge_failed':'微信窗口连接失败',
'account_changed':'检测到账号变化，请检查账号后重新连接','listener_timeout':'微信监听超时，将重试',
'configuration_error':'微信配置无效','disconnected':'微信连接已断开','starting':'正在启动'}
class WechatDialog(QDialog):
    def __init__(self,controller,parent=None):
        super().__init__(parent);self.controller=controller;self.setWindowTitle('Ativia · 微信群接入');self.resize(520,520)
        form=QFormLayout(self)
        self.enabled=QCheckBox('启用微信群入口（随 Ativia 的完全暂停一起停止）');form.addRow(self.enabled)
        self.dry_run=QCheckBox('Dry Run（生成回复但不发送）');form.addRow(self.dry_run)
        self.preview=QLabel();self.preview.setTextFormat(Qt.PlainText);self.preview.setWordWrap(True);form.addRow(self.preview)
        self.groups=QPlainTextEdit();self.groups.setMaximumHeight(90);self.groups.setPlaceholderText('每行一个准确群名，最多 5 个');form.addRow('白名单群',self.groups)
        self.bot=QLineEdit();self.owner=QLineEdit();self.owner_ids=QLineEdit()
        form.addRow('机器人群昵称（逗号分隔）',self.bot);form.addRow('你的群昵称（逗号分隔）',self.owner);form.addRow('你的稳定微信 ID（可留空）',self.owner_ids)
        self.memory=QCheckBox('保存筛选后的群记忆');self.proactive=QCheckBox('偶尔主动参与（至少间隔 120 秒）');self.debug=QCheckBox('调试：额外记录新群消息的短文本')
        for field in (self.memory,self.proactive,self.debug):form.addRow(field)
        note=QLabel('复用当前 Ativia 的模型和人格。桌面私有记忆不会进入群聊。\n没有稳定微信 ID 时，昵称身份可能因重名或改名而不可靠。');note.setWordWrap(True);form.addRow(note)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.PlainText);form.addRow('状态',self.status)
        save=QPushButton('保存并重新连接');save.clicked.connect(self.save);form.addRow(save)
        close=QPushButton('关闭');close.clicked.connect(self.close);form.addRow(close)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.refresh)
        try:self.config=controller.store.load()
        except Exception:
            from .config import WechatConfig
            self.config=WechatConfig()
        c=self.config;self.enabled.setChecked(c.enabled);self.groups.setPlainText('\n'.join(c.allowed_groups))
        self.dry_run.setChecked(c.dry_run)
        self.bot.setText(', '.join(c.bot_names));self.owner.setText(', '.join(c.owner_names));self.owner_ids.setText(', '.join(c.owner_ids))
        self.memory.setChecked(c.memory_enabled);self.proactive.setChecked(c.proactive_enabled);self.debug.setChecked(c.debug)
    def showEvent(self,event):self.refresh();self.timer.start();super().showEvent(event)
    def hideEvent(self,event):self.timer.stop();super().hideEvent(event)
    def refresh(self):
        row=self.controller.snapshot();status=STATUS.get(row['status'],row['status'])
        label='DRY RUN · 不会向微信发送消息' if row.get('dry_run',self.config.dry_run) else '真实发送模式'
        self.status.setText(label+'\n'+status+' · '+row['app_state']+'\n接收 {} / 模型调用 {} / 已发送 {} / Dry Run {}'.format(row.get('received',0),row.get('model_calls',0),row.get('sent',0),row.get('dry_runs',0)))
        service=self.controller.service;preview=getattr(service,'preview',None)
        if preview:
            content='群：{} · 触发：{}\n模型：{} · 召回：{} · 候选：{}\nWould send {} messages · SEND BLOCKED'.format(preview['group'],preview['trigger'],preview.get('model','unknown'),preview.get('memory_recalled',0),preview['memory_candidate_queued'],len(preview['chunks']))
            if self.config.debug:
                from memory.gate import sensitive
                content+='\n发送人：'+preview['sender']+'\nInput: '+('[sensitive omitted]' if sensitive(preview['input']) else preview['input'])
                content+='\n'+'\n'.join('{}/{} {}'.format(i,len(preview['chunks']),'[sensitive omitted]' if sensitive(chunk) else chunk) for i,chunk in enumerate(preview['chunks'],1))
            self.preview.setText(content)
        else:self.preview.clear()
    def save(self):
        def names(text):return tuple(x.strip() for x in text.replace('，',',').split(',') if x.strip())
        try:
            config=replace(self.config,enabled=self.enabled.isChecked(),dry_run=self.dry_run.isChecked(),allowed_groups=tuple(x.strip() for x in self.groups.toPlainText().splitlines() if x.strip()),
                bot_names=names(self.bot.text()),owner_names=names(self.owner.text()),owner_ids=names(self.owner_ids.text()),
                memory_enabled=self.memory.isChecked(),proactive_enabled=self.proactive.isChecked(),debug=self.debug.isChecked())
            self.controller.store.save(config);self.config=config;self.controller.reload();self.refresh()
        except Exception as exc:self.status.setText('配置未保存：'+type(exc).__name__+'；请检查群名、机器人昵称和你的身份。')
