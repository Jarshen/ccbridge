// Isolated Python CLI + SQLite + host handoff controller. No account queues.
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {handoffs} from './handoff.mjs';

const daily=path.dirname(fileURLToPath(import.meta.url));
const Database=createRequire(path.join(os.homedir(),'.cloudcli/server/1.37.9/package.json'))('better-sqlite3');
function fixture(t,side='cc') {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'ccb-handoff-'));
  const store=path.join(dir,'bridge.sqlite3'),index=path.join(dir,'index.sqlite');
  const old=randomUUID(),native=randomUUID(),desktop=randomUUID(),candidate=randomUUID(),boot=randomUUID();
  const py=(args,input)=>execFileSync('python',['-B','-X','utf8',...args],{cwd:daily,input,encoding:'utf8',windowsHide:true,stdio:['pipe','pipe','pipe']});
  const cli=(...args)=>JSON.parse(py(['handoff.py','--store',store,...args]));
  const route=(id,native_id)=>({id,native_id,title:id,cwd:dir});
  py(['-c','import json,sys;from storage import Store;Store(sys.argv[1]).bind(*json.load(sys.stdin))',store],
    JSON.stringify([route(old,native),route(desktop,desktop)]));
  const db=new Database(store), idx=new Database(index);
  t.after(()=>{db.close();idx.close();fs.rmSync(dir,{recursive:true,force:true});});
  idx.exec('CREATE TABLE sessions(session_id,provider_session_id,custom_name,project_path,provider,isArchived);CREATE TABLE threads(id,title,cwd,source,archived); CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,completed_at,final_agent_item_id); CREATE TABLE thread_items(thread_id,turn_id,item_id,item_type,item_json);');
  for(const [id,sid] of [[candidate,candidate],[old,native]]) idx.prepare("INSERT INTO sessions VALUES(?,?,?,?,'claude',0)").run(id,sid,id,dir);
  for(const id of [candidate,desktop]){
    idx.prepare("INSERT INTO threads VALUES(?,?,?,'desktop',0)").run(id,id,dir);
    idx.prepare("INSERT INTO thread_turns VALUES(?,'turn',1,'completed',unixepoch()-10,'final')").run(id);
  }
  const bootFile=path.join(dir,'status.json');
  fs.writeFileSync(bootFile,JSON.stringify({store,state:'ready',instance:boot}));
  db.exec("UPDATE pairing_notices SET state='sent'");
  const sample=path.join(dir,'sample');fs.writeFileSync(sample,'v1');
  const common=['--binding','1'];
  const note=(base,topic,text)=>cli('note',...common,'--base',String(base),'--topic',topic,'--text',text,'--kind','用户局部认可','--evidence','week 1 original','--file',sample);
  note(0,'colour','only colour accepted');
  assert.throws(()=>note(0,'colour','late result'));
  const ticket=cli('prepare',...common,'--side',side,'--index',index,'--candidate',candidate,'--work','new owner compares shape; colour kept');
  const status=()=>cli('status',ticket.ticket);
  const state={busy:false,backlog:false};
  const host=handoffs({db,desktopDb:idx,sessions:{getSessionById:id=>idx.prepare('SELECT * FROM sessions WHERE session_id=?').get(id)},
    runtime:{hasBackgroundWork:()=>false},runRegistry:{isProcessing:()=>state.busy},externalBusy:async()=>false,
    history:()=>idx,backlog:()=>state.backlog,boot});
  const response='colour only; compare shape\n接续核对 '+ticket.ticket;
  const ready=()=>{
    if(side==='cc') host.claimReadback('接续检查 '+ticket.ticket,{sessionId:candidate,cwd:dir}).finish(response);
    else idx.prepare("INSERT INTO thread_items VALUES(?,'turn','final','agentMessage',?)").run(candidate,JSON.stringify({type:'agentMessage',text:response}));
  };
  return {status,db,idx,old,native,desktop,store,candidate,host,state,sample,cli,note,py,ticket,ready,
    request:()=>cli('request',ticket.ticket),async attempt(){this.request();await host.tick();return status();}};
}
for(const side of ['cc','desktop']) test('atomic takeover, preserved history, stale sender, direction and strict policy: '+side,async t=>{
  const f=fixture(t,side);
  if(side==='cc') assert.throws(()=>f.request()); else assert.match((await f.attempt()).detail,/核对答复/);
  f.ready();
  f.db.exec('CREATE TABLE paired_inboxes(binding_id INTEGER PRIMARY KEY,turn TEXT,listen_until INTEGER DEFAULT 0); INSERT INTO paired_inboxes(binding_id) VALUES(1); DELETE FROM auto_inboxes');
  const r=await f.attempt();assert.equal(r.state,'committed',r.detail);
  assert.equal((await f.attempt()).successor,r.successor);
  assert.deepEqual(f.db.prepare('SELECT active FROM bindings ORDER BY id').all().map(r=>r.active),[0,1]);
  const next=f.db.prepare('SELECT * FROM bindings WHERE active=1').get();
  assert.equal(next.cc, side==='cc'?f.candidate:f.old);
  assert.equal(next.desktop,side==='cc'?f.desktop:f.candidate);
  assert.ok(f.db.prepare('SELECT 1 FROM paired_inboxes WHERE binding_id=? AND turn IS NULL').get(next.id));
  assert.equal(f.cli('show','--binding',String(next.id),'--side',side).notes[0].body,'only colour accepted');
  assert.throws(()=>f.py(['-c','import sys;from storage import Store;Store(sys.argv[1]).cc_send(1,*sys.argv[2:])',f.store,f.old,f.native,'late',randomUUID()]));
  assert.throws(()=>f.py(['-c','import sys;from storage import Store;Store(sys.argv[1]).desktop_send(sys.argv[2],"late",sys.argv[3],expected_binding=1)',f.store,f.desktop,randomUUID()]));
});
for(const [label,breakIt,expected] of [
  ['queue',f=>f.state.backlog=true,/原生队列/],['unknown queue',f=>f.state.backlog=undefined,/原生队列/],
  ['active Claude',f=>f.state.busy=true,/运行中的回合/],
  ['active Desktop',f=>f.idx.exec("UPDATE thread_turns SET status='inProgress'"),/仍在执行/],
  ['changed Desktop after staging',f=>f.idx.exec('UPDATE thread_turns SET completed_at=unixepoch()'),/有活动/],
  ['new direction',f=>f.note(1,'colour','revised direction'),/已变化/],
  ['replaced sample',f=>fs.writeFileSync(f.sample,'v2'),/样品已改变/],
  ['late mail',f=>f.db.prepare("INSERT INTO mail(id,binding_id,source,body) VALUES(?,1,'cc','late')").run(randomUUID()),/来信已变化/],
  ['restart',f=>f.db.exec("UPDATE continuity_tickets SET boot='other'"),/桥已重启/],
  ['uncertain receipt',f=>f.db.exec("UPDATE pairing_notices SET state='unknown'"),/pairing_notices/],
  ['retired candidate',f=>f.idx.prepare('UPDATE sessions SET isArchived=1 WHERE session_id=?').run(f.candidate),/原生身份/],
  ['strict extension',f=>f.db.exec('CREATE TABLE strict_route(binding_id); INSERT INTO strict_route VALUES(1)'),/strict_route/]
]) test('keep original binding on '+label,async t=>{
  const f=fixture(t);f.ready();f.request();breakIt(f);await f.host.tick();
  assert.match(f.status().detail,expected);assert.equal(f.db.prepare('SELECT id FROM bindings WHERE active=1').get().id,1);
});
for(const state of ['queued','unknown','notified']) test('preexisting '+state+' mail needs disposition or native receipt',async t=>{
  const f=fixture(t),id=randomUUID();
  f.db.prepare("INSERT INTO mail(id,binding_id,source,body,state,detail) VALUES(?,1,'cc','original',?,'Desktop 原生队列已接收')").run(id,state);
  // Model a ticket prepared after this letter already existed.
  f.db.exec('UPDATE continuity_tickets SET mail_seq=1');
  f.ready();const r=await f.attempt();assert.match(r.detail,/待处理原信|缺少目标历史/);
  assert.deepEqual(f.db.prepare('SELECT binding_id,body,state FROM mail WHERE id=?').get(id),{binding_id:1,body:'original',state});
});

