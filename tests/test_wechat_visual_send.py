"""Offline send admission/state machine; all native actions and observations fake."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.wechat.visual.send import SendContext, SendReply, SendRejected, WechatSendController, normalized


WINDOW=dict(hwnd=123,pid=456,process_started=100.,class_name='QtTest',title='微信',
            client_rect=[0,0,1000,800],client_screen=[10,20],dpi=168,minimized=False,process_name='Weixin.exe')


class Backend:
    def __init__(self):
        self.calls=[];self.submit_attempts=self.clicks=self.keys=0
        self.guard=None;self.fail=None
    def begin(self, context, guard):
        self.guard=guard;self.calls.append('begin')
    def check(self, context, *, focused):
        if not self.guard() or self.fail:
            raise SendRejected(self.fail or 'stale_or_paused')
    def focus(self, composer):
        self.calls.append('focus');self.clicks+=1
    def write(self, text):
        self.check(None,focused=True);self.calls.append('write');self.keys+=len(text)*2
    def submit(self):
        self.check(None,focused=True);self.calls.append('submit');self.submit_attempts+=1
    def end(self):
        self.calls.append('end')


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.now=100.;self.wall=1700000000.;self.valid=True
        self.backend=Backend();self.requests=[];self.ended=[]
        self.c=WechatSendController(self.root,self.backend,self.requests.append,self.ended.append,
                                    valid=lambda context:self.valid,clock=lambda:self.now,wall=lambda:self.wall)
        self.context=SendContext('binding','capture',2,dict(WINDOW))
    def tearDown(self):
        self.c.disable('test_end',stopped=True);self.tmp.cleanup()
    def arm(self):
        self.c.arm(self.context,confirmed=True,ready=True)
    def task(self, identity='reply-a', **changes):
        values=dict(reply_id=identity,text='测试 '+identity,source_event_id='event-'+identity,
                    source_track_id='track-'+identity,source_binding_id='binding',conversation_id='wechat:group:test',
                    model_request_id='model-'+identity,created_at=self.wall,epoch=self.c.session.epoch)
        values.update(changes)
        return SendReply(**values)
    def observation(self, **changes):
        request=self.requests[-1]
        row=dict(request_id=request['request_id'],binding_id='binding',capture_session_id='capture',capture_epoch=2,
                 group='Ativia测试群',header_exact=True,window=dict(WINDOW),observed_at=self.wall,
                 composer=[350,570,550,140],input_empty=self.c.phase!='PRE_SUBMIT',input_focused=True,
                 input_complete=True,input_text=self.c.active.text,own_bubbles=[])
        if self.c.phase=='VERIFY':
            row['own_bubbles']=[dict(text=self.c.active.text,side='right',complete=True,confidence=.98,box=[700,300,200,60])]
        row.update(changes)
        return row
    def step(self, **changes):
        self.now+=.2;self.wall+=.2
        if self.c.active and self.c.phase!='WRITE':self.c.observe(self.observation(**changes))
        self.c.tick()
    def until(self, phase):
        for _ in range(100):
            if self.c.phase==phase:return
            self.step()
        self.fail('stage not reached '+phase)
    def complete(self):
        for _ in range(150):
            self.step()
            if self.c.active is None and not self.c.queue:return
        self.fail('transaction did not finish')

    def test_disabled_rejects_without_input(self):
        self.assertFalse(self.c.enqueue(self.task()))
        self.c.tick();self.assertEqual(self.backend.calls,[])

    def test_cleanup_failure_releases_transaction_without_replaying_result(self):
        self.arm();self.c.enqueue(self.task());self.until('VERIFY')
        def failed_end():raise OSError('native window disappeared')
        self.backend.end=failed_end
        self.complete()
        self.assertIsNone(self.c.active);self.assertFalse(self.c.session.armed)
        self.assertEqual(self.backend.submit_attempts,1)
        self.c.tick();self.assertEqual(self.backend.submit_attempts,1)

    def test_stopped_is_terminal_despite_late_invalidation(self):
        self.c.disable(stopped=True);self.c.disable('late',paused=True)
        self.assertEqual(self.c.session.state,'SEND_STOPPED')
    def test_confirmation_and_current_readiness_required(self):
        for confirmed,ready in [(False,True),(True,False)]:
            with self.assertRaises(SendRejected):self.c.arm(self.context,confirmed=confirmed,ready=ready)
        self.valid=False
        with self.assertRaises(SendRejected):self.arm()
    def test_enabled_real_sequence_and_confirmed_once(self):
        self.arm();self.assertTrue(self.c.enqueue(self.task()));self.complete()
        self.assertEqual(self.backend.calls,['begin','focus','write','submit','end'])
        self.assertEqual(self.c.snapshot()['confirmed'],1)
        self.assertTrue(self.c.session.armed)
    def test_multiple_distinct_replies_remain_armed_without_restarting(self):
        self.arm();identity=self.c.session.session_id;epoch=self.c.session.epoch
        for number in range(4):self.assertTrue(self.c.enqueue(self.task('reply-'+str(number))))
        self.complete()
        self.assertEqual(self.c.snapshot()['confirmed'],4)
        self.assertEqual(self.backend.submit_attempts,4)
        self.assertEqual(self.c.session.session_id,identity);self.assertEqual(self.c.session.epoch,epoch)
        self.assertTrue(self.c.session.armed)
    def test_duplicate_reply_and_same_event_cannot_queue_twice(self):
        self.arm();task=self.task();self.assertTrue(self.c.enqueue(task))
        self.assertFalse(self.c.enqueue(task));self.assertFalse(self.c.enqueue(replace(task,reply_id='other-reply')))
        self.complete();self.assertFalse(self.c.enqueue(task));self.assertEqual(self.backend.submit_attempts,1)
    def test_submit_ledger_survives_controller_restart(self):
        self.arm();task=self.task();self.c.enqueue(task);self.complete()
        other=WechatSendController(self.root,Backend(),lambda _:None,lambda _:None,valid=lambda _:True)
        other.arm(self.context,confirmed=True,ready=True)
        self.assertFalse(other.enqueue(replace(task,epoch=other.session.epoch,created_at=other.wall())))
        with self.assertRaises(SendRejected):other.ledger.submit(task.reply_id)
    def test_epoch_and_binding_mismatch_reject(self):
        self.arm()
        self.assertFalse(self.c.enqueue(self.task(epoch=0)))
        self.assertFalse(self.c.enqueue(self.task(source_binding_id='wrong')))
    def test_disable_during_generation_and_reenable_drops_old_generation(self):
        self.arm();old=self.task();self.c.disable();self.arm()
        self.assertFalse(self.c.enqueue(old));self.assertTrue(self.c.enqueue(self.task('fresh')))
    def test_draft_blocks_without_focus_write_or_enter(self):
        self.arm();self.c.enqueue(self.task());self.c.tick();self.step(input_empty=False)
        self.assertEqual(self.backend.calls,['begin','end'])
        self.assertEqual(self.c.last_result['reason'],'INPUT_NOT_EMPTY');self.assertTrue(self.c.session.armed)
    def test_pause_before_focus(self):
        self.arm();self.c.enqueue(self.task());self.c.tick();self.c.disable(paused=True)
        self.assertNotIn('focus',self.backend.calls);self.assertEqual(self.backend.submit_attempts,0)
    def test_pause_after_focus(self):
        self.arm();self.c.enqueue(self.task());self.until('FOCUSED');self.c.disable(paused=True)
        self.assertNotIn('write',self.backend.calls);self.assertEqual(self.backend.submit_attempts,0)
    def test_pause_after_write(self):
        self.arm();self.c.enqueue(self.task());self.until('PRE_SUBMIT');self.c.disable(paused=True)
        self.assertIn('write',self.backend.calls);self.assertEqual(self.backend.submit_attempts,0)
    def test_last_native_guard_prevents_enter_after_validation(self):
        self.arm();self.c.enqueue(self.task());self.until('PRE_SUBMIT')
        submit=self.backend.submit
        def cancelled_submit():
            self.valid=False;submit()
        self.backend.submit=cancelled_submit
        self.step();self.assertEqual(self.backend.submit_attempts,0)
        self.assertEqual(self.c.last_result['status'],'SEND_UNCERTAIN')
        self.assertFalse(self.c.enqueue(self.task()))
    def test_changed_composer_or_focus_prevents_submit(self):
        for bad in [dict(input_text='user changed draft'),dict(input_complete=False),dict(input_focused=False)]:
            with self.subTest(bad=bad):
                self.arm();self.c.enqueue(self.task(str(bad)));self.until('PRE_SUBMIT');self.step(**bad)
                self.assertEqual(self.backend.submit_attempts,0)
    def test_switch_target_after_write_disables_session(self):
        self.arm();self.c.enqueue(self.task());self.until('PRE_SUBMIT');self.step(group='other group')
        self.assertFalse(self.c.session.armed);self.assertEqual(self.backend.submit_attempts,0)
    def test_native_identity_changes_rejected_for_pid_geometry_dpi(self):
        for field,value in [('pid',999),('hwnd',999),('process_started',900),('dpi',144),('client_screen',[40,20]),('client_rect',[0,0,900,800])]:
            with self.subTest(field=field):
                self.arm();self.c.enqueue(self.task('change-'+field));self.c.tick()
                window=dict(WINDOW);window[field]=value;self.step(window=window)
                self.assertFalse(self.c.session.armed);self.assertEqual(self.backend.submit_attempts,0)
    def test_user_interaction_cancels_this_reply_not_entire_session(self):
        self.arm();self.backend.fail='SEND_CANCELLED_USER_INTERACTION';self.c.enqueue(self.task());self.complete()
        self.assertTrue(self.c.session.armed);self.assertEqual(self.backend.submit_attempts,0)
        self.backend.fail=None;self.c.enqueue(self.task('next'));self.complete();self.assertEqual(self.backend.submit_attempts,1)
    def test_destroyed_window_disarms(self):
        self.arm();self.backend.fail='window_closed';self.c.enqueue(self.task());self.complete()
        self.assertFalse(self.c.session.armed)
    def test_uncertain_disarms_cancels_queue_and_does_not_retry(self):
        self.arm();self.c.enqueue(self.task());self.until('VERIFY')
        self.c.enqueue(self.task('already-queued'))
        while self.c.active:self.step(own_bubbles=[])
        self.assertEqual(self.c.last_result['status'],'SEND_UNCERTAIN')
        self.assertFalse(self.c.session.armed);self.assertEqual(self.backend.submit_attempts,1)
        self.assertFalse(self.c.queue);self.assertFalse(self.c.enqueue(self.task('next')))
        self.c.tick();self.assertEqual(self.backend.submit_attempts,1)
    def test_restart_with_durable_intent_reports_unknown_and_never_replays(self):
        self.arm();task=self.task();self.c.ledger.reserve(task);self.c.ledger.submit(task.reply_id)
        queued=self.task('queued-before-crash');self.c.ledger.reserve(queued)
        backend=Backend()
        other=WechatSendController(self.root,backend,lambda _:None,lambda _:None,valid=lambda _:True)
        self.assertEqual(other.last_result['status'],'SEND_UNCERTAIN')
        self.assertEqual(other.snapshot()['recovered_counts']['DROP_STALE_REPLY'],1)
        self.assertFalse(other.session.armed);other.tick();self.assertEqual(backend.calls,[])
        with self.assertRaises(SendRejected):other.ledger.submit(task.reply_id)
        other.arm(self.context,confirmed=True,ready=True)
        self.assertFalse(other.enqueue(replace(task,epoch=other.session.epoch,created_at=other.wall())))
    def test_terminal_callback_once_and_sink_failure_never_changes_delivery_result(self):
        results=[]
        def sink(task,status,reason,wall):
            results.append((task.reply_id,status));raise RuntimeError('sink failure')
        self.c.on_result=sink;self.arm();self.c.enqueue(self.task());self.complete();self.c.tick()
        self.assertEqual(results,[('reply-a','SEND_CONFIRMED')])
        self.assertEqual(self.c.last_result['status'],'SEND_CONFIRMED')
        self.assertEqual(self.backend.submit_attempts,1)
    def test_result_io_failure_after_submit_disarms_and_clears_queue(self):
        for confirmed in (True,False):
            with self.subTest(confirmed=confirmed):
                self.arm();self.c.enqueue(self.task('io-'+str(confirmed)));self.until('VERIFY')
                self.c.enqueue(self.task('queued-'+str(confirmed)))
                before=self.backend.submit_attempts
                with patch.object(self.c.ledger,'result',side_effect=OSError('disk failure')):
                    if confirmed:self.complete()
                    else:
                        while self.c.active:self.step(own_bubbles=[])
                self.assertEqual(self.c.last_result['status'],'SEND_UNCERTAIN')
                self.assertFalse(self.c.session.armed);self.assertFalse(self.c.queue)
                self.c.tick();self.assertEqual(self.backend.submit_attempts,before)
    def test_terminal_log_failure_after_enter_is_visible_unknown_and_stops_sends(self):
        self.arm();self.c.enqueue(self.task());self.until('VERIFY')
        self.step()
        original=self.c.log
        def failing_log(stage,*args,**kwargs):
            if stage=='SEND_CONFIRMED':raise OSError('log full')
            return original(stage,*args,**kwargs)
        with patch.object(self.c,'log',side_effect=failing_log):self.step()
        self.assertFalse(self.c.session.armed)
        self.assertEqual(self.c.last_result['status'],'SEND_UNCERTAIN')
        self.assertEqual(self.backend.submit_attempts,1)
    def test_old_identical_bubble_does_not_confirm(self):
        self.arm();self.c.enqueue(self.task());self.c.tick()
        old=dict(text=self.c.active.text,side='right',complete=True,confidence=.99,box=[700,300,200,60])
        self.step(own_bubbles=[old]);self.until('VERIFY')
        while self.c.active:self.step(own_bubbles=[old])
        self.assertEqual(self.c.last_result['status'],'SEND_UNCERTAIN')
    def test_left_or_low_confidence_bubble_never_confirms(self):
        self.arm();self.c.enqueue(self.task());self.until('VERIFY')
        wrong=dict(text=self.c.active.text,side='left',complete=True,confidence=.99,box=[100,300,200,60])
        while self.c.active:self.step(own_bubbles=[wrong])
        self.assertEqual(self.c.last_result['status'],'SEND_UNCERTAIN')
    def test_stale_queue_item_drops_without_input(self):
        self.arm();self.c.enqueue(self.task());self.wall+=31;self.c.tick()
        self.assertEqual(self.backend.calls,['end']);self.assertEqual(self.c.last_result['reason'],'DROP_STALE_REPLY')
    def test_stop_cancels_queue_and_does_not_close_external_apps(self):
        self.arm();self.c.enqueue(self.task());self.c.enqueue(self.task('b'));self.c.disable(stopped=True)
        self.assertEqual(self.c.snapshot()['queue_size'],0);self.assertEqual(self.backend.calls,[])
        self.assertEqual(self.c.session.state,'SEND_STOPPED')
    def test_observation_timeout_no_retry(self):
        self.arm();self.c.enqueue(self.task());self.c.tick();self.now+=7;self.c.tick()
        self.assertEqual(self.c.last_result['status'],'SEND_FAILED');self.assertEqual(self.backend.submit_attempts,0)
    def test_serial_queue_and_minimum_submit_interval(self):
        times=[];original=self.backend.submit
        def submit():times.append(self.now);original()
        self.backend.submit=submit;self.arm()
        self.c.enqueue(self.task());self.c.enqueue(self.task('b'));self.complete()
        self.assertEqual(len(times),2);self.assertGreaterEqual(times[1]-times[0],2)
        self.assertEqual(self.backend.calls.count('end'),2)
    def test_normalization_multiline_unicode_without_fuzzy_char_matching(self):
        self.assertEqual(normalized('Ａtivia\r\n 你好'),normalized('Ativia你好'))
        self.assertNotEqual(normalized('Ativia你好'),normalized('Ativia你坏'))
    def test_log_and_status_do_not_save_body(self):
        self.arm();self.c.enqueue(self.task(text='PRIVATE_BODY_CANARY'));self.complete()
        log=(self.root/'send-lifecycle.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE_BODY_CANARY',log);self.assertNotIn('PRIVATE_BODY_CANARY',json.dumps(self.c.snapshot()))
        self.assertIn('SEND_VISUAL_CONFIRMATION',log)


if __name__=='__main__':unittest.main()
