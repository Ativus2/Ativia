"""Synthetic regression contracts, explicitly NOT real-client acceptance evidence."""
import ast,json,sqlite3,time,unittest,tempfile,threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from integrations.wechat.config import WechatConfig
from integrations.wechat.sender import send_checked,send_reply_checked,split_reply
from integrations.wechat.message_parser import normalize
from integrations.wechat.message_contract import VisibleBaseline,serialize_message
from memory.memory_candidate import MemoryCandidateEvaluator
from memory.scope import RecallScope,visible
from memory.gate import MemoryGate
from tests_support_wechat import make_event
import test_wechat_service as service_tests


class SendBoundaryTests(unittest.TestCase):
    def test_default_config_and_boundary_block_sendmsg(self):
        self.assertTrue(WechatConfig().dry_run)
        self.assertTrue(WechatConfig.parse({'wechat':{}}).dry_run)
        with self.assertRaises(ValueError):WechatConfig.parse({'dry_run':'false'})
        chat=SimpleNamespace(ChatInfo=lambda:dict(chat_type='group',chat_name='g'),SendMsg=Mock())
        self.assertEqual(send_checked(chat,'g','hello',lambda:True),'dry_run')
        chat.SendMsg.assert_not_called()
        self.assertEqual(send_checked(chat,'g','hello',lambda:False),'cancelled')
        self.assertEqual(send_checked(chat,'other','hello',lambda:True),'target_mismatch')
    def test_lossless_chunks_paragraph_newline_sentence_and_hard_limit(self):
        for text in ('a'*2000,('hello。\n\n世界！ '*400),'\n'*4000,'🙂'*4000,'x\n'+'y'*3000,' leading '+('a'*1900)+'  '):
            with self.subTest(length=len(text)):
                chunks=split_reply(text);self.assertEqual(''.join(chunks),text)
                self.assertTrue(all(0<len(c)<=1800 for c in chunks))
        self.assertEqual(split_reply('abc\n\ndefgh',6),['abc\n\n','defgh'])
    def test_dry_run_multi_chunk_checks_gate_without_any_send(self):
        chat=SimpleNamespace(ChatInfo=lambda:dict(chat_type='group',chat_name='g'),SendMsg=Mock())
        result=send_reply_checked(chat,'g','x'*4000,lambda:True)
        self.assertEqual(result,dict(status='dry_run',chunks=3,completed=3));chat.SendMsg.assert_not_called()
    def test_real_send_stops_on_unknown_or_changed_context(self):
        # Mock-only opt-in to exercise the existing real boundary path.
        chat=SimpleNamespace(ChatInfo=lambda:dict(chat_type='group',chat_name='g'),SendMsg=Mock(return_value=None))
        result=send_reply_checked(chat,'g','x'*4000,lambda:True,dry_run=False)
        self.assertEqual(result['status'],'unknown');self.assertEqual(chat.SendMsg.call_count,1)


class AliasContractTests(unittest.TestCase):
    def test_alias_used_by_trigger_and_memory_assertion(self):
        config=WechatConfig(allowed_groups=('g',),bot_names=('Ativia','gpt'))
        evaluator=MemoryCandidateEvaluator(config.bot_names)
        for name in config.bot_names:
            event=normalize(dict(attr='friend',type='text',sender='test',content='@'+name+' 我不吃香菜',id=name),'g',config)
            self.assertTrue(event.is_at_me);self.assertIsNotNone(evaluator.evaluate(event))
        self.assertIsNone(MemoryCandidateEvaluator().evaluate(make_event('@gpt 我不吃香菜')))
        self.assertIsNone(evaluator.evaluate(make_event('@gptOther 我不吃香菜')))
    def test_synthetic_self_and_historical_contract_are_ignored(self):
        config=WechatConfig(allowed_groups=('g',))
        row=dict(attr='friend',type='text',sender='test',content='@Ativia hello',id='1')
        self.assertIsNone(normalize(dict(row,attr='self'),'g',config))
        self.assertIsNone(normalize(dict(row,historical=True),'g',config))
        self.assertIsNone(normalize(dict(row,id=''),'g',config))
    def test_baseline_and_duplicate_id_excluded_repeated_text_preserved(self):
        baseline=VisibleBaseline()
        self.assertFalse(baseline.accept({'id':'pre-ready'}))
        baseline.establish([SimpleNamespace(id=[1,2,3])])
        self.assertFalse(baseline.accept({'id':'[1,2,3]'}))
        self.assertTrue(baseline.accept({'id':'new1','content':'你好'}))
        self.assertFalse(baseline.accept({'id':'new1','content':'你好'}))
        self.assertTrue(baseline.accept({'id':'new2','content':'你好'}))
    def test_serializer_does_not_invent_stable_sender_or_source_timestamp(self):
        msg=SimpleNamespace(id=[1,2],attr='self',type='text',sender='test',content='hello')
        result=serialize_message(msg)
        self.assertEqual(result['sender_id'],'');self.assertEqual(result['id'],'[1,2]')
        self.assertEqual(result['timestamp_source'],'received')
        msg.timestamp=time.time();self.assertEqual(serialize_message(msg)['timestamp_source'],'source')


