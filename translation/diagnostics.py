"""In-memory layer diagnostics and opt-in export / human ratings only."""
from collections import Counter, OrderedDict, deque
import json
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QLabel, QPushButton, QFileDialog, QComboBox)

class DiagnosticBuffer:
    def __init__(self):
        self.events=deque(maxlen=2000); self.latest={}; self.sentences=OrderedDict(); self.ratings={}
    def add(self, event):
        event=dict(event); self.events.append(event); self.latest[event['stage']]=event
        if event['stage']=='reset':
            self.latest={}; return
        if event['stage'] in ('stable_final','translated'):
            key=(event['provider'],event.get('generation',0),event['epoch'],event['caption_id'])
            previous=self.sentences.get(key)
            if previous and previous['revision'] != event['revision']: self.ratings.pop(key,None)
            self.sentences[key]=event
            while len(self.sentences)>200:
                old,_=self.sentences.popitem(last=False); self.ratings.pop(old,None)
    def rate(self,key,rating):
        if key in self.sentences and self.sentences[key].get('translation') and rating in ('正确','基本正确','错误'):
            self.ratings[key]=rating
    def summary(self):
        counts=Counter((key[0],value) for key,value in self.ratings.items())
        if not counts:return '人工评估：尚无标记；不计算自动准确率。'
        return '；'.join(f'{provider}：'+ ' / '.join(f'{label} {counts[(provider,label)]}' for label in ('正确','基本正确','错误'))
                        for provider in sorted({key[0] for key in self.ratings}))
    def export(self,path):
        with Path(path).open('w',encoding='utf-8') as stream:
            for event in self.events:stream.write(json.dumps(event,ensure_ascii=False)+'\n')
            for key,rating in self.ratings.items():
                row=self.sentences[key]
                stream.write(json.dumps(dict(stage='human_rating',provider=key[0],generation=key[1],epoch=key[2],
                    caption_id=key[3],revision=row['revision'],rating=rating),ensure_ascii=False)+'\n')

