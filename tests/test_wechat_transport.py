import json,time,unittest
from dataclasses import replace
from types import SimpleNamespace
from integrations.wechat.config import WechatConfig
from integrations.wechat.message_parser import normalize
from integrations.wechat.identity import IdentityResolver,group_id
from integrations.wechat.context_buffer import GroupContextBuffer
from integrations.wechat.response_policy import GroupResponsePolicy,ResponseDecision
from integrations.wechat.sender import send_checked
from runtime_settings import RuntimeSettings

CFG=WechatConfig(enabled=True,allowed_groups=('test-group',),owner_names=('owner',))
def event(text='hello',sender='A',identity='1',**kw):
    return normalize(dict(attr='friend',type='text',sender=sender,content=text,id=identity,**kw),'test-group',CFG,now=1000)

class TransportTests(unittest.TestCase):
    def test_config_whitelist_owner_and_types(self):
        self.assertEqual(WechatConfig.parse(CFG.document()),CFG)
        for doc in ({'enabled':'false'},{'enabled':True,'allowed_groups':['g']},{'proactive_reply':{'min_cooldown_seconds':1}},{'allowed_groups':'g'}):
            with self.assertRaises(ValueError):WechatConfig.parse(doc)
    def test_non_whitelist_self_media_and_malformed_are_ignored(self):
        row=dict(attr='friend',type='text',sender='A',content='@Ativia',id='1')
        self.assertIsNone(normalize(row,'other',CFG))
        for change in ({'attr':'self'},{'type':'image'},{'sender':None},{'content':123},{'id':None}):
            self.assertIsNone(normalize(dict(row,**change),'test-group',CFG))
    def test_mentions_quotes_and_identity(self):
        self.assertTrue(event('@Ativia 你人呢').is_at_me)
        self.assertFalse(event('@AtiviaOther hello').is_at_me)
        quoted=normalize(dict(attr='friend',type='quote',sender='A',content='什么意思',id='2',quote_nickname='Ativia',quote_content='hi'),'test-group',CFG)
        self.assertTrue(quoted.is_reply_to_me);self.assertEqual(quoted.reply_to_text,'hi')
        ids=IdentityResolver(CFG)
        self.assertNotEqual(ids.resolve('g','A').person_id,ids.resolve('g','B').person_id)
        self.assertNotEqual(ids.resolve('g','A').person_id,ids.resolve('h','A').person_id)
        self.assertEqual(ids.resolve('g','owner').person_id,'owner:local')
        self.assertNotEqual(ids.resolve('g','owner','not-the-owner').person_id,'owner:local')
    def test_buffer_deduplicates_and_expires(self):
        b=GroupContextBuffer(limit=20,seconds=100);a=event()
        self.assertTrue(b.append(a,1000));self.assertFalse(b.append(a,1001));self.assertEqual(b.version,1)
        self.assertEqual(b.snapshot(1101),())
    def test_direct_replies_bypass_proactive_cooldown(self):
        p=GroupResponsePolicy(CFG);b=GroupContextBuffer()
        self.assertFalse(p.decide(event('今天好困'),b,1000).respond)
        self.assertTrue(p.decide(event('@Ativia 你人呢'),b,1000).respond)
        self.assertTrue(p.decide(event('Ativia你人呢'),b,1000).strong)
        p.consecutive=2;p.last_proactive=1000
        self.assertTrue(p.decide(event('Ativia你觉得呢'),b,1001).respond)
    def test_weak_participation_cooldown_two_people_and_budget(self):
        cfg=replace(CFG,proactive_enabled=True);p=GroupResponsePolicy(cfg,lambda:0);b=GroupContextBuffer()
        for n in 'AB':b.append(event('今天老师拖堂',n,n),1000)
        self.assertFalse(p.decide(event('今天老师拖堂'),b,1001).respond)
        b.append(event('今天老师拖堂','C','C'),1000)
        self.assertTrue(p.decide(event('今天老师拖堂'),b,1001).respond)
        d=ResponseDecision(True,'weak',False);p.sent(d,1001)
        self.assertFalse(p.decide(event('今天老师拖堂'),b,1002).respond)
        p.sent(d,1122);self.assertFalse(p.decide(event('今天老师拖堂'),b,1250).respond)
        for _ in range(cfg.max_calls_per_minute):self.assertTrue(p.admit_call(1300))
        self.assertFalse(p.admit_call(1301))
    def test_sender_never_falls_back_and_never_retries_unknown(self):
        calls=[];chat=SimpleNamespace(ChatInfo=lambda:dict(chat_type='friend',chat_name='test-group'),SendMsg=lambda s:calls.append(s))
        self.assertEqual(send_checked(chat,'test-group','hello',lambda:True),'target_mismatch');self.assertFalse(calls)
        chat.ChatInfo=lambda:dict(chat_type='group',chat_name='test-group')
        self.assertEqual(send_checked(chat,'test-group','hello',lambda:False),'cancelled');self.assertFalse(calls)
        self.assertEqual(send_checked(chat,'test-group','hello',lambda:True,dry_run=False),'unknown');self.assertEqual(calls,['hello'])
    def test_group_prompt_excludes_desktop_context_and_keeps_speakers(self):
        from vision import build_text_request
        ctx=dict(channel='group',current_speaker_id='A',current_speaker_name='Alice',group_id='g',group_messages=[dict(speaker_id='B',text='B-only')])
        req=build_text_request('hi','DESKTOP-SECRET','SCREEN-SECRET',RuntimeSettings(),companion_context=ctx)
        body=json.dumps(req['messages'],ensure_ascii=False)
        self.assertNotIn('DESKTOP-SECRET',body);self.assertNotIn('SCREEN-SECRET',body)
        self.assertIn('B-only',body);self.assertIn('Alice',body);self.assertIn('Ativia',body)
