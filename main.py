from dotenv import load_dotenv
from PySide6.QtWidgets import QApplication



def main() -> None:
    import argparse
    from pathlib import Path
    import hashlib
    import json
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    parser=argparse.ArgumentParser();parser.add_argument('--paused',action='store_true')
    parser.add_argument('--window',action='store_true');parser.add_argument('--stop',action='store_true')
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--text-only',action='store_true');modes.add_argument('--voice',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    load_dotenv(root/'.env')
    app = QApplication([])
    app.setApplicationName("Ativia")
    app.setQuitOnLastWindowClosed(False)
    name='Ativia-daily-'+hashlib.sha256(str(root).lower().encode()).hexdigest()[:12]
    from device_profile import DeviceProfileStore
    device=DeviceProfileStore()
    requested='text' if args.text_only else 'voice' if args.voice else None
    mode=requested or device.load()
    (root/'local_tts/logs').mkdir(parents=True,exist_ok=True)
    lock=QLockFile(str(root/'local_tts/logs/daily.lock'))
    if not lock.tryLock(0):
        socket=QLocalSocket();socket.connectToServer(name)
        if not socket.waitForConnected(2000):raise SystemExit('Ativia 实例正在启动或失联；没有创建第二个实例。')
        payload=b'quit' if args.stop else json.dumps(dict(action='open',requested_mode=requested)).encode()
        socket.write(payload);socket.waitForBytesWritten(1500);socket.disconnectFromServer();return
    if args.stop:lock.unlock();return
    server=QLocalServer();server.setSocketOptions(QLocalServer.UserAccessOption);QLocalServer.removeServer(name)
    if not server.listen(name):lock.unlock();raise SystemExit('无法建立 Ativia 本机实例控制。')
    # Resolve the device preset before importing any audio-capable application code.
    if mode=='text':
        from text_session import TextSession
        overlay=TextSession(device,start_paused=args.paused)
    else:
        from overlay import OverlayApp
        overlay=OverlayApp(start_paused=True)
    if requested:device.save(requested)
    from daily_desktop import DailyDesktop
    from integrations.wechat.controller import WechatController
    overlay.wechat=WechatController(overlay)
    app.aboutToQuit.connect(overlay.wechat.close)
    daily=DailyDesktop(overlay)
    connections=[]
    def receive():
        socket=server.nextPendingConnection();connections.append(socket)
        def dispatch():
            command=bytes(socket.readAll()).decode('utf-8',errors='replace')
            if command=='quit':overlay._quit()
            elif command=='open':daily.open_chat()
            elif command=='hotkey-status':
                hotkey=getattr(overlay,'_hotkey',None)
                socket.write(json.dumps(hotkey.snapshot() if hotkey else {'available':False,'backend':'starting'}).encode());socket.flush()
            elif command=='resource-status':
                import os
                status=overlay.snapshot() if mode=='text' else dict(pid=os.getpid(),mode='voice',state=overlay._pause_gate.state)
                socket.write(json.dumps(status).encode());socket.flush()
            else:
                try:message=json.loads(command)
                except ValueError:message={}
                if str(message.get('action','')).startswith('wechat-'):
                    result=overlay.wechat.command(message)
                    def respond(value):
                        if socket.state()!=QLocalSocket.UnconnectedState:
                            socket.write(json.dumps(value,ensure_ascii=False).encode());socket.flush();socket.disconnectFromServer()
                    if hasattr(result,'done'):
                        def poll():
                            if result.done():
                                try:result.result();respond({'ok':not overlay.companion.memory.error})
                                except Exception as exc:respond({'error':type(exc).__name__})
                            else:QTimer.singleShot(30,poll)
                        poll();return
                    respond(result);return
                if message.get('action')=='open':
                    daily.open_chat()
                    if message.get('requested_mode') not in (None,mode):
                        overlay.control.set_failure('当前实例仍在运行。请先退出 Ativia，再从所选入口启动以切换设备运行方式。')
            socket.disconnectFromServer()
        socket.readyRead.connect(dispatch)
        socket.disconnected.connect(lambda:(connections.remove(socket) if socket in connections else None,socket.deleteLater()))
        if socket.bytesAvailable():dispatch()
    server.newConnection.connect(receive)
    app.aboutToQuit.connect(server.close);app.aboutToQuit.connect(lock.unlock)
    if args.window:daily.open_chat()
    print('Ativia 已启动：'+('仅文字 · 未启用麦克风。' if mode=='text' else '当前已暂停，点击恢复才启用保存的输入偏好。'),flush=True)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    try:main()
    except Exception as exc:
        from pathlib import Path
        import traceback
        log=Path(__file__).resolve().parent/'local_tts/logs/daily-start-error.log'
        log.parent.mkdir(parents=True,exist_ok=True);log.write_text('Startup failed: '+type(exc).__name__,encoding='utf-8')
        from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QPushButton
        app=QApplication.instance() or QApplication([])
        # A normal Qt dialog avoids Windows' audible critical-message notification.
        box=QDialog();box.setWindowTitle('Ativia 启动失败');layout=QVBoxLayout(box)
        label=QLabel(type(exc).__name__+'：启动未完成。\n安全诊断：'+str(log));label.setWordWrap(True);layout.addWidget(label)
        close=QPushButton('关闭');close.clicked.connect(box.accept);layout.addWidget(close);box.exec()
        raise SystemExit(1)
