// PowerShell, from the workspace root (DAILY_CLOUDCLI_ROOT and DAILY_PYTHON are optional):
// $env:ELECTRON_RUN_AS_NODE='1'
// & "$env:LOCALAPPDATA/Programs/@cloudcli-aicloudcli-desktop/CloudCLI.exe" --test daily/test-worker.mjs
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {installWorker} from './worker.mjs';
import {queueCli} from './codex-cli.mjs';

test('Desktop installation resolves even when Explorer PATH contains no Codex CLI', async () => {
  const oldPath=process.env.PATH;
  try {
    process.env.PATH=path.join(process.env.SystemRoot,'System32');
    const file=await queueCli();
    assert.ok(path.isAbsolute(file));
    assert.equal(path.basename(file).toLowerCase(),'codex.exe');
  } finally {process.env.PATH=oldPath;}
});

const daily = path.dirname(fileURLToPath(import.meta.url));
const root = process.env.DAILY_CLOUDCLI_ROOT ?? path.join(
  process.env.CLOUDCLI_SERVER_DIR ?? path.join(os.homedir(), '.cloudcli', 'server'), '1.37.9');
const Database = createRequire(path.join(root, 'package.json'))('better-sqlite3');
const python = process.env.DAILY_PYTHON ?? 'python';
const packet = (text,mode) => 'ASTRA ' + JSON.stringify({text,mode});
const reply = (id,text) => 'ASTRA-REPLY '+JSON.stringify({id,text});
const canonicalReply = (reply_to,text) => 'ASTRA '+JSON.stringify({text,reply_to});
function envelope(prompt) {
  const notice=/^Claude〔([\da-f-]{36})〕\n([\s\S]*)$/.exec(prompt);
  if(notice) return {id:notice[1],text:notice[2],reply_mode:'none'};
  return JSON.parse(prompt.split('\n')[1]);
}
function checkReply(prompt,original,from,to,text) {
  const wire=envelope(prompt);
  assert.match(wire.id,/^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(wire.id,original);
  assert.deepEqual(wire,{kind:'reply',id:wire.id,reply_to:original,reply_mode:'none',from,to,text});
  return wire.id;
}

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'daily-worker-'));
  const storePath = path.join(dir, 'bridge.sqlite3');
  const rows = new Map();
  const desktopDb = new Database(':memory:');
  let db;
  t.after(() => {
    db?.close();
    desktopDb.close();
    fs.rmSync(dir, {recursive: true, force: true});
  });
  desktopDb.exec('CREATE TABLE threads(id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER)');

  function store(method, ...args) {
    const script = 'import json,sys; from storage import Store; ' +
      'print(json.dumps(getattr(Store(sys.argv[1]),sys.argv[2])(*json.load(sys.stdin))))';
    return JSON.parse(execFileSync(python, ['-B', '-X', 'utf8', '-c', script, storePath, method],
      {cwd: daily, input: JSON.stringify(args), encoding: 'utf8', windowsHide: true}));
  }
  function session(bound = true, legacy = !db) {
    const cc = randomUUID(), desktop = randomUUID();
    const cwd = path.join(dir, cc), desktopCwd = path.join(dir, desktop);
    const row = {provider: 'claude', provider_session_id: randomUUID(), project_path: cwd, isArchived: false};
    rows.set(cc, row);
    if (bound) {
      store('bind', {id: cc, native_id: row.provider_session_id, title: 'CC', cwd},
        {id: randomUUID(), native_id: desktop, title: 'Desktop', cwd: desktopCwd});
      desktopDb.prepare('INSERT INTO threads VALUES(?,?,0)').run(desktop, desktopCwd);
      if(legacy) {
        // Model a binding already present before the pairing-notices migration.
        const old=new Database(storePath);
        try {old.prepare('DELETE FROM pairing_notices WHERE binding_id=(SELECT id FROM bindings WHERE cc=?)').run(cc);}
        finally {old.close();}
      }
    }
    return {cc, desktop, cwd};
  }
  const pair = session();
  function boot(transport={}) {
    db?.close();
    db = new Database(storePath, {fileMustExist: true});
    const processing = new Set(), background = new Set();
    const h = {
      processing, background, calls: [], requests: [], queued: [], diagnostics: [],
      answer: 'synthetic answer', queue: async () => 'synthetic ack',
    };
    const query = request => request;
    const provider = {
      async run(command, options, writer, context) {
        const request = (context.createQuery ?? query)({prompt: command, options: {systemPrompt: 'fixture'}});
        h.calls.push({command, options, writer, system: request.options.systemPrompt});
        for(const event of h.events ?? [{kind:'text', role:'assistant', content:h.answer}]) writer.send(event);
        h.beforeComplete?.();
        writer.send({kind: 'complete', exitCode: h.exitCode ?? 0, aborted: h.aborted ?? false});
        for(const event of h.afterComplete ?? []) writer.send(event);
      },
    };
    const registry = new Map([['claude', provider]]);
    const runtime = {
      run: (name, cmd, opts, writer) => registry.get(name).run(cmd, opts, writer, {}),
      hasBackgroundWork: id => background.has(id),
    };
    // Like dispatchRun, reserve the app session and pass the same per-run writer
    // through the supplied runtime (including the worker's detached observer).
    async function dispatch(input, dispatchRuntime) {
      const {sessionId, options, content, userId} = input;
      if (processing.has(sessionId)) return {started: false, error: 'A run was already in progress for this session.'};
      processing.add(sessionId);
      const row = rows.get(sessionId);
      const writer = {userId, events: [], send(event) { this.events.push(event); }};
      try {
        await dispatchRuntime.run(row.provider, content,
          {sessionId, cwd: row.project_path, ...options}, writer);
        return {started: true};
      } finally {
        processing.delete(sessionId);
      }
    }
    const worker = installWorker({db, desktopDb, storePath, python, provider, runtime, query,...transport,
      sessions: {getSessionById: id => rows.get(id)}, runRegistry: {isProcessing: id => processing.has(id)},
      notify: message=>h.diagnostics.push(message),
      detached: (input, deps) => {
        h.requests.push(input);
        return dispatch(input, deps.runtime);
      },
      queue: (thread, prompt) => {
        h.queued.push({thread, prompt});
        return h.queue(thread, prompt);
      },
    });
    h.turn = (session, content, options = {}, userId = 17) =>
      dispatch({sessionId: session.cc, content, options, userId}, runtime);
    h.tick = worker.tick;
    return h;
  }
  return {pair, session, store, boot, storePath, get db(){return db;}, mail: () => db.prepare('SELECT * FROM mail ORDER BY seq').all()};
}

