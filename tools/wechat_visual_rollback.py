"""Preview or restore the October 12 source checkpoint; runtime data stays intact."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def rollback_plan(project, checkpoint):
    project=Path(project).resolve();checkpoint=Path(checkpoint).resolve()
    if not checkpoint.is_relative_to(project):raise ValueError('checkpoint_outside_project')
    state=json.loads(checkpoint.read_text(encoding='utf-8'))
    if Path(state['project']).resolve()!=project:raise ValueError('checkpoint_project_mismatch')
    plan=[]
    for row in state['files']:
        relative=row['relative'];target=(project/relative).resolve()
        if not target.is_relative_to(project):raise ValueError('target_outside_project')
        if row['original_sha256']==row['deployed_sha256']:continue
        if digest(target)!=row['deployed_sha256']:raise ValueError('file_changed_after_deploy: '+relative)
        original=checkpoint.parent/'source'/relative
        archive=(checkpoint.parent/'rollback-new'/relative).resolve()
        if not original.resolve().is_relative_to(checkpoint.parent) or not archive.is_relative_to(checkpoint.parent):
            raise ValueError('backup_outside_checkpoint')
        if row['original_sha256'] is not None and digest(original)!=row['original_sha256']:
            raise ValueError('backup_hash_mismatch: '+relative)
        if row['original_sha256'] is None and archive.exists():raise ValueError('archive_already_exists: '+relative)
        plan.append((relative,target,original,archive,row['original_sha256'] is None))
    return plan


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Restore after stopping the visual runner; default only previews.')
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[1]
    checkpoint=project/'runtime/wechat-visual/backups/goal-20261012/checkpoint.json'
    sys.path.insert(0,str(project))
    from integrations.wechat.visual.safety import ExclusiveLease
    # Enforce one owner even for rollback. Never stop or kill another process.
    with ExclusiveLease():
        plan=rollback_plan(project,checkpoint)
        if args.apply:
            for relative,target,original,archive,is_new in plan:
                if is_new:
                    archive.parent.mkdir(parents=True,exist_ok=True)
                    shutil.move(str(target),str(archive))
                else:shutil.copy2(original,target)
        print(json.dumps(dict(applied=args.apply,files=[row[0] for row in plan],runtime_data='unchanged'),ensure_ascii=False))


if __name__=='__main__':main()
