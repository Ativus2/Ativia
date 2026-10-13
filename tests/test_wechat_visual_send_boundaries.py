"""No desktop input: pixel fixtures, fake Win32 APIs and isolated Ativia core."""
import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
from assistant_pause import PauseGate
from integrations.wechat.visual.desktop import DesktopStatus
from runtime_settings import RuntimeSettings
from integrations.wechat.visual import send_native as native
from integrations.wechat.visual.send import SendContext, SendRejected, WechatSendController
from integrations.wechat.visual.send_observe import inspect_composer, composer_region
from integrations.wechat.visual.layout import Layout, OCRRow
from integrations.wechat.visual.types import Rect, TARGET_GROUP
from integrations.wechat.visual.worker import parse_command
from integrations.wechat.visual.coordinator import VisualCoordinator
from integrations.wechat.visual.session import VisualSession
import test_wechat_visual_send as state_fixture
import test_wechat_visual_coordinator as coordinator_fixture
import test_wechat_visual_service as service_fixture


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.image=Image.new('RGB',(600,350),(30,30,31))
        self.region=Rect(100,150,450,140)
    def test_blank_and_narrow_caret_are_empty(self):
        self.assertTrue(inspect_composer(self.image,self.region,[],168)['input_empty'])
        ImageDraw.Draw(self.image).rectangle((103,155,104,180),fill='white')
        self.assertFalse(inspect_composer(self.image,self.region,[],168)['input_empty'])
        self.assertTrue(inspect_composer(self.image,self.region,[],168,allow_caret=True)['input_empty'])
    def test_unrecognized_draft_emoji_or_attachment_is_not_empty(self):
        for box in [(115,156,180,180),(130,170,150,190),(100,150,400,240)]:
            with self.subTest(box=box):
                im=self.image.copy();ImageDraw.Draw(im).rectangle(box,fill='white')
                self.assertFalse(inspect_composer(im,self.region,[],168)['input_empty'])
    def test_multiline_rows_are_aggregated_and_verified(self):
        rows=[OCRRow('第二行',Rect(115,210,170,30),.99),OCRRow('第一行',Rect(115,160,170,30),.99)]
        draw=ImageDraw.Draw(self.image)
        for row in rows:draw.rectangle((row.box.x+2,row.box.y+2,row.box.right-2,row.box.bottom-2),fill='white')
        result=inspect_composer(self.image,self.region,rows,168)
        self.assertFalse(result['input_empty']);self.assertTrue(result['input_complete'])
        self.assertEqual(result['input_text'],'第一行\n第二行')
    def test_ocr_confidence_boundary_clipping_and_unexplained_ink_fail_closed(self):
        for row in [OCRRow('draft',Rect(115,160,170,30),.89),OCRRow('draft',Rect(100,160,170,30),.99)]:
            result=inspect_composer(self.image,self.region,[row],168)
            self.assertFalse(result['input_complete']);self.assertFalse(result['input_empty'])
        ImageDraw.Draw(self.image).rectangle((450,240,500,265),fill='white')
        self.assertFalse(inspect_composer(self.image,self.region,[OCRRow('draft',Rect(115,160,170,30),.99)],168)['input_complete'])
    def test_unknown_theme_is_not_empty(self):
        im=Image.new('RGB',self.image.size,'white')
        self.assertFalse(inspect_composer(im,self.region,[],168)['input_empty'])

    def test_first_editor_line_cannot_hide_above_empty_detection_roi(self):
        im=Image.new('RGB',(1397,1131),(30,30,31))
        layout=Layout(Rect(526,0,871,140),Rect(526,141,871,636),Rect(526,778,871,353))
        with patch('integrations.wechat.visual.send_observe.detect_layout',return_value=layout):
            region,_=composer_region(im,168)
        self.assertLess(region.y,800)
        ImageDraw.Draw(im).rectangle((565,800,710,820),fill='white')
        self.assertFalse(inspect_composer(im,region,[],168)['input_empty'])
    def test_region_recomputed_from_actual_layout_and_dpi(self):
        for width,height,dpi,x,y in [(1000,800,96,350,580),(1600,1200,168,530,800)]:
            im=Image.new('RGB',(width,height))
            layout=Layout(Rect(x,0,width-x,80),Rect(x,80,width-x,y-80),Rect(x,y,width-x,height-y))
            with patch('integrations.wechat.visual.send_observe.detect_layout',return_value=layout):
                region,_=composer_region(im,dpi)
            self.assertGreater(region.x,x);self.assertGreater(region.y,y)
            self.assertLess(region.right,width);self.assertLess(region.bottom,height)

    def test_active_send_button_does_not_invalidate_verified_draft(self):
        # Measured 175% DPI: enabling the green Send button adds 85 pixels
        # at y=1050. They belong to the toolbar, outside the editor.
        im=Image.new('RGB',(1397,1131),(30,30,31))
        layout=Layout(Rect(526,0,871,140),Rect(526,141,871,636),Rect(526,778,871,353))
        draw=ImageDraw.Draw(im)
        draw.rectangle((565,799,640,823),fill='white')
        draw.rectangle((1263,1050,1347,1090),fill=(7,193,96))
        rows=[OCRRow('测试01',Rect(562,796,81,30),.99856)]
        with patch('integrations.wechat.visual.send_observe.detect_layout',return_value=layout):
            region,_=composer_region(im,168)
        result=inspect_composer(im,region,rows,168)
        self.assertTrue(result['input_complete'])
        self.assertEqual(result['input_text'],'测试01')
        self.assertFalse(result['input_empty'])

    def test_toolbar_exclusion_still_rejects_extra_ink_and_bottom_clipping(self):
        layout=Layout(Rect(526,0,871,140),Rect(526,141,871,636),Rect(526,778,871,353))
        for clipped in (False,True):
            with self.subTest(clipped=clipped):
                im=Image.new('RGB',(1397,1131),(30,30,31))
                with patch('integrations.wechat.visual.send_observe.detect_layout',return_value=layout):
                    region,_=composer_region(im,168)
                draw=ImageDraw.Draw(im)
                draw.rectangle((565,799,640,823),fill='white')
                rows=[OCRRow('测试01',Rect(562,796,81,30),.99)]
                draw.rectangle((700,region.bottom-8,740,region.bottom-2),fill='white')
                if clipped:rows.append(OCRRow('extra',Rect(698,region.bottom-12,46,12),.99))
                result=inspect_composer(im,region,rows,168)
                self.assertFalse(result['input_complete'])
                self.assertFalse(result['input_empty'])


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.context=SendContext('binding','capture',2,dict(state_fixture.WINDOW))
        self.foreground=999;self.activity=(10,(20,30));self.guard=True
        self.u=Mock()
        self.u.GetForegroundWindow.side_effect=lambda:self.foreground
        self.u.GetAsyncKeyState.return_value=0
        self.u.GetSystemMetrics.side_effect=lambda code:{76:0,77:0,78:1920,79:1080}[code]
        self.u.SetForegroundWindow.side_effect=self.focus
        self.u.SendInput.side_effect=lambda count,array,size:count
        self.windows=Mock(u=self.u)
        self.windows.snapshot.side_effect=lambda hwnd:SimpleNamespace(pid=os.getpid(),minimized=False,data=lambda:dict(self.context.window))
        self.windows.pointer_over_window.return_value=False
        with patch.object(native,'Win32Windows',return_value=self.windows):self.backend=native.NativeWechatInput(999)
        self.backend._activity=lambda:self.activity
    def focus(self, hwnd):
        self.foreground=hwnd;return 1
    def begin(self):self.backend.begin(self.context,lambda:self.guard)
    def test_construct_has_no_input_and_native_abi_is_correct(self):
        self.u.SendInput.assert_not_called()
        self.assertEqual(ctypes.sizeof(native.INPUT),40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)
    def test_unicode_surrogates_and_shift_enter_never_bare_enter(self):
        events=native.unicode_inputs('你😀\n好')
        units=[e.ki.wScan for e in events if e.ki.dwFlags==4]
        self.assertEqual(units,[ord('你'),0xD83D,0xDE00,ord('好')])
        keys=[(e.ki.wVk,e.ki.dwFlags) for e in events if not e.ki.wScan]
        self.assertEqual(keys,[(0x10,0),(0x0D,0),(0x0D,2),(0x10,2)])
    def test_foreground_or_held_key_blocks_all_input(self):
        self.foreground=123
        with self.assertRaises(SendRejected):self.begin()
        self.foreground=999;self.u.GetAsyncKeyState.return_value=0x8000
        with self.assertRaises(SendRejected):self.begin()
        self.u.SendInput.assert_not_called()
    def test_native_sequence_checks_identity_then_click_unicode_submit_once(self):
        self.begin();self.backend.focus([350,570,550,140]);self.backend.write('你好');self.backend.submit()
        self.assertEqual(self.backend.submit_attempts,1);self.assertEqual(self.u.SendInput.call_count,3)
        self.backend.end();self.assertEqual(self.foreground,999)
        self.u.SetCursorPos.assert_called_once_with(20,30)
    def test_physical_activity_during_write_blocks_submit_and_restoration(self):
        self.begin();self.backend.focus([350,570,550,140]);self.backend.write('你好')
        self.activity=(11,(30,40))
        with self.assertRaises(SendRejected):self.backend.submit()
        self.assertEqual(self.backend.submit_attempts,0)
        self.backend.end();self.u.SetCursorPos.assert_not_called()
    def test_target_changed_before_input_is_rejected(self):
        self.begin();original=self.windows.snapshot.side_effect
        bad=dict(self.context.window,pid=999)
        self.windows.snapshot.side_effect=lambda hwnd:SimpleNamespace(minimized=False,data=lambda:bad)
        with self.assertRaises(SendRejected):self.backend.focus([350,570,550,140])
        self.u.SendInput.assert_not_called()
    def test_partial_input_never_retries(self):
        self.begin();self.u.SendInput.side_effect=lambda count,array,size:count-1
        with self.assertRaisesRegex(SendRejected,'native_input_partial'):self.backend.focus([350,570,550,140])
        self.assertEqual(self.u.SendInput.call_count,1)
    def test_window_closed_during_end_revokes_input_without_error(self):
        self.begin();self.backend.focus([350,570,550,140]);self.windows.snapshot.side_effect=OSError('closed')
        self.backend.end();self.assertIsNone(self.backend.guard);self.assertIsNone(self.backend.context)
    def test_user_moves_during_focus_restore_cursor_is_left_alone(self):
        self.begin();self.backend.focus([350,570,550,140])
        def restore(hwnd):self.foreground=hwnd;self.activity=(12,(80,90));return 1
        self.u.SetForegroundWindow.side_effect=restore
        self.backend.end();self.u.SetCursorPos.assert_not_called()


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        python=self.root/'python.exe';python.touch();resource=self.root/'ocr';resource.mkdir()
        self.now=100.;self.wall=1700000000.
        self.extra={}
        def factory(*args,continuous_model=False,**kwargs):
            self.extra['continuous_model']=continuous_model
            result=coordinator_fixture.FakeSession(*args,**kwargs)
            result.service=SimpleNamespace(job=None)
            return result
        config=dict(python=str(python),resource_root=str(resource),backend='win32_printwindow',
                    calibration=dict(header=[350,0,650,80],message=[350,80,650,480],input=[350,560,650,240]))
        self.c=VisualCoordinator(self.root,'run',config,PauseGate(True),object(),session_factory=factory,
                                lease_factory=coordinator_fixture.FakeLease,process_factory=coordinator_fixture.FakeProcess,
                                parent_start=1234,clock=lambda:self.now,wall_clock=lambda:self.wall,
                                desktop_probe=lambda:DesktopStatus(True,'desktop_available'))
        self.c.set_external_status({'app_state':'RUNNING'});self.c.start()
        self.backend=state_fixture.Backend();self.c.attach_sender(self.backend)
        self.c.last_heartbeat=self.now
    def tearDown(self):self.c.stop();self.temp.cleanup()
    def ready(self):
        self.c._packet(dict(stage='group_bound',binding_id='binding',capture_session_id=self.c.capture_session_id,
                            capture_epoch=2,window=dict(state_fixture.WINDOW)))
        self.c._packet(dict(stage='baseline_complete'));self.c._packet(dict(stage='listener_ready'))
    def preview(self,identity='request'):
        return dict(trace_id=identity,reply_id='reply-'+identity,reply='模型模拟回复',source_event_id='event-'+identity,
                    source_track_id='track-'+identity,source_binding_id='binding',conversation_id='group',
                    model_request_id=identity,generated_at=self.wall,source_transport='visual',source_self_state='incoming')
    def generation(self,identity='request'):
        self.c.session.service.job=SimpleNamespace(ticket=SimpleNamespace(request_id=identity));self.c._tag_generating_reply()
    def test_run_separate_from_budget_and_worker_has_no_input_payload(self):
        self.assertTrue(self.extra['continuous_model']);self.assertIn('--send-inspection',self.c.process.command)
        self.assertEqual(self.c.send_controller.session.state,'SEND_DISABLED')
        self.ready();self.c.enable_send(confirmed=True);self.c.send_test_message('人工测试');self.c.send_controller.tick()
        command=json.loads(self.c.process.stdin.getvalue().splitlines()[-1])
        self.assertEqual(command['action'],'inspect_send');self.assertNotIn('人工测试',json.dumps(command,ensure_ascii=False))
    def test_generation_while_off_cannot_be_sent_after_enable(self):
        self.ready();self.generation();self.c.enable_send(confirmed=True);self.c._offer_preview_to_send(self.preview())
        self.assertFalse(self.c.send_controller.queue)
    def test_disable_during_generation_drops_but_listener_stays_ready(self):
        self.ready();self.c.enable_send(confirmed=True);self.generation();self.c.disable_send()
        self.assertTrue(self.c.listener_ready);self.assertTrue(self.c.bound)
        self.c.enable_send(confirmed=True);self.c._offer_preview_to_send(self.preview())
        self.assertFalse(self.c.send_controller.queue)
    def test_normal_incoming_preview_queues_once_but_self_does_not(self):
        self.ready();self.c.enable_send(confirmed=True);self.generation();self.c._offer_preview_to_send(self.preview())
        self.c._offer_preview_to_send(self.preview());self.assertEqual(len(self.c.send_controller.queue),1)
        self.generation('self');self.c._offer_preview_to_send(dict(self.preview('self'),source_self_state='self'))
        self.assertEqual(len(self.c.send_controller.queue),1)
    def test_invalidation_revokes_queue_and_stop_state_survives_cleanup(self):
        self.ready();self.c.enable_send(confirmed=True);self.c.send_test_message('测试')
        self.c._invalidate('target_header_changed');self.assertFalse(self.c.send_controller.queue)
        self.assertFalse(self.c.send_controller.session.armed)
        self.c.stop();self.assertEqual(self.c.send_controller.session.state,'SEND_STOPPED')
        self.c._invalidate('late_cleanup');self.assertEqual(self.c.send_controller.session.state,'SEND_STOPPED')
    def test_worker_rejects_write_payload_and_invalid_observation_protocol(self):
        for command in [dict(action='send',text='forbidden'),dict(action='inspect_send',send_request=dict(text='forbidden')),
                        dict(action='end_send',send_request=dict(transaction_id='a',text='forbidden'))]:
            with self.assertRaises(ValueError):parse_command(json.dumps(command))


