import {RealtimeAgent, RealtimeSession, OpenAIRealtimeWebRTC} from '@openai/agents/realtime';
import {VoiceLifecycle} from './lifecycle.js';
import {LocalBridge} from './bridge.js';

const $=id=>document.getElementById(id);
const config=await fetch('/config').then(r=>r.json());
$('model').textContent=`${config.model} · ${config.voice} · 官方 SDK 0.18.0`;
const diagnostics=[]; // Metadata only, in RAM. No tracing export, audio recording or transcript logs.
const completedHistory=[]; // Local RAM only; not replayed into a new connection.
let audio=null;
let bridge,hubState,userApproved=false,thinking=false,speaking=false,caption='',activeResponse=null,cancelledResponse=null;
let captionTimer;const settledWaiters=new Set();
function snapshot(c) {
  const resources=[...c.owned];
  return {state:c.state,error:c.error,mic_enabled:c.micEnabled,
    mic_active:resources.some(r=>r.stream?.getTracks().some(t=>t.readyState==='live')),
    cloud_connected:resources.some(r=>r.handle?.transport.status==='connected'),
    thinking:c.state==='RUNNING'&&thinking,speaking:c.state==='RUNNING'&&speaking,
    caption:c.state==='RUNNING'?caption:''};
}
function publish(){bridge?.state(snapshot(controller));}
function render(s) {
  $('state').textContent=names[s.state]||'状态未知';$('error').textContent=s.error||'';
  $('capture').textContent=s.mic_active===null?'麦克风状态未知':s.mic_active?'● 麦克风正在采集':'麦克风已释放';
  $('playback').textContent=s.speaking?'检测到播放中的非零音频':'当前未检测到有声播放';
  $('start').disabled=!bridge?.owner||!['IDLE','ENDED'].includes(s.state);
  $('pause').disabled=!bridge?.connected||['PAUSING'].includes(s.state);
  $('pause').textContent=s.state==='PAUSED'?'恢复助手':s.state==='FAILED'?'重试完全暂停':'完全暂停';
  $('interrupt').disabled=!bridge?.connected||s.state!=='RUNNING';
  $('end').disabled=!bridge?.connected||s.state==='PAUSING';
  $('mic').disabled=!bridge?.connected||s.state==='PAUSING';
  $('mic').textContent=s.mic_enabled?'关闭麦克风':'开启麦克风';
  $('caption').textContent=s.caption||'';
}
function record(event) {
  const row={type:event.type, ms:Math.round(performance.now())};
  if(event.type==='response.done') {
    row.status=event.response?.status;row.usage=event.response?.usage;
    row.response_id=event.response?.id;
    row.error_code=event.response?.status_details?.error?.code;
    row.error_type=event.response?.status_details?.error?.type;
    row.status_reason=event.response?.status_details?.reason;
  }
  if(event.type==='session.created'||event.type==='session.updated') {
    row.model=event.session?.model;row.voice=event.session?.audio?.output?.voice;
    row.transcription=event.session?.audio?.input?.transcription;
    row.turn_detection=event.session?.audio?.input?.turn_detection;
  }
  if(event.type==='error') row.code=event.error?.code || 'sdk_error';
  diagnostics.push(row);if(diagnostics.length>500)diagnostics.shift();
}
const names={IDLE:'尚未启动',CONNECTING:'连接中',RUNNING:'运行中',PAUSING:'正在暂停',PAUSED:'已完全暂停',RESUMING:'恢复中',FAILED:'停止 / 连接失败（新任务已禁止）',ENDED:'已结束',UNKNOWN:'失联 · 资源状态未知'};
const controller=new VoiceLifecycle({
  capture:()=>navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false}),
  token:async signal=>{
    const response=await fetch('/token',{method:'POST',signal,headers:{'X-Voice-Lease':bridge.lease,'X-Voice-Generation':String(bridge.generation)}});
    const body=await response.json();
    if(!response.ok) throw new Error(body.error || `token_http_${response.status}`);
    return body.value;
  },
  create(stream,event) {
    const element=document.createElement('audio');element.autoplay=true;audio=element;
    element.addEventListener('playing',()=>event({type:'BROWSER_PLAYING'}));
    element.addEventListener('error',()=>event({type:'error',error:{code:'audio_element_error'}}));
    const transport=new OpenAIRealtimeWebRTC({mediaStream:stream,audioElement:element});
    const agent=new RealtimeAgent({name:'Ativia',instructions:config.instructions,voice:config.voice});
    const session=new RealtimeSession(agent,{transport,model:config.model,historyStoreAudio:false,tracingDisabled:true,
      config:{outputModalities:['audio'],audio:{input:{transcription:null,
        turnDetection:{type:'server_vad',threshold:0.5,prefixPaddingMs:300,silenceDurationMs:500,
          createResponse:true,interruptResponse:true}},output:{voice:config.voice}},
        providerData:{max_output_tokens:1024}}});
    session.on('transport_event',event);
    session.on('error',e=>event({type:'error',error:{code:e.error?.code || 'sdk_error'}}));
    transport.on('connection_change',state=>{
      event({type:`CONNECTION_${state}`});
      if(state==='disconnected') event({type:'error',error:{code:'connection_closed'}});
    });
    session.on('history_updated',items=>event({type:'LOCAL_HISTORY',items}));
    let pc,meter,lastEnergy=0,lastClock=0;
    return {
      session, transport, element,
      async connect(apiKey) {
        await session.connect({apiKey});
        pc=transport.connectionState.peerConnection;
        // Explicitly observe autoplay rejection rather than declaring a silent success.
        if(element.srcObject) await element.play();
        // Passive WebRTC statistics + HTMLAudioElement clock, not a new PCM output pipeline.
        meter=setInterval(async()=>{
          try {
            if(!pc||pc.connectionState==='closed')return;
            let energy=lastEnergy;
            for(const row of (await pc.getStats()).values())
              if(row.type==='inbound-rtp'&&row.kind==='audio')energy=row.totalAudioEnergy??lastEnergy;
            const audible=energy-lastEnergy>0.000001 && !element.paused && element.currentTime>lastClock;
            lastEnergy=energy;lastClock=element.currentTime;
            event({type:'PLAYBACK_ENERGY',audible});
          }catch {event({type:'error',error:{code:'playback_statistics_failed'}});}
        },200);
      },
      async setMic(nextStream){
        const sender=pc?.getSenders().find(s=>s.track?.kind==='audio');
        if(!sender)throw new Error('microphone_sender_missing');
        await sender.replaceTrack(nextStream.getAudioTracks()[0]);
      },
      interrupt(){session.interrupt();},
      close(){
        clearInterval(meter);
        pc ||= transport.connectionState.peerConnection;
        let failure;
        const attempt=fn=>{try{fn();}catch(e){failure ||= e;}};
        if(pc) pc.ontrack=null;
        attempt(()=>element.pause());
        for(const track of element.srcObject?.getTracks()||[]) attempt(()=>track.stop());
        element.srcObject=null;
        attempt(()=>session.close());
        if(pc && pc.connectionState!=='closed') attempt(()=>pc.close());
        if(pc && pc.connectionState!=='closed') failure ||= new Error('peer_connection_not_closed');
        if(failure) throw failure;
      }
    };
  },
  status(c) {
    if(c.state!=='RUNNING'){thinking=false;speaking=false;caption='';clearTimeout(captionTimer);}
    if(!bridge||bridge.owner)render(snapshot(c));
    bridge?.state(snapshot(c));
    if(['PAUSED','ENDED','FAILED'].includes(c.state))for(const resolve of settledWaiters)resolve();
  },
  event(event,c) {
    if(event.type==='LOCAL_HISTORY') {
      for(const item of event.items) if(item.status==='completed'&&!completedHistory.some(x=>x.itemId===item.itemId)) completedHistory.push(item);
      $('history').textContent=completedHistory.filter(i=>i.role==='assistant').map(i=>i.content.map(p=>p.transcript||p.text||'').join('')).join('\n\n');
      return;
    }
    if(event.type==='PLAYBACK_ENERGY') {
      if(speaking!==event.audible){speaking=event.audible;publish();}return;
    }
    record(event);
    if(event.type==='response.created'){activeResponse=event.response?.id;thinking=true;caption='';publish();}
    if(event.type==='USER_CANCELLED'){cancelledResponse=activeResponse;caption='';thinking=false;publish();}
    if(event.type==='response.output_audio_transcript.delta'&&event.response_id!==cancelledResponse){
      caption+=event.delta||'';clearTimeout(captionTimer);captionTimer=setTimeout(publish,120);
    }
    if(event.type==='output_audio_buffer.started'){thinking=false;publish();}
    if(event.type==='output_audio_buffer.stopped'||event.type==='output_audio_buffer.cleared'){speaking=false;thinking=false;publish();}
    if(event.type==='error') c.fail(event.error?.code || 'realtime_error');
    if(event.type==='response.done' && event.response?.status==='failed') c.fail(event.response?.status_details?.error?.code || 'response_failed');
    if((event.type==='session.created'||event.type==='session.updated')&&event.session?.model&&event.session.model!==config.model)
      c.fail('unexpected_model_no_substitution_allowed');
  }
});
async function untilStopped() {
  if(['PAUSED','ENDED','FAILED'].includes(controller.state))return;
  await new Promise((resolve,reject)=>{
    const done=()=>{clearTimeout(timer);settledWaiters.delete(done);resolve();};
    const timer=setTimeout(()=>{settledWaiters.delete(done);reject(new Error('资源仍在停止，请在浏览器处理授权提示。'));},10000);
    settledWaiters.add(done);
  });
}
bridge=new LocalBridge(config,{
  ownerReady(data){$('instance').textContent=`实例 ${data.instance} · ${data.owner?'本页承载唯一语音会话':'本页仅控制，另一页面承载语音'}`;if(data.owner)publish();},
  changed(data){hubState=data;render(data);},
  lost(){controller.error='本机控制通道失联，已关闭自身音频；请重新打开页面。';controller.stop('FAILED');render({...snapshot(controller),state:'UNKNOWN'});},
  async execute(data){
    if(['start','resume'].includes(data.command)){
      if(!userApproved){controller.error='请在本语音页点击开始会话，授权麦克风和播放。';controller.emit();throw new Error(controller.error);}
      await controller.start();
    }else if(data.command==='pause'||data.command==='end'){
      controller.stop(data.command==='end'?'ENDED':'PAUSED');await untilStopped();
    }else if(data.command==='interrupt')controller.interrupt();
    else if(data.command==='mic'){
      if(data.enabled&&!userApproved)throw new Error('请先在语音页授权。');
      await controller.setMic(data.enabled);
    }else if(data.command==='focus')window.focus();
    if(controller.state==='FAILED')throw new Error(controller.error||'资源停止失败');
  }
});
async function command(name,extra){const ack=await bridge.command(name,extra);if(ack.error)$('error').textContent=ack.error;}
$('start').onclick=()=>{userApproved=true;command('start');};
$('pause').onclick=()=>{if(hubState?.state==='PAUSED')userApproved=true;command(hubState?.state==='PAUSED'?'resume':'pause');};
$('interrupt').onclick=()=>command('interrupt');
$('end').onclick=()=>command('end');
$('mic').onclick=()=>command('mic',{enabled:!hubState?.mic_enabled});
function closePage(){controller.stop('ENDED');bridge.close();}
window.addEventListener('pagehide',closePage);
window.addEventListener('beforeunload',closePage);
window.addEventListener('unhandledrejection',()=>controller.fail('unhandled_async_error'));
$('save').onclick=async()=>{
  try {
    const response=await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({note:$('feedback').value})});
    $('saved').textContent=response.ok?'已保存到本机 logs/feedback.jsonl':'保存失败';
  } catch {$('saved').textContent='保存失败';}
};
// Readable local debugging surface; never includes credentials in diagnostic records.
window.nativeVoice={controller,bridge,diagnostics,completedHistory,get audio(){return audio;}};
controller.emit();
