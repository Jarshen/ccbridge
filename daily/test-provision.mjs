// Use the installed CloudCLI Electron runtime with ELECTRON_RUN_AS_NODE=1.
// Only the provider answer is scripted. All three databases and profiles are disposable.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import test, {after} from 'node:test';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {installProvisioner} from './provision.mjs';
import {installWorker} from './worker.mjs';
import {fromPanel} from './panel-draft.mjs';
import {desktopRequests,createDesktop,cancelDesktop} from './desktop-request.mjs';

const root = process.env.DAILY_CLOUDCLI_ROOT ?? path.join(os.homedir(), '.cloudcli/server/1.37.9');
const daily = path.dirname(fileURLToPath(import.meta.url));
const Database = createRequire(path.join(root, 'package.json'))('better-sqlite3');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'daily-provision-'));
Object.assign(process.env, {HOME:temp, USERPROFILE:temp, CODEX_HOME:path.join(temp,'codex'),
  CLAUDE_CONFIG_DIR:path.join(temp,'claude'), DATABASE_PATH:path.join(temp,'cloud.sqlite3')});
fs.closeSync(fs.openSync(process.env.DATABASE_PATH, 'wx')); // Prevent legacy DB migration.
const load = file => import(pathToFileURL(path.join(root, 'dist-server/server/modules', file)));
const native = await load('database/index.js');
await native.initializeDatabase();
const cloudDb = native.getConnection();
const {sessionsDb, userDb} = native;
userDb.createUser('inactive fixture', 'fixture'); // The sole active account will not have ID 1.
const {runDetachedChatTurn:detached} = await load('websocket/index.js');
after(() => { native.closeConnection(); fs.rmSync(temp, {recursive:true, force:true}); });

function fixture(t) {
  cloudDb.exec('UPDATE users SET is_active=0');
  const userId = userDb.createUser(randomUUID(), 'fixture').id;
  const thread = randomUUID(), id = randomUUID(), nativeId = randomUUID();
  const store = path.join(temp, id + '.db');
  const desktopDb = new Database(':memory:');
  const db = new Database(store);
  t.after(() => {db.close(); desktopDb.close();});
  desktopDb.exec('CREATE TABLE threads(id TEXT PRIMARY KEY, cwd, title, archived, source, originator, thread_source, agent_path)');
  desktopDb.prepare("INSERT INTO threads VALUES(?,?,'selected',0,'vscode','Codex Desktop','user',NULL)")
    .run(thread,path.toNamespacedPath(temp) + '\\');
  function py(code) {
    return execFileSync(process.env.DAILY_PYTHON ?? 'python', ['-B','-X','utf8','-c',
      'import sys,json;from provision import *;p,d,i=sys.argv[1:];' + code,store,thread,id],
      {cwd:daily,encoding:'utf8',windowsHide:true});
  }
  py('Store(p)');
  const submit = () => JSON.parse(py("print(json.dumps(request_for_desktop(p,d,i,'fixture title')))"));
  const status = () => JSON.parse(py('print(json.dumps(get_request(p,i)))'));
  const calls = [];
  const runtime = {hasRuntime: p => p === 'claude', async run(provider, cmd, opts, writer) {
    calls.push(opts.sessionId);
    assert.equal(provider, 'claude');
    assert.equal(writer.userId, userId);
    assert.equal(opts.permissionMode, 'default');
    assert.equal(opts.cwd, temp);
    assert.ok(!cmd.includes('fixture title'));
    assert.equal(sessionsDb.getSessionById(opts.sessionId).provider_session_id,null);
    writer.setSessionId(nativeId);
    writer.send({provider, kind:'session_created', newSessionId:nativeId, sessionId:nativeId});
    writer.send({provider, kind:'complete', exitCode:0, sessionId:nativeId});
  }};
  installWorker({db,desktopDb,sessions:sessionsDb,provider:{run:async()=>{}},runtime,detached,
    python:'fixture',storePath:store,queue:()=>assert.fail('no Desktop ack needed')});
  const boot = (overrides={}) => installProvisioner({db,desktopDb,sessionsDb,userDb,cloudDb,detached,runtime,...overrides});
  function competingBinding() {
    return db.prepare("INSERT INTO bindings(cc,native_cc,cc_title,cc_cwd,desktop,desktop_host,desktop_title,desktop_cwd) VALUES(?,?,'existing',?,?,?,'existing',?)")
      .run(randomUUID(),randomUUID(),temp,thread,thread,temp).lastInsertRowid;
  }
  return {db, desktopDb, submit, status, boot, runtime, calls, id, thread, nativeId, userId, competingBinding};
}