test('real hook and take compete with the worker, preserving the lane and the expiry deadline',async t=>{
  const f=fixture(t), turn=randomUUID(), h=f.boot({nativeBacklog:()=>false});
  const env={...process.env,CODEX_THREAD_ID:f.pair.desktop};
  function inbox(args,event) {
    return JSON.parse(execFileSync(python,['-B','inbox.py','--store',f.storePath,...args],
      {cwd:daily,encoding:'utf8',env,input:event && JSON.stringify(event),windowsHide:true}));
  }
  const hook=name=>inbox(['tool-hook'],{session_id:f.pair.desktop,turn_id:turn,hook_event_name:name});
  await h.turn(f.pair,'prepare'); await h.tick(); h.queued.length=0;
  hook('PreToolUse');
  for (const body of ['first private body','second private body']) await h.turn(f.pair,'发给 Astra：'+body);
  await h.tick(); await h.tick();
  assert.equal(h.queued.length,0);
  const notice=hook('PostToolUse').hookSpecificOutput.additionalContext;
  assert.ok(!notice.includes('private body'));
  const [first,second]=f.mail();
  assert.ok(notice.includes(first.id) && !notice.includes(second.id));
  const receipt=notice.match(/--receipt '([a-f0-9]{32})'/)[1];
  const take=(mail,token)=>inbox(['take','--binding',String(mail.binding_id),'--id',mail.id,'--receipt',token]);
  assert.equal(take(second,receipt).status,'unavailable');
  assert.equal(take(first,'wrong').status,'unavailable');
  assert.equal(take(first,receipt).text,first.body);
  assert.equal(take(first,receipt).status,'unavailable');
  assert.ok(f.mail()[0].detail.includes(turn));
  await h.tick();
  const next=hook('PostToolUse').hookSpecificOutput.additionalContext;
  const other=next.match(/--receipt '([a-f0-9]{32})'/)[1];
  const db=new Database(f.storePath);
  try {db.prepare('UPDATE inbox_offers SET expires_at=unixepoch() WHERE mail_id=?').run(second.id);}
  finally {db.close();}
  assert.equal(take(second,other).status,'unavailable');
  const restarted=f.boot({nativeBacklog:()=>false});
  await restarted.tick();await restarted.tick();
  assert.deepEqual(restarted.queued.map(q=>envelope(q.prompt).id),[second.id]);
});

test('enabled daily inbox holds a real worker packet across restart and once-only take',async t=>{
  const f=fixture(t), h=f.boot(), turn=randomUUID();
  const db=f.db;
  const binding=db.prepare('SELECT id FROM bindings WHERE desktop=?').get(f.pair.desktop).id;
  db.prepare('INSERT INTO paired_inboxes(binding_id,turn,listen_until) VALUES(?,?,unixepoch()+60)').run(binding,turn);
  await h.turn(f.pair,'发给 Astra：daily inbox test');
  await h.tick();
  const [mail]=f.mail();
  assert.equal(mail.state,'unknown');
  assert.equal(db.prepare('SELECT target_turn FROM paired_letters WHERE mail_id=?').get(mail.id).target_turn,turn);
  assert.deepEqual(h.queued,[]);
  const reboot=f.boot();
  await reboot.tick();
  assert.deepEqual(reboot.queued,[]);
  const historyFile=f.storePath+'.history';
  const history=new Database(historyFile);
  history.exec('CREATE TABLE thread_turns(thread_id,turn_id,status,rollout_ordinal)');
  history.prepare("INSERT INTO thread_turns VALUES(?,?,'inProgress',1)").run(f.pair.desktop,turn);
  history.close();
  const inbox=(...args)=>JSON.parse(execFileSync(python,['-B','paired_inbox.py','--history',historyFile,
    '--store',f.storePath,...args,'--turn',turn],{cwd:daily,env:{...process.env,CODEX_THREAD_ID:f.pair.desktop},windowsHide:true}));
  assert.equal(inbox('watch').id,mail.id);
  assert.equal(inbox('take','--id',mail.id).text,'daily inbox test');
  assert.equal(inbox('take','--id',mail.id).status,'unavailable');
  assert.equal(f.db.prepare('SELECT count(*) AS n FROM paired_submissions').get().n,0);
});