class ProductionDryRunTests(unittest.TestCase):
    def setUp(self):
        service_tests.ServiceTests.setUp(self)
        self.service.config=replace(self.service.config,dry_run=True)
        self.chat=SimpleNamespace(ChatInfo=lambda:dict(chat_type='group',chat_name='g'),SendMsg=Mock())
        chat=self.chat
        class BoundaryAdapter(service_tests.FakeAdapter):
            def send_message(self,group,text,request_id,sequence):
                super().send_message(group,text,request_id,sequence)
                result=send_reply_checked(chat,group,text,lambda:self.active,dry_run=True)
                self.packets.append(dict(stage='send_result',request_id=request_id,**result))
        self.adapter=BoundaryAdapter();self.service.adapter=self.adapter
    tearDown=service_tests.ServiceTests.tearDown
    receive=service_tests.ServiceTests.receive
    start=service_tests.ServiceTests.start
    finish=service_tests.ServiceTests.finish
    def test_real_core_model_memory_and_personality_execute_before_boundary(self):
        from conversation_core import AtiviaConversation
        from runtime_settings import RuntimeSettings
        self.runtime.memory.change('save',MemoryCandidateEvaluator().evaluate(make_event('我不吃香菜','A','unrelated'))).result(3)
        self.service.core=AtiviaConversation(self.runtime,RuntimeSettings())
        reply='完整回复。\n'*500
        with patch('vision.respond_to_voice',return_value=reply) as model, \
             patch.object(self.runtime.memory,'recall',wraps=self.runtime.memory.recall) as recall, \
             patch.object(self.runtime.planner,'plan',wraps=self.runtime.planner.plan) as planner:
            self.receive('@Ativia 我不吃香菜');self.start();self.finish()
            for packet in self.adapter.receive_event():self.service.sent_result(packet,self.wall+4)
            model.assert_called_once();recall.assert_called_once();planner.assert_called_once()
        self.chat.SendMsg.assert_not_called();self.assertEqual(self.service.counts['sent'],0)
        self.assertFalse(self.adapter.sends);self.assertFalse(self.service.pending_sends)
        preview=self.service.preview
        self.assertEqual(preview['group'],'g');self.assertTrue(preview['memory_candidate_queued'])
        self.assertEqual(''.join(preview['chunks']),reply);self.assertGreater(len(preview['chunks']),1)
        self.assertTrue(preview['personality_called']);self.assertEqual(self.service.counts['dry_runs'],1)
    def test_dry_run_respects_pause_gate(self):
        self.receive('@Ativia 我不吃香菜');self.gate.transition('PAUSING');self.start()
        self.assertFalse(self.core.calls);self.assertFalse(self.adapter.sends);self.chat.SendMsg.assert_not_called()
    def test_dry_run_cannot_accept_sent_ack(self):
        self.receive('@Ativia hello');self.start();self.finish()
        preview=self.service.preview
        self.assertFalse(self.adapter.sends)
        self.service.sent_result(dict(request_id=preview['trace_id'],status='sent'),self.wall+4)
        self.assertEqual(self.service.counts['sent'],0);self.assertIs(self.service.preview,preview)
        self.assertEqual(self.service.counts['dry_runs'],1)
    def test_dry_run_never_enters_transport_or_vendor_ui(self):
        self.adapter.send_message=Mock(side_effect=AssertionError('send path must stay unreachable'))
        with patch('integrations.wechat.sender.send_reply_checked',side_effect=AssertionError('vendor sender entered')):
            self.receive('@Ativia hello');self.start();self.finish()
        self.adapter.send_message.assert_not_called();self.chat.SendMsg.assert_not_called()
        self.assertEqual(self.service.preview['send_blocked'],'local_preview_sink')
    def test_pause_discards_pending_preview_and_requires_fresh_epoch(self):
        self.receive('@Ativia hello');self.start();self.finish()
        self.assertIsNotNone(self.service.preview)
        self.gate.transition('PAUSING');self.assertIsNone(self.service.preview)
        self.gate.transition('RESUMING');self.service.reset_epoch();self.gate.transition('RUNNING')
        self.assertFalse(self.service.groups);self.assertFalse(self.service.pending_sends)
        self.assertIsNone(self.service.preview)
    def test_pause_during_preview_construction_prevents_late_publication(self):
        self.receive('@Ativia hello');self.start();self.service.job.future.result(4)
        entered=threading.Event();release=threading.Event();errors=[]
        def split(text):
            entered.set()
            if not release.wait(3):raise TimeoutError('synthetic preview barrier')
            return [text]
        def finish():
            try:self.service.finish_reply(self.now+4,self.wall+4)
            except BaseException as exc:errors.append(exc)
        with patch('integrations.wechat.sender.split_reply',side_effect=split):
            worker=threading.Thread(target=finish);worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.gate.transition('PAUSING')
                self.assertIsNone(self.service.preview)
            finally:
                release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertFalse(errors)
        self.assertIsNone(self.service.preview);self.assertEqual(self.service.counts['dry_runs'],0)
        self.assertFalse(self.adapter.sends)
    def test_recovery_ambiguous_member_has_no_persistent_person_memory_or_recall(self):
        self.service.require_stable_memory=True
        with patch.object(self.runtime.memory,'person_relationship',side_effect=AssertionError('nickname memory lookup')):
            self.receive('@Ativia 我不吃香菜');self.start();self.finish()
        self.assertFalse(self.service.candidates.pending);self.assertFalse(self.service.people)
        self.assertFalse(self.core.calls[-1][2]['memory_enabled'])
        self.assertIsNotNone(self.service.preview)
    def test_recovery_stable_member_keeps_scoped_memory_path(self):
        self.service.require_stable_memory=True
        self.receive('@Ativia 我不吃香菜',sender_id='synthetic-stable-id');self.start();self.finish()
        self.assertTrue(self.service.candidates.pending);self.assertTrue(self.service.people)
        self.assertTrue(self.core.calls[-1][2]['memory_enabled'])
    def test_recovery_ambiguous_current_member_cannot_recall_after_context_expires(self):
        self.service.require_stable_memory=True
        self.receive('@Ativia 我不吃香菜',timestamp=self.wall-self.service.config.context_seconds-1,
            timestamp_source='source')
        self.start();self.finish()
        self.assertFalse(self.core.calls[-1][1])
        self.assertFalse(self.core.calls[-1][2]['memory_enabled'])
        self.assertFalse(self.service.candidates.pending)
    def test_trace_metadata_cannot_replace_preview_identity_or_content(self):
        self.receive('@Ativia hello');self.start()
        ticket=self.service.job.ticket
        ticket.wechat_trace=dict(trace_id='foreign-request',group='other-group',input='OTHER_MESSAGE_CANARY',
            personality_called=True,memory_recalled=0,model='synthetic-model')
        self.finish()
        self.assertEqual(self.service.preview['trace_id'],ticket.request_id)
        self.assertEqual(self.service.preview['group'],'g')
        self.assertEqual(self.service.preview['input'],'@Ativia hello')
        self.assertEqual(self.service.preview['model'],'synthetic-model')
    def test_scoped_sql_before_limit_with_1001_owner_private_rows(self):
        store=self.runtime.memory.store
        target=MemoryCandidateEvaluator().evaluate(make_event('我不吃香菜','A','g'))
        store.save(target)
        public=replace(target,subject_id='B',group_id='other',visibility='public',content='我喜欢香菜',fact_key='public-test')
        store.save(public)
        store.save(replace(target,group_id='other',content='OTHER_GROUP_CANARY 香菜'))
        with closing(store.connection()) as db,db:
            db.executemany('INSERT INTO memories(id,content,created_at,updated_at) VALUES (?,?,?,?)',
                [(str(i),'PRIVATE_CANARY 香菜',time.time(),time.time()) for i in range(1001)])
        self.runtime.memory.refresh()
        self.assertFalse(any(r.get('group_id')=='g' for r in self.runtime.memory.snapshot))
        scope=RecallScope.group('g',('A','B'))
        rows=store.rows(scope=scope)
        self.assertEqual(len(rows),2);self.assertTrue(all(visible(r,scope) for r in rows))
        result=self.runtime.memory.recall('香菜','NORMAL',scope=scope)
        self.assertIn('我不吃香菜',result.text);self.assertIn('我喜欢香菜',result.text)
        self.assertNotIn('PRIVATE_CANARY',result.text);self.assertNotIn('OTHER_GROUP_CANARY',result.text)


