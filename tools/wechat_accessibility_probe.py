"""Structure-only Windows accessibility preflight; no input or message APIs.

IAccessible (oleacc.dll) and IUIAutomation are public OS APIs. No Name/Value/Text
is requested from WeChat. The synthetic fixture contains only our known strings.
A semantic candidate is not proof of group binding, sender, or new-message support.
"""
from __future__ import annotations
import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

MAX_NODES = 512
MAX_DEPTH = 16


def write(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def error(exc):
    return {"type": type(exc).__name__, "hresult": getattr(exc, "hresult", None)}


def fixture(path):
    import win32api, win32con, win32gui
    name = "AtiviaRecoveryPositiveControl"
    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = name
    wc.lpfnWndProc = win32gui.DefWindowProc
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE, name,
        "Ativia accessibility positive control", win32con.WS_OVERLAPPEDWINDOW,
        40, 40, 380, 190, 0, 0, wc.hInstance, None)
    common = win32con.WS_CHILD | win32con.WS_VISIBLE
    win32gui.CreateWindowEx(0, "BUTTON", "Synthetic button", common | win32con.BS_PUSHBUTTON,
                           15, 15, 160, 32, hwnd, 101, wc.hInstance, None)
    win32gui.CreateWindowEx(0, "EDIT", "Synthetic text", common | win32con.WS_BORDER,
                           15, 60, 250, 28, hwnd, 102, wc.hInstance, None)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    write(path, {"hwnd": hwnd, "pid": os.getpid()})
    deadline = time.monotonic() + 45
    stop = Path(str(path) + ".stop")
    try:
        while time.monotonic() < deadline and not stop.exists():
            win32gui.PumpWaitingMessages()
            time.sleep(.02)
    finally:
        win32gui.DestroyWindow(hwnd)


def uia_tree(hwnd, synthetic=False):
    import comtypes.client
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    auto = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                                        interface=mod.IUIAutomation)
    root = auto.ElementFromHandle(hwnd)
    walker = auto.RawViewWalker
    nodes, errors, stack = [], [], [(root, 0)]
    limited = False
    while stack and len(nodes) < MAX_NODES:
        element, depth = stack.pop()
        try:
            row = {"depth": depth, "control_type": element.CurrentControlType,
                   "class_name": element.CurrentClassName}
            # Name queries are confined to our disposable test window.
            if synthetic:
                row["synthetic_name"] = element.CurrentName
                if row["control_type"] == 50004:
                    pattern = element.GetCurrentPattern(10002).QueryInterface(mod.IUIAutomationValuePattern)
                    row["synthetic_value"] = pattern.CurrentValue
            nodes.append(row)
            child = walker.GetFirstChildElement(element)
            if depth >= MAX_DEPTH:
                limited |= bool(child)
                continue
            children = []
            while child and len(children) < MAX_NODES:
                children.append(child)
                child = walker.GetNextSiblingElement(child)
            limited |= bool(child)
            stack.extend((c, depth + 1) for c in reversed(children))
        except Exception as exc:
            errors.append(error(exc))
    return {"node_count": len(nodes), "max_depth": max((n["depth"] for n in nodes), default=0),
            "mmui_nodes": sum(n["class_name"].startswith("mmui::") for n in nodes),
            "control_types": dict(Counter(str(n["control_type"]) for n in nodes)),
            "truncated": bool(stack) or limited, "errors": errors, "nodes": nodes}