test('CC output keeps the listening turn observed before generation',async t=>{
  const f=fixture(t), h=f.boot(), oldTurn=randomUUID(), nextTurn=randomUUID();
  const binding=f.db.prepare('SELECT id FROM bindings WHERE desktop=?').get(f.pair.desktop).id;
  f.db.prepare('INSERT INTO paired_inboxes VALUES(?,?,unixepoch()+60)').run(binding,oldTurn);
  h.beforeComplete=()=>f.db.prepare('UPDATE paired_inboxes SET turn=? WHERE binding_id=?').run(nextTurn,binding);
  h.answer=packet('late design opinion','none');
  await h.turn(f.pair,'review this design');
  await h.tick();
  const letter=f.db.prepare('SELECT target_turn,taken_at FROM paired_letters').get();
  assert.deepEqual(letter,{target_turn:oldTurn,taken_at:null});
  assert.deepEqual(h.queued,[]);
});

test('direct address is inert outside the binding and queues exactly once inside it', async t => {
  const f = fixture(t), h = f.boot();
  const unbound = f.session(false);
  h.answer = packet('synthetic assistant echo');
  const command = '@Astra 请核对这个版本';
  await h.turn(unbound, command);
  await h.turn(f.pair, command, {cwd: unbound.cwd});
  assert.deepEqual(f.mail(), []);
  for (const call of h.calls) {
    assert.equal(call.command, command);
    assert.equal(call.system, 'fixture');
  }
  await h.turn(f.pair, command);
  const [mail] = f.mail();
  assert.deepEqual(f.mail().map(m => [m.source, m.body, m.state]), [['cc', '请核对这个版本', 'queued']]);
  await h.tick();
  assert.equal(h.queued.length, 1);
  assert.equal(h.queued[0].thread, f.pair.desktop);
  assert.deepEqual(envelope(h.queued[0].prompt),
    {id:mail.id,reply_mode:'none',text:mail.body});
});

for (const outcome of ['active','earlier queued','wrong cwd','no active turn','lost ack']) {
  test('native steer delivery: '+outcome,async t=>{
    const f=fixture(t);
    const turn=randomUUID();
    const h=f.boot({nativeBacklog:()=>outcome==='earlier queued',async steer(binding,mail,prompt,eligible) {
      assert.equal(envelope(prompt).id,mail.id);
      if(outcome==='no active turn' || !eligible(outcome==='wrong cwd'?binding.cc_cwd:binding.desktop_cwd)) return null;
      if(outcome==='lost ack') throw Error('turn/steer acknowledgement lost');
      return turn;
    }});
    await h.turn(f.pair,'发给 Astra：测试');
    await h.tick();
    const [mail]=f.mail();
    assert.equal(mail.state,outcome==='lost ack'?'unknown':'notified');
    assert.equal(h.queued.length,['active','lost ack'].includes(outcome)?0:1);
    if (outcome==='active') assert.ok(mail.detail.includes(turn));
    const restarted=f.boot();
    await restarted.tick();
    assert.ok(!restarted.queued.some(item=>item.prompt.includes(mail.id)));
  });
}

test('early Desktop receipt survives the queue ack and restart; CC notification cannot forward', async t => {
  const f = fixture(t), {pair} = f;
  const first = f.boot();
  const settings = {model: 'fixture-model', effort: 'high', permissionMode: 'plan',
    toolsSettings: {disallowedTools: ['Bash']}};
  await first.turn(pair, packet('check','required'), {...settings, attachments: ['turn-specific']}, 41);
  const [{id}] = f.mail();
  first.queue = () => {
    f.store('desktop_reply', pair.desktop, id, 'verified');
    return 'late ack';
  };
  await first.tick();
  assert.equal(f.mail()[0].state, 'received');

  const second = f.boot();
  second.answer = packet('must never be forwarded');
  await second.tick();
  assert.deepEqual(f.mail().map(m => [m.id, m.reply, m.state]), [[id, 'verified', 'returned']]);
  assert.equal(second.requests.length, 1);
  const {content, ...dispatch} = second.requests[0];
  assert.deepEqual(dispatch, {sessionId: pair.cc, userId: 41, options: settings, interruptActiveRun: false});
  checkReply(content,id,pair.desktop,pair.cc,'verified');
  assert.equal(second.calls[0].writer.events[0].content, second.answer);
  await second.tick(); // May send the one-time introduction.
  await second.tick();
  assert.equal(second.requests.length, 1);
  assert.equal(f.mail().length, 1);
});

