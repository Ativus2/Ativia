"""Configuration boundary and safe error reporting regressions (offline)."""
import asyncio
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from translation.asr import make_provider
from translation.asr.base import ASRError
from translation.asr.openai_asr import OpenAIASRProvider
from translation.asr.openai_errors import error_fields, transport_error
from live_translation import TranslationSettings
from translation.diagnostics import DiagnosticBuffer, DiagnosticsDialog
from PySide6.QtWidgets import QApplication


def transcription(provider):
    return provider.session_update()['session']['audio']['input']['transcription']


class SessionConfigTests(unittest.TestCase):
    def test_auto_markers_are_omitted_even_when_set_after_construction(self):
        for language in ('auto', 'und', '', '  ', None, ' AUTO '):
            p=OpenAIASRProvider();p.set_language(language)
            payload=transcription(p)
            self.assertNotIn('languages',payload)
            self.assertNotIn('language',payload)
            self.assertEqual(p.language,'auto')

    def test_fixed_languages_are_explicit_arrays(self):
        for code in ('ko','en','ja','zh'):
            self.assertEqual(transcription(OpenAIASRProvider(language=code))['languages'],[code])

    def test_blank_keywords_filtered_and_disabled_factory_omits_field(self):
        p=OpenAIASRProvider(hotwords=('', ' ', '\n', ' Elden Ring ', 'Elden Ring', 'Malenia'))
        self.assertEqual(transcription(p)['keywords'],['Elden Ring','Malenia'])
        settings=TranslationSettings(backend='openai',hotwords_enabled='no',hotwords='Malenia;Miquella')
        self.assertNotIn('keywords',transcription(make_provider(settings)))

    def test_forbidden_or_structured_keyword_rejected_before_connect_without_echo(self):
        for word in ('private<value>','private\nvalue','private\rvalue',{'private':'value'},123):
            p=OpenAIASRProvider(hotwords=(word,));events=[];p.on_diagnostic=events.append
            with self.assertRaises(ASRError) as cm:transcription(p)
            self.assertNotIn('private',str(cm.exception))
            self.assertEqual(events[-1]['param'],'session.audio.input.transcription.keywords')
            self.assertEqual(events[-1]['transport'],'local_validation')

    def test_prompt_bounded_string_and_empty_omitted(self):
        p=OpenAIASRProvider(prompt='가'*2000)
        self.assertEqual(len(transcription(p)['prompt']),400)
        # Repeated session creation never appends prior transcription/history.
        self.assertEqual(transcription(p),transcription(p))
        for prompt in ('', ' ', None):
            self.assertNotIn('prompt',transcription(OpenAIASRProvider(prompt=prompt)))
        for prompt in ({'private':'history'},['history']):
            with self.assertRaises(ASRError) as cm:transcription(OpenAIASRProvider(prompt=prompt))
            self.assertNotIn('history',str(cm.exception))

    def test_config_diagnostic_logs_counts_not_context(self):
        p=OpenAIASRProvider(prompt='private context',hotwords=('private term',));events=[]
        p.on_diagnostic=events.append;p.session_update();p._report_config(False)
        row=events[-1]
        self.assertEqual(row['keywords_count'],1)
        self.assertEqual(row['prompt_chars'],15)
        self.assertNotIn('private',json.dumps(row))
        self.assertIsNone(row['turn_detection']);self.assertEqual(row['sample_rate'],24000)


