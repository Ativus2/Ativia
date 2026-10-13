import {test} from 'node:test';
import assert from 'node:assert/strict';
import {VoiceLifecycle} from '../src/lifecycle.js';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=()=>new Promise(r=>setImmediate(r));
function setup(overrides={}) {
  const counts={capture:0,token:0,create:0,close:0,interrupt:0,event:0};
  const tracks=[];let callback;
  const io={status(){},capture:async()=>{counts.capture++;const track={readyState:'live',stop(){this.readyState='ended';}};tracks.push(track);return {getTracks:()=>[track]};},
    token:async()=>{counts.token++;return 'ephemeral-test';},create:(stream,cb)=>{counts.create++;callback=cb;return {connect:async()=>{},close(){counts.close++;},interrupt(){counts.interrupt++;}};},
    event(){counts.event++;},...overrides};
  return {c:new VoiceLifecycle(io),counts,tracks,late:()=>callback({type:'late'})};
}
test('pause running closes owned tracks and connection, no capture until explicit resume',async()=>{
  const {c,counts,tracks,late}=setup();await c.start();c.stop();const eventsAfterPause=counts.event;late();
  assert.equal(c.state,'PAUSED');assert.equal(tracks[0].readyState,'ended');assert.equal(counts.event,eventsAfterPause);
  assert.equal(counts.capture,1);await c.start();assert.equal(counts.capture,2);c.stop('ENDED');assert.equal(c.state,'ENDED');
});
test('pause pending microphone permission waits honestly and closes late track',async()=>{
  const d=deferred();const {c,counts}=setup({capture:()=>d.promise});const starting=c.start();c.stop();assert.equal(c.state,'PAUSING');
  const track={readyState:'live',stop(){this.readyState='ended';}};d.resolve({getTracks:()=>[track]});await starting;
  assert.equal(c.state,'PAUSED');assert.equal(track.readyState,'ended');assert.equal(counts.token,0);
});
test('pause pending token aborts and no connection is admitted',async()=>{
  const d=deferred();let signal;const {c,counts}=setup({token:s=>{signal=s;return d.promise;}});const starting=c.start();await tick();c.stop();
  assert.equal(signal.aborted,true);d.resolve('stale');await starting;assert.equal(counts.create,0);assert.equal(c.state,'PAUSED');
});
test('pause during connection cannot be undone by late success',async()=>{
  const d=deferred();let closed=0;const {c}=setup({create:()=>({connect:()=>d.promise,close:()=>closed++})});
  const starting=c.start();await tick();c.stop();d.resolve();await starting;assert.equal(c.state,'PAUSED');assert.ok(closed>=1);
});
test('duplicate starts create only one connection',async()=>{
  const {c,counts}=setup();await Promise.all([c.start(),c.start()]);assert.equal(counts.create,1);c.stop();
});
test('close errors remain FAILED, block resume and allow explicit cleanup retry',async()=>{
  let fail=true;const {c,tracks}=setup({create:()=>({connect:async()=>{},close(){if(fail)throw Error('mock');}})});
  await c.start();c.stop();assert.equal(c.state,'FAILED');assert.equal(tracks[0].readyState,'ended');await c.start();assert.equal(c.state,'FAILED');
  fail=false;c.stop();assert.equal(c.state,'PAUSED');
});
test('connection errors clean up, with no fallback/retry',async()=>{
  const {c,counts,tracks}=setup({token:async()=>{throw Error('unavailable');}});await c.start();
  assert.equal(c.state,'FAILED');assert.equal(tracks[0].readyState,'ended');assert.equal(counts.create,0);
});
test('interrupt leaves microphone and session active',async()=>{
  const {c,counts,tracks}=setup();await c.start();c.interrupt();assert.equal(c.state,'RUNNING');assert.equal(counts.interrupt,1);assert.equal(tracks[0].readyState,'live');c.stop();
});
test('independent pages own separate resources',async()=>{
  const a=setup(),b=setup();await a.c.start();await b.c.start();a.c.stop();assert.equal(b.c.state,'RUNNING');assert.equal(b.tracks[0].readyState,'live');b.c.stop();
});
test('resume with disabled microphone preserves preference without opening capture',async()=>{
  const {c,counts}=setup();await c.setMic(false);await c.start();
  assert.equal(c.state,'RUNNING');assert.equal(counts.capture,0);assert.equal(counts.create,0);
  c.stop();await c.start();assert.equal(counts.capture,0);
  await c.setMic(true);assert.equal(counts.capture,1);assert.equal(counts.create,1);c.stop();
});
test('pending microphone re-enable cannot survive full pause',async()=>{
  const d=deferred();const {c}=setup();await c.start();await c.setMic(false);
  c.io.capture=()=>d.promise;const reopening=c.setMic(true);c.stop();assert.equal(c.state,'PAUSING');
  const track={readyState:'live',stop(){this.readyState='ended';}};d.resolve({getTracks:()=>[track]});await reopening;
  assert.equal(track.readyState,'ended');assert.equal(c.state,'PAUSED');
});
