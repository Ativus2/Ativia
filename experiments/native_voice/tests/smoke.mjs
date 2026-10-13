// Finite paid smoke. Two sessions maximum, no retries. Synthetic fixture, no real microphone.
import {createRequire} from 'node:module';
import {writeFileSync,readFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const {chromium}=require('C:/Users/kzcru/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const finalAttempt=process.argv.includes('--final');
const output=new URL(`../../../docs/pause-native-voice/native-smoke${finalAttempt?'-final':''}.json`,import.meta.url);
const report={synthetic_input:true,real_microphone:false,external_speaker_human_check:'NOT_COMPLETED',sessions:[],errors:[]};
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true,
  ignoreDefaultArgs:['--mute-audio'],args:['--autoplay-policy=no-user-gesture-required']});
const page=await browser.newPage();
page.on('pageerror',e=>report.errors.push({kind:'page',message:e.message.replace(/(?:sk-|ek_)[\w-]+/g,'[redacted]')}));
await page.route('**/synthetic-input.wav',route=>route.fulfill({contentType:'audio/wav',body:readFileSync(new URL('../../../docs/realtime-benchmark/fixtures/01.wav',import.meta.url))}));
await page.addInitScript(()=>{
  navigator.mediaDevices.getUserMedia=async()=>{
    const ctx=new AudioContext();await ctx.resume();const destination=ctx.createMediaStreamDestination();
    window.smoke={ctx,destination,tracks:destination.stream.getTracks(),async feed(){
      const bytes=await fetch('/synthetic-input.wav').then(r=>r.arrayBuffer());
      const buffer=await ctx.decodeAudioData(bytes);const source=ctx.createBufferSource();source.buffer=buffer;source.connect(destination);source.start();
      return buffer.duration;
    }};
    return destination.stream;
  };
});
async function waitState(state) {
  await page.waitForFunction(s=>window.nativeVoice && (window.nativeVoice.controller.state===s||window.nativeVoice.controller.state==='FAILED'),state,{timeout:35000});
  const result=await page.evaluate(()=>({state:nativeVoice.controller.state,error:nativeVoice.controller.error}));
  if(result.state!==state)throw Error(JSON.stringify(result));
}
async function snapshot() {
  return page.evaluate(async()=>{
    const r=[...nativeVoice.controller.owned][0],pc=r?.handle?.transport.connectionState.peerConnection;
    const stats=[];if(pc)for(const row of (await pc.getStats()).values())if(row.type==='inbound-rtp'&&row.kind==='audio')stats.push({packetsReceived:row.packetsReceived,totalAudioEnergy:row.totalAudioEnergy,totalSamplesDuration:row.totalSamplesDuration});
    return {state:nativeVoice.controller.state,error:nativeVoice.controller.error,diagnostics:nativeVoice.diagnostics,
      browser_audio_time:nativeVoice.audio?.currentTime,stats,track_states:window.smoke.tracks.map(t=>t.readyState),
      synthetic_completed_replies:nativeVoice.completedHistory.filter(i=>i.role==='assistant').map(i=>i.content)};
  });
}
try {
  await page.goto('http://127.0.0.1:9884');await page.waitForFunction(()=>!!window.nativeVoice);
  await page.screenshot({path:new URL('../../../docs/pause-native-voice/native-page.png',import.meta.url).pathname.replace(/^\/([A-Z]:)/,'$1'),fullPage:true});
  await page.click('#start');await waitState('RUNNING');
  await page.evaluate(()=>smoke.feed());
  await page.waitForFunction(()=>nativeVoice.diagnostics.some(e=>e.type==='output_audio_buffer.started')||nativeVoice.controller.state==='FAILED',null,{timeout:30000});
  await waitState('RUNNING');
  await page.waitForTimeout(1000);
  if(finalAttempt) {
    await page.waitForFunction(()=>nativeVoice.diagnostics.some(e=>e.type==='output_audio_buffer.stopped')||nativeVoice.controller.state==='FAILED',null,{timeout:30000});
    await waitState('RUNNING');
  }
  report.sessions.push({phase:'first_native_audio',...await snapshot()});
  await page.click('#interrupt');await page.waitForTimeout(600);
  report.interrupt={...await snapshot()};
  await page.click('#pause');await waitState('PAUSED');
  const before=await snapshot();await page.evaluate(()=>smoke.feed());await page.waitForTimeout(1800);
  report.pause={before,after:await snapshot()};
  await page.evaluate(()=>smoke.ctx.close());
  if(!finalAttempt) {
  await page.click('#pause');await waitState('RUNNING');
  await page.evaluate(()=>smoke.feed());
  await page.waitForFunction(()=>nativeVoice.diagnostics.filter(e=>e.type==='output_audio_buffer.started').length>=2,null,{timeout:30000});
  await page.waitForTimeout(1500);
  report.sessions.push({phase:'resumed_new_connection',...await snapshot()});
  }
  await page.click('#end');await waitState('ENDED');report.end=await snapshot();
} catch(error) {
  report.errors.push({kind:'smoke',message:error.message});
  try {report.failure=await snapshot();}catch{}
} finally {
  try {await page.evaluate(()=>{nativeVoice.controller.stop('ENDED');return window.smoke?.ctx.close();});}catch{}
  await browser.close();
  writeFileSync(output,JSON.stringify(report,null,2));
}
console.log(JSON.stringify({sessions:report.sessions.length,errors:report.errors,output:output.pathname}));
if(report.errors.length)process.exitCode=1;