class DiagnosticsDialog(QDialog):
    def __init__(self,buffer,parent=None):
        super().__init__(parent);self.buffer=buffer;self._signature=None
        self.setWindowTitle('Ativia · Translation Diagnostics');self.resize(780,650)
        layout=QVBoxLayout(self)
        label=QLabel('仅内存保留最近 2000 个事件。导出会包含识别原文与译文；不含密钥或音频；会话配置仅记录语言、字段名和数量。\n'
                     'Windows ASR latency 不可得；OpenAI 为音频 commit 发出到 completed 到达的耗时，非整句端到端延迟。')
        label.setWordWrap(True);layout.addWidget(label)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);layout.addWidget(self.details,1)
        self.choose=QComboBox();self.choose.currentIndexChanged.connect(self._selected);layout.addWidget(self.choose)
        self.sentence=QPlainTextEdit();self.sentence.setReadOnly(True);self.sentence.setMaximumHeight(110);layout.addWidget(self.sentence)
        buttons=QHBoxLayout()
        for label in ('正确','基本正确','错误'):
            button=QPushButton(label);button.clicked.connect(lambda checked=False,value=label:self._rate(value));buttons.addWidget(button)
        export=QPushButton('导出 JSONL…');export.clicked.connect(self._export);buttons.addWidget(export);layout.addLayout(buttons)
        self.stats=QLabel();self.stats.setWordWrap(True);layout.addWidget(self.stats)
        self.timer=QTimer(self);self.timer.setInterval(200);self.timer.timeout.connect(self.refresh);self.timer.start();self.refresh()
    def refresh(self):
        latest=self.buffer.latest;status=latest.get('status',{});lines=['Provider: '+{'openai':'OpenAI','windows':'Windows','deepgram':'Deepgram'}.get(status.get('provider'),status.get('provider','—'))+'\nModel: '+str(status.get('model') or '—')+'\n'+status.get('message','尚未开始')]
        config = latest.get('asr_session_config')
        if config:
            fields = ('accepted','languages','sample_rate','turn_detection','delay',
                      'transcription_fields','prompt_chars','keywords_count','event_id')
            lines.append('OpenAI session.update（安全配置摘要）\n'+'\n'.join(
                f'{key}: {config.get(key)}' for key in fields))
        # Preserve the last error through reconnect resets, with its original
        # generation/epoch clearly shown; this is history, not current status.
        error = next((e for e in reversed(self.buffer.events) if e['stage']=='asr_error'), None)
        if error:
            fields = ('transport','exception_type','http_status','close_code','phase',
                      'type','code','message','param','event_id','server_event_id',
                      'session_event_id','generation','epoch','timestamp')
            lines.append('最近一次 ASR 错误（历史记录）\n'+'\n'.join(
                f'{key}: {error.get(key) if error.get(key) is not None else "—"}' for key in fields))
        for stage,label in [('raw_partial','Raw Partial'),('raw_final','Raw Final'),('stable_final','Stable Final'),('provisional','Provisional（临时）'),('translated','Translation')]:
            event=latest.get(stage,{})
            lines.append(label+' ['+event.get('provider','—')+']\n'+event.get('translation' if stage in ('translated','provisional') else 'text','—'))
        recent=latest.get('translated') or latest.get('raw_final') or {}
        for field,label in [('asr_latency_ms','ASR latency'),('translation_latency_ms','Translation latency')]:
            value=recent.get(field);lines.append(f'{label}: '+('不可得 / 尚无结果' if value is None else f'{value} ms'))
        if recent.get('latency_basis'): lines.append('ASR latency basis: '+recent['latency_basis'])
        segment=latest.get('segment_metrics',{})
        if segment:
            fields=['segment_id','commit_reason','segment_audio_ms','silence_before_commit_ms',
                'previous_commit_time','first_audio_after_commit_time','first_speech_after_commit_time',
                'first_partial_time','commit_time','raw_final_time','stable_final_time',
                'provisional_translation_time','formal_translation_time','capture_gap_after_commit_ms',
                'first_speech_to_first_provisional_translation_ms','speech_end_to_raw_final_ms',
                'speech_end_to_stable_final_ms','speech_end_to_first_provisional_translation_ms',
                'speech_end_to_formal_translation_ms']
            lines.append('片段时间 / speech end 均为客户端估计\n'+'\n'.join(f'{key}: {segment.get(key) if segment.get(key) is not None else "—"}' for key in fields))
        value='\n\n'.join(lines)
        if self.details.toPlainText()!=value:self.details.setPlainText(value)
        signature=tuple((k,r['revision'],r.get('translation')) for k,r in self.buffer.sentences.items())
        if signature!=self._signature:
            selected=self.choose.currentData();self.choose.blockSignals(True);self.choose.clear()
            for key,row in self.buffer.sentences.items():
                self.choose.addItem(f"{key[0]} · {row['text'][:65]}",key)
            index=self.choose.findData(selected);self.choose.setCurrentIndex(index if index>=0 else self.choose.count()-1)
            self.choose.blockSignals(False);self._signature=signature;self._selected()
        self.stats.setText(self.buffer.summary())
    def showEvent(self,event):
        self.timer.start();self.refresh();super().showEvent(event)
    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
    def _selected(self):
        row=self.buffer.sentences.get(self.choose.currentData(),{})
        self.sentence.setPlainText('原文：'+row.get('text','')+'\n译文：'+row.get('translation','（待翻译）'))
    def _rate(self,value):self.buffer.rate(self.choose.currentData(),value);self.refresh()
    def _export(self):
        path,_=QFileDialog.getSaveFileName(self,'导出字幕诊断','asr-diagnostics.jsonl','JSONL (*.jsonl)')
        if path:
            try:self.buffer.export(path)
            except OSError:self.stats.setText('导出失败，请选择可写目录。')