def msaa_tree(hwnd, object_id, synthetic=False):
    import comtypes.client
    from comtypes.automation import VARIANT
    mod = comtypes.client.GetModule("oleacc.dll")
    IA = mod.IAccessible
    dll = ctypes.OleDLL("oleacc")
    dll.AccessibleObjectFromWindow.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.POINTER(comtypes.GUID),
        ctypes.POINTER(ctypes.POINTER(IA))]
    dll.AccessibleObjectFromWindow.restype = ctypes.c_long
    dll.AccessibleChildren.argtypes = [ctypes.POINTER(IA), ctypes.c_long, ctypes.c_long,
                                      ctypes.POINTER(VARIANT), ctypes.POINTER(ctypes.c_long)]
    dll.AccessibleChildren.restype = ctypes.c_long
    root = ctypes.POINTER(IA)()
    hr = dll.AccessibleObjectFromWindow(hwnd, object_id & 0xffffffff,
                                        ctypes.byref(IA._iid_), ctypes.byref(root))
    if hr < 0 or not root:
        return {"status": "UNAVAILABLE", "hresult": hr, "nodes": [], "node_count": 0}
    nodes, errors, stack, limited = [], [], [(root, 0, 0)], False
    while stack and len(nodes) < MAX_NODES:
        obj, child_id, depth = stack.pop()
        try:
            role = obj.accRole[child_id]
            row = {"depth": depth, "role": role if isinstance(role, int) else "custom"}
            if synthetic:
                row["synthetic_name"] = obj.accName[child_id]
                if role == 42:
                    row["synthetic_value"] = obj.accValue[child_id]
            nodes.append(row)
            if child_id != 0:
                continue
            count = obj.accChildCount
            row["children_count"] = count
            if depth >= MAX_DEPTH:
                limited |= count > 0
                continue
            if count <= 0:
                continue
            cap = min(count, MAX_NODES - len(nodes))
            limited |= cap < count
            if cap <= 0:
                continue
            variants, obtained = (VARIANT * cap)(), ctypes.c_long()
            hr = dll.AccessibleChildren(obj, 0, cap, variants, ctypes.byref(obtained))
            if hr < 0:
                errors.append({"operation": "AccessibleChildren", "hresult": hr})
                continue
            children = []
            for v in variants[:obtained.value]:
                value = v.value
                if isinstance(value, int):
                    children.append((obj, value, depth + 1))
                elif value:
                    try:
                        children.append((value.QueryInterface(IA), 0, depth + 1))
                    except Exception as exc:
                        errors.append(error(exc))
            stack.extend(reversed(children))
        except Exception as exc:
            errors.append(error(exc))
    return {"status": "OK", "node_count": len(nodes),
            "max_depth": max((n["depth"] for n in nodes), default=0),
            "roles": dict(Counter(str(n["role"]) for n in nodes)),
            "truncated": bool(stack) or limited, "errors": errors, "nodes": nodes}


def wechat_windows():
    import psutil, win32api, win32gui, win32process
    pids, processes, windows = set(), [], []
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        if (proc.info["name"] or "").lower() not in ("weixin.exe", "wechat.exe"):
            continue
        pids.add(proc.pid)
        row = {"pid": proc.pid, "name": proc.info["name"],
               "started_utc": datetime.fromtimestamp(proc.create_time(), timezone.utc).isoformat()}
        try:
            data = win32api.GetFileVersionInfo(proc.exe(), "\\")
            hi, lo = data["FileVersionMS"], data["FileVersionLS"]
            row["version"] = ".".join(map(str, (hi >> 16, hi & 65535, lo >> 16, lo & 65535)))
        except Exception as exc:
            row["error"] = error(exc)
        processes.append(row)
    def visit(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid not in pids:
            return
        title = win32gui.GetWindowText(hwnd)
        windows.append({"hwnd": hwnd, "pid": pid, "class_name": win32gui.GetClassName(hwnd),
                        "title_is_main": title in ("微信", "Weixin", "WeChat"),
                        "visible": bool(win32gui.IsWindowVisible(hwnd)),
                        "minimized": bool(win32gui.IsIconic(hwnd)),
                        "owner": win32gui.GetWindow(hwnd, 4)})
    win32gui.EnumWindows(visit, None)
    return processes, windows


def point_probe(hwnd, pid):
    import comtypes.client, win32gui, win32process
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    auto = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                                        interface=mod.IUIAutomation)
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    rows = []
    for fx, fy in ((.25, .3), (.5, .5), (.75, .3), (.75, .75)):
        x, y = win32gui.ClientToScreen(hwnd, (int(right * fx), int(bottom * fy)))
        hit = win32gui.WindowFromPoint((x, y))
        _, hit_pid = win32process.GetWindowThreadProcessId(hit)
        if hit_pid != pid:
            rows.append({"fraction": [fx, fy], "status": "SKIPPED_OCCLUDED"})
            continue
        try:
            point = mod.tagPOINT(x=x, y=y)
            element = auto.ElementFromPoint(point)
            if element.CurrentProcessId != pid:
                rows.append({"fraction": [fx, fy], "status": "SKIPPED_FOREIGN_PID"})
                continue
            rows.append({"fraction": [fx, fy], "status": "OK",
                         "control_type": element.CurrentControlType,
                         "class_name": element.CurrentClassName})
        except Exception as exc:
            rows.append({"fraction": [fx, fy], "status": "TOOL_ERROR", "error": error(exc)})
    return rows


