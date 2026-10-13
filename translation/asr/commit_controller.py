"""Client endpoint decisions; never starts/stops capture or waits for ASR/translation."""
from collections import deque
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class EndpointConfig:
    silence_ms: int = 500
    soft_ms: int = 5000
    hard_ms: int = 9000
    short_pause_ms: int = 280
    lookback_ms: int = 1500
    lookahead_ms: int = 320
    pre_roll_ms: int = 400

    @classmethod
    def from_settings(cls, settings):
        values={'low':(400,4000,7500),'balanced':(500,5000,9000),'accurate':(700,7500,14000)}
        silence,soft,hard=values.get(settings.recognition_mode,values['balanced'])
        return cls(int(getattr(settings,'commit_silence_ms','0')) or silence,
                   int(getattr(settings,'commit_soft_ms','0')) or soft,
                   int(getattr(settings,'commit_hard_ms','0')) or hard)

class CommitController:
    def __init__(self, config=None, rate=24000):
        self.config=config or EndpointConfig();self.rate=rate
        self.cursor=0;self.start=0;self.has_partial=False
        self.energy=deque(maxlen=250);self.frames=deque(maxlen=1100)
        self.silence=0;self.was_speech=False;self.candidates=deque(maxlen=100)
        self.threshold=200;self.frame_count=0

    def observe(self, pcm, captured_end):
        import audioop
        samples=len(pcm)//2;begin=self.cursor;self.cursor+=samples
        level=audioop.rms(pcm,2)
        self.energy.append(level);self.frame_count+=1
        if len(self.energy)>=25 and self.frame_count%5==0:
            values=sorted(self.energy)
            self.threshold=max(150,min(values[len(values)//5]*2.5,values[len(values)*4//5]*.35))
        speech=level>=self.threshold
        self.frames.append((begin,self.cursor,speech,captured_end,level))
        if speech:
            if self.silence>=self.rate*self.config.short_pause_ms/1000:
                self.candidates.append((begin-self.silence,begin))
            self.silence=0
        else:self.silence+=samples
        self.was_speech=speech

    def metadata(self, end=None):
        end=self.cursor if end is None else end
        frames=[f for f in self.frames if f[1]>self.start and f[0]<end]
        spoken=[f for f in frames if f[2]]
        first=(spoken[0][3]-(spoken[0][1]-max(spoken[0][0],self.start))/self.rate) if spoken else None
        speech_end=spoken[-1][3] if spoken else None
        return dict(segment_audio_ms=round((end-self.start)*1000/self.rate,1),
            first_speech_time=first,estimated_speech_end_time=speech_end,
            speech_end_estimated=True,voiced_audio_ms=round(sum(f[1]-max(f[0],self.start) for f in spoken)*1000/self.rate,1),
            audio_start_sample=self.start,audio_end_sample=end,
            rms_threshold=round(self.threshold,1))

    def decide(self, sent_cursor):
        cfg=self.config;duration=(self.cursor-self.start)*1000/self.rate
        spoken=any(f[2] for f in self.frames if f[1]>self.start)
        quiet_ms=self.silence*1000/self.rate
        if spoken and quiet_ms>=cfg.silence_ms and duration>=400:
            # Already-uploaded PCM cannot be moved back. Keep the unsent quiet
            # tail as the next segment's pre-roll, with zero duplicate samples.
            boundary=max(sent_cursor,self.cursor-int(cfg.lookahead_ms*self.rate/1000))
            if boundary-self.start>=self.rate*.1:
                return boundary,'silence',quiet_ms
        if duration>=cfg.soft_ms:
            candidates=list(self.candidates)
            if quiet_ms>=cfg.short_pause_ms:
                candidates.append((self.cursor-self.silence,self.cursor))
            for begin,end in reversed(candidates):
                if self.cursor-end>cfg.lookback_ms*self.rate/1000:continue
                boundary=(begin+end)//2
                if boundary>=sent_cursor and boundary-self.start>=self.rate*2:
                    return boundary,'soft_endpoint',quiet_ms
        if duration>=cfg.hard_ms and (spoken or self.has_partial):
            # Leave a short buffered prefix for B; no stop/restart and no resend.
            boundary=max(sent_cursor,self.cursor-int(cfg.lookahead_ms*self.rate/1000))
            return boundary,'hard_timeout',quiet_ms
        if duration>=20000:return self.cursor,'safety_timeout',quiet_ms
        return None

    def committed(self,boundary):
        self.start=boundary;self.has_partial=False
        self.candidates=deque((p for p in self.candidates if p[1]>boundary),maxlen=100)
