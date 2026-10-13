// Per-page ownership. The SDK owns native audio; this gate only owns lifecycle.
export class VoiceLifecycle {
  constructor(io) { this.io=io; this.state='IDLE'; this.epoch=0; this.owned=new Set(); this.pending=new Set(); this.error=''; this.micEnabled=true; }
  emit() { this.io.status(this); }
  valid(epoch) { return epoch===this.epoch && ['CONNECTING','RESUMING','RUNNING'].includes(this.state); }
  async start() {
    if (!['IDLE','PAUSED','ENDED'].includes(this.state)) return;
    const epoch=++this.epoch;
    this.state=this.state==='PAUSED'?'RESUMING':'CONNECTING'; this.error=''; this.emit();
    if(!this.micEnabled) {this.state='RUNNING';this.emit();return;} // Idle with no capture or cloud session.
    const r={abort:new AbortController()}; this.owned.add(r); this.pending.add(r);
    let timer;
    try {
      r.stream=await this.io.capture();
      if (!this.valid(epoch)) return;
      const token=await this.io.token(r.abort.signal);
      if (!this.valid(epoch)) return;
      r.handle=this.io.create(r.stream, event=>{ if(this.valid(epoch)) this.io.event(event,this); });
      const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('connection_timeout')),25000);});
      await Promise.race([r.handle.connect(token),timeout]);
      if (!this.valid(epoch)) return;
      this.state='RUNNING'; this.emit();
    } catch(error) {
      if (this.valid(epoch)) { this.error=error.message || 'connection_failed'; this.stop('FAILED'); }
    } finally {
      clearTimeout(timer); this.pending.delete(r);
      if (!this.valid(epoch)) this.release(r);
      this.finishStop();
    }
  }
  release(r) {
    let failed=false;
    const attempt=fn=>{ try {fn();} catch {failed=true;} };
    attempt(()=>r.abort.abort());
    for(const track of r.stream?.getTracks() || []) {
      attempt(()=>track.stop());
      if(track.readyState!=='ended') failed=true;
    }
    attempt(()=>r.handle?.close());
    if(failed) {this.error='resource_close_failed';this.state='FAILED';}
    else if(!this.pending.has(r)) this.owned.delete(r);
    this.emit();
  }
  stop(target='PAUSED') {
    ++this.epoch; // Gate is closed before stopping any resource or invoking SDK callbacks.
    this.target=target; this.state='PAUSING'; this.emit();
    this.io.event({type:target==='FAILED'?'SESSION_FAILED':target==='ENDED'?'USER_ENDED':'USER_PAUSED'},this);
    for(const r of [...this.owned]) this.release(r);
    this.finishStop();
  }
  finishStop() {
    if(this.state==='PAUSING' && !this.pending.size && !this.owned.size) this.state=this.target;
    this.emit();
  }
  interrupt() {
    if(this.state!=='RUNNING') return;
    try {for(const r of this.owned) r.handle.interrupt(); this.io.event({type:'USER_CANCELLED'},this);}
    catch {this.error='interrupt_failed';this.stop('FAILED');}
  }
  async setMic(enabled) {
    this.micEnabled=enabled;
    if(!enabled) {
      for(const r of this.owned) for(const track of r.stream?.getTracks()||[]) track.stop();
      if(['CONNECTING','RESUMING'].includes(this.state)) this.stop();
      this.emit();return;
    }
    if(this.state!=='RUNNING') {this.emit();return;}
    const r=[...this.owned][0];
    if(!r) {this.state='IDLE';return this.start();}
    if(r.stream?.getTracks().some(t=>t.readyState==='live')||this.pending.has(r))return;
    const epoch=this.epoch;this.pending.add(r);
    try {
      r.stream=await this.io.capture();
      if(this.valid(epoch)&&this.micEnabled) await r.handle.setMic(r.stream);
      else for(const track of r.stream.getTracks())track.stop();
    }catch(error){if(this.valid(epoch)){this.error='microphone_resume_failed';this.stop('FAILED');}}
    finally {this.pending.delete(r);if(!this.valid(epoch))this.release(r);this.finishStop();}
  }
  fail(message) {
    if(!['RUNNING','CONNECTING','RESUMING'].includes(this.state)) return;
    this.error=message;this.stop('FAILED');
  }
}
