// Same production app, lifecycle and localhost bridge. ONLY cloud SDK/capture are doubled.
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const {chromium}=require('C:/Users/kzcru/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const base=new URL('../logs/',import.meta.url);
const stage=name=>writeFileSync(new URL('integration-stage.json',base),JSON.stringify({stage:name}));
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const page=await browser.newPage();let forbidden=0;const checks=[];
await page.route('https://api.openai.com/**',route=>{forbidden++;return route.abort();});
await page.route('**/app.js',route=>route.fulfill({contentType:'application/javascript',body:readFileSync(new URL('offline-app.js',base))}));
await page.route('**/token',route=>route.fulfill({contentType:'application/json',body:'{"value":"ephemeral-offline-test"}'}));
await page.addInitScript(()=>{
  window.testTracks=[];window.testContexts=[];
  navigator.mediaDevices.getUserMedia=async()=>{
    const context=new AudioContext();testContexts.push(context);const destination=context.createMediaStreamDestination();
    testTracks.push(...destination.stream.getTracks());return destination.stream;
  };
});
try{
  await page.goto('http://127.0.0.1:9884');await page.waitForFunction(()=>window.nativeVoice?.bridge.owner);
  stage('PAGE_READY');
  await page.waitForFunction(()=>document.getElementById('error').textContent.includes('点击开始'),null,{timeout:15000});
  checks.push('pet start requires first browser approval, no capture before approval');
  await page.click('#start');await page.waitForFunction(()=>nativeVoice.controller.state==='RUNNING');
  const second=await browser.newPage();await second.goto('http://127.0.0.1:9884');
  await second.waitForFunction(()=>window.nativeVoice?.bridge.connected);
  if(await second.evaluate(()=>nativeVoice.bridge.owner))throw Error('second page acquired microphone ownership');
  if(!await second.locator('#start').isDisabled())throw Error('second page can start another capture');
  checks.push('duplicate debug page is a controller only, never a second capture owner');await second.close();stage('LIVE');
  await page.waitForFunction(()=>nativeVoice.controller.state==='PAUSED',null,{timeout:15000});
  if(!await page.evaluate(()=>testTracks.every(t=>t.readyState==='ended')))throw Error('pause leaked track');
  const count=await page.evaluate(()=>fakeSdk.sessions);
  await page.evaluate(()=>fakeSdk.latest.emit('transport_event',{type:'response.output_audio_transcript.delta',response_id:'late',delta:'迟到结果不能显示'}));
  if(await page.locator('#caption').textContent())throw Error('late caption admitted');
  checks.push('pet pause -> browser resource close -> same confirmed paused state; late callback discarded');stage('PAUSED');
  await page.waitForFunction(()=>nativeVoice.controller.state==='RUNNING',null,{timeout:15000});
  if(await page.evaluate(()=>fakeSdk.sessions)!==count+1)throw Error('resume did not create exactly one session');
  stage('RESUMED');
  await page.waitForFunction(()=>!nativeVoice.controller.micEnabled&&testTracks.every(t=>t.readyState==='ended'),null,{timeout:15000});
  checks.push('mic off stops actual test MediaStream tracks without closing session');stage('MIC_OFF');
  await page.waitForFunction(()=>nativeVoice.controller.micEnabled&&testTracks.some(t=>t.readyState==='live'),null,{timeout:15000});
  if(await page.evaluate(()=>fakeSdk.sessions)!==count+1)throw Error('mic toggle created duplicate session');
  checks.push('mic on replaces input track in the existing session');stage('MIC_ON');
  await page.waitForFunction(()=>fakeSdk.interrupts===1,null,{timeout:15000});
  if(await page.evaluate(()=>nativeVoice.controller.state)!=='RUNNING')throw Error('interrupt paused entire session');
  checks.push('pet interrupt controls same session and keeps capture');stage('INTERRUPTED');
  await page.waitForFunction(()=>nativeVoice.controller.state==='ENDED',null,{timeout:15000});
  if(!await page.evaluate(()=>testTracks.every(t=>t.readyState==='ended')))throw Error('exit leaked capture');
  checks.push('launcher exit receives ended confirmation after all tracks stopped');
}catch(error){checks.push('ERROR: '+error.message);process.exitCode=1;}
finally{
  try{await page.evaluate(()=>{nativeVoice.controller.stop('ENDED');return Promise.all(testContexts.map(c=>c.close()));});}catch{}
  await browser.close();
  writeFileSync(new URL('../../../docs/desktop-pet-trial/browser-pet-link.json',import.meta.url),JSON.stringify({paid_calls:0,provider:'offline test double',physical_mic:false,forbidden_api_attempts:forbidden,checks},null,2));
}