test('wrong sample answer keeps original pair; evidence check stays outside candidate packet',async t=>{
  const f=fixture(t);
  const ticket=f.cli('prepare','--binding','1','--side','cc','--index',path.join(path.dirname(f.store),'index.sqlite'),
    '--candidate',f.candidate,'--work','read actual version','--expect','hidden version');
  const inspect=f.host.claimReadback('接续检查 '+ticket.ticket,{sessionId:f.candidate,cwd:path.dirname(f.store)});
  assert.ok(!inspect.prompt.includes('hidden version'));
  inspect.finish('I looked at another sheet');
  f.cli('request',ticket.ticket);await f.host.tick();
  assert.match(f.cli('status',ticket.ticket).detail,/客观证据不符/);
  assert.equal(f.db.prepare('SELECT id FROM bindings WHERE active=1').get().id,1);
});
test('changed actor, stale preparation and late exposure are blocked before readback',t=>{
  const f=fixture(t),cmd='接续检查 '+f.ticket.ticket,opts={sessionId:f.candidate,cwd:path.dirname(f.store)};
  f.db.exec(`CREATE TABLE receiver(binding_id,user_id,options);INSERT INTO receiver VALUES(1,7,'{"permissionMode":"plan"}')`);
  assert.throws(()=>f.host.claimReadback(cmd,opts,{userId:3}),/用户或审批/);
  assert.throws(()=>f.host.claimReadback(cmd,opts,{userId:7}),/用户或审批/);
  assert.ok(f.host.claimReadback(cmd,{...opts,permissionMode:'plan'},{userId:7}));
  f.db.exec("UPDATE continuity_stage SET boot='prior-process'");
  assert.throws(()=>f.host.claimReadback(cmd,opts,{userId:7}),/重新准备/);
});
test('a new old-owner turn invalidates a staged request; cancellation wins a queued request',async t=>{
  const f=fixture(t);f.ready();f.request();f.host.changedSession(f.old);await f.host.tick();
  assert.equal(f.status().state,'stale');
  assert.throws(()=>f.request());
  const g=fixture(t);g.ready();g.request();g.cli('cancel',g.ticket.ticket);await g.host.tick();
  assert.equal(g.status().state,'cancelled');assert.equal(g.db.prepare('SELECT count(*) n FROM bindings').get().n,1);
});