test('batch stays FIFO while a held Desktop submission allows an independent pair to progress', async t => {
  const f = fixture(t), other = f.session(), h = f.boot();
  h.answer = [packet('one'), packet('two')].join('\n');
  await h.turn(f.pair, 'please review');
  await h.turn(other, '@Astra three');
  assert.deepEqual(f.mail().map(m => m.body), ['one', 'two', 'three']);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  h.queue = thread => thread === f.pair.desktop ? held : 'ack';
  const pending = h.tick();
  const overlap = h.tick();
  try {
    await nextTurn();
    assert.deepEqual(f.mail().map(m => m.state), ['submitting', 'queued', 'notified']);
    assert.equal(h.queued.length, 2);
    assert.deepEqual(new Map(h.queued.map(q => [q.thread, envelope(q.prompt).text])),
      new Map([[f.pair.desktop, 'one'], [other.desktop, 'three']]));
  } finally {
    release('ack');
    await Promise.all([pending, overlap]);
  }
  await h.tick();
  assert.deepEqual(f.mail().map(m => m.state), ['notified', 'notified', 'notified']);
  assert.deepEqual(h.queued.filter(q => q.thread===f.pair.desktop).map(q => envelope(q.prompt).text), ['one','two']);
});

test('one-way delivery survives bridge restart before and after native queue submission', async t => {
  const f=fixture(t), first=f.boot();
  await first.turn(f.pair,'问 Astra：核对库存');
  const [letter]=f.mail();
  const reopened=f.boot();
  await reopened.tick();
  assert.equal(envelope(reopened.queued[0].prompt).id,letter.id);
  assert.equal(reopened.queued[0].thread,f.pair.desktop);
  assert.equal(f.mail()[0].state,'notified');
  const final=f.boot();
  await final.tick();
  assert.equal(final.queued.filter(q=>q.prompt.includes(letter.id)).length,0);
  assert.equal(f.mail().length,1);
});

test('explicit CC delivery is a fresh one-way letter after the task, ordinary prose stays local', async t => {
  const f=fixture(t), h=f.boot();
  await h.turn(f.pair,'setup');
  const send=()=>f.store('desktop_send',f.pair.desktop,'task',randomUUID());
  const original=send();
  h.answer=packet('独立结果');
  await h.tick();
  const [task,result]=f.mail();
  assert.deepEqual([task.id,task.state,task.reply,result.body,result.reply_mode],
    [original,'notified',null,'独立结果','none']);
  assert.notEqual(result.id,original);
  send();
  h.answer='普通进度';
  await h.tick();
  assert.deepEqual(f.mail().map(row=>row.state),['notified','notified','notified']);
  assert.deepEqual(envelope(h.queued.at(-1).prompt),{id:result.id,text:result.body,reply_mode:'none'});
});

test('Desktop mail defers during active and background CC work, then returns once', async t => {
  const f = fixture(t), h = f.boot(), {cc, desktop} = f.pair;
  await h.turn(f.pair, 'remember receiver');
  const id = 'ab071923-beca-4afe-9b82-9a8f4d28e908';
  f.store('desktop_send', desktop, 'question', id, 'required');
  f.store('desktop_send', desktop, 'question', id, 'required');
  for (const busy of [h.processing, h.background]) {
    busy.add(cc);
    await h.tick();
    assert.deepEqual(f.mail().map(m => m.state), ['queued']);
    assert.equal(h.requests.length, 0);
    busy.delete(cc);
  }
  h.answer = packet('literal reply');
  await h.tick();
  assert.deepEqual(f.mail().map(m => [m.state, m.reply]), [['received', h.answer]]);
  h.queue = () => f.store('desktop_confirm', desktop, id);
  await h.tick();
  await h.tick();
  assert.deepEqual(f.mail().map(m => m.state), ['returned']);
  assert.equal(h.requests.length, 1);
  const receipts = h.queued.filter(q => q.prompt.includes(id));
  assert.equal(receipts.length, 1);
  assert.equal(receipts[0].thread, desktop);
  assert.equal(checkReply(receipts[0].prompt,id,cc,desktop,h.answer),'2af80c46-d842-5ec4-82a2-155649dbbeb4');
});

for (const [code, syscall, expected] of [
  ['ENOENT', 'spawn codex.exe', 'failed'],
  ['EACCES', 'spawn codex.exe', 'failed'],
  ['ETIMEDOUT', 'wait', 'unknown'],
]) {
  test(`Desktop queue ${code} is ${expected} and never automatically resubmitted`, async t => {
    const f = fixture(t), h = f.boot();
    await h.turn(f.pair, '@Astra test');
    h.queue = () => { throw Object.assign(new Error(`${syscall} ${code}`), {code, syscall}); };
    await h.tick();
    await h.tick();
    const restarted = f.boot();
    await restarted.tick();
    assert.deepEqual([h.queued.length, restarted.queued.length], [1, 0]);
    assert.deepEqual(f.mail().map(m => [m.state, m.detail]), [[expected, `${syscall} ${code}`]]);
  });
}

