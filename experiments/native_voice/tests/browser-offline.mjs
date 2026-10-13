import {createRequire} from 'node:module';
import {writeFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const {chromium}=require('C:/Users/kzcru/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const page=await browser.newPage();let tokenCalls=0;const checks=[];
await page.route('https://api.openai.com/**',route=>route.abort());
await page.route('**/token',route=>{tokenCalls++;return route.fulfill({status:503,contentType:'application/json',body:'{"error":"injected_unavailable"}'});});
await page.addInitScript(()=>{
  window.captures=[];
  navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{
    window.resolveCapture=()=>{const track={readyState:'live',stop(){this.readyState='ended';}};captures.push(track);resolve({getTracks:()=>[track]});};
  });
});
try {
  await page.goto('http://127.0.0.1:9884');await page.waitForFunction(()=>!!window.nativeVoice);
  await page.click('#start');await page.click('#pause');
  if(await page.locator('#state').textContent()!=='正在暂停')throw Error('premature paused state');
  await page.evaluate(()=>resolveCapture());await page.waitForFunction(()=>nativeVoice.controller.state==='PAUSED');
  if(tokenCalls!==0)throw Error('token requested after pause');checks.push('pause during permission: no request, late track ended');
  await page.click('#pause');await page.evaluate(()=>resolveCapture());
  await page.waitForFunction(()=>nativeVoice.controller.state==='FAILED');
  const states=await page.evaluate(()=>captures.map(t=>t.readyState));
  if(states.some(s=>s!=='ended')||tokenCalls!==1)throw Error('failed connect leaked or retried');
  checks.push('unavailable broker: explicit error, tracks ended, no retry');
  await page.click('#pause');await page.waitForFunction(()=>nativeVoice.controller.state==='PAUSED');
  await page.click('#end');await page.waitForFunction(()=>nativeVoice.controller.state==='ENDED');checks.push('cleanup retry and end remain explicit');
}finally{await browser.close();}
writeFileSync(new URL('../../../docs/pause-native-voice/browser-offline.json',import.meta.url),JSON.stringify({paid_calls:0,checks},null,2));
console.log(JSON.stringify({passed:checks.length,paid_calls:0}));
