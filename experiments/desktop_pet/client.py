import json
import time
import uuid
from PySide6.QtCore import QObject,Signal,QTimer,QUrl
from PySide6.QtWebSockets import QWebSocket

class ControlClient(QObject):
    state_changed=Signal(dict);ack=Signal(dict);ready=Signal();quit_requested=Signal();open_requested=Signal()
    def __init__(self,key,port=9884,parent=None):
        super().__init__(parent);self.key=key;self.port=port;self.connected=False;self.initial=True;self.attempts=0
        self.socket=QWebSocket(parent=self);self.socket.connected.connect(self.hello)
        self.socket.textMessageReceived.connect(self.message);self.socket.disconnected.connect(self.disconnected)
        self.socket.errorOccurred.connect(lambda error:self.initial_retry())
        self.last_seen=time.monotonic();self.timer=QTimer(self);self.timer.setInterval(2000);self.timer.timeout.connect(self.ping)
    def connect_local(self):self.attempts+=1;self.socket.open(QUrl(f'ws://127.0.0.1:{self.port}/bridge'))
    def initial_retry(self):
        if self.initial and self.attempts<20:QTimer.singleShot(300,self.connect_local)
    def hello(self):self.send(dict(type='hello',role='control',key=self.key))
    def send(self,data):self.socket.sendTextMessage(json.dumps(data,ensure_ascii=False))
    def message(self,text):
        self.last_seen=time.monotonic()
        try:data=json.loads(text)
        except ValueError:return
        if data.get('type')=='welcome':self.connected=True;self.initial=False;self.timer.start();self.ready.emit()
        elif data.get('type')=='state':self.state_changed.emit(data)
        elif data.get('type')=='ack':self.ack.emit(data)
        elif data.get('type')=='shutdown_requested':self.quit_requested.emit()
        elif data.get('type')=='open_panel_requested':self.open_requested.emit()
    def command(self,command,extra=None):
        ident=uuid.uuid4().hex
        if not self.connected:self.ack.emit(dict(id=ident,phase='rejected',error='本机控制服务失联'));return ident
        self.send(dict(type='command',id=ident,command=command,**(extra or {})));return ident
    def ping(self):
        if time.monotonic()-self.last_seen>7:self.socket.close();return
        self.send(dict(type='ping'))
    def disconnected(self):
        self.connected=False;self.timer.stop()
        self.state_changed.emit(dict(state='UNKNOWN',mic_active=None,mic_enabled=True,cloud_connected=None,
            error='本机控制服务失联，禁止自动恢复收音。',instance='失联',caption=''))
    def close(self):self.initial=False;self.timer.stop();self.socket.close()