test('CC review and evidence reach Desktop FIFO before any answer; a later reply returns only once', async t => {
  const f=fixture(t), h=f.boot(), {pair}=f;
  h.answer=packet('echo must not be queued');
  await h.turn(pair,packet('请分步复核，完成后再回复','deferred'));
  await h.turn(pair,'通知 Astra：第二份证据');
  await h.turn(pair,'通知 Astra：第三份证据');
  const [review,second,third]=f.mail();
  await h.tick();
  await h.tick();
  assert.deepEqual(f.mail().map(m=>[m.reply_mode,m.state,m.reply]),[
    ['deferred','awaiting_reply',null],['none','notified',null],['none','queued',null]]);
  assert.match(f.mail()[1].detail,/原生队列已接收，未确认已读/);
  assert.deepEqual(h.queued.map(q=>envelope(q.prompt).id),[review.id,second.id]);
  assert.match(h.queued[0].prompt,new RegExp('reply --id '+review.id));
  assert.doesNotMatch(h.queued[1].prompt,/ (?:reply|confirm) --id/);
  assert.equal(h.requests.length,0);
  f.store('desktop_reply',pair.desktop,review.id,'正式复核结果');
  h.processing.add(pair.cc);
  await h.tick();
  assert.equal(envelope(h.queued[2].prompt).id,third.id);
  assert.equal(f.mail()[0].state,'received');
  h.processing.delete(pair.cc);
  await h.tick();
  await h.tick();
  assert.equal(h.requests.length,1);
  checkReply(h.requests[0].content,review.id,pair.desktop,pair.cc,'正式复核结果');
  assert.deepEqual(f.mail().map(m=>m.state),['returned','notified','notified']);
});

test('Desktop review, evidence, explicit later CC reply and unconfirmed final queue ack', async t => {
  const f=fixture(t), {pair}=f, other=f.session(), h=f.boot();
  await h.turn(pair,'remember receiver');
  const send=(text,mode)=>f.store('desktop_send',pair.desktop,text,randomUUID(),mode);
  const review=send('核对证据、复现差异并提交正式结论','deferred');
  const evidence=send('第二份证据','none');
  h.answer='已开始核对';
  await h.tick();
  h.answer=reply(review,'不可从通知回合发出');
  await h.tick();
  assert.deepEqual(f.mail().map(m=>[m.state,m.reply]),[['awaiting_reply',null],['notified',null]]);
  assert.match(f.mail()[0].detail,/CC 回合已完成/);
  assert.equal(envelope(h.requests[0].content).id,review);
  assert.equal(h.requests[1].content,'Astra：第二份证据');
  assert.equal(h.queued.filter(q=>q.prompt.includes(review)).length,0);

  const later=f.boot();
  const command='回复 Desktop '+review+'：正式结论';
  await assert.rejects(later.turn(other,command),/当前配对/);
  await assert.rejects(later.turn(pair,'回复 Desktop '+evidence+'：不能回通知'),/未等待/);
  await later.turn(pair,command);
  later.answer=reply(review,'正式结论');
  await later.turn(pair,'重试同一封正式回信');
  await assert.rejects(later.turn(pair,command+'不同'),/已有其他答复/);
  assert.deepEqual(f.mail().map(m=>[m.id,m.state,m.reply]),[
    [review,'received','正式结论'],[evidence,'notified',null]]);
  await later.tick();
  assert.equal(f.mail()[0].state,'submitted');
  const appendix=send('第三份证据','none');
  await later.tick();
  assert.equal(f.mail()[0].state,'submitted');
  assert.equal(f.mail()[2].state,'notified');
  assert.deepEqual(later.requests.map(r=>r.content),['Astra：'+f.mail().find(m=>m.id===appendix).body]);
  f.store('desktop_confirm',pair.desktop,review);
  await later.tick();
  assert.equal(f.mail()[0].state,'returned');
  assert.equal(later.requests.length,1);
  const returns=later.queued.filter(q=>q.prompt.includes(review));
  assert.equal(returns.length,1);
  checkReply(returns[0].prompt,review,pair.cc,pair.desktop,'正式结论');
});

test('detached deferred task may use authorized tools and reply explicitly at successful completion', async t => {
  const f=fixture(t), h=f.boot(), {pair}=f;
  const settings={permissionMode:'default',toolsSettings:{allowedTools:['Read']}};
  await h.turn(pair,'review setup',settings);
  const id=f.store('desktop_send',pair.desktop,'复核用户指定的材料',randomUUID(),'deferred');
  const assistant=content=>({kind:'text',role:'assistant',content});
  h.events=[assistant('已接单'),{kind:'tool_use',toolName:'Read'},{kind:'tool_result'},
    assistant('复核完毕。\n\n'+canonicalReply(id,'核对完成，两份材料一致。'))];
  await h.tick();
  assert.deepEqual(h.requests[0].options,settings);
  assert.match(h.calls[0].command,/沿用用户已有授权/);
  assert.deepEqual(f.mail().map(m=>[m.id,m.state,m.reply]),[[id,'received','核对完成，两份材料一致。']]);
  h.queue=()=>f.store('desktop_confirm',pair.desktop,id);
  await h.tick();
  await h.tick();
  assert.equal(f.mail()[0].state,'returned');
  assert.equal(h.requests.length,1);
  assert.equal(h.queued.filter(q=>q.prompt.includes(id)).length,1);
});

