"""Bounded, read-only UIA structure probe. Never exports chat names or bodies."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STRUCTURAL_NAMES = {'微信', 'WeChat', 'Weixin', '消息', '搜索', '会话', '通讯录', '聊天信息', '发送', '更多', '输入'}
TARGETS = ('mmui::MainWindow', 'mmui::FramelessMainWindow', 'mmui::ChatMessagePage', 'mmui::SessionListView')


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def version(path):
    import win32api
    try:
        info = win32api.GetFileVersionInfo(str(path), '\\')
        hi, lo = info['FileVersionMS'], info['FileVersionLS']
        return '.'.join(str(x) for x in (hi >> 16, hi & 65535, lo >> 16, lo & 65535))
    except Exception as exc:
        return type(exc).__name__


def process_state():
    import psutil
    result = []
    for proc in psutil.process_iter(['pid', 'name', 'create_time']):
        if (proc.info['name'] or '').lower() not in ('weixin.exe', 'wechat.exe', 'narrator.exe'):
            continue
        row = {'pid': proc.pid, 'name': proc.info['name'], 'started_utc': utc(proc.info['create_time'])}
        try:
            row['version'] = version(proc.exe())
            row['UIAutomationCore_loaded'] = any(Path(m.path).name.lower() == 'uiautomationcore.dll' for m in proc.memory_maps())
        except Exception as exc:
            row['module_probe_error'] = type(exc).__name__
        result.append(row)
    return result


def probe(case):
    from integrations.wechat.diagnostics import environment
    from wxauto4 import uia
    evidence = environment()
    report = {'case': case, 'captured_utc': utc(datetime.now().timestamp()), 'environment': evidence,
              'processes_before_probe': process_state(), 'UIAutomationCore_system_version': version(Path(os.environ['WINDIR']) / 'System32/UIAutomationCore.dll'),
              'privacy': 'Only structural names allowlisted; other names omitted; no Value/Text patterns requested.', 'real_SendMsg_calls': 0}
    candidates = [w for w in evidence['windows'] if w['visible'] and not w['owner'] and w['title'] in ('微信', 'WeChat', 'Weixin')]
    if len(candidates) != 1:
        report['error'] = 'MainWindowNotUnique'
        return report
    window = candidates[0]
    report['hwnd'] = window['hwnd']
    root = uia.ControlFromHandle(window['hwnd'])
    stack = [(root, 0, None)]
    nodes = []
    while stack and len(nodes) < 2000:
        control, depth, parent = stack.pop()
        try:
            name = control.Name
            cls = control.ClassName
            aid = control.AutomationId
            # Runtime IDs can include user content. Only structural ASCII identifiers survive.
            safe_id = aid if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,119}', aid or '') else ('[omitted]' if aid else '')
            children = control.GetChildren() if depth < 20 else []
            row = {'depth': depth, 'parent_index': parent, 'control_type': control.ControlTypeName,
                   'class_name': cls, 'automation_id': safe_id, 'name': name if name in STRUCTURAL_NAMES else '[omitted]' if name else '',
                   'children_count': len(children) if depth < 20 else None}
            index = len(nodes)
            nodes.append(row)
            stack.extend((child, depth + 1, index) for child in reversed(children))
        except Exception as exc:
            nodes.append({'depth': depth, 'parent_index': parent, 'error': type(exc).__name__})
    report.update(nodes=nodes, node_count=len(nodes), max_depth=max((n['depth'] for n in nodes), default=0),
                  truncated=bool(stack) or any(n['depth'] == 20 for n in nodes),
                  type_counts=dict(Counter(n.get('control_type', 'error') for n in nodes)),
                  targets={target: any(n.get('class_name') == target for n in nodes) for target in TARGETS},
                  mmui_nodes=sum(n.get('class_name', '').startswith('mmui::') for n in nodes),
                  message_list_found=any(n.get('control_type') == 'ListControl' and n.get('name') == '消息' for n in nodes),
                  processes_after_probe=process_state())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', default='manual')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/wechat-integration/benchmark/uia-current-20260928.json')
    parser.add_argument('--timeout', type=int, default=40)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            result = probe(args.case)
        except Exception as exc:
            result = {'case': args.case, 'error': type(exc).__name__, 'real_SendMsg_calls': 0}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    with tempfile.TemporaryDirectory(prefix='ativia-uia-') as temp:
        result_path = Path(temp) / 'structure.json'
        env = {k: v for k, v in os.environ.items() if not any(part in k.upper() for part in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
        try:
            subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--worker', '--case', args.case, '--output', str(result_path)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(60, max(5, args.timeout)), check=True)
            report = json.loads(result_path.read_text(encoding='utf-8'))
        except (subprocess.SubprocessError, OSError) as exc:
            report = {'case': args.case, 'error': type(exc).__name__, 'real_SendMsg_calls': 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if 'error' in report else 0


if __name__ == '__main__':
    raise SystemExit(main())
