import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('wechat_rollback',Path(__file__).resolve().parents[1]/'tools/wechat_visual_rollback.py')
rollback=importlib.util.module_from_spec(spec);spec.loader.exec_module(rollback)


class RollbackTests(unittest.TestCase):
    def test_plan_validates_all_hashes_and_rejects_user_changes_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);backup=root/'backup';backup.mkdir();(backup/'source').mkdir()
            source=root/'app.py';source.write_text('new',encoding='utf-8')
            old=backup/'source/app.py';old.write_text('old',encoding='utf-8')
            manifest=backup/'checkpoint.json'
            row=dict(relative='app.py',original_sha256=rollback.digest(old),deployed_sha256=rollback.digest(source))
            state=dict(project=str(root),files=[row]);manifest.write_text(json.dumps(state),encoding='utf-8')
            self.assertEqual(len(rollback.rollback_plan(root,manifest)),1)
            self.assertEqual(source.read_text(),'new')
            source.write_text('user edited',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'changed_after_deploy'):rollback.rollback_plan(root,manifest)
            row['relative']='../outside.py';manifest.write_text(json.dumps(state),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'outside_project'):rollback.rollback_plan(root,manifest)


if __name__=='__main__':unittest.main()