class SafeErrorTests(unittest.TestCase):
    def test_preserves_actionable_fields_but_not_extra_server_fields(self):
        secret='opaque-secret-1234567';p=OpenAIASRProvider();p._diagnostic_secrets=(secret,)
        events=[];p.on_diagnostic=events.append;p._session_phase='session_update'
        data=dict(type='error',event_id='evt_server',error=dict(type='invalid_request_error',
            code='invalid_value',message='Unsupported setting '+secret,
            param='session.audio.input.transcription.languages',event_id='cfg_client',
            audio='PRIVATE_AUDIO',headers={'Authorization':'Bearer '+secret}))
        with self.assertRaises(ASRError) as cm:p.receive(data)
        row=events[-1];combined=str(cm.exception)+json.dumps(row)
        self.assertEqual(row['type'],'invalid_request_error')
        self.assertEqual(row['code'],'invalid_value')
        self.assertEqual(row['event_id'],'cfg_client')
        self.assertEqual(row['server_event_id'],'evt_server')
        self.assertIn('会话配置',str(cm.exception))
        for forbidden in (secret,'PRIVATE_AUDIO','Authorization'):self.assertNotIn(forbidden,combined)

    def test_error_message_redacts_embedded_credentials_and_audio(self):
        message=('sk-private-secret\nAuthorization: Bearer private-header\n'
                 'api_key="opaque-token"\naudio="AAEC"\npcm=shortpcm\n'+'A'*256)
        fields=error_fields(dict(error={'message':message,'param':{'audio':'private'}}))
        text=json.dumps(fields)
        for secret in ('sk-private','private-header','opaque-token','AAEC','shortpcm','A'*128):
            self.assertNotIn(secret,text)
        self.assertEqual(fields['param'],'[non-scalar omitted]')

    def test_no_unbounded_or_unstructured_error_logging(self):
        self.assertIsNone(error_fields({'error':'private raw body'})['message'])
        self.assertEqual(error_fields({'error':{'message':'x'*70000}})['message'],'[oversized field omitted]')
        self.assertLessEqual(len(error_fields({'error':{'message':'normal sentence '*200}})['message']),1200)

    def test_runtime_error_is_not_mislabelled_as_model_or_language_failure(self):
        p=OpenAIASRProvider();events=[];p.on_diagnostic=events.append
        p.receive(dict(type='session.updated',session=dict(audio=dict(input=dict(transcription=dict(model=p.model))))))
        with self.assertRaises(ASRError) as cm:
            p.receive(dict(type='error',error=dict(code='input_audio_buffer_commit_empty',
                message='Expected at least 100ms of audio.',param='audio')))
        self.assertEqual(events[-1]['phase'],'streaming')
        self.assertIn('连接成功后',str(cm.exception))
        self.assertNotIn('模型权限',str(cm.exception))

    def test_failed_transcription_uses_same_sanitizer(self):
        p=OpenAIASRProvider();rows=[];p.on_diagnostic=rows.append
        with self.assertRaises(ASRError) as cm:
            p.receive(dict(type='conversation.item.input_audio_transcription.failed',
                error=dict(type='server_error',code='transcription_failed',message='sk-private-secret')))
        self.assertEqual(rows[-1]['code'],'transcription_failed')
        self.assertNotIn('private',str(cm.exception))

    def test_transport_plain_body_and_exception_repr_never_logged(self):
        exc=RuntimeError('Authorization: Bearer PRIVATE')
        exc.response=SimpleNamespace(status_code=400,body=b'PRIVATE plaintext response')
        row=transport_error(exc)
        self.assertEqual(row['http_status'],400)
        self.assertIsNone(row['message']);self.assertNotIn('PRIVATE',json.dumps(row))


class SafeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_json_error_reaches_diagnostics_without_credentials(self):
        secret='opaque-secret-987';exc=RuntimeError('request headers '+secret)
        exc.response=SimpleNamespace(status_code=400,body=json.dumps(dict(error=dict(
            type='invalid_request_error',code='invalid_value',message='Invalid value '+secret,
            param='session.audio.input.transcription.prompt'))).encode())
        connector=Mock(side_effect=exc);p=OpenAIASRProvider(connector=connector,key_loader=lambda:secret)
        rows=[];p.on_diagnostic=rows.append
        with self.assertRaises(ASRError) as cm:await p.run()
        connector.assert_called_once()
        self.assertEqual(rows[-1]['transport'],'http_handshake')
        self.assertEqual(rows[-1]['code'],'invalid_value')
        self.assertNotIn(secret,str(cm.exception)+json.dumps(rows))


class DiagnosticsErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_error_survives_reset_in_history_and_exports_same_safe_fields(self):
        buffer=DiagnosticBuffer();p=OpenAIASRProvider();p.on_diagnostic=buffer.add
        with self.assertRaises(ASRError):
            p.receive(dict(type='error',event_id='evt_server',error=dict(
                type='invalid_request_error',code='invalid_value',message='Check configuration',
                param='session.audio.input.transcription.languages',event_id='cfg_client')))
        buffer.add(dict(stage='reset',epoch=1))
        dialog=DiagnosticsDialog(buffer)
        try:
            text=dialog.details.toPlainText()
            self.assertIn('最近一次 ASR 错误（历史记录）\ntransport:',text)
            for value in ('evt_server','cfg_client','invalid_value','Check configuration'):
                self.assertIn(value,text)
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'diagnostics.jsonl';buffer.export(path)
                row=json.loads(path.read_text(encoding='utf-8').splitlines()[0])
                self.assertEqual(row['param'],'session.audio.input.transcription.languages')
        finally:
            dialog.timer.stop();dialog.close()

if __name__=='__main__':unittest.main()
