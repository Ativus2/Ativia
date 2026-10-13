// OFFLINE TEST DOUBLE ONLY. Never imported by the production bundle.
class Emitter {constructor(){this.listeners={};}on(name,cb){(this.listeners[name]??=[]).push(cb);}emit(name,value){for(const cb of this.listeners[name]||[])cb(value);}}
export class RealtimeAgent {constructor(options){this.options=options;}}
export class OpenAIRealtimeWebRTC extends Emitter {
  constructor(options){super();this.options=options;this.status='disconnected';this.connectionState={};}
}
export class RealtimeSession extends Emitter {
  constructor(agent,options){super();this.options=options;window.fakeSdk??={sessions:0,interrupts:0};window.fakeSdk.latest=this;}
  async connect(){
    window.fakeSdk.sessions++;const t=this.options.transport;t.status='connected';
    const sender={track:t.options.mediaStream.getAudioTracks()[0],async replaceTrack(track){this.track=track;}};
    t.connectionState.peerConnection={connectionState:'connected',getSenders:()=>[sender],getStats:async()=>new Map(),close(){this.connectionState='closed';}};
    t.emit('connection_change','connected');
  }
  interrupt(){window.fakeSdk.interrupts++;}
  close(){const t=this.options.transport;t.connectionState.peerConnection?.close();t.status='disconnected';t.emit('connection_change','disconnected');}
}
