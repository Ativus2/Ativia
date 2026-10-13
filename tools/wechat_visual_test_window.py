"""Owned local input/capture fixture. It has no network or external message capability."""
import argparse,json,os,sys,time
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLabel,QPlainTextEdit,QPushButton

def main():
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);p.add_argument('--nonce',required=True)
    args=p.parse_args();args.directory.mkdir(parents=True,exist_ok=True)
    app=QApplication([]);w=QWidget();w.setWindowTitle('Ativia Visual Test '+args.nonce)
    w.setGeometry(60,70,860,610);w.setStyleSheet('QWidget{background:#f7f7f7;color:#202020;font:18px "Microsoft YaHei";} QPlainTextEdit{background:white;border:2px solid #3069ca;} QPushButton{padding:12px;background:#d8e8ff;}')
    layout=QVBoxLayout(w);title=QLabel('Ativia 本地视觉与输入测试窗口');layout.addWidget(title)
    tick=QLabel();tick.setMinimumHeight(70);layout.addWidget(tick)
    layout.addWidget(QLabel('仅本地测试 · 无微信和网络功能'))
    edit=QPlainTextEdit();edit.setObjectName('audit_input');layout.addWidget(edit)
    button=QPushButton('提交到本地计数器');button.setObjectName('audit_submit');layout.addWidget(button)
    status=QLabel('提交次数：0');layout.addWidget(status)
    state=dict(nonce=args.nonce,pid=os.getpid(),submits=0,last_submitted='',tick=0,closed=False);owned_dummies=[]
    def save():
        state.update(hwnd=int(w.winId()),text=edit.toPlainText(),focused=app.activeWindow() is w,
                     input_focused=edit.hasFocus(),window=[w.x(),w.y(),w.width(),w.height()],
                     input_client=[edit.x(),edit.y(),edit.width(),edit.height()],
                     submit_client=[button.x(),button.y(),button.width(),button.height()])
        tmp=args.directory/'fixture.tmp';tmp.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8');tmp.replace(args.directory/'fixture.json')
    def pulse():
        if (args.directory/'focus.fixture').exists():
            command=(args.directory/'focus.fixture').read_text(encoding='utf-8');(args.directory/'focus.fixture').unlink()
            if command==args.nonce:w.raise_();w.activateWindow()
        if (args.directory/'reset.fixture').exists():
            command=(args.directory/'reset.fixture').read_text(encoding='utf-8');(args.directory/'reset.fixture').unlink()
            if command==args.nonce:edit.clear()
        if (args.directory/'dummy.fixture').exists():
            command=json.loads((args.directory/'dummy.fixture').read_text(encoding='utf-8'));(args.directory/'dummy.fixture').unlink()
            if command.get('owner_nonce')==args.nonce:
                dummy=QWidget();dummy.setWindowTitle('Ativia Visual Test '+command['nonce']);dummy.setGeometry(100,100,440,180)
                dummy_layout=QVBoxLayout(dummy);dummy_layout.addWidget(QLabel('本地焦点中断测试窗口，无输入和外发功能'))
                owned_dummies.append(dummy);dummy.show();dummy.raise_();dummy.activateWindow()
                state['dummy_hwnd']=int(dummy.winId());state['dummy_nonce']=command['nonce']
        state['tick']+=1;tick.setText('动态序号 %06d  |  中文 English 123'%state['tick'])
        tick.setStyleSheet('background:%s;padding:12px;'%('#cce9ff' if state['tick']%2 else '#ffe6b8'));save()
        if (args.directory/'stop.fixture').exists():w.close();app.quit()
    def submit():
        state['submits']+=1;state['last_submitted']=edit.toPlainText();status.setText('提交次数：'+str(state['submits']));save()
    button.clicked.connect(submit);edit.textChanged.connect(save)
    w.show();timer=QTimer(w);timer.timeout.connect(pulse);timer.start(500);pulse()
    code=app.exec();state['closed']=True;save();return code

if __name__=='__main__':raise SystemExit(main())
