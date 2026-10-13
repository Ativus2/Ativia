const {chromium}=require('C:/Users/kzcru/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const readline=require('node:readline');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless:true,ignoreDefaultArgs:['--mute-audio'],args:['--autoplay-policy=no-user-gesture-required','--disable-background-timer-throttling']});
 const page=await browser.newPage({permissions:['microphone']});
 page.on('pageerror',e=>process.stderr.write(e.stack+'\n'));
 await page.goto('http://127.0.0.1:9883');await page.evaluate(()=>ativia.ready);
 process.stdout.write(JSON.stringify({ready:true})+'\n');
 try{for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
  const c=JSON.parse(line);try{
   let result;
   if(c.type==='vad')result=await page.evaluate(()=>ativia.vadAudit());
   else if(c.type==='replay')result=await page.evaluate(c=>ativia.replayDiagnostic(c),c);
   else if(c.type==='interruption')result=await page.evaluate(c=>ativia.interruptionProbe(c),c);
   else if(c.type==='echo')result=await page.evaluate(c=>ativia.echoProbe(c),c);
   else if(c.type==='screenshot'){await page.screenshot({path:c.path,fullPage:true});result={saved:true};}
   else throw Error('unknown_command');
   process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  }catch(e){process.stdout.write(JSON.stringify({ok:false,error:e.message})+'\n');}
 }}finally{await browser.close();}
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exit(1);});
