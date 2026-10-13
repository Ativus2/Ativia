/* MicVAD/Silero and browser AEC, as used by Open-LLM-VTuber. No animation runtime. */
const $=id=>document.getElementById(id), now=()=>performance.now()/1000;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
ort.env.wasm.wasmPaths='/ort/';ort.env.wasm.numThreads=1;
const options={model:'v5',baseAssetPath:'/vad/',onnxWASMBasePath:'/ort/',
  positiveSpeechThreshold:.5,negativeSpeechThreshold:.35,redemptionFrames:20,
  minSpeechFrames:4,preSpeechPadFrames:6,submitUserSpeechOnPause:false,
  ortConfig:runtime=>{runtime.env.wasm.wasmPaths='/ort/';runtime.env.wasm.numThreads=1;}};
let ctx,turn=null,nodes=new Set(),nextTime=0,eos=false,audible=false,startedPlayback=false,
    generation=0,mic=null,micStream=null,lastVoice=0,pendingResult=null,offset=0,readyResolve;
let gaps=[],clockSamples=[],frameHandler=null,realStartHandler=null,endHandler=null,outputGain=.8;
let voicedSegments=new Set(),meaningfulAudible=false,captureControl=null;
const connected=new Promise(resolve=>readyResolve=resolve);
const ws=new WebSocket(`ws://${location.host}/ws`);
function send(value){if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify(value));}
function b64(buffer){return vad.utils.arrayBufferToBase64(buffer);}
async function audioContext(){if(!ctx)ctx=new AudioContext({latencyHint:'interactive'});await ctx.resume();return ctx;}
function stopAudio(){generation++;for(const node of nodes){node.onended=null;try{node.stop();}catch{}node.disconnect();}nodes.clear();nextTime=0;eos=false;startedPlayback=false;}
function interrupt(){const at=now();stopAudio();send({type:'interrupt'});turn=null;$('status').textContent='已暂停';return {requested_at:at,stopped_at:now()};}
function finishIfDrained(){
  if(!eos||nodes.size!==0||!turn)return;
  // onended means rendered; the hardware can still hold the final ~50ms.
  const id=turn,epoch=generation;
  const confirm=()=>{
    if(!eos||nodes.size!==0||turn!==id||generation!==epoch)return;
    if(ctx.getOutputTimestamp().contextTime<nextTime){setTimeout(confirm,20);return;}
    send({type:'playback_done',turn_id:id,at:now(),evidence:'output timestamp reached final PCM end'});eos=false;
  };confirm();
}
function playableTime(contextTime){
  const stamp=ctx.getOutputTimestamp();
  if(!stamp.performanceTime)throw Error('输出设备时钟暂不可用');
  return stamp.performanceTime/1000+(contextTime-stamp.contextTime);
}
function schedulePCM(message){
  if(!ctx||ctx.state!=='running')throw Error('请先点击开始，允许播放声音');
  const bytes=Uint8Array.from(atob(message.pcm),c=>c.charCodeAt(0));
  if(bytes.length<2||bytes.length%2)throw Error('音频帧不完整');
  const pcm=new Int16Array(bytes.buffer),buffer=ctx.createBuffer(1,pcm.length,message.rate),output=buffer.getChannelData(0);
  let onset=-1;for(let i=0;i<pcm.length;i++){output[i]=pcm[i]/32768*outputGain;if(onset<0&&Math.abs(pcm[i])>256)onset=i;}
  const earliest=ctx.currentTime+.06;
  if(startedPlayback&&nextTime<ctx.currentTime-.1)gaps.push((ctx.currentTime-nextTime)*1000);
  const start=Math.max(nextTime,earliest),node=ctx.createBufferSource();node.buffer=buffer;node.connect(ctx.destination);
  const currentGeneration=generation,id=turn;
  nodes.add(node);node.onended=()=>{node.disconnect();nodes.delete(node);if(currentGeneration===generation)finishIfDrained();};
  node.start(start);nextTime=start+buffer.duration;startedPlayback=true;
  if(onset>=0){
    const events=[];
    if(!audible){audible=true;events.push('audible');}
    if(!meaningfulAudible&&message.meaningful!==false){meaningfulAudible=true;events.push('meaningful_audible');}
    if(message.segment&&!voicedSegments.has(message.segment)){voicedSegments.add(message.segment);events.push('segment_audible');}
    if(!events.length)return;
    const target=start+onset/message.rate;
    const confirm=()=>{
      if(currentGeneration!==generation||turn!==id)return;
      try{
        const at=playableTime(target);
        if(now()<at){setTimeout(confirm,Math.min(30,(at-now())*1000+2));return;}
        for(const type of events)send({type,turn_id:id,at,segment:message.segment,evidence:'Web Audio getOutputTimestamp + first voiced PCM frame; not acoustic measurement'});
      }catch(error){setTimeout(confirm,20);}
    };setTimeout(confirm,60);
  }
}
ws.onopen=()=>{for(let i=0;i<5;i++)setTimeout(()=>send({type:'clock',at:now()}),i*30);};
ws.onmessage=event=>{
  const m=JSON.parse(event.data);
  if(m.type==='clock'){
    clockSamples.push({rtt:now()-m.echo,offset:m.server-(m.echo+now())/2});
    if(clockSamples.length===5){clockSamples.sort((a,b)=>a.rtt-b.rtt);offset=clockSamples[0].offset;send({type:'clock_offset',offset});$('status').textContent='准备好了';readyResolve();}return;
  }
  if(m.type==='turn_started'){stopAudio();outputGain=m.volume??.12;turn=m.turn_id;audible=false;meaningfulAudible=false;voicedSegments.clear();gaps=[];$('reply').textContent='';$('error').textContent='';$('status').textContent='正在回答';return;}
  if(m.turn_id&&m.turn_id!==turn)return;
  if(m.type==='transcript')$('transcript').textContent=m.text;
  else if(m.type==='transcript_partial')$('transcript').textContent+=''+m.text;
  else if(m.type==='text_delta')$('reply').textContent+=m.text;
  else if(m.type==='pcm'){
    try{schedulePCM(m);}catch(error){stopAudio();$('error').textContent=error.message;send({type:'playback_failed',turn_id:turn});}
  }else if(m.type==='audio_eos'){eos=true;finishIfDrained();}
  else if(m.type==='error'){stopAudio();$('error').textContent=m.message;$('status').textContent='发生错误';}
  else if(m.type==='result'){
    $('status').textContent=mic?'正在听':'准备好了';
    m.result.browser_gaps_ms=gaps;m.result.clock_sync_rtt_ms=clockSamples[0].rtt*1000;
    if(pendingResult){pendingResult(m.result);pendingResult=null;}
  }
};
ws.onclose=()=>{stopAudio();$('status').textContent='连接已断开';$('error').textContent='请确认语音原型服务仍在运行。';};
async function submitAudio(audio,speechEnd,{index=0,parity=false,mode=$('mode').value}={}){
  send({type:'speech',audio:b64(audio.buffer),speech_end:speechEnd,index,parity,mode});
}
// Mirror the existing VAD's six-frame preroll and complete tail, without trimming.
function inputController({stt='file',onEnd,onStart=()=>interrupt()}={}){
  let prior=[],captured=null,id=null,total=0,sent=0,lastPositive=0,lastNegative=0;
  const append=frame=>{send({type:'input_append',input_id:id,audio:b64(frame.buffer)});sent+=frame.length;};
  return {
    onFrameProcessed:(p,frame)=>{
      frame=frame.slice();total+=frame.length;
      if(p.isSpeech>=.5)lastPositive=total/16000;
      if(p.isSpeech<.35)lastNegative++;
      if(captured){captured.push(frame);if(id)append(frame);}
      prior.push(frame);if(prior.length>7)prior.shift();
      if(captured&&captured.length>937){interrupt();captured=null;id=null;throw Error('单次说话超过30秒，请分段');}
    },
    onSpeechStart:()=>{captured=prior.slice();},
    onSpeechRealStart:()=>{
      onStart();
      if(stt==='realtime'){
        id=crypto.randomUUID();sent=0;
        send({type:'input_start',input_id:id});
        for(const frame of captured||[])append(frame);
      }
    },
    onSpeechEnd:audio=>{
      const info={input_id:id,stt,submitted_samples:audio.length,streamed_samples:sent,
        endpoint_frame:total/16000,last_positive_frame:lastPositive,redemption_frames:options.redemptionFrames,
        tail_trimmed_samples:0};
      if(stt==='realtime'&&sent!==audio.length){interrupt();throw Error('实时与文件音频边界不一致');}
      captured=null;id=null;prior=[];onEnd(audio,info);
    },
    onVADMisfire:()=>{captured=null;id=null;prior=[];},
  };
}
async function startMic({echo=true,probe=false}={}){
  await connected;await audioContext();
  micStream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:echo,noiseSuppression:true,autoGainControl:true}});
  const actual=micStream.getAudioTracks()[0].getSettings();
  $('micnote').textContent=`外放回声消除：${actual.echoCancellation?'已启用':'未启用'} · ${micStream.getAudioTracks()[0].label}`;
  captureControl=inputController({stt:$('stt').value,onEnd:(audio,info)=>{
    send({type:'speech',audio:info.stt==='file'?b64(audio.buffer):'',stt:info.stt,input_id:info.input_id,
      speech_end:lastVoice||now()-.640,mode:$('mode').value,input_timing:{...info,speech_end_basis:'last VAD positive frame, human endpoint unmeasured'}});
  }});
  mic=await vad.MicVAD.new({...options,stream:micStream,
    onFrameProcessed:(probs,frame)=>{if(probs.isSpeech>=.5)lastVoice=now();if(frameHandler)frameHandler(probs,frame);if(!probe)captureControl.onFrameProcessed(probs,frame);},
    onSpeechStart:()=>{if(!probe)captureControl.onSpeechStart();},
    onSpeechRealStart:()=>{if(realStartHandler)realStartHandler();if(!probe)captureControl.onSpeechRealStart();},
    onSpeechEnd:audio=>{if(endHandler)endHandler(audio);if(!probe)captureControl.onSpeechEnd(audio);},
    onVADMisfire:()=>{if(!probe){captureControl.onVADMisfire();$('status').textContent='正在听';}}});
  mic.start();$('mic').textContent='关闭麦克风';$('status').textContent='正在听';return actual;
}
async function stopMic(){if(mic){mic.destroy();mic=null;send({type:'input_cancel'});}if(micStream){micStream.getTracks().forEach(t=>t.stop());micStream=null;}$('mic').textContent='开始语音对话';}
$('mic').onclick=async()=>{try{if(mic)await stopMic();else await startMic();}catch(e){$('error').textContent=e.message;}};
$('interrupt').onclick=interrupt;
$('stt').onchange=()=>stopMic();
fetch('/health').then(r=>r.json()).then(h=>{if(h.diagnostic){$('mode').value='openai';$('mode').disabled=true;$('stt').value='realtime';}});
$('feedbackSave').onclick=async()=>{
  const text=$('feedbackText').value.trim();if(!text)return;
  const response=await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({environment:$('environment').value,stt:$('stt').value,text})});
  $('feedbackState').textContent=response.ok?'反馈已保存到本地':'保存失败，请重试';
};

