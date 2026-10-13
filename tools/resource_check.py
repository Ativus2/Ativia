"""User-triggered one-minute process-tree check. Never starts a model or a request."""
import argparse,hashlib,json,os,sys,time
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket
from tools.windows_resources import sample_tree,summarize

app=QCoreApplication.instance() or QCoreApplication([])
NAME='Ativia-daily-'+hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:12]


def status():
    socket=QLocalSocket();socket.connectToServer(NAME)
    if not socket.waitForConnected(2000):raise RuntimeError('请先打开 Ativia 的 start-text-only.bat。')
    socket.write(b'resource-status');socket.waitForBytesWritten(1500)
    data=bytearray();deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        socket.waitForReadyRead(500);data.extend(bytes(socket.readAll()))
        if socket.state()==QLocalSocket.UnconnectedState:break
    socket.abort()
    if not data:raise RuntimeError('当前实例未提供资源状态，请退出并更新启动。')
    return json.loads(data)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--scenario',choices=['pet','chat','image','paused','repeated'])
    parser.add_argument('--output');args=parser.parse_args()
    before=status()
    if before['mode']!='text':raise RuntimeError('当前为语音模式。请退出后使用 start-text-only.bat，再运行本检查。')
    scenario=args.scenario
    if scenario is None:
        print('选择当前场景：1 仅挂桌宠  2 文字对话  3 手动截图问答  4 完全暂停  5 重复操作后')
        scenario={'1':'pet','2':'chat','3':'image','4':'paused','5':'repeated'}.get(input('输入编号：').strip())
        if scenario is None:raise RuntimeError('未选择有效场景。')
    print('采样一分钟；可按所选场景正常操作。程序不会自动聊天或截图。')
    started_at=datetime.now().isoformat()
    rows=[sample_tree(before['pid'])]
    while time.monotonic()-rows[0]['at']<60:
        time.sleep(2);rows.append(sample_tree(before['pid']))
        if not any(p['pid']==before['pid'] for p in rows[-1]['processes']):raise RuntimeError('Ativia 已退出，本次采样未完成。')
    after=status()
    if after['pid']!=before['pid']:raise RuntimeError('采样期间实例已切换，请重新检查。')
    report=dict(version=1,scenario=scenario,started_at=started_at,
        note='Only this daily instance and its descendants/venv launcher; unrelated browsers and 9883 experiments excluded. Monitor process excluded.',
        instance_before=before,instance_after=after,samples=rows,summary=summarize(rows))
    directory=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Ativia'/'resource-checks'
    path=Path(args.output) if args.output else directory/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+scenario+'.json')
    if path.exists():raise RuntimeError('输出文件已存在；请选择新的文件名，保留原始记录。')
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    summary=report['summary'];memory=summary['memory_end']
    print(f"CPU 一分钟平均：{summary['cpu_normalized_mean']:.3f}%（按全部逻辑处理器归一化）")
    print(f"私有驻留：{memory['private_resident_mib']:.1f} MiB；私有提交：{memory['private_commit_mib']:.1f} MiB")
    print(f"工作集相加：{memory['working_set_mib']:.1f} MiB（含共享页，不能当独占内存）")
    print('报告：'+str(path))


if __name__=='__main__':
    try:main()
    except Exception as exc:print('检查未完成：'+str(exc));raise SystemExit(1)
