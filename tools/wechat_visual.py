"""Visual diagnostics and continuous UI. Sending requires current-binding opt-in."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def server_name():
    identity = str(ROOT).casefold() + "|" + os.environ.get("USERNAME", "local")
    return "Ativia-visual-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def control_existing(action='stop'):
    from PySide6.QtNetwork import QLocalSocket
    socket = QLocalSocket()
    socket.connectToServer(server_name())
    if not socket.waitForConnected(1500):
        print(json.dumps({"transport": "visual", "status": "NOT_RUNNING"}))
        return 0
    socket.write((json.dumps({'action':action})+'\n').encode())
    socket.waitForBytesWritten(1000)
    socket.waitForReadyRead(1500)
    result = bytes(socket.readAll())
    socket.disconnectFromServer()
    if not result.strip():
        print(json.dumps({"status": "STOP_NOT_CONFIRMED"}))
        return 2
    print(result.decode().strip())
    return 0


def stop_existing():
    return control_existing('stop')


class MainStatusMonitor:
    """The existing main-instance RPC runs outside the visual/UI event loop."""
    def __init__(self):
        self.rows = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="ativia-visual-main-status", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        from tools.wechat import rpc
        while not self.stopped.is_set():
            try:
                row = rpc({"action": "wechat-status"})
            except Exception:
                row = None
            try:
                self.rows.put_nowait(row)
            except queue.Full:
                try:
                    self.rows.get_nowait()
                except queue.Empty:
                    pass
                self.rows.put_nowait(row)
            self.stopped.wait(1)

    def close(self):
        self.stopped.set()


def create_window(coordinator, stop):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QCheckBox, QPushButton, QTextEdit, QHBoxLayout, QMessageBox

    class PreviewWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Ativia 微信视觉通道 · 本地预览 · 微信输入禁用")
            self.self_test_mode = coordinator.action == "self-test"
            self.continuous = coordinator.action == 'run'
            if self.continuous:
                self.setWindowTitle('Ativia 微信视觉通道 · 真实发送默认关闭')
            if self.self_test_mode:
                self.setWindowTitle("Ativia 微信 · 单次自身消息自测 · 仅本地预览")
            self.resize(720, 610)
            layout = QVBoxLayout(self)
            self.status = QLabel()
            self.status.setTextFormat(Qt.PlainText)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.candidate = QLabel("等待真实窗口标题和布局；尚未绑定。")
            self.candidate.setTextFormat(Qt.PlainText)
            self.candidate.setWordWrap(True)
            layout.addWidget(self.candidate)
            self.challenge = QTextEdit()
            self.challenge.setReadOnly(True)
            self.challenge.setMaximumHeight(90)
            self.challenge.setPlaceholderText("等待正式 Ativia 运行状态确认后生成本次测试文字。")
            self.challenge.setVisible(self.self_test_mode)
            layout.addWidget(self.challenge)
            self.self_sent = QCheckBox("我已用当前微信登录账号亲手发送上面的本次测试文字，并已回到此预览窗口")
            self.self_sent.setVisible(self.self_test_mode)
            layout.addWidget(self.self_sent)
            self.confirmed = QCheckBox("我确认当前打开的是“Ativia测试群”，聊天已在最新消息底部")
            layout.addWidget(self.confirmed)
            self.confirm_button = QPushButton("确认当前窗口与底部，建立本次绑定")
            if self.self_test_mode:
                self.confirm_button.setText("确认并读取本次自身消息（仅一次）")
            self.confirm_button.clicked.connect(self.confirm)
            layout.addWidget(self.confirm_button)
            controls = QHBoxLayout()
            self.pause = QPushButton("暂停视觉监听")
            self.pause.clicked.connect(self.toggle_pause)
            controls.addWidget(self.pause)
            close = QPushButton("停止自有 worker 并关闭预览")
            close.clicked.connect(stop)
            controls.addWidget(close)
            layout.addLayout(controls)
            self.send_status = QLabel('真实发送：OFF / SEND_DISABLED')
            self.send_status.setTextFormat(Qt.PlainText)
            self.send_status.setWordWrap(True)
            self.send_status.setVisible(self.continuous)
            layout.addWidget(self.send_status)
            send_controls=QHBoxLayout()
            self.send_enable=QPushButton('开启真实发送')
            self.send_enable.clicked.connect(self.enable_send)
            self.send_disable=QPushButton('关闭发送（保留监听与预览）')
            self.send_disable.clicked.connect(coordinator.disable_send if self.continuous else lambda:None)
            for widget in (self.send_enable,self.send_disable):
                widget.setVisible(self.continuous);send_controls.addWidget(widget)
            layout.addLayout(send_controls)
            self.send_text=QTextEdit()
            self.send_text.setPlainText('Ativia微信发送测试01')
            self.send_text.setMaximumHeight(60)
            self.send_text.setVisible(self.continuous)
            layout.addWidget(self.send_text)
            self.send_test=QPushButton('发送上面的测试文字到当前目标群')
            self.send_test.setVisible(self.continuous)
            self.send_test.clicked.connect(self.test_send)
            layout.addWidget(self.send_test)
            self.preview = QTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setPlaceholderText("模型回复仅显示在这里，不会写入微信输入框。")
            if self.continuous:
                self.preview.setPlaceholderText('模型回复预览。真实发送关闭时仅在本地显示；开启后发送结果见上方。')
            layout.addWidget(self.preview)
            self.notice = QLabel("请等 GROUP_BOUND → BASELINE_COMPLETE → LISTENER_READY 后，再人工安排入站测试。")
            self.notice.setTextFormat(Qt.PlainText)
            self.notice.setWordWrap(True)
            layout.addWidget(self.notice)
            self._preview_id = None
            self._candidate_key = None
            self._challenge_text = None

        def confirm(self):
            try:
                coordinator.confirm_candidate(bottom_confirmed=self.confirmed.isChecked(),
                                              self_sent_confirmed=self.self_sent.isChecked())
                self.notice.setText("已提交本次确认；等待读取指定的右侧自身消息。请保持此预览在前台。" if self.self_test_mode else
                                    "已提交本次确认；等待稳定基线和监听就绪。请保持微信非最小化，并将焦点移回此预览窗口。")
                self.confirmed.setChecked(False)
                self.self_sent.setChecked(False)
            except Exception:
                self.notice.setText("当前画面、暂停状态或底部确认不满足条件；等待新鲜候选后重新确认。")

        def toggle_pause(self):
            coordinator.set_user_paused(not coordinator.user_paused)
            self.pause.setText("恢复视觉监听（需要新基线与绑定）" if coordinator.user_paused else "暂停视觉监听")

        def enable_send(self):
            context=coordinator.send_context
            if not context or not coordinator.listener_ready:
                self.notice.setText('请先完成 GROUP_BOUND → BASELINE_COMPLETE → LISTENER_READY。')
                return
            answer=QMessageBox.question(self,'开启当前群的真实微信发送',
                '目标：Ativia测试群\n当前绑定已就绪。开启后允许连续回复不同消息，不会每条重新确认。\n'
                '可随时关闭发送、暂停或停止。切群或绑定失效后需重新开启。\n\n现在开启当前绑定的真实发送？',
                QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)
            if answer!=QMessageBox.Yes:
                return
            try:
                if coordinator.send_context!=context:
                    raise ValueError('binding changed during confirmation')
                coordinator.enable_send(confirmed=True)
                self.notice.setText('真实发送已开启。只有新的合法回复或你点击的测试文字会进入发送队列。')
            except Exception:
                self.notice.setText('当前绑定或暂停状态已变化，未启用发送。')

        def test_send(self):
            try:
                coordinator.send_test_message(self.send_text.toPlainText())
                self.send_test.setEnabled(False)
                self.notice.setText('已加入发送队列。请暂时保持预览前台、鼠标键盘静止，等待发送与视觉确认。')
            except Exception:
                self.notice.setText('未接受发送：请检查发送开关、绑定和文本（1–1800字）；不要重复点击同一待处理任务。')

        def refresh(self):
            state = coordinator.snapshot()
            self.pause.setText('恢复视觉监听（需要新基线与绑定）' if coordinator.user_paused else '暂停视觉监听')
            remaining = state["model_remaining"]
            self.status.setText(
                f"Transport=visual | 状态={state['status']} | Capture={state['capture']}\n"
                f"Memory=shadow | DryRun=ON | 微信点击/按键/粘贴/发送=0 | 模型剩余={remaining if remaining is not None else '待核验'}\n"
                f"帧={state['frames']} | 入站事件={state['events']} | 绑定={state['bound']} | 主应用/本地暂停={state['paused']}")
            if self.continuous:
                self.status.setText(f"Transport=visual | 状态={state['status']} | 目标=Ativia测试群\n"
                    f"群记忆={state.get('memory','persistent_group')} | 处理={state.get('pipeline_status','等待就绪')}\n"
                    f"帧={state['frames']} | 入站事件={state['events']} | 主应用/本地暂停={state['paused']}")
                send=state.get('send') or {}
                armed=send.get('enabled',False)
                self.send_status.setText(f"真实发送：{'ON' if armed else 'OFF'} / {send.get('state','SEND_DISABLED')}\n"
                    f"提交={send.get('submit_attempts',0)} | 确认={send.get('confirmed',0)} | 未确认={send.get('uncertain',0)} | "
                    f"失败={send.get('failed',0)} | 取消={send.get('cancelled',0)} | 队列={send.get('queue_size',0)}\n"
                    f"最近结果：{send.get('last_result') or '尚无发送'}")
                recovered=send.get('recovered_counts',{}).get('SEND_UNCERTAIN',0)
                if recovered:self.send_status.setText(self.send_status.text()+f'\n历史未确认发送={recovered}（不自动重试）')
                self.send_status.setStyleSheet('font-weight:bold; color:#b33;' if armed else 'font-weight:bold;')
                self.send_enable.setEnabled(state['listener_ready'] and not state['paused'] and not armed)
                self.send_disable.setEnabled(armed)
                self.send_test.setEnabled(armed and not send.get('active_stage') and send.get('queue_size',0)==0)
            if self.self_test_mode:
                self.status.setText(self.status.text() +
                    f"\n独立自测：读取={state['self_test_observations']} | 预览={state['self_test_preview_count']} | {state['self_test_status'] or '等待确认'}")
                challenge = coordinator.self_test_text
                if challenge != self._challenge_text:
                    self.challenge.setPlainText(challenge)
                    self._challenge_text = challenge
                    self.confirmed.setChecked(False)
                    self.self_sent.setChecked(False)
            candidate = coordinator.candidate
            if candidate:
                window = candidate["window"]
                key = (window.get("hwnd"), window.get("pid"), window.get("process_started"), window.get("dpi"),
                       str(window.get("client_rect")), str(window.get("client_screen")), candidate.get("header"),
                       json.dumps(candidate.get("layout", {}), sort_keys=True), state["paused"])
                if key != self._candidate_key:
                    self.confirmed.setChecked(False)
                    self.self_sent.setChecked(False)
                    self._candidate_key = key
                self.candidate.setText(
                    f"本地标题核对：{candidate['header'] or '未完整匹配目标群'}\n"
                    f"HWND={window.get('hwnd')} PID={window.get('pid')} DPI={window.get('dpi')}\n"
                    f"客户区={window.get('client_rect')} 屏幕起点={window.get('client_screen')}\n"
                    f"Header ROI={candidate['layout'].get('header')}\n"
                    f"Message ROI={candidate['layout'].get('message')}\n"
                    f"Input ROI={candidate['layout'].get('input')}")
            if not candidate or state["paused"] or state["status"] in {"BLOCKED", "UNBOUND", "SELF_TEST_EXPIRED"}:
                self.confirmed.setChecked(False)
                self.self_sent.setChecked(False)
            self.confirm_button.setEnabled(coordinator.action != "diagnose" and not state["paused"] and bool(candidate))
            if self.self_test_mode:
                self.confirm_button.setEnabled(self.confirm_button.isEnabled() and bool(coordinator.self_test_token)
                                               and not state["self_test_consumed"] and not state["self_test_expired"])
            preview = coordinator.preview
            if preview is None:
                self.preview.clear()
                self._preview_id = None
            elif preview.get("trace_id") != self._preview_id:
                self._preview_id = preview.get("trace_id")
                self.preview.setPlainText(("模型回复预览（真实发送结果见上方状态）\n\n" if self.continuous else
                                          "自身消息单次自测预览（未发送到微信，不计群友入站验收）\n\n" if self.self_test_mode else
                                           "本地回复预览（发送已阻断）\n\n") + preview.get("reply", ""))
            if self.self_test_mode:
                if state["self_test_expired"]:
                    notice = "本次测试码已过期，没有调用模型。关闭后可重新启动获取新码；不要重复发送旧码。"
                elif state["self_test_consumed"]:
                    notice = "本次消息已接纳，不能再次触发。回复仅在此预览；失败或取消不会自动重试。"
                elif state["bound"]:
                    notice = "正在本地读取指定自身气泡；请保持此窗口在前台，鼠标留在此窗口内。"
                else:
                    notice = "先亲手将上方完整文字发到“Ativia测试群”，再返回此窗口勾选两项确认并点击读取。测试码5分钟有效；无需等待 LISTENER_READY。"
                self.notice.setText(notice)
            if state["listener_ready"]:
                if self.continuous:
                    self.notice.setText('监听已就绪。真实发送由上方开关控制；保持预览在前台。发送期间请让鼠标键盘保持静止。'
                                        '若取消后输入框留有文字，请自行检查，程序不会清除或重发。')
                else:
                    self.notice.setText("GROUP_BOUND / BASELINE_COMPLETE / LISTENER_READY：现在可以由另一位群成员人工发送 @Ativia 测试一下。")

        def closeEvent(self, event):
            stop()
            event.accept()

    return PreviewWindow()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("diagnose", "listen", "dry-run", "self-test", "run", "send-enable", "send-disable", "pause", "resume", "stop"))
    parser.add_argument("--seconds", type=int, default=None, help="Optional duration; run defaults to continuous, self-test to 600s, other modes to 180s")
    parser.add_argument("--ui", action="store_true", help="Show the explicit local binding and reply-preview window")
    parser.add_argument("--confirmation", type=Path, help="One-use recent manual confirmation JSON; never a reusable HWND binding")
    args = parser.parse_args()
    if args.seconds is None:
        args.seconds = 0 if args.command == 'run' else 600 if args.command == "self-test" else 180
    if args.command == 'run' and not args.ui:
        parser.error('run requires --ui for explicit target and send controls')
    if args.command == "self-test" and (not args.ui or args.confirmation is not None):
        parser.error("self-test requires --ui and fresh manual confirmation; --confirmation is disabled")
    if not (1 <= args.seconds <= 86400 or args.command == 'run' and args.seconds == 0):
        parser.error("--seconds must be from 1 to 86400")
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtNetwork import QLocalServer
    if args.ui:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
    else:
        app = QCoreApplication.instance() or QCoreApplication([])
    if args.command in {'stop','pause','resume','send-enable','send-disable'}:
        return control_existing(args.command)
    config_path = ROOT / "docs" / "wechat-visual" / "runtime.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    confirmation = json.loads(args.confirmation.read_text(encoding="utf-8-sig")) if args.confirmation else None
    if args.command in {"dry-run", "self-test", "run"}:
        # Existing provider/configuration only. The worker receives an explicit
        # environment whitelist, never these credentials or this .env file.
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    from assistant_pause import PauseGate
    from runtime_settings import SettingsStore
    from integrations.wechat.visual.coordinator import VisualCoordinator
    coordinator = VisualCoordinator(ROOT, args.command, config, PauseGate(True), SettingsStore().load())
    coordinator.reserve()
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    QLocalServer.removeServer(server_name())  # safe only after acquiring the common lease
    if not server.listen(server_name()):
        coordinator.stop("STOP_SERVER_FAILED")
        raise RuntimeError("cannot create private stop endpoint")
    monitor = MainStatusMonitor()
    sockets = set()
    stopping = False

    def stop():
        nonlocal stopping
        if stopping:
            return
        stopping = True
        timer.stop()
        monitor.close()
        exit_code = 2 if coordinator.status == 'STOP_FAILED' else 0
        try:
            coordinator.stop()
        except Exception as exc:
            exit_code = 2
            print(json.dumps({"status": "VISUAL_STOP_FAILED", "error_type": type(exc).__name__}), flush=True)
        finally:
            try:
                server.close()
                print(json.dumps(coordinator.snapshot(), ensure_ascii=False), flush=True)
            finally:
                app.exit(exit_code)

    def connected():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            sockets.add(socket)
            buffer = bytearray()
            def read(s=socket, data=buffer):
                data.extend(bytes(s.readAll()))
                if len(data) > 256:
                    s.abort()
                    return
                if b"\n" not in data:
                    return
                try:
                    row = json.loads(data)
                except ValueError:
                    s.abort()
                    return
                if not isinstance(row,dict) or set(row)!={'action'} or row['action'] not in {'stop','pause','resume','send-enable','send-disable'}:
                    s.abort()
                    return
                action=row['action']
                status='CONTROL_ACCEPTED'
                if action=='stop':
                    status='STOP_ACCEPTED';QTimer.singleShot(20,stop)
                elif action=='pause':coordinator.set_user_paused(True)
                elif action=='resume':coordinator.set_user_paused(False)
                elif action=='send-disable':coordinator.disable_send()
                elif window is not None and coordinator.action=='run':
                    status='USER_CONFIRMATION_REQUIRED';QTimer.singleShot(20,window.enable_send)
                else:status='SEND_UNAVAILABLE'
                s.write((json.dumps({'status':status})+'\n').encode());s.flush()
            socket.readyRead.connect(read)
            socket.disconnected.connect(lambda s=socket: (sockets.discard(s), s.deleteLater()))
    server.newConnection.connect(connected)
    window = create_window(coordinator, stop) if args.ui else None
    if window is not None:
        window.show()
        if args.command=='run':
            from integrations.wechat.visual.send_native import NativeWechatInput
            coordinator.attach_sender(NativeWechatInput(int(window.winId())))
    began = time.monotonic()
    last_status = None
    started = False
    timer = QTimer()

    def poll():
        nonlocal started, last_status, confirmation
        try:
            try:
                row = monitor.rows.get_nowait()
            except queue.Empty:
                pass
            else:
                coordinator.set_external_status(row)
            # A startup resume would invalidate a one-use confirmation in the
            # worker. Wait for verified RUNNING before spawning it the first time.
            if not started and coordinator.gate.allows():
                coordinator.start(confirmation=confirmation)
                confirmation = None
                started = True
            coordinator.poll()
            state = coordinator.snapshot()
            identity = (state["status"], state["listener_ready"], state["model_admissions"],json.dumps(state.get('send'),sort_keys=True))
            if identity != last_status:
                print(json.dumps(state, ensure_ascii=False), flush=True)
                last_status = identity
            if window is not None:
                window.refresh()
            if coordinator.closed or args.seconds and time.monotonic() - began >= args.seconds:
                stop()
        except Exception as exc:
            # No exception body: provider errors may include confidential input.
            print(json.dumps({"status": "VISUAL_RUNNER_FAILED", "error_type": type(exc).__name__}), flush=True)
            stop()
    timer.timeout.connect(poll)
    timer.start(100)
    monitor.start()
    app.aboutToQuit.connect(stop)
    try:
        return app.exec()
    finally:
        stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "VISUAL_START_FAILED", "error_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)