async function fixture(index){
  const data=await fetch(`/fixtures/${String(index).padStart(2,'0')}.wav`).then(r=>r.arrayBuffer());
  const offline=new OfflineAudioContext(1,16000,16000);const decoded=await offline.decodeAudioData(data);
  const input=decoded.getChannelData(0);let first=0,last=input.length-1;
  while(first<input.length&&Math.abs(input[first])<=.008)first++;
  while(last>first&&Math.abs(input[last])<=.008)last--;
  first=Math.max(0,first-3200);const length=last-first+1,audio=new Float32Array(length+32000);audio.set(input.slice(first,last+1));
  return {audio,end:length/16000};
}
async function detectInput(input,frameCallback){
  const context=await audioContext();let frameAt=0,segments=[],starts=[];
  const detector=await vad.AudioNodeVAD.new(context,{...options,
    onSpeechEnd:audio=>segments.push({audio,at:frameAt}),
    onSpeechRealStart:()=>{starts.push(frameAt);if(frameCallback)frameCallback(frameAt);}});
  detector.start();
  for(let i=0;i<input.length;i+=512){const frame=new Float32Array(512);frame.set(input.slice(i,i+512));frameAt=(i+512)/16000;await detector.processFrame(frame);}
  detector.destroy();return {segments,starts};
}
async function replay(config){
  await connected;await audioContext();await stopMic();
  const input=await fixture(config.index),detected=await detectInput(input.audio);
  if(detected.segments.length!==1)throw Error(`prototype_vad_segments_${detected.segments.length}`);
  const delay=Math.max(0,detected.segments[0].at-input.end),speechEnd=now();
  await sleep(delay*1000);
  const result=new Promise(resolve=>pendingResult=resolve);
  submitAudio(detected.segments[0].audio,speechEnd,{index:config.index,parity:true,mode:config.mode||'auto'});
  let timer;
  const timeout=new Promise((_,reject)=>timer=setTimeout(()=>{pendingResult=null;interrupt();reject(Error('prototype_round_timeout'));},180000));
  try{return await Promise.race([result,timeout]);}finally{clearTimeout(timer);}
}
function probeOutput(input,volume=.12){
  stopAudio();turn='local-probe';audible=false;outputGain=volume;
  const pcm=Int16Array.from(input,x=>Math.round(Math.max(-1,Math.min(1,x))*32767));
  for(let i=0;i<3;i++)schedulePCM({pcm:b64(pcm.buffer),rate:16000});
}
async function interruptionProbe({count=20}={}){
  await connected;await audioContext();await stopMic();const rows=[];
  for(let index=1;index<=count;index++){
    const input=await fixture((index-1)%20+1);let voiceIndex=input.audio.findIndex(x=>Math.abs(x)>.008),detected=null;
    let began=0;
    const detector=await vad.AudioNodeVAD.new(ctx,{...options,onSpeechRealStart:()=>{
      const sourcesBefore=nodes.size,stopContext=ctx.currentTime+128/ctx.sampleRate;
      const result=interrupt();
      detected={index,sources_before:sourcesBefore,sources_after:nodes.size,
        command_latency_ms:(result.stopped_at-began-voiceIndex/16000)*1000,
        estimated_dac_stop_latency_ms:(playableTime(stopContext)-began-voiceIndex/16000)*1000,
        evidence:'real-time fixture frames -> Silero RealStart -> actual Web Audio source.stop; DAC stop estimated from device clock, not acoustic measurement'};
    }});
    detector.start();probeOutput(input.audio);await sleep(200);began=now();
    try{
      for(let i=0;i<input.audio.length&&!detected;i+=512){
        await sleep(Math.max(0,(began+(i+512)/16000-now())*1000));
        const frame=new Float32Array(512);frame.set(input.audio.slice(i,i+512));await detector.processFrame(frame);
      }
      rows.push(detected??{index,error:'no_vad_start'});
    }finally{detector.destroy();stopAudio();turn=null;}
  }return rows;
}
async function echoProbe({seconds=6}={}){
  await connected;await audioContext();await stopMic();stopAudio();const input=await fixture(12),results=[];
  // Only aggregate microphone energy/probabilities are retained. No ASR or upload.
  for(const echo of [false,true]){
    let phase='warmup',frames=[],starts=0;
    frameHandler=(p,a)=>{
      if(phase==='warmup')return;
      let sum=0;for(const x of a)sum+=x*x;
      frames.push({phase,rms:Math.sqrt(sum/a.length),speech:p.isSpeech});
    };
    realStartHandler=()=>{if(phase==='playback')starts++;};
    try{
      const actual=await startMic({echo,probe:true});await sleep(1200);
      phase='baseline';await sleep(1200);phase='playback';probeOutput(input.audio,.35);await sleep(seconds*1000);
      const stats=name=>{
        const group=frames.filter(f=>f.phase===name);
        return {frames:group.length,rms:group.length?Math.sqrt(group.reduce((s,f)=>s+f.rms*f.rms,0)/group.length):null,
          speech_frames:group.filter(f=>f.speech>=.5).length};
      };
      results.push({requested_echo:echo,actual,baseline:stats('baseline'),playback:stats('playback'),false_start_candidates:starts});
    }finally{stopAudio();turn=null;await stopMic();frameHandler=null;realStartHandler=null;}
    await sleep(400);
  }
  return {method:'physical default microphone with real speaker output; no double-talk participant; ambient noise uncontrolled',results};
}
window.ativia={replay,fixture,detectInput,audioContext,interrupt,startMic,stopMic,interruptionProbe,echoProbe,
  ready:connected,get state(){return {turn,activeSources:nodes.size,ctx:ctx?.state,clock:clockSamples[0]};}};

