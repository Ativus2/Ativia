"""Owned native-voice broker + transparent Qt pet; browser opens only on explicit action."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from PySide6.QtCore import QLockFile,QTimer,Qt
from PySide6.QtWidgets import QApplication,QMessageBox

HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT))
from experiments.desktop_pet.client import ControlClient
from experiments.desktop_pet.window import PetWindow
from experiments.desktop_pet.owned_process import OwnedProcess

class Desktop:
    def __init__(self,app, *, port=9884, limit=3, open_panel=False, pet_settings_path=None):
        self.app=app;self.port=port;self.exiting=False;self.browser=None
        self.logs=HERE/'logs';self.logs.mkdir(exist_ok=True)
        self.lock=QLockFile(str(self.logs/'desktop.lock'));self.lock.setStaleLockTime(0)
        if not self.lock.tryLock(0):raise RuntimeError('桌宠试验品已运行；请使用系统托盘，避免重复收音。')
        self.server=None;self.runtime=self.logs/'runtime.json'
        try:
            with socket.socket() as check:
                if check.connect_ex(('127.0.0.1',port))==0:raise RuntimeError(f'{port} 已被占用。请先结束已有原生语音服务；不会接管其它实例。')
            key=secrets.token_urlsafe(32);env=os.environ.copy();env['ATIVIA_CONTROL_KEY']=key
            python=ROOT/'experiments/realtime_prototype/.venv/Scripts/python.exe'
            self.server=OwnedProcess([str(python),'server.py','--port',str(port),'--session-limit',str(limit)],
                cwd=str(ROOT/'experiments/native_voice'),env=env,log=self.logs/'service.log')
            self.runtime.write_text(json.dumps(dict(pid=os.getpid(),port=port,key=key)),encoding='utf-8')
            self.pet=PetWindow(pet_settings_path);self.client=ControlClient(key,port)
            self.pet.command.connect(self.command);self.pet.open_panel.connect(self.open_browser);self.pet.quit_requested.connect(self.exit)
            self.client.state_changed.connect(self.state);self.client.ack.connect(self.ack)
            self.client.quit_requested.connect(self.exit);self.client.open_requested.connect(self.open_browser)
            self.client.ready.connect(lambda:self.open_browser() if open_panel else None)
            self.pet.show();QTimer.singleShot(150,self.client.connect_local)
        except Exception:
            if self.server:self.server.close()
            self.lock.unlock();raise

    def command(self,name,extra):
        if self.exiting:return
        if name=='start' and not self.pet.state.get('page_connected'):
            self.open_browser()
        self.client.command(name,extra)
    def ack(self,data):
        if data.get('error') and not self.exiting:
            self.pet.tray.showMessage('Ativia · 操作尚未完成',str(data['error']),msecs=3500)
    def state(self,data):
        self.pet.update_state(data)
        if self.exiting and data.get('state') in ('PAUSED','ENDED') and data.get('mic_active') is False and data.get('cloud_connected') is False:
            QTimer.singleShot(0,self.finish_exit)
    def open_browser(self):
        if self.exiting:return
        if self.browser and self.browser.running():
            # Raise only our dedicated browser window, and only after an explicit click.
            user=ctypes.WinDLL('user32');pid=self.browser.process.pid;found=[]
            callback=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
            def visit(hwnd,lparam):
                value=ctypes.c_ulong();user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),ctypes.byref(value))
                if value.value==pid and user.IsWindowVisible(ctypes.c_void_p(hwnd)):found.append(hwnd)
                return True
            user.EnumWindows(callback(visit),0)
            if found:
                user.ShowWindow(ctypes.c_void_p(found[0]),9);user.SetForegroundWindow(ctypes.c_void_p(found[0]));return
            self.browser.close();self.browser=None
        edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        if not edge.exists():
            QMessageBox.warning(self.pet,'浏览器未找到','未找到 Microsoft Edge。桌宠保持不收音。');return
        browser_env=os.environ.copy();browser_env.pop('OPENAI_API_KEY',None);browser_env.pop('ATIVIA_CONTROL_KEY',None)
        self.browser=OwnedProcess([str(edge),f'--app=http://127.0.0.1:{self.port}',
            '--user-data-dir='+str(self.logs/'browser-profile'),'--no-first-run','--disable-background-mode',
            '--disable-features=msEdgeSidebarV2,msStartupBoost'],cwd=str(HERE),env=browser_env,visible=True)
    def exit(self):
        if self.exiting:return
        self.exiting=True;self.pet.bar.hide();self.pet.bubble.hide()
        self.pet.update_state({**self.pet.state,'state':'PAUSING','caption':'','error':'正在结束此启动器拥有的会话与服务。'})
        self.client.command('end')
        QTimer.singleShot(2500,self.finish_exit)
    def finish_exit(self):
        if getattr(self,'finished',False):return
        self.finished=True
        try:
            stopped=self.pet.state.get('state') in ('PAUSED','ENDED') and self.pet.state.get('mic_active') is False and self.pet.state.get('cloud_connected') is False
            if not stopped and self.browser is None and self.pet.state.get('mic_active') is not False:
                raise RuntimeError('Unowned browser resource cleanup is not confirmed')
            # If a page cannot acknowledge, close ONLY its owned process job, releasing its device handles.
            if self.browser:self.browser.close();self.browser=None
            self.client.close()
            if self.server:self.server.close();self.server=None
            self.pet.dispose();self.runtime.unlink(missing_ok=True);self.lock.unlock();self.app.quit()
        except Exception as exc:
            self.finished=False;self.exiting=False
            self.pet.update_state({**self.pet.state,'state':'FAILED','error':'退出清理失败：'+type(exc).__name__})
            QMessageBox.warning(self.pet,'退出尚未完成','资源清理失败，请重试退出。'+type(exc).__name__)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=9884)
    parser.add_argument('--session-limit',type=int,default=3);parser.add_argument('--open-panel',action='store_true')
    parser.add_argument('--probe',action='store_true');args=parser.parse_args()
    app=QApplication(sys.argv);app.setQuitOnLastWindowClosed(False)
    try:desktop=Desktop(app,port=args.port,limit=args.session_limit,open_panel=args.open_panel)
    except Exception as exc:
        if args.open_panel and (HERE/'logs/runtime.json').exists():
            try:
                runtime=json.loads((HERE/'logs/runtime.json').read_text(encoding='utf-8'))
                client=ControlClient(runtime['key'],runtime['port'])
                client.ready.connect(lambda:client.command('focus'))
                client.ack.connect(lambda data:app.quit() if data.get('phase') in ('accepted','completed','rejected') else None)
                QTimer.singleShot(0,client.connect_local);QTimer.singleShot(4000,app.quit);app.exec();return 0
            except Exception:pass
        if args.probe:print(type(exc).__name__+': '+str(exc))
        else:QMessageBox.warning(None,'Ativia 桌宠未启动',str(exc))
        return 2
    return app.exec()

if __name__=='__main__':raise SystemExit(main())
