import {createHash} from 'node:crypto';
import {openSync,readSync,closeSync} from 'node:fs';
import {samePath as same} from './worker.mjs';

const finalMarker = id => '接续核对 '+id;
const fail = message => { throw Error(message); };

function fileDigest(file) {
  const fd=openSync(file,'r'), buffer=Buffer.alloc(256*1024), hash=createHash('sha256');
  try {
    let size;
    while ((size=readSync(fd,buffer,0,buffer.length,null))) hash.update(buffer.subarray(0,size));
    return hash.digest('hex');
  } finally {closeSync(fd);}
}

// All route changes run on the existing bridge event loop. The CLI can request a
// change but never retire an address. Only the host that was asked may commit.
export function handoffs({db,desktopDb,sessions,runtime,runRegistry,externalBusy,history,backlog,boot}) {
  const exists = table => !!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table);
  const installed = () => exists('continuity_tickets');
  function basis(t) {
    if (db.prepare('SELECT boot FROM continuity_stage WHERE ticket=?').get(t.id)?.boot!==boot)
      fail('桥已重启或缺少准备凭据，请重新准备');
    const b=db.prepare('SELECT * FROM bindings WHERE id=? AND active=1').get(t.binding_id);
    if (!b) fail('原配对已变更');
    if (db.prepare('SELECT coalesce(max(rev),0) n FROM continuity_notes WHERE root=?').get(t.root).n!==t.revision ||
        db.prepare('SELECT coalesce(max(seq),0) n FROM mail WHERE binding_id=?').get(b.id).n!==t.mail_seq)
      fail('交接后方向或来信已变化，请重新准备');
    if (db.prepare('SELECT 1 FROM bindings WHERE cc=? OR native_cc=? OR desktop=? OR desktop_host=?').get(t.candidate,t.native,t.native,t.candidate))
      fail('候选身份已被配对使用');
    return b;
  }
  function changedSession(id) {
    if (installed()) db.prepare(`UPDATE continuity_tickets SET state='stale',detail='交接准备后出现了新的 Claude 回合，请重新准备'
      WHERE state IN ('prepared','reviewed','requested','blocked') AND (candidate=? OR binding_id IN
      (SELECT id FROM bindings WHERE cc=? AND active=1))`).run(id,id);
  }
  function claimReadback(command,opts,writer) {
    const match = /^接续检查 ([0-9a-f-]{36})$/i.exec(command?.trim?.());
    if (!match || !installed()) return;
    const t = db.prepare("SELECT * FROM continuity_tickets WHERE id=? AND side='cc' AND state IN ('prepared','reviewed','blocked')").get(match[1]);
    const session=sessions.getSessionById(opts.sessionId);
    if (!t || t.candidate!==opts.sessionId || session?.provider_session_id!==t.native ||
        session.isArchived || !same(opts.cwd,JSON.parse(t.packet).project)) fail('候选会话身份或交接编号已改变');
    const b=basis(t);
    const owner=exists('receiver') && db.prepare('SELECT user_id,options FROM receiver WHERE binding_id=?').get(b.id);
    if (owner && (owner.user_id!==writer?.userId ||
        (JSON.parse(owner.options).permissionMode || 'default')!==(opts.permissionMode || 'default')))
      fail('候选与原配对的用户或审批模式不同');
    db.prepare("UPDATE continuity_tickets SET readback=NULL,state='prepared',detail=NULL WHERE id=? AND state IN ('prepared','reviewed','blocked')").run(t.id);
    return {prompt:t.packet,finish(text) {
      // The provider already correlated this successful native turn to the
      // candidate and ticket. Markdown or paraphrasing the ID is immaterial.
      if (!text.trim() || text.trim()===finalMarker(t.id)) return;
      db.prepare("UPDATE continuity_tickets SET readback=?,state='reviewed',detail=NULL WHERE id=? AND state IN ('prepared','reviewed','blocked')")
        .run(text,t.id);
    }};
  }
  function currentCC(id,native,cwd) {
    const s=sessions.getSessionById(id);
    if (s?.provider!=='claude' || s.isArchived || s.provider_session_id!==native || !same(cwd,s.project_path))
      fail('Claude 原生身份、目录或归档状态改变');
    if (runRegistry.isProcessing(id) || runtime.hasBackgroundWork(id)) fail('Claude 仍有运行中的回合');
  }
  function desktopTurn(id,cwd) {
    const s=desktopDb.prepare('SELECT * FROM threads WHERE id=?').get(id);
    if (!s || s.archived || !same(s.cwd,cwd) || !['desktop','vscode'].includes(s.source) ||
        (s.originator && !['codex desktop','codex_work_desktop'].includes(s.originator.toLowerCase())) ||
        s.agent_role || s.agent_path || s.agent_nickname ||
        (s.thread_source && !['user','agent_created_thread'].includes(s.thread_source))) fail('Desktop 身份或目录改变');
    if (backlog(id)!==false) fail('Desktop 原生队列尚有未处理项或无法核对');
    const last=history().prepare('SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1').get(id);
    if (!last || !['completed','interrupted','failed'].includes(last.status) ||
        history().prepare("SELECT 1 FROM thread_turns WHERE thread_id=? AND status='inProgress'").get(id)) fail('Desktop 回合仍在执行或状态未知');
    return last;
  }
  function bridgeClear(b) {
    if(exists('review_requests')) {
      const review=db.prepare("SELECT q.id FROM review_requests r JOIN mail q ON q.id=r.request_id WHERE q.binding_id=? AND q.state<>'cancelled' AND r.conclusion_id IS NULL AND r.closed_reason IS NULL LIMIT 1").get(b.id);
      if(review) fail('评审尚待回传 '+review.id+'，请核对或用 close-review 记录处置');
    }
    const pending=db.prepare("SELECT id,state FROM mail WHERE binding_id=? AND state NOT IN ('returned','cancelled','notified') LIMIT 1").get(b.id);
    if (pending) fail('待处理原信 '+pending.id+' · '+pending.state);
    for (const [table,condition] of [
      ['pairing_notices',"state<>'sent'"],['send_feedback',"state<>'shown'"],
      ['receiver',"introduction IN ('sending','unknown')"],['auto_inboxes','watch_id IS NOT NULL AND expires_ms>unixepoch()*1000'],
      ['turn_handoffs','expires_ms>unixepoch()*1000'],['paired_inboxes','listen_until>=unixepoch()'],
      ['strict_route','1=1']]) {
      if (exists(table) && db.prepare('SELECT 1 FROM '+table+' WHERE binding_id=? AND '+condition).get(b.id))
        fail('交接前需核对 '+table+'，原地址继续有效');
    }
    // A queue acknowledgment may outlive its native item (including a manually
    // removed item). Require an independently matched native user message.
    const queued=db.prepare("SELECT id,body FROM mail WHERE binding_id=? AND source='cc' AND state='notified' AND detail LIKE 'Desktop 原生队列已接收%'").all(b.id);
    for (const m of queued) {
      const ack=history().prepare("SELECT item_json FROM thread_items WHERE thread_id=? AND item_type='userMessage' AND instr(item_json,?)>0").all(b.desktop,m.id);
      const expected='Claude〔'+m.id+'〕\n'+m.body;
      if (!ack.some(({item_json})=>{const x=JSON.parse(item_json);return x.type==='userMessage' && x.content?.length===1 &&
        x.content[0].type==='text' && [expected,expected+'\n',expected+'\r\n'].includes(x.content[0].text);}))
        fail('原生队列回执缺少目标历史证据 '+m.id);
    }
  }
  const commit=db.transaction(t=>{
    if (!db.prepare("SELECT 1 FROM continuity_tickets WHERE id=? AND state='requested'").get(t.id)) return;
    if (t.boot!==boot || !db.prepare('SELECT 1 WHERE unixepoch()<=?').get(t.expires)) fail('接管申请已过期或桥已重启，请重新核对');
    const b=basis(t);
    currentCC(b.cc,b.native_cc,b.cc_cwd);
    const last=desktopTurn(b.desktop,b.desktop_cwd);
    if (!Number.isInteger(last.completed_at) || last.completed_at>=t.prepared_at)
      fail('Desktop 在交接准备后有活动，或缺少完成时间，请重新准备');
    bridgeClear(b);
    for (const n of JSON.parse(t.packet).notes)
      if (n.path && fileDigest(n.path)!==n.digest) fail('样品已改变，请重新核对 '+n.path);
    let readback=t.readback;
    if (t.side==='cc') {
      currentCC(t.candidate,t.native,b.cc_cwd);
      if (!readback) fail('缺少候选 Claude 原生核对答复');
    } else {
      const turn=desktopTurn(t.native,b.desktop_cwd);
      const item=history().prepare("SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=? AND item_type='agentMessage'").get(t.native,turn.turn_id,turn.final_agent_item_id);
      readback=item && JSON.parse(item.item_json).text;
      if (turn.status!=='completed' || !readback?.trimEnd().endsWith(finalMarker(t.id)) ||
          readback.trim()===finalMarker(t.id)) fail('候选 Desktop 最后完成的原生回合缺少本次核对答复');
    }
    if (db.prepare('SELECT literal FROM continuity_checks WHERE ticket=?').all(t.id)
      .some(({literal})=>!readback.normalize('NFKC').includes(literal.normalize('NFKC'))))
      fail('候选答复与预先登记的客观证据不符，原配对保留');
    db.prepare('UPDATE bindings SET active=0 WHERE id=?').run(b.id);
    const next={...b};
    if (t.side==='cc') Object.assign(next,{cc:t.candidate,native_cc:t.native,cc_title:t.title});
    else Object.assign(next,{desktop:t.native,desktop_host:t.candidate,desktop_title:t.title});
    const id=db.prepare('INSERT INTO bindings(cc,native_cc,cc_title,cc_cwd,desktop,desktop_host,desktop_title,desktop_cwd) VALUES(@cc,@native_cc,@cc_title,@cc_cwd,@desktop,@desktop_host,@desktop_title,@desktop_cwd)').run(next).lastInsertRowid;
    if (!db.prepare('SELECT 1 FROM auto_inboxes WHERE binding_id=?').get(b.id)) db.prepare('DELETE FROM auto_inboxes WHERE binding_id=?').run(id);
    if (exists('paired_inboxes') && db.prepare('SELECT 1 FROM paired_inboxes WHERE binding_id=?').get(b.id))
      db.prepare('INSERT INTO paired_inboxes(binding_id) VALUES(?)').run(id);
    if (exists('receiver'))
      db.prepare('INSERT INTO receiver(binding_id,user_id,options) SELECT ?,user_id,options FROM receiver WHERE binding_id=?').run(id,b.id);
    db.prepare('INSERT INTO continuity_links VALUES(?,?)').run(id,t.root);
    if(t.side==='cc' && exists('continuity_cc_progress')) {
      const inherited=JSON.parse(t.packet).notes.map(n=>n.rev);
      db.prepare("UPDATE continuity_cc_progress SET state='included' WHERE state='pending' AND rev IN (SELECT value FROM json_each(?))").run(JSON.stringify(inherited));
    }
    db.prepare("UPDATE continuity_tickets SET state='committed',successor=?,readback=?,detail=NULL WHERE id=?").run(id,readback,t.id);
  });
  let checking=false;
  async function tick() {
    if (checking || !installed()) return;
    const t=db.prepare("SELECT * FROM continuity_tickets WHERE state='requested' ORDER BY rowid LIMIT 1").get();
    if (!t) return;
    checking=true;
    try {
      const b=db.prepare('SELECT cc,native_cc FROM bindings WHERE id=?').get(t.binding_id);
      for (const id of new Set([b.cc,b.native_cc,...(t.side==='cc'?[t.candidate,t.native]:[])]))
        if (await externalBusy(id)) fail('Claude 有外部运行');
      commit.immediate(t);
    } catch(error) {
      db.prepare("UPDATE continuity_tickets SET state='blocked',detail=? WHERE id=? AND state='requested'").run(error.message,t.id);
    } finally {checking=false;}
  }
  function notice(id) {
    if (!installed()) return '';
    const t=db.prepare("SELECT binding_id,revision,side,packet FROM continuity_tickets WHERE successor=?").get(id);
    return t ? '\n接管已完成。原配对 #'+t.binding_id+' 的执行归属已转至 #'+id+'。方向版本 '+t.revision+'。未完工作：'+JSON.parse(t.packet).work : '';
  }
  function retiredOwner(cc) {
    if (!installed()) return;
    return db.prepare("SELECT t.successor FROM continuity_tickets t JOIN bindings b ON b.id=t.binding_id WHERE b.cc=? AND t.side='cc' AND t.state='committed' ORDER BY t.rowid DESC LIMIT 1").get(cc)?.successor;
  }
  return {tick,claimReadback,changedSession,notice,retiredOwner};
}
