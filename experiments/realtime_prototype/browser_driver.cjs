const {chromium}=require('C:/Users/kzcru/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const readline=require('node:readline');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
   headless:true,ignoreDefaultArgs:['--mute-audio'],args:['--autoplay-policy=no-user-gesture-required','--disable-background-timer-throttling']});
 const context=await browser.newContext({viewport:{width:1000,height:820},permissions:['microphone']});
 const page=await context.newPage();page.on('pageerror',error=>process.stderr.write(error.stack+'\n'));
 await page.goto('http://127.0.0.1:9882');await page.evaluate(()=>window.ativia.ready);
 process.stdout.write(JSON.stringify({ready:true})+'\n');
 try{
  for await (const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
   try{
    const command=JSON.parse(line);let result;
    if(command.type==='replay')result=await page.evaluate(c=>window.ativia.replay(c),command);
    else if(command.type==='screenshot'){await page.screenshot({path:command.path});result={saved:true};}
    else if(command.type==='check_vad')result=await page.evaluate(async()=>{
      const rows=[];for(let i=1;i<=20;i++){const f=await ativia.fixture(i),d=await ativia.detectInput(f.audio);rows.push({index:i,segments:d.segments.length,endpoint_ms:d.segments[0]?(d.segments[0].at-f.end)*1000:null,start_ms:d.starts[0]*1000});}return rows;
    });
    else if(command.type==='interruption')result=await page.evaluate(c=>ativia.interruptionProbe(c),command);
    else if(command.type==='echo')result=await page.evaluate(c=>ativia.echoProbe(c),command);
    else if(command.type==='state')result=await page.evaluate(()=>ativia.state);
    else throw Error('unknown_command');
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
   }catch(error){process.stdout.write(JSON.stringify({ok:false,error:error.message})+'\n');}
  }
 }finally{await browser.close();}
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exit(1);});
