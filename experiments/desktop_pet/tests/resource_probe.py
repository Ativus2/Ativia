"""Offline UI resource check; synthetic animation states, no browser/mic/API."""
import ctypes,json,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from experiments.desktop_pet.window import PetWindow

class MeterPet(PetWindow):
    paints=0
    menu_refreshes=0
    def refresh_tray_menu(self):self.menu_refreshes+=1;super().refresh_tray_menu()
    def paintEvent(self,event):self.paints+=1;super().paintEvent(event)
class Counters(ctypes.Structure):
    _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ('peak','working','quota_peak_paged','quota_paged','quota_peak_nonpaged','quota_nonpaged','pagefile','peak_pagefile','private')]
def memory():
    kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=ctypes.c_void_p
    psapi=ctypes.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong]
    c=Counters();c.cb=ctypes.sizeof(c);psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(c),c.cb)
    return dict(working_set_mib=round(c.working/1048576,2),peak_working_set_mib=round(c.peak/1048576,2))
def gpu():
    try:return subprocess.check_output(['nvidia-smi','--query-gpu=utilization.gpu,memory.used','--format=csv,noheader,nounits'],creationflags=subprocess.CREATE_NO_WINDOW,timeout=3).decode().strip()
    except Exception:return None
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
with tempfile.TemporaryDirectory() as temp:
    pet=MeterPet(Path(temp)/'settings.json');pet.show();QTest.qWait(250);records=[]
    for mode in ('idle','animation_test_double','paused','hidden'):
        state=dict(state='RUNNING' if mode=='animation_test_double' else 'PAUSED' if mode=='paused' else 'IDLE',
                   mic_active=False,speaking=False,thinking=mode=='animation_test_double',instance='offline-resource-check')
        pet.update_state(state)
        if mode=='hidden':pet.hide_pet()
        QTest.qWait(500);before=pet.paints;menus=pet.menu_refreshes;cpu=time.process_time();wall=time.monotonic();QTest.qWait(2500);elapsed=time.monotonic()-wall
        records.append(dict(mode=mode,seconds=round(elapsed,3),process_cpu_one_core_percent=round((time.process_time()-cpu)/elapsed*100,2),
            observed_paints_per_second=round((pet.paints-before)/elapsed,2),menu_rebuilds_in_window=pet.menu_refreshes-menus,**memory(),whole_gpu_util_percent_and_mib=gpu()))
    pet.dispose()
(ROOT/'docs/desktop-pet-trial/resource-probe-followup.json').write_text(json.dumps(dict(physical_mic=False,paid_calls=0,
    process_gpu_attribution=None,note='Whole-device GPU readings include other applications; no per-process GPU conclusion. Synthetic UI animation only.',records=records),indent=2),encoding='utf-8')
print(json.dumps(records))
