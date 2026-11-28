import {claudeSender} from './cc-sender.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {installWorker} from './worker.mjs';
import {queueCli} from './codex-cli.mjs';
import {installProvisioner} from './provision.mjs';
import {handoffs} from './handoff.mjs';
import {desktopRequests} from './desktop-request.mjs';
import {localSteerer} from './steer.mjs';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

// The daily launcher checks the original host before starting this single CloudCLI instance.
const root=process.env.DAILY_CLOUDCLI_ROOT;
const storePath=process.env.DAILY_BRIDGE_STORE;
const statusFile=process.env.DAILY_STATUS_FILE;
const port=Number(process.env.SERVER_PORT);
if(!root || !storePath || !statusFile || process.env.HOST!=='127.0.0.1' ||
   !Number.isInteger(port) || port<1 || port>65535) throw Error('请从日常启动入口启动');
const require=createRequire(path.join(root,'package.json'));
const instance=randomUUID();
function status(state,message) {
  const tmp=statusFile+'.'+process.pid+'.tmp';
  fs.writeFileSync(tmp,JSON.stringify({pid:process.pid,port,instance,store:storePath,
    heartbeat:Math.floor(Date.now()/1000),state,message,reviewReturn:true,ccProgress:true}),'utf8');
  fs.renameSync(tmp,statusFile);
}
async function main() {
  if(require('./package.json').version!=='1.37.9') throw Error('此桥已验证 CloudCLI 1.37.9，当前版本不匹配');
  console.log('Desktop 投递程序',await queueCli());
  const load=f=>import(pathToFileURL(path.join(root,'dist-server/server',f)));
  await load('load-env.js');
  const {sessionsDb,userDb,getConnection}=await load('modules/database/index.js');
  const {providerRuntimeService:runtime}=await load('modules/providers/services/provider-runtime.service.js');
  const {sessionsService}=await load('modules/providers/services/sessions.service.js');
  const {providerRegistry}=await load('modules/providers/provider.registry.js');
  const {chatRunRegistry:runRegistry,runDetachedChatTurn:detached}=await load('modules/websocket/index.js');
  const {query,makeSendServer}=await claudeSender(require);
  const Database=require('better-sqlite3');
  const db=new Database(storePath,{fileMustExist:true});
  const desktopDb=new Database(process.env.DAILY_DESKTOP_DB,{readonly:true,fileMustExist:true});
  status('starting','正在连接 CloudCLI 本地服务');
  await load('index.js');
  // index.js starts listening asynchronously. An occupied port must never adopt another host's queue.
  const marker=path.join(os.homedir(),'.cloudcli','local-server.json');
  let ready=false;
  for(let i=0;i<100;i++) {
    try {
      const current=JSON.parse(fs.readFileSync(marker,'utf8'));
      if(current.pid===process.pid && current.port===port && current.host==='127.0.0.1') {ready=true;break;}
    } catch(error) {if(error.code!=='ENOENT' && !(error instanceof SyntaxError)) throw error;}
    await delay(200);
  }
  if(!ready) throw Error('未取得本机 CloudCLI 服务，请检查原服务是否仍在运行');
  const desktopProvision=desktopRequests({db,desktopDb,sessions:sessionsDb,
    openDraft:url=>promisify(execFile)(process.env.DAILY_PYTHON,
      ['-c','import os,sys;os.startfile(sys.argv[1])',url],{windowsHide:true,timeout:15000})});
  let steer;
  if (process.env.DAILY_DESKTOP_WS) {
    const tokenFile=process.env.DAILY_DESKTOP_WS_TOKEN_FILE;
    if (!tokenFile || !path.isAbsolute(tokenFile)) throw Error('请提供已配置的本机 app-server 令牌文件绝对路径');
    const WebSocketClient=require('ws');
    steer=localSteerer(process.env.DAILY_DESKTOP_WS,{connect:url=>new WebSocketClient(url,{
      headers:{Authorization:'Bearer '+fs.readFileSync(tokenFile,'utf8').trim()}})});
  }
  let queueDb;
  function nativeBacklog(threadId) {
    const file=path.join(path.dirname(process.env.DAILY_DESKTOP_DB),'queue_1.sqlite');
    if (!fs.existsSync(file)) return false;
    queueDb ??= new Database(file,{readonly:true,fileMustExist:true});
    return !!queueDb.prepare('SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1').get(threadId);
  }
  const activeUser=getConnection().prepare('SELECT min(id) AS id FROM users WHERE is_active=1 HAVING count(*)=1');
  let historyDb;
  const desktopFile=name=>path.join(path.dirname(process.env.DAILY_DESKTOP_DB),name);
  const externalBusy=async sid=>(await sessionsService.listRunningSessions()).some(row=>row.sessionId===sid);
  const handoff=handoffs({db,desktopDb,sessions:sessionsDb,runtime,runRegistry,externalBusy,boot:instance,
    history:()=>historyDb ??= new Database(desktopFile('thread_history_1.sqlite'),{readonly:true,fileMustExist:true}),
    backlog:id=>fs.existsSync(desktopFile('queue_1.sqlite')) ? nativeBacklog(id) : undefined});
  const worker=installWorker({db,desktopDb,storePath,python:process.env.DAILY_PYTHON,desktopProvision,steer,nativeBacklog,handoff,
    defaultUserId:()=>activeUser.get()?.id,
    sessions:sessionsDb,provider:providerRegistry.resolveProvider('claude').runtime,runtime,runRegistry,detached,query,makeSendServer,
    externalBusy});
  const provision=installProvisioner({db,desktopDb,sessionsDb,userDb,cloudDb:getConnection(),detached,runtime});
  const message='日常桥已连接原生会话服务';
  status('ready',message);
  setInterval(()=>status('ready',message),3000).unref();
  setInterval(()=>worker.tick().catch(e=>console.error('配对调度失败',e.message)),500).unref();
  setInterval(()=>provision.tick().catch(e=>console.error('新建配对失败',e.message)),1000).unref();
  setInterval(()=>handoff.tick().catch(e=>console.error('换新核对失败',e.message)),1000).unref();
  setInterval(()=>{try {desktopProvision.tick();} catch(e) {console.error('Desktop 配对失败',e.message);}},1000).unref();
}
try {await main();}
catch(error){status('error',error.message); console.error(error.message);process.exit(1);}
