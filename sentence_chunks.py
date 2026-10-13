"""Incremental spoken sentence chunks; no second LLM and no partial rewrites."""
import re
class SentenceChunks:
    def __init__(self):self.pending='';self.index=0
    def feed(self,delta,final=False):
        self.pending+=delta;ready=[]
        while self.pending:
            found=re.search(r'[。！？!?；;\n]|[.](?=\s|$)',self.pending)
            end=found.end() if found else 0
            # Bound very long punctuation-free output without splitting a short sentence.
            if not end and len(self.pending)>180:
                candidates=[self.pending.rfind(c,0,180) for c in (' ', '，', ',')]
                end=max(candidates)+1 or 160
            if not end:
                if not final:break
                end=len(self.pending)
            text=self.pending[:end].strip();self.pending=self.pending[end:]
            if text:self.index+=1;ready.append((self.index,text))
        return ready
