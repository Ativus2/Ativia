"""Unavailable/locked desktop and long-running binding: fake state only."""
import unittest
from dataclasses import asdict
from unittest.mock import patch
from integrations.wechat.visual import desktop,worker
import test_wechat_visual_worker as worker_fixture
import test_wechat_visual_send_boundaries as coordinator_fixture


class DesktopTests(unittest.TestCase):
    def test_default_active_interactive_session_only(self):
        self.assertTrue(desktop.classify_desktop(1,0,'Default','Default').available)
        for values in [(0,0,'Default','Default'),(1,4,'Default','Default'),(1,0,'Winlogon','Default'),(1,0,'Default','Other')]:
            with self.subTest(values=values):self.assertFalse(desktop.classify_desktop(*values).available)
    def test_native_access_error_is_unknown_and_never_allowed(self):
        with patch.object(desktop,'_native_state',side_effect=OSError('access denied')):
            self.assertEqual(desktop.probe_desktop(),desktop.DesktopStatus(False,'desktop_state_unknown'))
    def test_old_unconsumed_confirmation_rejected_but_accepted_binding_identity_stays_valid(self):
        for elapsed in (1801,7201):
            with self.subTest(elapsed=elapsed),patch.object(worker.time,'time',return_value=1000+elapsed):
                self.assertFalse(worker.matches_confirmation(worker_fixture.WINDOW,worker_fixture.confirmation()))
                self.assertTrue(worker.same_confirmed_window(worker_fixture.WINDOW,worker_fixture.confirmation()))
                moved=dict(worker_fixture.WINDOW,client_screen=(2,3))
                self.assertFalse(worker.same_confirmed_window(moved,worker_fixture.confirmation()))
    def test_worker_bound_session_survives_two_hours_without_an_artificial_reconfirmation(self):
        h=worker_fixture.WorkerHarness(wall_jump=7201,new_message=True)
        packets=h.run()
        self.assertTrue(any(p['stage']=='listener_ready' for p in packets))
        self.assertEqual(sum(p['stage']=='event' for p in packets),1)
        self.assertFalse(any(p.get('reason')=='window_geometry_or_confirmation_changed' for p in packets))
    def test_lock_with_valid_cached_window_revokes_and_unlock_does_not_reuse_confirmation(self):
        h=worker_fixture.WorkerHarness(loops=10,locked_after=4,unlock_after=6,new_message=True)
        packets=h.run()
        self.assertTrue(any(p.get('reason')=='desktop_unavailable' for p in packets))
        self.assertFalse(any(p['stage']=='event' for p in packets))
        self.assertEqual(sum(p['stage']=='group_bound' for p in packets),1)
    def test_initial_lock_prevents_even_capture_or_ocr(self):
        h=worker_fixture.WorkerHarness(locked_after=1)
        h.run();self.assertEqual(h.captures,0);self.assertEqual(h.header_calls,0);self.assertEqual(h.body_calls,0)


class CoordinatorDesktopTests(unittest.TestCase):
    def setUp(self):
        self.fixture=coordinator_fixture.CoordinatorTests();self.fixture.setUp()
        self.c=self.fixture.c;self.fixture.ready();self.c.enable_send(confirmed=True)
    def tearDown(self):self.fixture.tearDown()
    def test_lock_cancels_queue_and_blocks_model_tick_admission(self):
        self.c.send_test_message('模拟回复')
        self.c._desktop_probe=lambda:desktop.DesktopStatus(False,'desktop_unavailable')
        self.c.poll()
        self.assertFalse(self.c.gate.allows());self.assertFalse(self.c.listener_ready)
        self.assertFalse(self.c.send_controller.queue);self.assertFalse(self.c.send_controller.session.armed)
        self.assertEqual(self.fixture.backend.submit_attempts,0)
        self.c._desktop_probe=lambda:desktop.DesktopStatus(True,'desktop_available')
        self.c.poll();self.assertTrue(self.c.gate.allows())
        self.assertFalse(self.c.listener_ready);self.assertFalse(self.c.send_controller.session.armed)
    def test_sleep_gap_revokes_old_reply_even_with_new_main_state_and_heartbeat(self):
        self.c.send_test_message('模拟回复')
        self.fixture.now+=6;self.c.last_heartbeat=self.fixture.now
        self.c.set_external_status({'app_state':'RUNNING'});self.c.poll()
        self.assertFalse(self.c.send_controller.queue);self.assertFalse(self.c.send_controller.session.armed)
        self.assertFalse(self.c.bound);self.assertEqual(self.fixture.backend.submit_attempts,0)
    def test_final_send_admission_checks_current_desktop_not_only_old_snapshot(self):
        context=self.c.send_context
        self.assertTrue(self.c._send_valid(context))
        self.c._desktop_probe=lambda:desktop.DesktopStatus(False,'desktop_unavailable')
        self.assertFalse(self.c._send_valid(context))
    def test_sleep_gap_does_not_resume_an_existing_user_pause(self):
        self.c.set_user_paused(True)
        with patch.object(self.c,'_command') as command:
            self.fixture.now+=6;self.c.last_heartbeat=self.fixture.now
            self.c.set_external_status({'app_state':'RUNNING'});self.c.poll()
            self.assertFalse(self.c.gate.allows())
            self.assertNotIn(unittest.mock.call('resume'),command.call_args_list)
            self.assertIn(unittest.mock.call('pause'),command.call_args_list)
    def test_gap_discards_old_lifecycle_and_events_until_matching_worker_ack(self):
        from integrations.wechat.visual.types import VisualEvent,TARGET_GROUP
        from test_wechat_visual_send import WINDOW
        capture=self.c.capture_session_id
        old=[dict(stage='group_bound',binding_id='old',capture_session_id=capture,capture_epoch=2,window=dict(WINDOW)),
             dict(stage='baseline_complete'),dict(stage='listener_ready'),
             dict(stage='event',event=asdict(VisualEvent(observation_id=capture+':t00000001',binding_id='old',group_name=TARGET_GROUP,
                  speaker_session_id='wechat:visual:person:'+capture+':1',sender_name='Synthetic',text='@Ativia 旧消息',
                  observed_at=self.fixture.wall,capture_session_id=capture,capture_epoch=2,sequence=1,confidence=.99,source='local_ocr')))]
        for packet in old:self.c._queue.put(packet)
        self.fixture.now+=6;self.c.last_heartbeat=self.fixture.now
        self.c.set_external_status({'app_state':'RUNNING'});self.c.poll()
        self.assertFalse(self.c.listener_ready);self.assertFalse(self.c.bound)
        self.assertEqual(self.c.session.events,[]);barrier=self.c._reset_barrier
        self.c._packet(dict(stage='unbound',reason='resumed',reset_id='old-reset'))
        self.assertEqual(self.c._reset_barrier,barrier)
        self.c._packet(dict(stage='unbound',reason='resumed',reset_id=barrier))
        self.assertIsNone(self.c._reset_barrier);self.assertFalse(self.c.bound)


if __name__=='__main__':unittest.main()
