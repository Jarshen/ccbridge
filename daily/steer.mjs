// A short-lived second client of an explicitly configured local app-server.
// Only inspect metadata. Never start, resume, or interrupt the destination thread.
export function localSteerer(address, {connect = url => new WebSocket(url), timeoutMs = 2000} = {}) {
  const url = new URL(address);
  if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1' || !url.port ||
      url.pathname !== '/' || url.search || url.hash || url.username || url.password)
    throw Error('DAILY_DESKTOP_WS 需要本机 ws://127.0.0.1:<端口>');
  return async function steer(binding, mail, prompt, eligible) {
    let socket, pending, nextId=0, attempted=false;
    function wait(id, send) {
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>reject(Error('app-server 超时')),timeoutMs);
        pending={id,resolve,reject};
        try { send?.(); } catch (e) {reject(e);}
        pending.clear=()=>clearTimeout(timer);
      }).finally(()=>{pending.clear();pending=null;});
    }
    function rpc(method,params) {
      const id=++nextId;
      return wait(id,()=>socket.send(JSON.stringify({id,method,params})));
    }
    try {
      socket=connect(url.href);
      socket.addEventListener('open',()=>pending?.resolve());
      socket.addEventListener('message',event=>{
        let frame;
        try {frame=JSON.parse(event.data);} catch {return;}
        if (!pending || frame.id!==pending.id || frame.method) return;
        if (frame.error) pending.reject(Error(frame.error.message));
        else pending.resolve(frame.result);
      });
      socket.addEventListener('close',()=>pending?.reject(Error('app-server 已断开')));
      socket.addEventListener('error',()=>pending?.reject(Error('app-server 连接失败')));
      await wait(null);
      await rpc('initialize',{clientInfo:{name:'daily-bridge',version:'1'}});
      socket.send(JSON.stringify({method:'initialized'}));
      const threadId=binding.desktop;
      const {thread}=await rpc('thread/read',{threadId,includeTurns:false});
      if (thread.id!==threadId || thread.status?.type!=='active') return null;
      const {data}=await rpc('thread/turns/list',{threadId,limit:1,sortDirection:'desc',itemsView:'notLoaded'});
      const turn=data[0];
      if (turn?.status!=='inProgress' || !turn.id || !eligible(thread.cwd)) return null;
      attempted=true;
      const {turnId}=await rpc('turn/steer',{threadId,expectedTurnId:turn.id,clientUserMessageId:mail.id,
        input:[{type:'text',text:prompt,text_elements:[]}]});
      if (turnId!==turn.id) throw Error('回执 turn ID 不匹配');
      return turnId;
    } catch (error) {
      // A timeout before the sole mutation is safe to fall back from. After the
      // mutation even a lost response must keep this letter out of the queue.
      if (attempted) throw Error('已尝试 turn/steer，请按信件 ID 核对目标聊天：'+error.message);
      return null;
    } finally { socket?.close(); }
  };
}