test('first native turn binds and seeds receiver before any Desktop introduction', async t => {
  const f = fixture(t), p = f.boot();
  await p.tick(); // Host started before first enqueue.
  const {cc} = f.submit();
  await Promise.all([p.tick(), f.boot().tick()]);
  const row = f.status();
  assert.equal(row.state,'bound',row.detail);
  assert.equal(row.native_cc,f.nativeId);
  assert.notEqual(row.native_cc,cc);
  const receiver = f.db.prepare('SELECT * FROM receiver WHERE binding_id=?').get(row.binding_id);
  assert.equal(receiver.introduction,'queued');
  assert.equal(receiver.user_id,f.userId);
  assert.deepEqual(JSON.parse(receiver.options),{permissionMode:'default'});
  const binding = f.db.prepare('SELECT * FROM bindings WHERE id=?').get(row.binding_id);
  assert.deepEqual([binding.cc,binding.native_cc,binding.desktop,binding.desktop_host,binding.cc_cwd,
    binding.desktop_cwd,binding.cc_title,binding.desktop_title,binding.active],
    [cc,f.nativeId,f.thread,f.thread,temp,temp,'fixture title','selected',1]);
  f.db.prepare('UPDATE bindings SET active=0 WHERE id=?').run(row.binding_id);
  assert.deepEqual(f.submit(),row);
  await f.boot().tick();
  assert.deepEqual(f.calls,[cc]);
});

test('a user-owned Desktop-created chat can provision its own Claude partner',async t=>{
  const f=fixture(t);
  f.desktopDb.prepare("UPDATE threads SET thread_source='agent_created_thread' WHERE id=?").run(f.thread);
  f.submit();
  await f.boot().tick();
  assert.equal(f.status().state,'bound',f.status().detail);
  assert.equal(f.calls.length,1);
});

test('reverse creation correlates a single sent draft across restarts and preserves existing bindings',async t=>{
  const f=fixture(t), cc=randomUUID(), native=randomUUID(), drafts=[];
  const opts={sessionId:cc,cwd:temp,permissionMode:'default'};
  f.desktopDb.exec('ALTER TABLE threads ADD first_user_message; ALTER TABLE threads ADD created_at');
  const sessions={getSessionById:id=>id===cc ? {session_id:cc,provider:'claude',provider_session_id:native,project_path:temp} : null};
  const boot=()=>desktopRequests({...f,sessions,openDraft:url=>drafts.push(new URL(url))});
  const start=()=>boot().handle(createDesktop,opts,f.userId);
  await start();
  const first=drafts[0].searchParams.get('prompt');
  assert.equal(drafts[0].searchParams.get('path'),temp);
  await start();
  assert.equal(drafts.length,1);
  await boot().handle(cancelDesktop,opts,f.userId);
  await start();
  const prompt=drafts[1].searchParams.get('prompt');
  const insert=(text,cwd=temp,origin='Codex Desktop')=>{
    const id=randomUUID();
    f.desktopDb.prepare("INSERT INTO threads(id,title,cwd,archived,source,originator,first_user_message,created_at) VALUES(?,'test',?,0,'vscode',?,?,unixepoch())")
      .run(id,cwd,origin,text);
    return id;
  };
  insert(first);insert(prompt,path.dirname(temp));insert(prompt,temp,'codex_vscode');
  boot().tick();
  assert.equal(f.db.prepare('SELECT count(*) n FROM bindings').get().n,0);
  const target=insert(prompt);
  boot().tick();
  await start();
  boot().tick();
  assert.equal(drafts.length,2);
  assert.deepEqual(f.db.prepare('SELECT cc,desktop,active FROM bindings').all(),[{cc,desktop:target,active:1}]);
});

test('panel draft and CC command share identity, cancellation, deduplication and binding', async t => {
  const f=fixture(t), cc=randomUUID(), native=randomUUID(), drafts=[];
  f.desktopDb.exec('ALTER TABLE threads ADD first_user_message; ALTER TABLE threads ADD created_at');
  sessionsDb.createAppSession(cc,'claude',temp,'panel source');
  cloudDb.prepare('UPDATE sessions SET provider_session_id=? WHERE session_id=?').run(native,cc);
  const openDraft=url=>drafts.push(new URL(url));
  const args={cc,native,cwd:temp,action:'create'};
  const action=(changes={})=>fromPanel({...args,...changes},{...f,cloud:cloudDb,openDraft});
  const bridge=desktopRequests({...f,sessions:sessionsDb,openDraft});
  const opts={sessionId:cc,cwd:temp,permissionMode:'default'};
  await assert.rejects(action({native:randomUUID()}),/原生身份/);
  await action();
  await bridge.handle(createDesktop,opts,f.userId);
  assert.equal(drafts.length,1);
  const pending=()=>f.db.prepare("SELECT * FROM desktop_requests WHERE cc=? AND state='pending'").get(cc);
  assert.equal(pending().detail,'已请求打开 Desktop 草稿');
  await assert.rejects(action({action:'cancel',request:randomUUID()}),/已变更/);
  await action({action:'cancel',request:pending().id});
  assert.equal(pending(),undefined);
  await bridge.handle(createDesktop,opts,f.userId);
  await action();
  assert.equal(drafts.length,2);
  const insert=prompt=>f.desktopDb.prepare(`INSERT INTO threads(id,title,cwd,archived,source,originator,
    thread_source,first_user_message,created_at) VALUES(?,'panel target',?,0,'vscode','Codex Desktop','user',?,unixepoch())`)
    .run(randomUUID(),temp,prompt);
  insert(drafts[0].searchParams.get('prompt'));
  bridge.tick();
  assert.ok(pending());
  insert(drafts[1].searchParams.get('prompt'));
  bridge.tick();
  assert.match(await action(),/已有配对/);
  assert.equal(drafts.length,2);
  assert.equal(f.db.prepare('SELECT native_cc FROM bindings WHERE cc=?').get(cc).native_cc,native);
});