def msaa_semantic_nodes(tree):
    """Exclude standard nonclient chrome by ancestry, not by total node count."""
    ancestors, result = [], []
    for node in tree.get("nodes", []):
        depth = node["depth"]
        ancestors = ancestors[:depth]
        role = node["role"]
        chrome = role in (1, 2, 3, 4) or (role in (39, 43) and any(r in (1, 3) for r in ancestors))
        if not chrome and role not in (9, 10):
            result.append(node)
        ancestors.append(role)
    return result


def classify_target(report):
    uia = report["uia"]
    clients = [report["msaa_client"]] + [c["msaa"] for c in report["child_clients"]]
    semantic = [node for client in clients for node in msaa_semantic_nodes(client)]
    report["msaa_client_semantic_node_count"] = len(semantic)
    report["msaa_interpretation"] = "SEMANTIC_CANDIDATE" if semantic else "GENERIC_WINDOW_CHROME_ONLY"
    failed = (uia["errors"] or uia["truncated"] or report["native_children_truncated"]
              or any(c.get("status") != "OK" or c.get("errors") or c.get("truncated") for c in clients)
              or any(p["status"] == "TOOL_ERROR" for p in report["hit_test"]))
    candidate = (uia["node_count"] > 2 and uia["max_depth"] > 1
                 or bool(semantic)
                 or any(p.get("control_type") not in (50032, 50033)
                        for p in report["hit_test"] if p["status"] == "OK"))
    return "TOOL_ERROR" if failed else "SEMANTIC_CANDIDATE" if candidate else "NO_SEMANTIC_STRUCTURE"


def target_probe():
    import win32gui, win32process
    processes, windows = wechat_windows()
    candidates = [w for w in windows if w["visible"] and not w["owner"] and w["title_is_main"]]
    report = {"processes": processes, "windows": windows}
    if len(candidates) != 1:
        return dict(report, status="CLIENT_UNAVAILABLE",
                    reason="WechatNotRunning" if not processes else "MainWindowNotUnique")
    window = candidates[0]
    if window["minimized"]:
        return dict(report, status="CLIENT_UNAVAILABLE", reason="MainWindowMinimized")
    hwnd, pid = window["hwnd"], window["pid"]
    children = []
    def visit(child, _):
        _, child_pid = win32process.GetWindowThreadProcessId(child)
        if child_pid == pid:
            children.append({"hwnd": child, "pid": child_pid,
                             "class_name": win32gui.GetClassName(child),
                             "visible": bool(win32gui.IsWindowVisible(child))})
    win32gui.EnumChildWindows(hwnd, visit, None)
    report.update(hwnd=hwnd, native_child_windows=children, uia=uia_tree(hwnd),
                  msaa_client=msaa_tree(hwnd, -4), msaa_window=msaa_tree(hwnd, 0),
                  hit_test=point_probe(hwnd, pid))
    # Native child enumeration must be complete before accepting a negative.
    report["native_children_truncated"] = len(children) > 32
    report["child_clients"] = [
        {"hwnd": c["hwnd"], "class_name": c["class_name"], "msaa": msaa_tree(c["hwnd"], -4)}
        for c in children[:32] if c["visible"]]
    report["status"] = classify_target(report)
    report["group_verified"] = report["new_message_verified"] = report["sender_verified"] = False
    return report