test('intermediate packets, wrong IDs and aborted detached tasks cannot reply; unknown holds the lane', async t => {
  const f=fixture(t), h=f.boot(), {pair}=f;
  await h.turn(pair,'setup');
  const send=()=>f.store('desktop_send',pair.desktop,'复核',randomUUID(),'deferred');
  const first=send(), second=send(), third=send();
  h.events=[{kind:'text',role:'assistant',content:reply(first,'进度')},{kind:'tool_use'},
    {kind:'text',role:'assistant',parentToolUseId:'child',content:reply(first,'子任务结果')}];
  await h.tick();
  h.events=null;
  h.answer=reply(first,'不能替另一封回信');
  await h.tick();
  h.aborted=true;
  h.answer=reply(third,'中止');
  await h.tick();
  const fourth=send();
  const restart=f.boot();
  await restart.tick();
  assert.deepEqual(f.mail().map(m=>[m.id,m.state,m.reply]),[
    [first,'awaiting_reply',null],[second,'awaiting_reply',null],[third,'unknown',null],[fourth,'queued',null]]);
  assert.equal(restart.requests.length,0);
  assert.equal(h.queued.filter(q=>q.prompt.includes(first)).length,0);
});

test('ASTRA JSON modes and direct default preserve FIFO; invalid batch is atomic',async t=>{
  const f=fixture(t), h=f.boot();
  h.answer=[packet('review','deferred'),'ASTRA {"reply_mode":"none","text":"evidence"}'].join('\n');
  await h.turn(f.pair,'send review and evidence');
  await h.turn(f.pair,packet('direct JSON','none'));
  await h.turn(f.pair,'问 Astra：结论呢');
  h.answer=[packet('should roll back'),packet('bad','later')].join('\n');
  await assert.rejects(h.turn(f.pair,'send'),/reply_mode/);
  assert.deepEqual(f.mail().map(m=>[m.body,m.reply_mode]),[
    ['review','deferred'],['evidence','none'],['direct JSON','none'],['结论呢','none']]);
  for(let i=0;i<4;i++) await h.tick();
  assert.deepEqual(h.queued.map(q=>envelope(q.prompt).reply_mode),['deferred','none','none','none']);
});

test('early formal Desktop reply wins over deferred queue ack',async t=>{
  const f=fixture(t), h=f.boot(), {pair}=f;
  await h.turn(pair,packet('复核','deferred'));
  await h.turn(pair,'通知 Astra：附件');
  const [review,evidence]=f.mail();
  h.queue=()=>{
    f.store('desktop_reply',pair.desktop,review.id,'已完成');
    return 'late queue ack';
  };
  await h.tick();
  assert.deepEqual(f.mail().map(m=>m.state),['received','queued']);
  h.queue=async ()=>'queue ack';
  await h.tick();
  await h.tick();
  assert.deepEqual(h.queued.map(q=>envelope(q.prompt).id),[review.id,evidence.id]);
  assert.deepEqual(f.mail().map(m=>m.state),['returned','notified']);
});

test('normal assistant prose can end in one atomic block of new mail and formal replies',async t=>{
  const f=fixture(t), h=f.boot(), {pair}=f;
  await h.turn(pair,'setup');
  const first=f.store('desktop_send',pair.desktop,'review A',randomUUID(),'deferred');
  const second=f.store('desktop_send',pair.desktop,'review B',randomUUID(),'deferred');
  await h.tick();
  await h.tick();
  const block=[
    canonicalReply(first.toUpperCase(),'review A done'),
    reply(second,'review B done'),
    'ASTRA {"text":"evidence","reply_to":null,"reply_mode":"none"}',
  ];
  h.answer=['Example in code:', '```json',packet('example'),'```','Reviews complete.','',...block,''].join('\r\n');
  await h.turn(pair,'finish reviews');
  assert.deepEqual(f.mail().map(m=>[m.state,m.reply,m.body]),[
    ['received','review A done','review A'],['received','review B done','review B'],['queued',null,'evidence']]);
  await h.turn(pair,block[0]); // An explicit user retry also suppresses the assistant's echo.
  assert.equal(f.mail().length,3);
});

test('only unquoted contiguous assistant tail packets or whole user packet commands send',async t=>{
  const f=fixture(t), h=f.boot(), p=packet('example');
  for(const example of [`inline ${p}`,`> ${p}`,`    ${p}`,`\t${p}`,
    `${p}\nmore prose`,`~~~/example\n${p}\n~~~`,'prose\n\n    '+p,
    ['````js','```',p].join('\n'),[p,'separate blocks',p].join('\n')]) {
    h.answer=example;
    await h.turn(f.pair,'show syntax');
  }
  h.answer='OK';
  for(const pasted of ['Example:\n\n'+p,'    '+p,['```',p,'```'].join('\n')])
    await h.turn(f.pair,pasted);
  const assistant=content=>({kind:'text',role:'assistant',content});
  for(const event of [{kind:'tool_result'},{kind:'thinking'},{kind:'error'},{kind:'stream_delta'},assistant('ordinary final'),
    {...assistant(p),role:'user'},{...assistant(p),isLocalCommandStdout:true},
    {...assistant(p),isCompactSummary:true}]) {
    h.events=[assistant(p),event];
    await h.turn(f.pair,'inspect');
  }
  h.events=[{...assistant(p),parentToolUseId:'child'}];
  await h.turn(f.pair,'inspect');
  h.events=[{...assistant('```json'),id:'answer_0'},{...assistant(p),id:'answer_1'}];
  await h.turn(f.pair,'inspect a multipart code example');
  h.events=[assistant(p)];
  h.aborted=true;
  h.afterComplete=[{kind:'complete',exitCode:0}];
  await h.turn(f.pair,'abort');
  h.aborted=false;
  h.exitCode=1;
  await h.turn(f.pair,'failure');
  h.exitCode=0;
  h.events=[assistant('final')];
  h.afterComplete=[assistant(p)];
  await h.turn(f.pair,'background after completion');
  assert.deepEqual(f.mail(),[]);
});