test('aborted second readback cannot reuse the previous successful answer',t=>{
  const f=fixture(t);
  f.ready();
  f.host.claimReadback('接续检查 '+f.ticket.ticket,{sessionId:f.candidate,cwd:path.dirname(f.store)});
  assert.equal(f.status().readback,null);
  assert.throws(()=>f.request(),/候选 Claude/);
});

test('shared direction keeps authorization, recommendation and measurement independently through handoff',t=>{
  const f=fixture(t);
  let rev=1;
  const note=(kind,source,text)=>f.cli('note','--binding','1','--base',String(rev),'--topic','contour',
    '--kind',kind,'--source',source,'--text',text,'--evidence','native turn or measured sample').revision;
  rev=note('用户授权','用户','continue refinement');rev=note('方案建议','Opus','try a smaller tip');
  rev=note('实测事实','Astra','first measurement');rev=note('实测事实','Astra','verified measurement');
  assert.throws(()=>note('用户授权','Opus','model suggestion'));
  const packet=f.cli('prepare','--binding','1','--side','cc','--index',path.join(path.dirname(f.store),'index.sqlite'),
    '--candidate',f.candidate,'--work','continue the comparison');
  assert.deepEqual(packet.notes.filter(n=>n.topic==='contour').map(n=>[n.source,n.kind,n.body]),[
    ['用户','用户授权','continue refinement'],['Opus','方案建议','try a smaller tip'],['Astra','实测事实','verified measurement']]);
  assert.equal(packet.notes.find(n=>n.topic==='colour').source,null);
});

test('focus replaces history in both packets, preserves live constraints, version guards and historical lookup',async t=>{
  const f=fixture(t);let rev=1;
  rev=f.note(rev,'trial A','old test').revision;
  rev=f.note(rev,'trial B','abandoned').revision;
  const focus=base=>f.cli('focus','--binding','1','--base',String(base),'--adopted','released passive variant',
    '--closed','A and B closed','--next-step','mouth only','--keep','1','--evidence','user decision and release report');
  assert.throws(()=>focus(1));
  const selected=focus(rev);rev=selected.revision;
  assert.equal(f.cli('show','--binding','1','--side','desktop').overview.next_step,'mouth only');
  const scope=['prepare','--binding','1','--index',path.join(path.dirname(f.store),'index.sqlite'),'--candidate',f.candidate,'--work','verify mouth'];
  for(const side of ['cc','desktop']){
    const packet=f.cli(...scope,'--side',side);
    assert.equal(packet.overview.adopted,'released passive variant');assert.equal(packet.overview.closed,'A and B closed');
    assert.deepEqual(packet.notes.map(n=>n.topic),['colour']);assert.match(packet.history,/--history --through/);
  }
  f.note(rev,'new evidence','verified after focus');
  const view=f.cli('show','--binding','1','--side','cc');
  assert.deepEqual(view.notes.map(n=>n.topic),['colour','new evidence']);
  const archived=f.cli('show','--binding','1','--side','cc','--history','--through',String(rev));
  assert.deepEqual(archived.notes.map(n=>n.topic),['colour','trial A','trial B']);assert.equal(archived.overviews.length,1);
});

for(const side of ['cc','desktop']) test('silent progress follows an explicit takeover: '+side,async t=>{
  const f=fixture(t,side);
  f.cli('note','--binding','1','--base','1','--topic','delivery','--kind','实测事实','--source','Astra',
    '--text','stage complete','--evidence','workspace result','--next-cc');
  const packet=f.cli('prepare','--binding','1','--side',side,'--index',path.join(path.dirname(f.store),'index.sqlite'),
    '--candidate',f.candidate,'--work','compare the sample');
  const answer='Only compare the delivered sample.\n接续核对 '+packet.ticket;
  if(side==='cc') f.host.claimReadback('接续检查 '+packet.ticket,{sessionId:f.candidate,cwd:path.dirname(f.store)}).finish(answer);
  else f.idx.prepare("INSERT INTO thread_items VALUES(?,'turn','final','agentMessage',?)").run(f.candidate,JSON.stringify({type:'agentMessage',text:answer}));
  f.cli('request',packet.ticket);await f.host.tick();
  const state=f.cli('status',packet.ticket);
  assert.equal(state.state,'committed',state.detail);
  const inherited=f.cli('show','--binding',String(state.successor),'--side','cc','--progress').progress;
  assert.deepEqual(inherited.map(n=>n.state),[side==='cc'?'included':'pending']);
});