def worker(args):
    # No network traffic from the diagnostic runtime or dependency imports.
    def audit(event, _):
        if event in ("socket.connect", "socket.getaddrinfo", "urllib.Request"):
            raise RuntimeError("DIAGNOSTIC_NETWORK_BLOCKED")
    sys.addaudithook(audit)
    report = {"captured_utc": datetime.now(timezone.utc).isoformat(),
              "real_external_send_actions": 0,
              "privacy": "No WeChat Name, Value, Text, accName, accValue, accDescription, process memory, database, screenshot or input APIs"}
    try:
        if args.hwnd:
            uia, msaa = uia_tree(args.hwnd, True), msaa_tree(args.hwnd, -4, True)
            utypes = {n["control_type"] for n in uia["nodes"]}
            roles = {n["role"] for n in msaa["nodes"]}
            names = {n.get("synthetic_name") for n in uia["nodes"]}
            ok = {50000, 50004}.issubset(utypes) and {42, 43}.issubset(roles)
            ok &= "Synthetic button" in names
            ok &= any(n.get("synthetic_value") == "Synthetic text" for n in uia["nodes"])
            ok &= any(n.get("synthetic_value") == "Synthetic text" for n in msaa["nodes"])
            ok &= not (uia["errors"] or uia["truncated"] or msaa.get("errors") or msaa.get("truncated"))
            report.update(status="POSITIVE_CONTROL_PASS" if ok else "TOOL_ERROR",
                          positive_control={"uia": uia, "msaa_client": msaa})
        else:
            report["target"] = target_probe()
            report["status"] = report["target"]["status"]
    except Exception as exc:
        report.update(status="TOOL_ERROR", error=error(exc))
    write(args.output, report)
    return report["status"]


def exit_code(status):
    return 0 if status in ("POSITIVE_CONTROL_PASS", "SEMANTIC_CANDIDATE") else 1 if status == "TOOL_ERROR" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive-control", action="store_true")
    parser.add_argument("--wechat", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hwnd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture:
        fixture(args.output)
        return 0
    if args.worker:
        return exit_code(worker(args))
    if args.positive_control == args.wechat:
        parser.error("choose exactly one of --positive-control and --wechat")
    with tempfile.TemporaryDirectory(prefix="ativia-accessibility-") as temp:
        result = Path(temp) / "result.json"
        fixture_file = Path(temp) / "fixture.json"
        fixture_proc = None
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve())]
        try:
            if args.positive_control:
                fixture_proc = subprocess.Popen(command + ["--fixture", "--output", str(fixture_file)],
                                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 8
                while not fixture_file.exists() and time.monotonic() < deadline:
                    if fixture_proc.poll() is not None:
                        raise RuntimeError("FixtureExited")
                    time.sleep(.05)
                data = json.loads(fixture_file.read_text(encoding="utf-8"))
                command += ["--hwnd", str(data["hwnd"])]
            subprocess.run(command + ["--worker", "--output", str(result)], env=env,
                           timeout=30, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            report = json.loads(result.read_text(encoding="utf-8"))
        except Exception as exc:
            report = {"status": "TOOL_ERROR", "error": error(exc),
                      "captured_utc": datetime.now(timezone.utc).isoformat(),
                      "real_external_send_actions": 0}
        finally:
            if fixture_proc:
                Path(str(fixture_file) + ".stop").touch()
                try:
                    fixture_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # Only the disposable child created above, never a user app.
                    fixture_proc.terminate()
                    fixture_proc.wait(timeout=3)
        write(args.output, report)
        print(json.dumps({"status": report["status"], "output": str(args.output),
                          "real_external_send_actions": 0}))
        return exit_code(report["status"])


if __name__ == "__main__":
    raise SystemExit(main())


