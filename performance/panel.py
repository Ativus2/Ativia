"""Read-only developer panel; hidden panel does no resource sampling."""
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QPlainTextEdit,QLabel
from concurrent.futures import ThreadPoolExecutor
from .metrics import metrics
import os,json
def asr_partial_latency(event):
    """Use existing provider timestamps; never invent latency for Windows."""
    meta=event.get('metadata') or {}
    start=meta.get('first_speech_time');end=meta.get('first_partial_time')
    if isinstance(start,(int,float)) and isinstance(end,(int,float)) and end>=start:
        return f'{(end-start)*1000:.1f} ms (estimated speech start → first partial)'
    value=event.get('asr_latency_ms')
    return f'{value:.1f} ms (provider)' if isinstance(value,(int,float)) else '—'

class PerformancePanel(QDialog):
    def __init__(self,owner,parent=None):
        super().__init__(parent);self.owner=owner;self.previous=None;self.pending=None;self.resource={}
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ativia-resource-read')
        self.setWindowTitle('Ativia · Developer Performance');self.resize(760,650)
        layout=QVBoxLayout(self);note=QLabel('耗时来自真实事件；— 表示未测。Prompt 数值为 UTF-8 保守上界，不是模型实际 token 数。');note.setWordWrap(True);layout.addWidget(note)
        self.view=QPlainTextEdit();self.view.setReadOnly(True);layout.addWidget(self.view)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.refresh)
        self.destroyed.connect(lambda:self.pool.shutdown(wait=False,cancel_futures=True))
    def showEvent(self,e):self.timer.start();self.refresh();super().showEvent(e)
    def hideEvent(self,e):self.timer.stop();super().hideEvent(e)
    def sample(self):
        try:
            from tools.windows_resources import sample_tree,summarize
            now=sample_tree(os.getpid());before=self.previous;self.previous=now
            result={'cpu_percent':None,'ram_mib':sum(p.get('private_resident_mib',0) for p in now['processes'])}
            if before:result['cpu_percent']=summarize([before,now])['cpu_normalized_mean']
            monitor=getattr(self.owner,'_gpu_monitor',None)
            if monitor:
                history=monitor.history();sample=history[-1] if history else None
                result.update(gpu_percent=getattr(sample,'utilization',None),vram_mib=getattr(sample,'used_mb',None))
            return result
        except Exception as exc:return {'unavailable':type(exc).__name__}
    def refresh(self):
        if self.pending and self.pending.done():
            self.resource=self.pending.result();self.pending=None
        if self.pending is None:self.pending=self.pool.submit(self.sample)
        snap=metrics.snapshot();recent=self.owner.translation.diagnostics.latest
        lines=[]
        for name,stage,field in [('ASR partial latency','raw_partial','asr_latency_ms'),
            ('ASR final latency','raw_final','asr_latency_ms'),('Translation latency','translated','translation_latency_ms')]:
            value=recent.get(stage,{}).get(field)
            shown=asr_partial_latency(recent.get(stage,{})) if stage=='raw_partial' else (f'{value:.1f} ms' if isinstance(value,(int,float)) else '—')
            lines.append(name+': '+shown)
        for stage,label in [('agent_start','Agent queue time'),('agent_first_token','Agent TTFT'),
            ('agent_done','Agent full response'),('tts_first_audio','TTS first audio'),
            ('vision_done','Vision latency'),('memory_retrieval','Memory retrieval latency')]:
            row=snap['latest'].get(stage,{})
            lines.append(label+': '+str(row.get('ms','—'))+' ms')
        tasks=self.owner.scheduler.snapshot();tasks['independent_subtitle_worker']=bool(self.owner.translation.worker)
        lines+=['','Prompt budget (upper bounds)',json.dumps(snap['prompt'],ensure_ascii=False),
            '', 'Resources',json.dumps(self.resource,ensure_ascii=False),
            '', 'Tasks',json.dumps(tasks,ensure_ascii=False,indent=2)]
        self.view.setPlainText('\n'.join(lines))