test('stale explicit replies identify the original in chat and roll back the whole batch',async t=>{
  const f=fixture(t), h=f.boot(), {pair}=f;
  await h.turn(pair,'setup');
  const send=()=>f.store('desktop_send',pair.desktop,'review',randomUUID(),'deferred');
  const open=send(), closed=send();
  await h.tick();
  await h.tick();
  await h.turn(pair,canonicalReply(closed,'recorded'));
  f.store('reconcile',closed,'received','operator closure');
  h.answer=['Summary','',packet('must roll back'),reply(open,'must roll back'),canonicalReply(closed,'recorded')].join('\n');
  await assert.rejects(h.turn(pair,'finish'),/cancelled/);
  const events=h.calls.at(-1).writer.events;
  assert.equal(events.at(-2).kind,'error');
  assert.match(events.at(-2).content,new RegExp(closed+'.*cancelled'));
  assert.deepEqual(f.mail().map(m=>[m.state,m.reply]),[['awaiting_reply',null],['cancelled','recorded']]);
  for(const invalid of ['ASTRA {"reply_to":"bad","text":"wrong"}','ASTRA {"text":"bad json"',
    'ASTRA {"id":"typo","text":"not new mail"}']) {
    h.answer=['Summary','',reply(open,'must roll back'),invalid].join('\n');
    await assert.rejects(h.turn(pair,'finish'));
  }
  f.store('reconcile',open,'awaiting_reply','operator closure');
  h.answer='Summary\n\n'+canonicalReply(open,'stale');
  h.beforeComplete=()=>f.store('unbind',f.mail()[0].binding_id);
  await assert.rejects(h.turn(pair,'finish'),/配对已变/);
  assert.match(h.calls.at(-1).writer.events.at(-2).content,new RegExp(open));
  assert.deepEqual(f.mail().map(m=>[m.state,m.reply]),[['cancelled',null],['cancelled','recorded']]);
});

test('detached deferred delivery rejects a mixed batch and reports cancellation during the turn',async t=>{
  const f=fixture(t), h=f.boot(), {pair}=f;
  await h.turn(pair,'setup');
  const send=()=>f.store('desktop_send',pair.desktop,'review',randomUUID(),'deferred');
  const first=send(), second=send();
  h.answer=['Review complete','',packet('new mail'),canonicalReply(first,'result')].join('\n');
  await h.tick();
  assert.match(f.mail()[0].detail,/只接受对原信的一封回信/);
  h.beforeComplete=()=>f.store('reconcile',second,'submitting','closed by operator');
  h.answer='Review complete\n\n'+canonicalReply(second,'stale');
  await h.tick();
  assert.match(h.diagnostics.at(-1),new RegExp(second+'.*未投递'));
  assert.deepEqual(f.mail().map(m=>[m.state,m.reply]),[['awaiting_reply',null],['cancelled',null]]);
  assert.match(f.mail()[1].detail,/closed by operator/);
});

test('first inbound briefing survives restart and preserves multiline letters verbatim',async t=>{
  const f=fixture(t), h=f.boot(), {pair}=f;
  const binding=f.db.prepare('SELECT id FROM bindings WHERE desktop=?').get(pair.desktop).id;
  // Provisioning seeds the receiver without a user-initiated CC turn.
  f.db.prepare("INSERT INTO receiver(binding_id,user_id,options,introduction) VALUES(?,1,'{}','sent')").run(binding);
  const body='请看这段代码：\n  {"name":"例子"}\n第二段。';
  f.store('desktop_send',pair.desktop,body,randomUUID(),'none');
  await h.tick();
  assert.ok(h.requests[0].content.startsWith('Astra：'+body+'\n\n配对约定：'));
  assert.equal(f.db.prepare('SELECT count(*) n FROM cc_briefings').get().n,1);
  const next=f.boot();
  f.store('desktop_send',pair.desktop,body,randomUUID(),'none');
  await next.tick();
  assert.equal(next.requests[0].content,'Astra：'+body);
  assert.equal(next.calls[0].system,'fixture');
});

