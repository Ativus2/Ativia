"""Verify this round's backups; --apply restores only unchanged deployed files.

Stop visual first. No process is restarted, no private database is copied, and
runtime evidence/model budget is preserved. Later user edits abort the restore.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = ROOT / "runtime/wechat-visual/backups/20260930/deployment.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if Path(data["project"]).resolve() != ROOT:
        raise RuntimeError("wrong project manifest")
    from integrations.wechat.visual.safety import ExclusiveLease
    with ExclusiveLease():
        entries = []
        for row in data["files"]:
            destination = (ROOT / row["relative"]).resolve()
            if not destination.is_relative_to(ROOT):
                raise RuntimeError("unsafe manifest path")
            entries.append((destination, row))
        config = data.get("legacy_config")
        if config:
            expected = Path.home() / "AppData/Local/Ativia/wechat.json"
            if Path(config["path"]).resolve() != expected.resolve():
                raise RuntimeError("unsafe configuration path")
            entries.append((expected, config))
        conflicts = []
        for destination, row in entries:
            if sha(destination) != row["deployed_sha256"]:
                conflicts.append(str(destination))
            if row["original_sha256"] is not None:
                saved = Path(row["backup"]).resolve()
                if not saved.is_relative_to(manifest.parent) or sha(saved) != row["original_sha256"]:
                    conflicts.append("backup mismatch: " + str(destination))
        print(json.dumps({"mode": "apply" if args.apply else "verify", "files": len(entries),
                          "conflicts": conflicts}, ensure_ascii=False, indent=2))
        if conflicts:
            return 2
        if args.apply:
            for destination, row in entries:
                if row["original_sha256"] is None:
                    destination.unlink()
                else:
                    shutil.copy2(row["backup"], destination)
            print("RESTORED. Main Ativia was not reloaded or restarted.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