for (const [name, mutate] of [
  ['two active users', () => userDb.createUser(randomUUID(),'fixture')],
  ['no active user', () => cloudDb.exec('UPDATE users SET is_active=0')],
  ['archived Desktop', f => f.desktopDb.exec('UPDATE threads SET archived=1')],
  ['agent thread', f => f.desktopDb.exec("UPDATE threads SET agent_path='child'")],
  ['another origin', f => f.desktopDb.exec("UPDATE threads SET originator='codex_vscode'")],
  ['ambiguous cwd', f => f.desktopDb.exec("UPDATE threads SET cwd='E:relative'")],
  ['binding inserted after enqueue', f => f.competingBinding()],
]) {
  test(name + ' stops before CloudCLI creation', async t => {
    const f = fixture(t), {cc} = f.submit();
    mutate(f);
    await f.boot().tick();
    assert.equal(f.status().state,'failed');
    assert.equal(sessionsDb.getSessionById(cc),null);
    assert.deepEqual(f.calls,[]);
  });
}

test('competing binding wins during first turn and overlapping pollers leave it intact', async t => {
  const f = fixture(t), {cc} = f.submit();
  let release;
  const hold = new Promise(resolve => {release=resolve;});
  const pending = f.boot({detached:async (...args) => {const r=await detached(...args); await hold; return r;}}).tick();
  try {
    assert.equal(f.status().state,'starting');
    await f.boot().tick();
    f.competingBinding();
  } finally {release(); await pending;}
  assert.deepEqual(f.calls,[cc]);
  assert.equal(f.status().state,'failed');
  assert.equal(f.status().native_cc,f.nativeId);
  assert.deepEqual(f.db.prepare('SELECT cc_title,active FROM bindings').all(),[{cc_title:'existing',active:1}]);
  assert.equal(f.db.prepare('SELECT count(*) n FROM receiver').get().n,0);
});

test('receiver insertion failure rolls back binding and request completion', async t => {
  const f = fixture(t);
  f.submit();
  f.db.exec("CREATE TRIGGER refuse BEFORE INSERT ON receiver BEGIN SELECT RAISE(ABORT,'fixture'); END");
  await f.boot().tick();
  assert.equal(f.status().state,'failed');
  assert.equal(f.status().binding_id,null);
  assert.equal(f.db.prepare('SELECT count(*) n FROM bindings').get().n,0);
});

for (const scenario of ['lost acknowledgement','changed native mapping','missing completion','cwd changed']) {
  test(scenario + ' after the first turn cannot publish a binding or run again', async t => {
    const f = fixture(t), {cc} = f.submit();
    if (scenario === 'missing completion') f.runtime.run = async (_p,_c,opts,writer) => {
      f.calls.push(opts.sessionId);
      writer.setSessionId(f.nativeId);
    };
    await f.boot({detached:async (...args) => {
      const result = await detached(...args);
      if (scenario === 'lost acknowledgement') throw Error('fixture lost acknowledgement');
      if (scenario === 'changed native mapping')
        cloudDb.prepare('UPDATE sessions SET provider_session_id=? WHERE session_id=?').run(randomUUID(),cc);
      if (scenario === 'cwd changed') f.desktopDb.prepare('UPDATE threads SET cwd=?').run(path.dirname(temp));
      return result;
    }}).tick();
    await f.boot().tick();
    assert.equal(f.submit().state,scenario === 'cwd changed' ? 'failed' : 'unknown');
    assert.equal(f.status().native_cc,f.nativeId);
    assert.equal(f.db.prepare('SELECT count(*) n FROM bindings').get().n,0);
    assert.deepEqual(f.calls,[cc]);
  });
}

test('late native write error and crashed host intent are never replayed', async t => {
  const f = fixture(t), {cc} = f.submit();
  const lateError = {...sessionsDb,createAppSession(...args) {
    sessionsDb.createAppSession(...args);
    throw Error('lost acknowledgement');
  }};
  await f.boot({sessionsDb:lateError}).tick();
  assert.equal(f.status().state,'unknown');
  assert.equal(sessionsDb.getSessionById(cc).session_id,cc);
  for (const state of ['creating','starting']) {
    f.db.prepare("UPDATE provision_requests SET state=?, owner_pid=?,owner_boot='earlier boot' WHERE id=?")
      .run(state,process.pid,f.id);
    await f.boot().tick();
    assert.equal(f.submit().state,'unknown');
  }
  assert.deepEqual(f.calls,[]);
});