async function vadAudit(){
  await connected;await audioContext();await stopMic();const rows=[];
  for(const tail of [20,16])for(let index=1;index<=20;index++){
    const input=await fixture(index);let at=0,lastPositive=0,frames=[],segments=[];
    const detector=await vad.AudioNodeVAD.new(ctx,{...options,redemptionFrames:tail,
      onFrameProcessed:p=>{frames.push({at,p:p.isSpeech});if(p.isSpeech>=.5)lastPositive=at;},
      onSpeechEnd:audio=>segments.push({at,samples:audio.length,start:at-audio.length/16000,lastPositive})});
    detector.start();
    for(let i=0;i<input.audio.length;i+=512){const frame=new Float32Array(512);frame.set(input.audio.slice(i,i+512));at=(i+512)/16000;await detector.processFrame(frame);}
    detector.destroy();
    const uncovered=[];for(let i=0;i<input.audio.length;i++)if(Math.abs(input.audio[i])>.008&&!segments.some(s=>i/16000>=s.start-.0001&&i/16000<s.at))uncovered.push(i);
    rows.push({index,tail_frames:tail,speech_end:input.end,segments,frames,uncovered_voiced_samples:uncovered.length,
      early_commit:segments.some(s=>s.at<input.end),endpoint_ms:segments.length===1?(segments[0].at-input.end)*1000:null});
  }return rows;
}
async function replayDiagnostic(config){
  await connected;await audioContext();await stopMic();
  const input=await fixture(config.index);let started=0,ended=false,timing;
  const result=new Promise(resolve=>pendingResult=resolve);
  const controller=inputController({stt:config.stt,onEnd:(audio,info)=>{
    ended=true;timing={...info,known_speech_end:input.end,commit_browser_at:now(),
      endpoint_detection_ms:(info.endpoint_frame-input.end)*1000,
      last_positive_after_speech_end_ms:(info.last_positive_frame-input.end)*1000,
      vad_tail_after_last_positive_ms:(info.endpoint_frame-info.last_positive_frame)*1000,
      replay_scheduler_lag_ms:(now()-started-info.endpoint_frame)*1000};
    send({type:'speech',stt:config.stt,input_id:info.input_id,audio:config.stt==='file'?b64(audio.buffer):'',
      speech_end:started+input.end,index:config.index,parity:true,mode:'openai',input_timing:timing,
      audio_environment:'user-declared external speaker output; real-time digital fixture input; no human double-talk'});
  }});
  const detector=await vad.AudioNodeVAD.new(ctx,{...options,...controller});detector.start();started=now();
  try{
    for(let i=0;i<input.audio.length&&!ended;i+=512){
      await sleep(Math.max(0,(started+(i+512)/16000-now())*1000));
      const frame=new Float32Array(512);frame.set(input.audio.slice(i,i+512));await detector.processFrame(frame);
    }
    if(!ended)throw Error('no_vad_endpoint');
    let timer;try{return await Promise.race([result,new Promise((_,reject)=>timer=setTimeout(()=>reject(Error('round_timeout')),180000))]);}
    finally{clearTimeout(timer);}
  }catch(error){pendingResult=null;interrupt();throw error;}
  finally{detector.destroy();}
}
Object.assign(window.ativia,{vadAudit,replayDiagnostic});