class ContinuousCoreTests(unittest.TestCase):
    def test_more_than_six_calls_use_existing_core_policy_without_test_ledger(self):
        with tempfile.TemporaryDirectory() as temp, patch('vision.respond_to_voice',return_value='模拟回复') as model, \
                patch('integrations.wechat.adapter.WechatAdapter.start',side_effect=AssertionError('wxauto forbidden')):
            root=Path(temp);gate=PauseGate();session=VisualSession(root,gate,RuntimeSettings(),model_enabled=True,continuous_model=True)
            try:
                session.bind('manual-test-binding','capture-test',0)
                # Advance service policy time, do not disable the original rate limit.
                began=time.monotonic();wall=time.time()
                for i in range(8):
                    event=replace(service_fixture.observation('@Ativia 测试'+str(i),i+1),observed_at=wall+i*70)
                    now=began+i*70
                    self.assertTrue(session.receive(event,now=now,wall=wall+i*70))
                    session.tick(now=now+2,wall=wall+i*70+2)
                    self.assertIsNotNone(session.service.job)
                    session.service.job.future.result(timeout=5)
                    session.tick(now=now+3,wall=wall+i*70+3)
                    preview=session.preview
                    self.assertIsNotNone(preview)
                    self.assertEqual(preview['source_event_id'],event.observation_id)
                    self.assertEqual(preview['source_self_state'],'incoming')
                    self.assertTrue(preview['model_request_id']);self.assertTrue(preview['reply_id'])
                self.assertEqual(model.call_count,8);self.assertIsNone(session.budget)
                self.assertFalse((root/'model-budget.sqlite3').exists())
                self.assertEqual(session.snapshot()['memory_written'],0)
                self.assertTrue(session.runtime.memory.path.is_relative_to(root/'group-memory'))
            finally:session.close()


if __name__=='__main__':unittest.main()
