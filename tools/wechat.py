"""Local administration of the SINGLE running Ativia instance. Never starts a second bot."""
import argparse,hashlib,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def rpc(message):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    app=QCoreApplication.instance() or QCoreApplication([])
    name='Ativia-daily-'+hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:12]
    socket=QLocalSocket();socket.connectToServer(name)
    if not socket.waitForConnected(2000):raise RuntimeError('Ativia 未运行。请先通过日常入口启动。')
    socket.write(json.dumps(message,ensure_ascii=False).encode());socket.waitForBytesWritten(2000)
    data=bytearray();deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        socket.waitForReadyRead(250);data.extend(bytes(socket.readAll()))
        try:return json.loads(data)
        except ValueError:pass
        if socket.state()==QLocalSocket.UnconnectedState:break
    raise RuntimeError('Ativia 本机管理请求未完成。')

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('reload');sub.add_parser('config-path')
    memory=sub.add_parser('memory');memory.add_argument('--person');memory.add_argument('--group')
    delete=sub.add_parser('delete');delete.add_argument('--id',required=True)
    args=parser.parse_args()
    if args.command=='config-path':
        from integrations.wechat.config import WechatConfigStore
        print(WechatConfigStore().path);return
    message={k:v for k,v in vars(args).items() if k!='command' and v is not None};message['action']='wechat-'+args.command
    print(json.dumps(rpc(message),ensure_ascii=False,indent=2))
if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,file=sys.stderr);raise SystemExit(1)