class RecoveryRunnerSafetyTests(unittest.TestCase):
    def test_acceptance_rejects_preflight_manifest_before_runtime_or_transport(self):
        from tools.wechat_dry_run import acceptance_config
        manifest=dict(schema_version=1,dry_run=True,allowed_groups=['Ativia测试群'],
            send_enabled=False,voice_enabled=False,model_enabled=False,transport='unverified')
        with self.assertRaisesRegex(ValueError,'preflight_only'):acceptance_config(manifest)
        with self.assertRaisesRegex(ValueError,'preflight_only'):
            acceptance_config({'wechat':dict(allowed_groups=['Ativia测试群'],owner_names=['synthetic-owner'],model_enabled=False)})
    def test_acceptance_revalidates_forced_enabled_configuration(self):
        from tools.wechat_dry_run import acceptance_config
        raw={'wechat':dict(enabled=False,allowed_groups=['Ativia测试群'])}
        with self.assertRaisesRegex(ValueError,'whitelist_and_owner_required'):acceptance_config(raw)
        raw['wechat'].update(owner_names=['synthetic-owner'],dry_run=False,debug=True)
        config=acceptance_config(raw)
        self.assertTrue(config.enabled);self.assertTrue(config.dry_run)
        self.assertFalse(config.debug);self.assertFalse(config.proactive_enabled)
        with self.assertRaisesRegex(ValueError,'wechat_config_document'):acceptance_config(raw['wechat'])
    def test_evidence_redacts_custom_test_text_quote_and_sender(self):
        from tools.wechat_dry_run import redact_sample,fingerprint
        text='CUSTOM_MESSAGE_CANARY';quote='QUOTED_MESSAGE_CANARY';sender='PRIVATE_SENDER_CANARY'
        result=redact_sample('Ativia测试群',dict(attr='friend',type='quote',id='synthetic-message',
            content=text,quote_content=quote,quote_nickname=sender,sender=sender,sender_id='synthetic-member'))
        serialized=json.dumps(result,ensure_ascii=False)
        for value in (text,quote,sender,'synthetic-member','synthetic-message'):self.assertNotIn(value,serialized)
        self.assertEqual(result['content_hash'],fingerprint(text));self.assertEqual(result['content_length'],len(text))
        self.assertEqual(result['quoted_message']['content_hash'],fingerprint(quote))
        self.assertTrue(result['quoted_message']['present'])
    def test_adapter_rejects_send_and_raw_send_before_process_or_ui(self):
        from integrations.wechat.adapter import WechatAdapter
        adapter=WechatAdapter(WechatConfig(dry_run=True))
        with patch.object(adapter,'_write') as write:
            with self.assertRaisesRegex(RuntimeError,'DRY_RUN_SEND_IPC_BLOCKED'):
                adapter.send_message('g','hello','synthetic',1)
            write.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'DRY_RUN_SEND_IPC_BLOCKED'):
            adapter._write(dict(action='send',group='g',text='hello'))
        self.assertIsNone(adapter.process)
    def test_shadow_runtime_is_fresh_and_does_not_touch_formal_marker(self):
        from tools.wechat_dry_run import create_shadow_runtime
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);formal=root/'formal';formal.mkdir()
            marker=formal/'memory.sqlite3';marker.write_bytes(b'PRIVATE_CANARY_DO_NOT_OPEN')
            runtimes=[]
            try:
                for _ in range(2):
                    runtime=create_shadow_runtime(root/'shadow');runtimes.append(runtime)
                    self.assertTrue(runtime.memory.ready.wait(3));self.assertIsNone(runtime.memory.error)
                    self.assertTrue(runtime.memory.path.is_relative_to(root/'shadow'))
                    self.assertFalse(runtime.memory.snapshot)
                self.assertNotEqual(runtimes[0].memory.path,runtimes[1].memory.path)
            finally:
                for runtime in runtimes:
                    runtime.close();runtime.memory.close_future.result(4)
            self.assertEqual(marker.read_bytes(),b'PRIVATE_CANARY_DO_NOT_OPEN')
    def test_budget_allows_at_most_six_model_admissions_including_errors(self):
        from tools.wechat_dry_run import BudgetedConversation
        core=SimpleNamespace(handle=Mock(side_effect=TimeoutError('synthetic timeout')))
        budget=BudgetedConversation(core)
        kwargs=dict(cancelled=lambda:False,ticket=SimpleNamespace(valid=lambda:True))
        for _ in range(6):
            with self.assertRaises(TimeoutError):budget.handle(None,(),**kwargs)
        with self.assertRaisesRegex(RuntimeError,'budget_exhausted'):budget.handle(None,(),**kwargs)
        self.assertEqual(core.handle.call_count,6);self.assertEqual(budget.calls,6)
    def test_authentication_failure_is_not_retried_on_next_event(self):
        from tools.wechat_dry_run import BudgetedConversation
        auth=RuntimeError('synthetic auth');auth.status_code=401
        wrapped=RuntimeError('model failed');wrapped.__cause__=auth
        core=SimpleNamespace(handle=Mock(side_effect=wrapped));budget=BudgetedConversation(core)
        kwargs=dict(cancelled=lambda:False,ticket=SimpleNamespace(valid=lambda:True))
        with self.assertRaises(RuntimeError):budget.handle(None,(),**kwargs)
        with self.assertRaisesRegex(RuntimeError,'model_authentication_failed'):budget.handle(None,(),**kwargs)
        self.assertEqual(core.handle.call_count,1)
    def test_cancelled_work_does_not_consume_budget(self):
        from tools.wechat_dry_run import BudgetedConversation
        core=SimpleNamespace(handle=Mock());budget=BudgetedConversation(core)
        self.assertEqual(budget.handle(None,(),cancelled=lambda:True,ticket=SimpleNamespace(valid=lambda:True)),'')
        self.assertEqual(budget.calls,0);core.handle.assert_not_called()
    def test_existing_model_path_is_one_request_with_sdk_retries_disabled(self):
        # Budget correctness depends on this production path, not a mocked retry claim.
        source=Path(__file__).resolve().parents[1]/'vision.py'
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='respond_to_voice')
        calls=[n for n in ast.walk(function) if isinstance(n,ast.Call)]
        clients=[n for n in calls if isinstance(n.func,ast.Name) and n.func.id=='OpenAI']
        self.assertEqual(len(clients),1)
        self.assertTrue(any(k.arg=='max_retries' and isinstance(k.value,ast.Constant) and k.value.value==0 for k in clients[0].keywords))
        requests=[n for n in calls if isinstance(n.func,ast.Attribute) and n.func.attr=='create']
        self.assertEqual(len(requests),1)
