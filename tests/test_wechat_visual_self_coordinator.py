"""The one-shot self route is isolated from ordinary incoming acceptance."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from assistant_pause import PauseGate
from integrations.wechat.visual.desktop import DesktopStatus
from integrations.wechat.visual.coordinator import VisualCoordinator
from integrations.wechat.visual.safety import SafetyError
from integrations.wechat.visual.types import TARGET_GROUP
from test_wechat_visual_coordinator import FakeLease, FakeProcess, FakeSession


class SelfSession(FakeSession):
    def __init__(self, *args, self_test_mode=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.self_test_mode = self_test_mode
        self.self_observations = []

    def receive_self_test(self, observation, token):
        self.self_observations.append((observation, token))
        return True


class SelfCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        python = self.root / 'fake-python.exe'
        python.touch()
        resource = self.root / 'ocr'
        resource.mkdir()
        self.config = dict(python=str(python), resource_root=str(resource), backend='win32_printwindow',
                           calibration=dict(header=[0, 0, 500, 60], message=[0, 60, 500, 500], input=[0, 560, 500, 100]))
        self.now, self.wall = 100., 1700000000.
        self.all = []
        self.c = self.make()

    def make(self, action='self-test'):
        c = VisualCoordinator(self.root, action, self.config, PauseGate(True), object(),
                              session_factory=SelfSession, lease_factory=FakeLease,
                              process_factory=FakeProcess, parent_start=1234,
                              clock=lambda: self.now, wall_clock=lambda: self.wall,
                              desktop_probe=lambda:DesktopStatus(True,'desktop_available'))
        self.all.append(c)
        c.set_external_status({'app_state': 'RUNNING'})
        c.start()
        return c

    def tearDown(self):
        for c in self.all:
            c.stop()
        self.temp.cleanup()

    def candidate(self):
        self.c._packet(dict(stage='candidate', window=dict(hwnd=123, pid=456),
                            header=dict(exact_match=True, complete=True), layout=self.config['calibration']))

    def bound(self):
        self.c.confirm_candidate(bottom_confirmed=True, self_sent_confirmed=True)
        self.c._packet(dict(stage='unbound', reason='manual_confirmation', capture_epoch=1,reset_id=self.c._reset_barrier))
        self.candidate()  # Fresh post-reset layout, as emitted by the real worker.
        self.c._packet(dict(stage='group_bound', binding_id='self-binding',
                            capture_session_id=self.c.capture_session_id, capture_epoch=1))

    def observation(self):
        return dict(stage='self_test_observation', observation=dict(
            token=self.c.self_test_token, text=self.c.self_test_text, group_name=TARGET_GROUP,
            binding_id='self-binding', capture_session_id=self.c.capture_session_id, capture_epoch=1,
            observed_at=self.wall, source='local_ocr', self_state='self', side='right', confidence=.99,
            box=[300, 100, 150, 70], evidence_kind='real_self_test', kind='text', complete=True, frame_sequence=8,
            stable_frames=2))

    def ready_self(self):
        self.candidate()
        self.bound()

    def test_mode_opt_in_and_worker_arguments(self):
        self.assertTrue(self.c.session.self_test_mode)
        self.assertTrue(self.c.session.model_enabled)
        self.assertIn('--self-test-token', self.c.process.command)
        self.assertIn('--self-test-issued-at', self.c.process.command)
        self.assertRegex(self.c.self_test_token, '^[0-9A-F]{12}$')
        normal = self.make('dry-run')
        self.assertFalse(normal.session.self_test_mode)
        self.assertNotIn('--self-test-token', normal.process.command)

    def test_requires_both_fresh_human_confirmations(self):
        self.candidate()
        with self.assertRaises(SafetyError):
            self.c.confirm_candidate(bottom_confirmed=True)
        with self.assertRaises(SafetyError):
            self.c.confirm_candidate(bottom_confirmed=False, self_sent_confirmed=True)
        self.now += 6
        with self.assertRaises(SafetyError):
            self.c.confirm_candidate(bottom_confirmed=True, self_sent_confirmed=True)

    def test_consumed_once_and_never_counted_as_incoming(self):
        self.ready_self()
        packet = self.observation()
        self.c._packet(packet)
        self.c._packet(packet)
        self.assertEqual(len(self.c.session.self_observations), 1)
        self.assertEqual(self.c.events, 0)
        self.assertFalse(self.c.listener_ready)
        self.assertFalse(self.c.baseline_complete)
        self.assertTrue(self.c.self_test_consumed)
        with self.assertRaises(SafetyError):
            self.c.confirm_candidate(bottom_confirmed=True, self_sent_confirmed=True)

    def test_self_mode_rejects_normal_event_and_listener_ready(self):
        self.ready_self()
        self.c._packet(dict(stage='event', event={}))
        self.assertEqual(self.c.session.events, [])
        with self.assertRaises(ValueError):
            self.c._packet(dict(stage='listener_ready'))

    def test_normal_mode_cannot_accept_self_packet(self):
        normal = self.make('dry-run')
        normal._packet(self.observation())
        self.assertEqual(normal.session.self_observations, [])

    def test_wrong_provenance_and_challenge_are_rejected(self):
        self.ready_self()
        bad = dict(token='A'*12, text='@Ativia unbound', group_name='wrong group', self_state='incoming',
                   side='left', source='synthetic', binding_id='old', capture_session_id='old', capture_epoch=8,
                   observed_at=self.wall-6, confidence=.89, evidence_kind='real_incoming',
                   complete=False, kind='quote', stable_frames=1, frame_sequence=0, box=[-1, 80, 80, 20])
        for key, value in bad.items():
            with self.subTest(key=key):
                packet = self.observation()
                packet['observation'][key] = value
                with self.assertRaises(ValueError):
                    self.c._packet(packet)
        self.assertEqual(self.c.session.self_observations, [])
        self.assertFalse(self.c.self_test_consumed)

    def test_stale_challenge_pauses_capture_without_rearm(self):
        self.ready_self()
        self.now += 301
        self.wall += 301
        self.c.set_external_status(dict(app_state='RUNNING'))
        self.c._packet(dict(stage='heartbeat'))
        self.c.poll()
        self.assertTrue(self.c.self_test_expired)
        self.assertFalse(self.c.bound)
        self.c._packet(self.observation())
        self.assertEqual(self.c.session.self_observations, [])
        self.assertIn('"action": "pause"', self.c.process.stdin.getvalue())

    def test_pause_and_navigation_revoke_pending_acceptance(self):
        self.ready_self()
        self.c._packet(dict(stage='unbound', reason='manual_interaction_bottom_uncertain'))
        self.c._packet(self.observation())
        self.assertEqual(self.c.session.self_observations, [])
        self.assertFalse(self.c.self_test_confirmed)
        self.c.set_user_paused(True)
        self.c._packet(self.observation())
        self.assertEqual(self.c.session.self_observations, [])

    def test_durable_consumption_precedes_session_and_failure_cannot_retry(self):
        self.ready_self()
        def broken(observation, token):
            with closing(sqlite3.connect(self.c.round_root/'self-test-use.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM used WHERE token_hash=?',
                    (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0], 1)
            raise RuntimeError('mock model admission failed')
        self.c.session.receive_self_test = broken
        with self.assertRaises(RuntimeError):
            self.c._packet(self.observation())
        self.assertTrue(self.c.self_test_consumed)
        self.c._packet(self.observation())

    def test_self_preview_count_and_durable_log_are_separate_and_redacted(self):
        self.ready_self()
        self.c._packet(self.observation())
        self.c.session.preview = dict(trace_id='self-preview', input='PRIVATE_INPUT_CANARY', reply='PRIVATE_REPLY_CANARY',
                                      chunks=['PRIVATE_REPLY_CANARY'], personality_called=True, memory_recalled=0)
        self.c.poll()
        self.assertEqual(self.c.preview_count, 0)
        self.assertEqual(self.c.self_test_preview_count, 1)
        self.assertEqual(self.c.last_preview_metadata['evidence_kind'], 'real_self_test')
        text = (self.c.round_root/'status.json').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE_', text)
        self.assertNotIn(self.c.self_test_token, text)
        self.assertEqual(json.loads(text)['events'], 0)
        self.c._packet(dict(stage='unbound', reason='target_header_changed'))
        self.assertIsNone(self.c.preview)


if __name__ == '__main__':
    unittest.main()
