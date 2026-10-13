import os,tempfile,unittest
from pathlib import Path
from personality.engine import BehaviorPlanner,BehaviorContext,MODES,core_prompt
from companion_runtime import CompanionRuntime
from runtime_settings import RuntimeSettings
from vision import build_text_request

class PersonalityTests(unittest.TestCase):
    def setUp(self):self.planner=BehaviorPlanner()
    def test_all_modes_use_one_core_and_explicit_questions_are_answered(self):
        for mode in MODES:
            d=self.planner.plan(BehaviorContext('这个任务怎么打？',mode=mode))
            self.assertEqual(d.action,'respond');self.assertTrue(d.allow_teaching)
            params=build_text_request('这个任务怎么打？','','[]',RuntimeSettings(),
                companion_context=dict(mode=mode,decision=d.__dict__))
            self.assertIn(core_prompt(),params['messages'][0]['content'])
    def test_class_focus_sleep_and_pause_skip_model_work(self):
        for mode in ('CLASS','FOCUS','SLEEP'):
            self.assertEqual(self.planner.plan(BehaviorContext(mode=mode,screen_event='death_screen')).action,'stay_silent')
        self.assertEqual(self.planner.plan(BehaviorContext('明确问题',paused=True)).action,'stay_silent')
    def test_ordinary_game_events_are_silent_and_death_never_teaches(self):
        for event in ('none','normal','ordinary_combat'):
            self.assertEqual(self.planner.plan(BehaviorContext(mode='GAME',screen_event=event)).action,'stay_silent')
        plan=self.planner.plan(BehaviorContext(mode='GAME',screen_event='death_screen'))
        self.assertFalse(plan.allow_teaching);self.assertFalse(plan.allow_question)
        self.assertEqual(plan.action,'screen_comment')
    def test_preference_not_mistaken_for_teaching_request(self):
        plan=self.planner.plan(BehaviorContext('以后我玩熟悉的游戏时，不要主动教我怎么玩。',mode='GAME'))
        self.assertFalse(plan.allow_teaching)
    def test_short_acknowledgments_do_not_require_a_followup_question(self):
        self.assertEqual(self.planner.plan(BehaviorContext('谢谢')).action,'acknowledge')
        self.assertFalse(self.planner.plan(BehaviorContext('今天有点累')).allow_question)
    def test_saved_modes_and_relationship_do_not_rewrite_api_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'companion.json';api=Path(directory)/'settings.json'
            api.write_text('opaque settings preserved');before=api.read_bytes()
            r=CompanionRuntime(path);r.set_mode('GAME');r.record('不要主动打扰我');r.close()
            r.memory.close_future.result(2)
            n=CompanionRuntime(path)
            self.assertEqual(n.mode,'GAME');self.assertEqual(n.relationship.initiative_tolerance,.1)
            n.set_mode('CLASS',manual=False);self.assertEqual(n.mode,'GAME')
            self.assertEqual(api.read_bytes(),before)
            n.close()
            n.memory.close_future.result(2)
    def test_class_blocks_preview_admission_as_well_as_regular_reading(self):
        from PySide6.QtWidgets import QApplication
        from controls import ControlBar
        from tts_pipeline import SpeechPipeline,SpeechJob
        from unittest.mock import Mock
        app=QApplication.instance() or QApplication([])
        control=ControlBar();control._set_mode('learning')
        pipe=SpeechPipeline(Mock());pipe.allow_output=control.tts_allowed
        try:
            self.assertFalse(control.speak_enabled())
            self.assertFalse(pipe.submit(SpeechJob('试听',RuntimeSettings(),source='preview')))
            self.assertIsNone(pipe._worker)
        finally:pipe.close();control.close()
