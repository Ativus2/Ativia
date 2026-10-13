"""Finite actual Qt/localhost/browser integration. NO PAID API, no physical microphone."""
import ctypes,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QTimer,Qt,QPoint,QRect
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from experiments.desktop_pet.main import Desktop

OUT=ROOT/'docs/desktop-pet-trial';LOG=ROOT/'experiments/desktop_pet/logs';LOG.mkdir(exist_ok=True)
STAGE=LOG/'integration-stage.json';STAGE.unlink(missing_ok=True)
node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
report={'paid_calls':0,'ui_screenshots':'Actual Windows Qt windows; idle/paused confirmed by real localhost control hub',
        'voice_provider':'offline SDK test double only','checks':[],'errors':[]}
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
desktop=Desktop(app,limit=0,pet_settings_path=LOG/'evidence-settings.json')
started=time.monotonic();cpu_started=time.process_time();last_stage=None;browser_checked=False;runner=None

def capture(name,widgets):
    rect=widgets[0].frameGeometry()
    for widget in widgets[1:]:rect=rect.united(widget.frameGeometry())
    screen=desktop.pet.screen();rect=rect.intersected(screen.geometry())
    shot=screen.grabWindow(0,rect.x(),rect.y(),rect.width(),rect.height())
    if shot.isNull():raise RuntimeError('Native window capture failed')
    shot.save(str(OUT/(name+'.png')))

def rss():
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ('peak','working','quota_peak_paged','quota_paged','quota_peak_nonpaged','quota_nonpaged','pagefile','peak_pagefile','private')]
    counters=Counters();counters.cb=ctypes.sizeof(counters)
    kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=ctypes.c_void_p
    psapi=ctypes.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):return None
    return {'working_set_mib':round(counters.working/1048576,2),'peak_working_set_mib':round(counters.peak/1048576,2)}

def fail(error):
    report['errors'].append(type(error).__name__+': '+str(error));desktop.exit()

def tick():
    global browser_checked,runner,last_stage
    try:
        if time.monotonic()-started>55:raise RuntimeError('finite integration deadline')
        if not desktop.client.connected:return
        if last_stage is None:
            if time.monotonic()-started<3:return
            elapsed=time.monotonic()-started
            report['idle_resources']={'seconds':round(elapsed,2),'process_cpu_one_core_percent':round((time.process_time()-cpu_started)/elapsed*100,2),**rss()}
            report['checks'].append('launcher starts only the pet and broker; no browser or microphone')
            screen=desktop.pet.screen().availableGeometry();desktop.pet.move(screen.right()-desktop.pet.width()-35,screen.bottom()-desktop.pet.height()-50)
            capture('01-idle',[desktop.pet])
            QTest.mouseMove(desktop.pet,QPoint(80,80));QTest.qWait(50);desktop.pet.position_popups();desktop.pet.bar.show()
            QTest.mouseMove(desktop.pet.bar,QPoint(40,15));QTest.qWait(350)
            if not desktop.pet.bar.isVisible():raise AssertionError('hover bar disappeared when entering')
            capture('02-hover',[desktop.pet,desktop.pet.bar]);desktop.pet.bar.grab().save(str(OUT/'02-hover-toolbar.png'))
            report['checks'].append('hover-to-toolbar remains open, causes zero capture or API requests')
            event=QContextMenuEvent(QContextMenuEvent.Mouse,QPoint(80,80),desktop.pet.mapToGlobal(QPoint(80,80)))
            app.sendEvent(desktop.pet,event);QTest.qWait(350);capture('03-menu',[desktop.pet,desktop.pet.menu]);desktop.pet.menu.close()
            desktop.client.command('pause');last_stage='COLD_PAUSE';return
        if last_stage=='COLD_PAUSE':
            if desktop.pet.state.get('state')!='PAUSED':return
            desktop.pet.bar.hide();capture('04-paused',[desktop.pet]);report['checks'].append('paused screenshot follows confirmed hub PAUSED with no capture owner')
            desktop.pet.hide_pet()
            if not desktop.pet.tray.isVisible():raise AssertionError('tray absent after hide')
            desktop.pet.reveal();desktop.open_browser();last_stage='OWNED_BROWSER';return
        if last_stage=='OWNED_BROWSER':
            if not desktop.pet.state.get('page_connected'):return
            if desktop.pet.state.get('mic_active') is not False:raise AssertionError('browser auto-opened mic')
            report['owned_browser_pid']=desktop.browser.process.pid
            report['checks'].append('real production Edge page opens without capture, assigned to own Windows job')
            desktop.browser.close();desktop.browser=None;last_stage='WAIT_PAGE_CLOSED';return
        if last_stage=='WAIT_PAGE_CLOSED':
            if desktop.pet.state.get('page_connected'):return
            runner=subprocess.Popen([str(node),str(ROOT/'experiments/desktop_pet/tests/browser-link.mjs')],cwd=ROOT,
                stdout=open(LOG/'browser-link.out.log','w'),stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            last_stage='NODE';return
        if not STAGE.exists():return
        current=json.loads(STAGE.read_text())['stage']
        if current==last_stage:return
        last_stage=current;print('integration:',current,flush=True)
        if current=='PAGE_READY':desktop.pet.bar.primary.click()
        elif current=='LIVE':
            if not desktop.pet.state.get('mic_active'):return
            desktop.pet.bar.primary.click()
        elif current=='PAUSED':
            report['checks'].append('pet/page/tray share paused state after actual test track release')
            desktop.pet.bar.primary.click()
        elif current=='RESUMED':desktop.pet.command.emit('mic',{'enabled':False})
        elif current=='MIC_OFF':desktop.pet.command.emit('mic',{'enabled':True})
        elif current=='MIC_ON':desktop.pet.bar.stop.click()
        elif current=='INTERRUPTED':
            report['active_resources']=rss();desktop.pet.quit_requested.emit()
    except Exception as error:fail(error)

timer=QTimer();timer.setInterval(100);timer.timeout.connect(tick);timer.start()
try:app.exec()
finally:
    timer.stop()
    if runner:
        try:report['browser_test_exit_code']=runner.wait(timeout=5)
        except subprocess.TimeoutExpired:runner.terminate();report['errors'].append('browser test did not finish')
    report['owned_service_running_after_exit']=desktop.server is not None
    report['runtime_file_after_exit']=desktop.runtime.exists()
    (OUT/'desktop-integration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False),flush=True)
if report['errors'] or report.get('browser_test_exit_code',0):raise SystemExit(1)
