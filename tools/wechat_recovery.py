"""Safe recovery entrypoint: fresh accessibility evidence, never a message sender.

No transport has passed the local group/new-message/sender acceptance yet. Even
an accessible UI tree cannot make this entrypoint start a listener or model.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TARGET_GROUP = "Ativia测试群"
CONFIG_RELATIVE = Path("docs/wechat-recovery/recovery.dryrun.json")
RUNTIME_RELATIVE = Path("benchmark/wechat-recovery/entrypoint")
OWNER = "ativia-wechat-recovery-readonly-probe-v1"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("recovery_config_schema_invalid")
    if value.get("dry_run") is not True:
        raise ValueError("dry_run_must_be_true")
    for name in ("send_enabled", "voice_enabled", "model_enabled"):
        if value.get(name) is not False:
            raise ValueError(name + "_must_be_false")
    if value.get("allowed_groups") != [TARGET_GROUP]:
        raise ValueError("exact_target_group_required")
    if value.get("transport") != "unverified":
        raise ValueError("transport_requires_local_capability_acceptance_and_reviewed_integration")
    return value


class AlreadyRunning(RuntimeError):
    pass


class RunLease:
    """OS-held lock plus a scoped stop token; PID is never used to kill a process."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.token = uuid4().hex
        self.handle = None
        self.lease_path = directory / "owner.json"

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.directory / "owner.lock").open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning("recovery_probe_already_running") from exc
        try:
            write_json(self.lease_path, {"owner": OWNER, "pid": os.getpid(), "token": self.token})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def stop_requested(self) -> bool:
        try:
            request = json.loads((self.directory / "stop.json").read_text(encoding="utf-8"))
            return request.get("owner") == OWNER and request.get("token") == self.token
        except (OSError, ValueError, AttributeError):
            return False

    def __exit__(self, *unused):
        if self.handle is not None:
            # Remove metadata while still owning the OS lock. A new owner cannot
            # be mistaken for this run, even after a crash or PID reuse.
            self.lease_path.unlink(missing_ok=True)
            self.handle.close()
            self.handle = None


def request_stop(directory: Path) -> dict:
    try:
        owner = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "NOT_RUNNING", "action": "no_process_touched"}
    token = owner.get("token") if isinstance(owner, dict) else None
    if not isinstance(owner, dict) or owner.get("owner") != OWNER or not isinstance(token, str) or len(token) != 32:
        raise ValueError("invalid_recovery_owner_metadata")
    # A stale record can receive a harmless stale token. It cannot target a
    # process by PID, and a future run has a different token.
    write_json(directory / "stop.json", {"owner": OWNER, "token": token})
    return {"status": "STOP_REQUESTED", "action": "own_probe_cooperative_cancel_only"}


def run_probe(root: Path, output: Path, lease: RunLease, timeout: float) -> dict:
    probe = root / "tools/wechat_accessibility_probe.py"
    if not probe.is_file():
        return {"status": "PARTIAL_EXTERNAL", "reason": "readonly_probe_missing", "exit_code": 1}
    command = [sys.executable, str(probe), "--worker", "--wechat", "--output", str(output)]
    # Only this fresh child may be terminated. Never enumerate, attach to, or
    # signal WeChat, Ativia, or a PID loaded from owner.json.
    with output.with_suffix(".stdout.log").open("w", encoding="utf-8") as stdout, \
            output.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen(command, cwd=root, stdout=stdout, stderr=stderr, shell=False)
        deadline = time.monotonic() + timeout
        cancelled = False
        expired = False
        try:
            while child.poll() is None:
                cancelled = lease.stop_requested()
                expired = time.monotonic() >= deadline
                if cancelled or expired:
                    break
                time.sleep(.1)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        if cancelled:
            return {"status": "STOPPED", "reason": "own_probe_cancelled", "exit_code": 130}
        if expired:
            return {"status": "PARTIAL_EXTERNAL", "reason": "readonly_probe_timeout", "exit_code": 1}
    try:
        report = json.loads(output.read_text(encoding="utf-8-sig"))
        target = report.get("target", report)
        probe_status = target.get("status", report.get("status"))
    except (OSError, ValueError, AttributeError):
        probe_status = "TOOL_ERROR"
    reason = "readonly_probe_error"
    code = 1
    if probe_status == "CLIENT_UNAVAILABLE" and child.returncode == 2:
        reason, code = "client_unavailable_or_login_or_unique_window_required", 2
    elif probe_status == "NO_SEMANTIC_STRUCTURE" and child.returncode == 2:
        reason, code = "current_probe_path_has_no_usable_chat_semantics", 2
    elif probe_status == "SEMANTIC_CANDIDATE" and child.returncode == 0:
        reason, code = "semantic_candidate_requires_local_group_new_message_sender_evidence_and_transport_integration", 2
    # This one-path daily recheck is not evidence that every authorized route
    # has been exhausted. That broader judgment belongs to PLAN/STATE/RESULT.
    return {"status": "PARTIAL_EXTERNAL", "reason": reason, "probe_path_status": probe_status,
            "probe_exit_code": child.returncode, "exit_code": code}


def execute(action: str, root: Path, config_path: Path, timeout: float = 45) -> dict:
    directory = root / RUNTIME_RELATIVE
    if action == "stop":
        return request_stop(directory)
    load_config(config_path)
    with RunLease(directory) as lease:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + lease.token[:8]
        output = directory / (run_id + ".probe.json")
        result = run_probe(root, output, lease, timeout)
        result.update(action=action, run_id=run_id, captured_utc=datetime.now(timezone.utc).isoformat(),
                      evidence=str(output), target_group=TARGET_GROUP,
                      group_identity_verified=False, real_new_messages_verified=False,
                      sender_attribution_verified=False, listener_started=False,
                      model_calls=0, dry_run=True, real_outbound_actions=0,
                      send_audit_scope="this_entrypoint_and_its_readonly_probe; no_send_or_ui_input_path_loaded",
                      live_send_test="NOT_AUTHORIZED", automatic_group_chat="NO")
        write_json(directory / (run_id + ".result.json"), result)
        write_json(directory / "latest.json", result)
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "diagnose", "stop"))
    parser.add_argument("--config", type=Path, default=ROOT / CONFIG_RELATIVE)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    print("ZERO SEND / 零发送模式: dry_run=true; no listener/model/voice started.", flush=True)
    try:
        expected = ROOT / ".venv/Scripts/python.exe"
        if Path(sys.executable).resolve() != expected.resolve():
            raise ValueError("use_project_venv_via_the_batch_entrypoints")
        if not args.config.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("recovery_config_must_be_within_this_project")
        result = execute(args.action, ROOT, args.config, args.timeout)
    except AlreadyRunning as exc:
        result = {"status": "ALREADY_RUNNING", "reason": str(exc), "exit_code": 3}
    except (OSError, ValueError) as exc:
        result = {"status": "PARTIAL_EXTERNAL", "reason": type(exc).__name__ + ": " + str(exc), "exit_code": 1}
    except KeyboardInterrupt:
        result = {"status": "STOPPED", "reason": "keyboard_interrupt", "exit_code": 130}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result.get("exit_code", 0)


if __name__ == "__main__":
    raise SystemExit(main())

