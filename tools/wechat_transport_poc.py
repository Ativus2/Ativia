"""Independent native UIA preflight; no UI actions, history reads, or sends.

A semantic root is a prerequisite, not proof of a working message transport.
This tool deliberately stops before group/message access when that prerequisite fails.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def send_text(*args, **kwargs):
    raise RuntimeError('POC_SEND_BLOCKED')


def probe():
    import comtypes.client
    from integrations.wechat.diagnostics import environment
    env = environment()  # Win32/PID evidence; does not instantiate vendor clients.
    windows = [w for w in env['windows'] if w['visible'] and not w['owner'] and w['title'] in ('微信', 'WeChat', 'Weixin')]
    if len(windows) != 1:
        return {'status': 'FAIL', 'reason': 'MainWindowNotUnique', 'real_SendMsg_calls': 0}
    module = comtypes.client.GetModule('UIAutomationCore.dll')
    automation = comtypes.client.CreateObject('{ff48dba4-60ef-4201-aa87-54103eef594e}', interface=module.IUIAutomation)
    root = automation.ElementFromHandle(windows[0]['hwnd'])
    views = {}
    for view_name in ('RawViewWalker', 'ControlViewWalker', 'ContentViewWalker'):
        walker = getattr(automation, view_name)
        stack = [(root, 0)]
        nodes = []
        while stack and len(nodes) < 2000:
            element, depth = stack.pop()
            # Never request Name, Value, Text, or user-content identifiers.
            nodes.append({'depth': depth, 'class_name': element.CurrentClassName, 'control_type_id': element.CurrentControlType})
            if depth >= 20:
                continue
            child = walker.GetFirstChildElement(element)
            children = []
            while child and len(children) < 2000:
                children.append(child)
                child = walker.GetNextSiblingElement(child)
            stack.extend((child, depth + 1) for child in reversed(children))
        views[view_name] = {'node_count': len(nodes), 'max_depth': max(n['depth'] for n in nodes),
                            'truncated': bool(stack) or any(n['depth'] == 20 for n in nodes),
                            'mmui_nodes': sum(n['class_name'].startswith('mmui::') for n in nodes),
                            'class_counts': dict(Counter(n['class_name'] for n in nodes)), 'nodes': nodes}
    semantic = any(v['mmui_nodes'] and v['max_depth'] > 1 for v in views.values())
    return {'captured_utc': datetime.now(timezone.utc).isoformat(), 'backend': 'Direct Windows IUIAutomation (no wxauto4 UI wrapper)',
            'status': 'INCOMPLETE' if semantic else 'FAIL', 'reason': 'NeedsDedicatedMessagePoC' if semantic else 'NoSemanticChatStructure',
            'environment': env, 'hwnd': windows[0]['hwnd'], 'ElementFromHandle': 'PASS', 'views': views,
            'group_verified': False, 'new_message_verified': False, 'sender_verified': False,
            'real_SendMsg_calls': 0, 'send_api': 'unconditionally blocked', 'privacy': 'No Name/Value/Text reads'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/wechat-integration/benchmark/native-uia-poc-20260928.json')
    args = parser.parse_args()
    if args.worker:
        try:
            report = probe()
        except Exception as exc:
            report = {'status': 'FAIL', 'error_type': type(exc).__name__, 'real_SendMsg_calls': 0}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    with tempfile.TemporaryDirectory(prefix='ativia-native-uia-') as temp:
        output = Path(temp) / 'result.json'
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
        try:
            subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--worker', '--output', str(output)],
                           env=env, timeout=40, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            report = json.loads(output.read_text(encoding='utf-8'))
        except (OSError, subprocess.SubprocessError) as exc:
            report = {'status': 'FAIL', 'error_type': type(exc).__name__, 'real_SendMsg_calls': 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'environment'}, ensure_ascii=False, indent=2))
    return 2 if report['status'] == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
