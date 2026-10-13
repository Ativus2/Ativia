// Local control only. Cloud audio remains entirely in the official WebRTC transport.
export class LocalBridge {
  constructor(config, {execute, changed, lost, ownerReady}) {
    this.owner=false;this.connected=false;this.generation=0;this.pending=new Map();
    this.socket=new WebSocket(`ws://${location.host}/bridge`);
    this.socket.onopen=()=>this.socket.send(JSON.stringify({type:'hello',role:'voice',key:config.control_key}));
    this.lastSeen=performance.now();
    this.socket.onmessage=async message=>{
      this.lastSeen=performance.now();const data=JSON.parse(message.data);
      if(data.type==='welcome') {
        this.connected=true;this.owner=data.owner;this.lease=data.lease;this.generation=data.generation;
        ownerReady(data);return;
      }
      if(data.type==='state'){changed(data);return;}
      if(data.type==='ack') {
        if(data.phase!=='accepted'){this.pending.get(data.id)?.(data);this.pending.delete(data.id);}
        return;
      }
      if(data.type==='command'&&this.owner) {
        if(data.generation<this.generation)return;
        this.generation=data.generation;
        try {
          await execute(data);
          this.send({type:'ack',id:data.id,phase:'completed'});
        } catch(error) {this.send({type:'ack',id:data.id,phase:'rejected',error:error.message});}
      }
    };
    const disconnected=()=>{if(this.closed)return;this.closed=true;this.connected=false;this.owner=false;clearInterval(this.heartbeat);lost();};
    this.socket.onclose=disconnected;
    this.socket.onerror=()=>{this.socket.close();disconnected();};
    this.heartbeat=setInterval(()=>{
      if(performance.now()-this.lastSeen>7000){this.socket.close();disconnected();return;}
      this.send({type:'ping'});
    },2000);
  }
  send(data){if(this.socket.readyState===WebSocket.OPEN)this.socket.send(JSON.stringify(data));}
  command(command,extra={}) {
    if(!this.connected)return Promise.reject(new Error('本机控制服务失联'));
    const id=crypto.randomUUID();
    return new Promise(resolve=>{
      this.pending.set(id,resolve);this.send({type:'command',id,command,...extra});
      setTimeout(()=>{if(this.pending.delete(id))resolve({phase:'timeout',error:'操作未确认'});},13000);
    });
  }
  state(data){if(this.connected&&this.owner)this.send({type:'state',generation:this.generation,...data});}
  close(){clearInterval(this.heartbeat);this.socket.close();}
}