test('a newly committed pair gets the project roles once on both sides, before ordinary mail',async t=>{
  const f=fixture(t), pair=f.session(true,false), h=f.boot({defaultUserId:()=>17});
  const roles=fs.readFileSync(path.join(daily,'pairing-brief.md'),'utf8').trimEnd();
  const binding=f.db.prepare('SELECT * FROM bindings WHERE cc=?').get(pair.cc);
  const reselect=()=>f.store('bind',{id:pair.cc,native_id:binding.native_cc,cwd:pair.cwd,title:'CC'},
    {id:binding.desktop_host,native_id:pair.desktop,cwd:binding.desktop_cwd,title:'Desktop'});
  assert.equal(reselect(),binding.id);
  h.processing.add(pair.cc);
  await Promise.all([h.tick(),h.tick()]);
  assert.equal(h.queued.length,1);
  assert.equal(h.queued[0].thread,pair.desktop);
  assert.ok(h.queued[0].prompt.startsWith(roles+'\n\n'));
  assert.equal(h.requests.length,0);
  const next=f.boot({defaultUserId:()=>17});
  next.answer=packet('a welcome is not an outgoing letter');
  await next.tick();
  assert.equal(next.requests.length,1);
  assert.equal(next.requests[0].sessionId,pair.cc);
  assert.ok(next.requests[0].content.startsWith(roles+'\n\n'));
  assert.equal(next.requests[0].options.permissionMode,'default');
  assert.equal(next.queued.length,0);
  assert.equal(f.mail().length,0);
  assert.equal(reselect(),binding.id);
  const again=f.boot();
  await again.tick();
  f.store('desktop_send',pair.desktop,'review',randomUUID(),'none');
  await again.tick();
  assert.deepEqual(again.requests.map(r=>r.content),['Astra：review']);
  assert.equal(again.queued.length,0);
  assert.deepEqual(f.db.prepare('SELECT recipient,state FROM pairing_notices ORDER BY recipient').all(),
    [{recipient:'cc',state:'sent'},{recipient:'desktop',state:'sent'}]);
});

test('uncertain welcome acknowledgements are never replayed on restart',async t=>{
  const f=fixture(t), h=f.boot({defaultUserId:()=>17}), pair=f.session();
  h.queue=async()=>{throw Error('missing Desktop receipt');};
  h.aborted=true;
  await h.tick();
  assert.equal(h.requests[0].sessionId,pair.cc);
  const after=f.boot({defaultUserId:()=>17});
  await after.tick();
  assert.deepEqual([after.requests.length,after.queued.length],[0,0]);
  assert.equal(f.db.prepare("SELECT count(*) n FROM pairing_notices WHERE state='unknown'").get().n,2);
});

test('pair instructions appear once; ordinary notifications contain only display content and formal replies stay correlated',async t=>{
  const f=fixture(t), h=f.boot(), {pair}=f;
  const action='Please run the regression tests already requested by the user.';
  const fyi='FYI: review notes are available.';
  await h.turn(pair,'setup');
  await h.tick(); // Deliver the one-time introduction before counting mail.
  h.queued.length=0;
  assert.match(h.calls[0].command,/配对约定：.*用户已有授权/);
  assert.equal(h.calls[0].system,'fixture');
  for(const text of [action,fyi]) {
    h.answer='Ordinary assistant answer';
    await h.turn(pair,'通知 Astra：'+text);
    const deskId=f.store('desktop_send',pair.desktop,text,randomUUID(),'none');
    await h.tick();
    const ccNotification=h.requests.at(-1).content, desktopNotification=h.queued.at(-1).prompt;
    assert.equal(ccNotification,'Astra：'+text);
    assert.equal(desktopNotification,'Claude〔'+f.mail().at(-2).id+'〕\n'+text);
    assert.ok(!ccNotification.includes(deskId));
    assert.ok(h.calls.slice(1).every(call=>!call.command.includes('配对约定：') && call.system==='fixture'));
    await h.turn(pair,packet('findings?','required'));
    const ccRequest=f.mail().at(-1).id;
    const deskRequest=f.store('desktop_send',pair.desktop,'findings?',randomUUID(),'required');
    h.answer=text;
    await h.tick();
    assert.equal(envelope(h.requests.at(-1).content).id,deskRequest);
    assert.equal(envelope(h.queued.at(-1).prompt).id,ccRequest);
    f.store('desktop_reply',pair.desktop,ccRequest,text);
    h.answer='Result\n\n'+packet('unwanted echo');
    h.queue=()=>f.store('desktop_confirm',pair.desktop,deskRequest);
    await h.tick();
    const ccReply=h.requests.at(-1).content, desktopReply=h.queued.at(-1).prompt;
    checkReply(ccReply,ccRequest,pair.desktop,pair.cc,text);
    checkReply(desktopReply,deskRequest,pair.cc,pair.desktop,text);
    for(const prompt of [ccReply,desktopReply]) {
      assert.doesNotMatch(prompt,/配对约定：/);
      assert.match(prompt,/无需另发信件/);
    }
    assert.match(desktopReply,new RegExp(' confirm --id '+deskRequest));
    h.queue=async ()=>'ack';
  }
  await h.tick();
  assert.deepEqual(f.mail().map(m=>m.state),Array(2).fill(['notified','notified','returned','returned']).flat());
  assert.deepEqual([h.queued.length,h.requests.length],[6,6]);
});
