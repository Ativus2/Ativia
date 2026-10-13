"""Low-frequency, read-only Windows process-tree counters; no third-party monitor."""
import ctypes as C
from ctypes import wintypes as W
import os
import time


class Entry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
               ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
               ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD), ('exe', W.WCHAR * 260)]


class Memory(C.Structure):
    _fields_ = [('cb', W.DWORD), ('faults', W.DWORD)] + [(n, C.c_size_t) for n in
        ('peak_working', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged',
         'pagefile', 'peak_pagefile', 'private', 'private_working')] + [('shared_commit', C.c_ulonglong)]


class Power(C.Structure):
    _fields_ = [('ac', W.BYTE), ('flag', W.BYTE), ('percent', W.BYTE), ('reserved', W.BYTE),
               ('life', W.DWORD), ('full_life', W.DWORD)]


k = C.WinDLL('kernel32', use_last_error=True)
k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
k.CreateToolhelp32Snapshot.restype = W.HANDLE
k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
k.CloseHandle.argtypes = [W.HANDLE]
k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
k.GetProcessHandleCount.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
p = C.WinDLL('psapi', use_last_error=True)
p.GetProcessMemoryInfo.argtypes = [W.HANDLE, C.POINTER(Memory), W.DWORD]


def processes():
    handle = k.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value: raise C.WinError(C.get_last_error())
    rows = {}; entry = Entry(); entry.size = C.sizeof(entry)
    try:
        ok = k.Process32FirstW(handle, C.byref(entry))
        while ok:
            rows[entry.pid] = dict(pid=entry.pid, parent=entry.parent, name=entry.exe, threads=entry.threads)
            ok = k.Process32NextW(handle, C.byref(entry))
        return rows
    finally: k.CloseHandle(handle)


def sample_tree(root, include_launcher=True):
    all_rows = processes(); owned = {root}
    while True:
        children = {pid for pid, row in all_rows.items() if row['parent'] in owned}
        if children <= owned: break
        owned |= children
    # CPython venv launchers are tiny waiting parents, still part of the instance.
    parent = all_rows.get(root, {}).get('parent')
    if include_launcher and all_rows.get(parent, {}).get('name', '').lower() in ('python.exe', 'pythonw.exe'):
        owned.add(parent)
    output = []
    for pid in sorted(owned):
        if pid not in all_rows: continue
        row = dict(all_rows[pid]); handle = k.OpenProcess(0x410, False, pid)
        if not handle:
            row['unavailable'] = C.get_last_error(); output.append(row); continue
        try:
            memory = Memory(); memory.cb = C.sizeof(memory)
            if not p.GetProcessMemoryInfo(handle, C.byref(memory), memory.cb):
                row['memory_unavailable'] = C.get_last_error()
            else:
                row.update(working_set_mib=memory.working/1048576,
                           private_commit_mib=memory.private/1048576,
                           private_resident_mib=memory.private_working/1048576)
            times = [W.FILETIME() for _ in range(4)]
            if k.GetProcessTimes(handle, *[C.byref(t) for t in times]):
                value = lambda t: ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7
                row['created'] = value(times[0]); row['cpu_seconds'] = value(times[2]) + value(times[3])
            handles = W.DWORD()
            if k.GetProcessHandleCount(handle, C.byref(handles)): row['handles'] = handles.value
        finally: k.CloseHandle(handle)
        output.append(row)
    power = Power()
    supply = None
    if k.GetSystemPowerStatus(C.byref(power)):
        supply = dict(ac={0:'battery', 1:'plugged_in'}.get(power.ac, 'unknown'), battery_percent=power.percent)
    return dict(at=time.monotonic(), logical_cpus=os.cpu_count(), power=supply, processes=output)


def summarize(samples):
    memory = ('private_resident_mib', 'working_set_mib', 'private_commit_mib')
    totals = [{key: sum(p.get(key, 0) for p in s['processes']) for key in memory} for s in samples]
    intervals=[]; per_process={}
    for before, after in zip(samples, samples[1:]):
        elapsed=after['at']-before['at']; previous={p['pid']:p for p in before['processes']}; total=0
        for row in after['processes']:
            old=previous.get(row['pid'])
            if old and old.get('created')==row.get('created') and 'cpu_seconds' in row and 'cpu_seconds' in old:
                cpu=max(0, row['cpu_seconds']-old['cpu_seconds'])/elapsed/after['logical_cpus']*100
                total+=cpu; per_process.setdefault(str(row['pid']), []).append(cpu)
        intervals.append(total)
    return dict(seconds=samples[-1]['at']-samples[0]['at'],
        cpu_normalized_mean=sum(intervals)/len(intervals) if intervals else None,
        cpu_normalized_peak=max(intervals, default=None),
        memory_start=totals[0], memory_end=totals[-1],
        memory_peak={key:max(s[key] for s in totals) for key in memory},
        process_peak=max(len(s['processes']) for s in samples),
        thread_peak=max(sum(p['threads'] for p in s['processes']) for s in samples),
        per_process_cpu={pid:dict(mean=sum(v)/len(v), peak=max(v)) for pid,v in per_process.items()},
        working_set_note='Sum includes shared pages; it is NOT exclusive physical memory.',
        cpu_note='Normalized by all logical processors; peak is a 2-second sample, not instantaneous.',
        missing_counters=any('unavailable' in p or 'memory_unavailable' in p for s in samples for p in s['processes']))
