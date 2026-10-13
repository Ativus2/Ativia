"""Official OpenAI streaming transcription adapter; no second credential store."""
import asyncio
import base64
from collections import OrderedDict, deque
import json
import logging
import os
from pathlib import Path
from time import monotonic
from .base import ASRProvider, ASRError
from .models import TranscriptEvent
from .openai_errors import error_fields, transport_error
from .commit_controller import CommitController, EndpointConfig


def load_api_key():
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key:
        from dotenv import dotenv_values
        key = (dotenv_values(Path(__file__).resolve().parents[2] / '.env').get('OPENAI_API_KEY') or '').strip()
    return key


class OpenAIASRProvider(ASRProvider):
    name = 'openai'
    model = 'gpt-live-transcribe'
    accepts_audio = True
    endpoint = 'wss://api.openai.com/v1/realtime?intent=transcription'
    wire_sample_rate = 24000
    latency_basis = 'commit_sent_to_completed'
    # The live service rejects server_vad for this model. Commit at audio pauses
    # inside this adapter; AudioCapture and the final merge window are unchanged.
    endpointing = 'client_commit_controller'
    rms_threshold = 200

    def __init__(self, language='ko', hotwords=(), endpoint_ms=650, sample_rate=16000,
                 prompt='', connector=None, key_loader=load_api_key, retry_delays=(.5, 1, 2), endpoint_config=None):
        super().__init__(language, (hotwords,) if isinstance(hotwords, str) else (hotwords or ()))
        self.endpoint_ms, self.sample_rate, self.prompt = endpoint_ms, sample_rate, prompt
        self.connector, self.key_loader, self.retry_delays = connector, key_loader, retry_delays
        self.endpoint_config=endpoint_config or EndpointConfig(silence_ms=endpoint_ms)
        self.on_diagnostic=lambda event:None
        self._diagnostic_secrets = ()
        self._session_phase = 'configuration'
        self._config_summary = {}
        self._session_event_id = None
        self.ws = None
        self.send_lock = asyncio.Lock()
        self._clear_stream()

    def _clear_stream(self):
        self.turns=OrderedDict();self.finished_ids=deque(maxlen=256)
        self.commits=deque();self.commit_acks=0;self.total_commits=0
        self.buffer_samples=0;self.rate_state=None;self.wire_tail=b''
        self.send_queue=asyncio.Queue(maxsize=100)
        self.controller=CommitController(self.endpoint_config,self.wire_sample_rate)
        self.held=deque();self.sent_cursor=0
        self.records=deque(maxlen=256);self.local_serial=0
        self.current_record=self._new_record()
        self.after_commit_record=None
        self.ingested_samples=0;self.sent_samples=0;self.queue_drops=0

    def _new_record(self,previous_commit=None):
        self.local_serial+=1
        record=dict(local_segment_id=f'{self.epoch}:{self.local_serial}',segment_number=self.local_serial,segment_id=None,
            provider=self.name,model=self.model,epoch=self.epoch,
            commit_reason=None,segment_audio_ms=None,silence_before_commit_ms=None,
            previous_commit_time=previous_commit,first_audio_after_commit_time=None,
            first_speech_after_commit_time=None,first_partial_time=None,commit_time=None,
            raw_final_time=None,stable_final_time=None,formal_translation_time=None,
            provisional_translation_time=None,capture_gap_after_commit_ms=None,
            first_sent_audio_after_commit_time=None,speech_end_estimated=True)
        self.records.append(record)
        return record

    def _telemetry(self,record):
        self.on_diagnostic(dict(record,stage='segment_metrics',timestamp=monotonic(),
            pcm_ingested_samples=self.ingested_samples,pcm_sent_samples=self.sent_samples,
            audio_queue_drops=self.queue_drops))

    def _bind_record(self,item_id):
        for record in self.records:
            if record['segment_id']==item_id:return record
        record=next((r for r in self.records if r['segment_id'] is None),None)
        if record is None:record=self._new_record()
        record['segment_id']=item_id
        return record

    def _report_error(self, fields, transport='websocket_event'):
        event = dict(fields, stage='asr_error', provider=self.name, model=self.model,
                     epoch=self.epoch, timestamp=monotonic(), phase=self._session_phase,
                     session_event_id=self._session_event_id)
        event.setdefault('transport', transport)
        self.on_diagnostic(event)
        return event

    def _config_error(self, message, param):
        self._report_error(dict(type='client_validation_error', code='invalid_session_config',
                                message=message, param=param, event_id=None,
                                server_event_id=None), 'local_validation')
        raise ASRError(message+' ['+param+']')

    def _server_error(self, data, segment_failed=False):
        event = self._report_error(error_fields(data, self._diagnostic_secrets))
        if event['code'] in ('invalid_api_key', 'authentication_error'):
            headline = 'OpenAI ASR 认证失败'
        elif event['code'] in ('insufficient_quota', 'rate_limit_exceeded'):
            headline = 'OpenAI ASR 额度不足或限流'
        elif segment_failed:
            headline = 'OpenAI ASR 本段转录失败'
        elif self._session_phase == 'streaming':
            headline = 'OpenAI ASR 连接成功后的请求失败'
        else:
            headline = 'OpenAI ASR 会话配置请求失败'
        details = ' · '.join(str(event[k]) for k in ('code', 'param', 'message') if event.get(k))
        raise ASRError(headline+'：'+(details or '服务端未提供原因，请查看 Diagnostics。'))

    def session_update(self):
        language = self.language
        if language is None:
            language = 'auto'
        if not isinstance(language, str):
            self._config_error('OpenAI 识别语言必须是语言代码。', 'session.audio.input.transcription.languages')
        language = language.strip().lower()
        if language in ('', 'und', 'auto'):
            language = 'auto'
        if language not in ('auto', 'ko', 'en', 'ja', 'zh'):
            self._config_error('OpenAI 识别语言无效。', 'session.audio.input.transcription.languages')
        self.language = language
        keywords = []
        for word in self.hotwords:
            if not isinstance(word, str):
                self._config_error('OpenAI 专有名词必须是单行字符串。', 'session.audio.input.transcription.keywords')
            if not word.strip():
                continue
            if any(c in word for c in '\r\n<>'):
                self._config_error('OpenAI 专有名词不能包含换行或尖括号。', 'session.audio.input.transcription.keywords')
            word = word.strip()
            if word not in keywords:
                keywords.append(word)
        prompt = self.prompt
        if prompt is None:
            prompt = ''
        if not isinstance(prompt, str):
            self._config_error('OpenAI 上下文提示必须是字符串。', 'session.audio.input.transcription.prompt')
        # Keep the existing bounded, per-session glossary; no transcript history.
        prompt = prompt.strip()[:400]
        transcription = dict(model=self.model, delay='low')
        if language != 'auto':
            transcription['languages'] = [language]
        if keywords:
            transcription['keywords'] = keywords[:100]
        if prompt:
            transcription['prompt'] = prompt
        self._config_summary = dict(languages=transcription.get('languages'),
            transcription_fields=list(transcription), prompt_chars=len(prompt),
            keywords_count=len(transcription.get('keywords', [])), sample_rate=self.wire_sample_rate,
            turn_detection=None, delay='low')
        return dict(type='session.update', session=dict(type='transcription',
            audio=dict(input=dict(format=dict(type='audio/pcm', rate=self.wire_sample_rate),
                transcription=transcription, turn_detection=None))))

    def _report_config(self, accepted):
        self.on_diagnostic(dict(self._config_summary, stage='asr_session_config',
            provider=self.name, model=self.model, epoch=self.epoch, timestamp=monotonic(),
            accepted=accepted, event_id=self._session_event_id))

    def _turn(self, item_id):
        if not item_id or item_id in self.finished_ids: return None
        if item_id not in self.turns:
            if len(self.turns) >= 64: raise ASRError('OpenAI 转录定稿积压，已停止识别，请重新开始。')
            self.turns[item_id] = dict(text='', final=None, revision=0, emitted=0,
                                      committed=False, commit_at=None, final_at=None, metadata=self._bind_record(item_id))
        return self.turns[item_id]

    def receive(self, data, now=None):
        """Normalize incremental deltas and order completed items by audio occurrence."""
        now = monotonic() if now is None else now
        typ = data.get('type')
        if typ == 'error':
            self._server_error(data)
        if typ == 'session.updated':
            model = data.get('session', {}).get('audio', {}).get('input', {}).get('transcription', {}).get('model')
            if model != self.model: raise ASRError('OpenAI 未确认 gpt-live-transcribe 配置，已停止识别。')
            self._session_phase = 'streaming'
            self._report_config(True)
            self.ready.set()
            self.set_status('OpenAI · Model: '+self.model+' · 已连接 · '+self.language)
            return
        if typ == 'conversation.item.input_audio_transcription.failed':
            self._server_error(data, segment_failed=True)
        if typ not in ('input_audio_buffer.committed', 'conversation.item.input_audio_transcription.delta',
                       'conversation.item.input_audio_transcription.completed'): return
        item_id = data.get('item_id')
        turn = self._turn(item_id)
        if turn is None: return
        if typ == 'input_audio_buffer.committed':
            self.commit_acks += 1
            turn['committed'] = True
            record=self.commits.popleft() if self.commits else turn['metadata']
            if isinstance(record,(float,int)):record=dict(turn['metadata'],commit_time=record)
            record['segment_id']=item_id
            turn['metadata']=record
            turn['commit_at']=record.get('commit_time') or now
            # An empty first turn can have no deltas. The ordered commit chain
            # takes precedence over first delta arrival for cross-turn ordering.
            previous = data.get('previous_item_id')
            items = list(self.turns.items())
            item = next(pair for pair in items if pair[0] == item_id)
            items.remove(item)
            position = next((i+1 for i, pair in enumerate(items) if pair[0] == previous), None)
            if position is None: position = 0 if previous is None or previous in self.finished_ids else len(items)
            items.insert(position, item)
            self.turns = OrderedDict(items)
        elif typ.endswith('.delta'):
            if turn['final'] is not None: return
            delta = data.get('delta') or ''
            if not isinstance(delta, str): return
            turn['text'] += delta
            if len(turn['text']) > 16000: raise ASRError('OpenAI 单段转录过长，已停止以避免字幕积压。')
            turn['revision'] += 1
            record=turn['metadata']
            if record.get('first_partial_time') is None:
                record['first_partial_time']=now;self._telemetry(record)
            if record is self.current_record:self.controller.has_partial=True
            # Every item's partial is immediate, even while A is still finalizing.
            self.emit(TranscriptEvent(turn['text'].strip(),False,None,
                self.language if self.language!='auto' else 'und',now,self.name,item_id,
                turn['revision'],self.epoch,cumulative=True,metadata=dict(record)))
            turn['emitted']=turn['revision']
        else:
            if turn['final'] is not None: return
            turn['final'] = (data.get('transcript') or '').strip()
            turn['final_at'] = now
            turn['metadata']['raw_final_time']=now
            self._telemetry(turn['metadata'])
            turn['revision'] += 1
        self._drain(now)

    def _drain(self, now):
        # Only finals are ordered. Partial display is never held behind another item.
        while self.turns:
            item_id,turn=next(iter(self.turns.items()))
            if turn['final'] is None or not turn['committed']:break
            text=turn['final'];record=turn['metadata']
            latency=round(max(0,turn['final_at']-turn['commit_at'])*1000,1)
            record['asr_latency_ms']=latency
            if text:
                self.emit(TranscriptEvent(text,True,None,
                    self.language if self.language!='auto' else 'und',turn['final_at'],
                    self.name,item_id,turn['revision'],self.epoch,True,
                    asr_latency_ms=latency,cumulative=True,metadata=dict(record)))
            elif turn['text'].strip():
                raise ASRError('OpenAI 返回空定稿，未翻译未完成的原文；请重新开始。')
            self.turns.popitem(last=False);self.finished_ids.append(item_id)

    async def _send(self, data):
        if not self.ws: return False
        await asyncio.wait_for(self.ws.send(json.dumps(data)), 3)
        return True

    async def _commit(self,reason='manual_stop',boundary=None,quiet_ms=0):
        if self.buffer_samples<self.wire_sample_rate//10:return False
        boundary=self.sent_cursor if boundary is None else boundary
        record=self.current_record
        record.update(self.controller.metadata(boundary))
        now=monotonic()
        record.update(commit_reason=reason,commit_time=now,silence_before_commit_ms=round(quiet_ms,1))
        self.commits.append(record);self.total_commits+=1
        self.controller.committed(boundary)
        self.current_record=self._new_record(previous_commit=now)
        self.after_commit_record=self.current_record
        self._telemetry(record)
        # This waits only for the tiny commit write, never for completed/translation.
        if await self._send(dict(type='input_audio_buffer.commit')):
            self.buffer_samples=0
            return True
        return False

    async def push_audio(self, pcm, captured_at=None):
        if not self.ready.is_set():return False
        now=monotonic();captured_at=now if captured_at is None else captured_at
        self.ingested_samples+=len(pcm)//2
        record=self.after_commit_record
        if record and record['first_audio_after_commit_time'] is None:
            record['first_audio_after_commit_time']=now
            record['capture_gap_after_commit_ms']=round((now-record['previous_commit_time'])*1000,2)
            self._telemetry(record)
        try:self.send_queue.put_nowait((pcm,captured_at))
        except asyncio.QueueFull:
            self.queue_drops+=1
            self.on_diagnostic(dict(stage='audio_discontinuity',provider=self.name,epoch=self.epoch,
                reason='network_send_queue_overflow',audio_queue_drops=self.queue_drops,timestamp=now))
            if self.ws:self.ws.transport.abort()
            return False
        return True

    async def _flush_until(self,target):
        while self.held and self.sent_cursor<target:
            pcm=self.held.popleft()
            count=min(len(pcm)//2,target-self.sent_cursor)
            outgoing=pcm[:count*2]
            if count*2<len(pcm):self.held.appendleft(pcm[count*2:])
            await self._send(dict(type='input_audio_buffer.append',
                audio=base64.b64encode(outgoing).decode('ascii')))
            self.sent_cursor+=count;self.sent_samples+=count;self.buffer_samples+=count
            record=self.current_record
            if record.get('previous_commit_time') and record['first_sent_audio_after_commit_time'] is None:
                record['first_sent_audio_after_commit_time']=monotonic()
                self._telemetry(record)

    async def _process_wire(self,pcm,captured_at):
        rate=self.wire_sample_rate
        # 20 ms analysis/send frames; unsent quiet tail provides pre-roll without overlap.
        for offset in range(0,len(pcm),rate//50*2):
            frame=pcm[offset:offset+rate//50*2]
            timestamp=captured_at-(len(pcm)-offset-len(frame))/(2*rate)
            self.held.append(frame)
            self.controller.observe(frame,timestamp)
            self.current_record.update(self.controller.metadata())
            if (self.controller.was_speech and self.current_record.get('previous_commit_time') and
                    self.current_record['first_speech_after_commit_time'] is None):
                self.current_record['first_speech_after_commit_time']=monotonic()
            decision=self.controller.decide(self.sent_cursor)
            if decision:
                boundary,reason,quiet_ms=decision
                await self._flush_until(boundary)
                await self._commit(reason,boundary,quiet_ms)
                self.current_record.update(self.controller.metadata())
            target=max(self.sent_cursor,self.controller.cursor-int(rate*self.endpoint_config.lookahead_ms/1000))
            await self._flush_until(target)

    async def _sender(self):
        await self.ready.wait()
        import audioop
        while True:
            item=await self.send_queue.get()
            try:
                if isinstance(item,asyncio.Future):
                    if self.wire_tail:
                        await self._process_wire(self.wire_tail,monotonic());self.wire_tail=b''
                    await self._flush_until(self.controller.cursor)
                    await self._commit('manual_stop',self.controller.cursor,self.controller.silence*1000/self.wire_sample_rate)
                    if not item.done():item.set_result(True)
                    continue
                pcm,captured_at=item
                if self.sample_rate!=self.wire_sample_rate:
                    pcm,self.rate_state=audioop.ratecv(pcm,2,1,self.sample_rate,self.wire_sample_rate,self.rate_state)
                pcm=self.wire_tail+pcm
                size=self.wire_sample_rate//50*2
                end=len(pcm)//size*size
                self.wire_tail=pcm[end:]
                await self._process_wire(pcm[:end],captured_at-len(self.wire_tail)/(2*self.wire_sample_rate))
            finally:self.send_queue.task_done()

    async def finalize(self):
        if not self.ready.is_set():raise ASRError('OpenAI 尚未就绪，无法结束转录。')
        marker=asyncio.get_running_loop().create_future()
        await asyncio.wait_for(self.send_queue.put(marker),5)
        await asyncio.wait_for(marker,10)
        deadline=monotonic()+20
        while self.commits or self.commit_acks<self.total_commits or self.turns:
            if not self.ready.is_set():raise ASRError('OpenAI 定稿期间连接中断，未完成片段不计成功。')
            if monotonic()>=deadline:raise ASRError('OpenAI 等待定稿超时，未将 Partial 当作 Final。')
            await asyncio.sleep(.02)

    async def _watchdog(self):
        await asyncio.wait_for(self.ready.wait(), 10)
        while True:
            await asyncio.sleep(1)
            oldest = min([t['commit_at'] for t in self.turns.values() if t['commit_at'] is not None]
                         + [r['commit_time'] for r in self.commits], default=monotonic())
            if monotonic()-oldest > 25:
                raise ASRError('OpenAI 定稿超时，已停止识别；请检查网络后重新开始。')

    async def _reader(self, ws):
        async for payload in ws: self.receive(json.loads(payload))
        raise ConnectionError('closed')

    async def run(self):
        key = self.key_loader()
        if not key: raise ASRError('OpenAI API 未配置，请在项目 .env 设置现有 OPENAI_API_KEY；可切换 Windows。')
        self._diagnostic_secrets = (key,)
        self._session_phase = 'configuration'
        update = self.session_update()
        connector = self.connector
        if connector is None:
            try: from websockets.asyncio.client import connect
            except ImportError:
                raise ASRError('缺少 websockets 依赖，请安装项目 requirements.txt。') from None
            connector = connect
        logger = logging.Logger('ativia.openai.asr.transport'); logger.disabled = True
        for attempt in range(len(self.retry_delays)+1):
            tasks = []
            try:
                self._clear_stream()
                self._session_phase = 'connection'
                self.set_status('OpenAI · Model: '+self.model+' · 正在连接…')
                async with connector(self.endpoint, additional_headers={'Authorization':'Bearer '+key},
                        open_timeout=10, close_timeout=1, max_size=262144, max_queue=16, logger=logger) as ws:
                    self.ws = ws
                    from uuid import uuid4
                    self._session_event_id = 'cfg_'+uuid4().hex
                    self._session_phase = 'session_update'
                    self._report_config(False)
                    await self._send(dict(update, event_id=self._session_event_id))
                    tasks = [asyncio.create_task(self._reader(ws)), asyncio.create_task(self._watchdog()),asyncio.create_task(self._sender())]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done: await task
            except asyncio.CancelledError: raise
            except ASRError: raise
            except Exception as exc:
                fields = transport_error(exc, self._diagnostic_secrets)
                self._report_error(fields)
                code = getattr(getattr(exc, 'response', None), 'status_code', None)
                if code in (400, 401, 403, 404, 429):
                    messages = {400:'转录参数不受支持',401:'现有 OPENAI_API_KEY 认证失败',
                        403:'转录模型权限不足',404:'转录接口或模型不可用',429:'额度不足或请求限流'}
                    detail = ' · '.join(str(fields[k]) for k in ('code', 'param', 'message') if fields.get(k))
                    raise ASRError('OpenAI ASR HTTP '+str(code)+'：'+messages[code]+
                                   (' · '+detail if detail else '')+'；详情见 Diagnostics。') from None
                self.ready.clear(); self.ws = None
                self.epoch += 1; self._clear_stream(); self.on_reset(self.epoch)
                if attempt >= len(self.retry_delays):
                    raise ASRError('OpenAI ASR 网络连接中断，已停止重试；可切换 Windows。') from None
                self.set_status('OpenAI ASR 网络中断，正在重连 '+str(attempt+1)+'/'+str(len(self.retry_delays)))
                await asyncio.sleep(self.retry_delays[attempt])
            finally:
                self.ready.clear(); self.ws = None
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        await super().stop()
        self._clear_stream()
