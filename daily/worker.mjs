import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {promisify} from 'node:util';
import {fileURLToPath} from 'node:url';
import {queueCli} from './codex-cli.mjs';
import {createDesktop,cancelDesktop} from './desktop-request.mjs';

const exec = promisify(execFile);
const relay = fileURLToPath(new URL('./relay.py',import.meta.url));
const busyError = 'A run was already in progress for this session.';
const quotePS = value => "'" + value.replaceAll("'","''") + "'";
export const samePath = (a,b) => typeof a === 'string' && typeof b === 'string' &&
  path.resolve(a).replace(/^\\\\\?\\UNC\\/i,'\\\\').replace(/^\\\\\?\\/,'').toLowerCase() ===
  path.resolve(b).replace(/^\\\\\?\\UNC\\/i,'\\\\').replace(/^\\\\\?\\/,'').toLowerCase();

export async function nativeQueue(threadId, message) {
  const {stdout} = await exec(await queueCli(),['queue','--thread',threadId,'--message',message],
    {windowsHide:true,timeout:20000,maxBuffer:65536});
  return stdout.trim();
}

const pairRule = '来信沿用用户已有授权；普通通知无需复述或回执。';
const ccBrief = '配对约定：'+pairRule+'需要给 Astra 交付时，在答复末尾每封独占一行：ASTRA {"text":"结果"}。';
const pairingBrief = readFileSync(new URL('./pairing-brief.md',import.meta.url),'utf8').trimEnd();

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
function shortText(text) {
  if(typeof text!=='string' || !text.trim() || Array.from(text).length>2500)
    throw Error('信件正文需要 1–2500 字');
  return text.trim();
}
function replyPacket(id,text) {
  if(typeof id!=='string' || !uuid.test(id)) throw Error('回信需要原信 UUID');
  return {id:id.toLowerCase(),text:shortText(text)};
}

function wireEnvelope(binding,mail,initial,toDesktop) {
  // UUIDv5: use the original mail UUID as namespace and "reply" as the name.
  // One formal reply per row, stable across restarts without a second stored ID.
  let id=mail.id;
  if(!initial) {
    const bytes=createHash('sha1').update(Buffer.from(mail.id.replaceAll('-',''),'hex'))
      .update('reply').digest().subarray(0,16);
    bytes[6]=(bytes[6]&15)|0x50;
    bytes[8]=(bytes[8]&63)|0x80;
    id=bytes.toString('hex').replace(/^(.{8})(.{4})(.{4})(.{4})(.{12})$/,'$1-$2-$3-$4-$5');
  }
  return JSON.stringify({kind:initial?'message':'reply',id,reply_to:initial?null:mail.id,
    reply_mode:initial?mail.reply_mode:'none',from:toDesktop?binding.cc:binding.desktop,
    to:toDesktop?binding.desktop:binding.cc,text:initial?mail.body:mail.reply});
}
function addressed(command) {
  if(typeof command!=='string') return null;
  const input=command.trim();
  const reply=input.match(/^回复\s*Desktop\s+([^\s：:]+)\s*[：:]\s*([\s\S]*)$/i);
  if(reply) return replyPacket(reply[1],reply[2]);
  const match=input.match(/^(?:@(?:Astra|Codex)(?:\s+|[：:]\s*)|(?:请)?(通知|异步问|问问?|发给|转告)\s*(?:Astra|Codex)\s*[：:]\s*)([\s\S]*)$/i);
  if(!match) return null;
  return {mode:'none',text:shortText(match[2])};
}

function outbound(text, {allowProse=false}={}) {
  if(typeof text!=='string') return [];
  const lines=text.trimEnd().split(/\r?\n/);
  const marker=/^ASTRA(?:-REPLY)?(?=[ \t]|$)/;
  if(!marker.test(lines.at(-1))) return [];
  let fence='', start=-1;
  for(const [i,line] of lines.entries()) {
    const delimiter=line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    if(fence) {
      if(delimiter?.[1].startsWith(fence) && !delimiter[2].trim()) fence='';
    } else if(delimiter) {
      fence=delimiter[1];
    } else if(marker.test(line)) {
      if(start<0) start=i;
      continue;
    }
    // Never send a subset of separated packet lines or parse prose in user input.
    if(start>=0 || (!allowProse && line.trim())) return [];
  }
  if(fence || start<0) return [];
  return lines.slice(start).map(line => {
    const prefix=line.match(marker)[0];
    const packet=JSON.parse(line.slice(prefix.length));
    if(!packet || typeof packet!=='object' || Array.isArray(packet)) throw Error('ASTRA 需要 JSON 对象');
    const {mode,reply_mode=mode===undefined ? 'none' : mode}=packet;
    const legacy=prefix==='ASTRA-REPLY';
    const key=legacy?'id':'reply_to';
    if(Object.keys(packet).some(k=>!['text',key,'mode','reply_mode'].includes(k)))
      throw Error('ASTRA 信件字段无效');
    if(legacy || packet.reply_to!=null) {
      if([mode,packet.reply_mode].some(m=>m!==undefined && m!=='none'))
        throw Error('ASTRA 正式回信的 reply_mode 必须为 none');
      return replyPacket(packet[key],packet.text);
    }
    if(mode!==undefined && mode!==reply_mode)
      throw Error('ASTRA mode 与 reply_mode 冲突');
    if(!['required','none','deferred'].includes(reply_mode)) throw Error('ASTRA reply_mode 无效');
    return {mode:reply_mode,text:shortText(packet?.text)};
  });
}

// A provider can keep its writer open for background activity after the terminal event.
function observeTurn() {
  let text='', messageId, settled=false, failed=false;
  return event=>{
    if(settled || event.parentToolUseId) return;
    if(event.kind==='complete') {
      settled=true;
      if(event.exitCode===0 && !event.aborted && !failed) return {text};
    } else if(event.kind==='error') failed=true;
    else if(event.kind==='text' && event.role==='assistant' && typeof event.content==='string'
        && !event.isCompactSummary && !event.isLocalCommandStdout && !event.compact) {
      // CloudCLI emits individual content parts as <message ID>_<part index>.
      const part=event.id?.match(/^(.*)_\d+$/);
      text=part && part[1]===messageId ? text+'\n'+event.content : event.content;
      messageId=part?.[1];
    } else if(['text','tool_use','tool_result','thinking','stream_delta'].includes(event.kind)) {
      text='';
      messageId=undefined;
    }
  };
}

export function installWorker({db, desktopDb, sessions, provider, runtime, runRegistry, detached,
    query, python, storePath, desktopProvision, queue = nativeQueue, steer, nativeBacklog,
    externalBusy = async () => false, defaultUserId = () => null, notify = console.error}) {
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  db.exec(readFileSync(new URL('./pairing-notices.sql',import.meta.url),'utf8'));
  db.exec(`CREATE TABLE IF NOT EXISTS receiver (
    binding_id INTEGER PRIMARY KEY REFERENCES bindings(id), user_id INTEGER NOT NULL, options TEXT NOT NULL,
    introduction TEXT NOT NULL DEFAULT 'queued');
    CREATE TABLE IF NOT EXISTS cc_briefings(binding_id INTEGER PRIMARY KEY REFERENCES bindings(id));
    UPDATE pairing_notices SET state='unknown',detail='服务在投递期间退出，请核对原生会话' WHERE state='sending';
    CREATE TABLE IF NOT EXISTS inbox_leases (
      binding_id INTEGER NOT NULL REFERENCES bindings(id),mail_id TEXT NOT NULL,expires_at INTEGER NOT NULL,
      receipt_token TEXT NOT NULL,PRIMARY KEY(binding_id,mail_id));
    CREATE TABLE IF NOT EXISTS inbox_tool_turns (
      binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),turn_id TEXT NOT NULL,seen_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS inbox_offers (
      mail_id TEXT PRIMARY KEY REFERENCES mail(id), binding_id INTEGER NOT NULL REFERENCES bindings(id),
      turn_id TEXT NOT NULL, expires_at INTEGER NOT NULL, receipt TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS paired_inboxes (
      binding_id INTEGER PRIMARY KEY REFERENCES bindings(id), turn TEXT, listen_until INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS paired_letters (
      mail_id TEXT PRIMARY KEY REFERENCES mail(id), target_turn TEXT, taken_at REAL);
    CREATE TABLE IF NOT EXISTS paired_submissions (mail_id TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS paired_submission AFTER UPDATE OF state ON mail
      WHEN NEW.state='submitting' AND EXISTS (SELECT 1 FROM paired_letters WHERE mail_id=NEW.id)
      BEGIN INSERT INTO paired_submissions VALUES(NEW.id); END;
    UPDATE mail SET state='unknown',detail='服务在提交期间退出，请核对目标聊天' WHERE state IN ('submitting','returning');
    UPDATE receiver SET introduction='unknown' WHERE introduction='sending';`);
  const active = db.prepare('SELECT * FROM bindings WHERE active=1 AND cc=?');
  const getReceiver = db.prepare('SELECT * FROM receiver WHERE binding_id=?');
  // Completed inbound turns from older bridge versions already carried the protocol.
  const briefed = db.prepare(`SELECT 1 FROM pairing_notices WHERE binding_id=@binding AND recipient='cc'
    UNION ALL SELECT 1 FROM cc_briefings WHERE binding_id=@binding
    UNION ALL SELECT 1 FROM mail WHERE binding_id=@binding AND source='desktop'
    AND reply_mode='none' AND state='notified' LIMIT 1`);
  const recordBrief = db.prepare('INSERT OR IGNORE INTO cc_briefings(binding_id) VALUES(?)');
  const recordReceiver = db.prepare(`INSERT INTO receiver(binding_id,user_id,options) VALUES(?,?,?)
    ON CONFLICT(binding_id) DO UPDATE SET user_id=excluded.user_id, options=excluded.options`);
  const addMail = db.prepare("INSERT INTO mail(id,binding_id,source,body,reply_mode,state) VALUES(?,?,'cc',?,?,'queued')");
  const set = db.prepare('UPDATE mail SET state=?,detail=? WHERE id=? AND state=?');
  const stillClaimed=db.prepare('SELECT 1 FROM mail WHERE id=? AND state=?');
  const recentTurn=db.prepare('SELECT turn_id FROM inbox_tool_turns WHERE binding_id=? AND seen_at>unixepoch()-60');
  const offer=db.prepare(`INSERT INTO inbox_offers(mail_id,binding_id,turn_id,expires_at,receipt)
    VALUES(?,?,?,unixepoch()+45,lower(hex(randomblob(16)))) ON CONFLICT(mail_id) DO NOTHING`);
  const held=db.prepare('SELECT 1 FROM inbox_offers WHERE mail_id=? AND turn_id=? AND expires_at>unixepoch()');
  const claimNotification=db.transaction((binding,mail)=>{
    if(!stillClaimed.get(mail.id,'queued')) return false;
    const turn=recentTurn.get(binding.id);
    if(turn && validCC(binding) && validDesktop(binding)) {
      let clear=false;
      try {clear=nativeBacklog(binding.desktop)===false;} catch { /* Normal native delivery remains available. */ }
      if(clear) {
        offer.run(mail.id,binding.id,turn.turn_id);
        if(held.get(mail.id,turn.turn_id)) return false;
      }
    }
    return !!set.run('submitting',null,mail.id,'queued').changes;
  });
  // A bounded in-turn receipt reservation competes on this exact row with native queue submission.
  const reserved=`EXISTS (SELECT 1 FROM inbox_leases l WHERE l.binding_id=mail.binding_id
    AND l.mail_id=mail.id AND l.expires_at>unixepoch())`;
  const claimDesktopReturn=db.prepare(`UPDATE mail SET state='returning',detail=NULL WHERE id=?
    AND state='received' AND source='desktop' AND NOT ${reserved}`);
  const setReply = db.prepare("UPDATE mail SET state='received',reply=?,detail=NULL WHERE id=? AND state='submitting'");
  const laterReply = db.prepare(`UPDATE mail SET state='received',reply=?,detail=NULL WHERE id=? AND binding_id=?
    AND source='desktop' AND reply_mode='deferred' AND state='awaiting_reply' AND reply IS NULL`);
  const priorReply = db.prepare(`SELECT state,reply FROM mail WHERE id=? AND binding_id=? AND source='desktop'
    AND reply_mode='deferred'`);
  const setIntro = db.prepare('UPDATE receiver SET introduction=? WHERE binding_id=? AND introduction=?');
  const getNotice=db.prepare('SELECT state FROM pairing_notices WHERE binding_id=? AND recipient=?');
  const claimNotice=db.prepare(`UPDATE pairing_notices SET state='sending' WHERE binding_id=? AND recipient=?
    AND state='queued' AND EXISTS(SELECT 1 FROM bindings WHERE id=binding_id AND active=1)`);
  const finishNotice=db.prepare(`UPDATE pairing_notices SET state=?,detail=? WHERE binding_id=? AND recipient=? AND state='sending'`);
  const seedReceiver=db.prepare(`INSERT OR IGNORE INTO receiver(binding_id,user_id,options) VALUES(?,?,?)`);
  const helper = '& '+quotePS(python)+' '+quotePS(relay)+' --store '+quotePS(storePath);
  const introduction = b => '给 Claude 发信：'+helper+' send --binding '+b.id+
    ' --id <新UUID> --text <正文>。重试沿用原编号。'+pairRule;

  function validCC(binding) {
    const row = sessions.getSessionById(binding.cc);
    return row?.provider==='claude' && !row.isArchived && row.provider_session_id===binding.native_cc &&
      samePath(row.project_path,binding.cc_cwd);
  }
  function validDesktop(binding) {
    const row = desktopDb.prepare('SELECT cwd,archived FROM threads WHERE id=?').get(binding.desktop);
    return row && !row.archived && samePath(row.cwd,binding.desktop_cwd);
  }
  const inboxRoute=db.prepare('SELECT CASE WHEN listen_until>=unixepoch() THEN turn END AS turn FROM paired_inboxes WHERE binding_id=?');
  const holdLetter=db.prepare('INSERT INTO paired_letters(mail_id,target_turn) VALUES(?,?)');
  const enqueue = db.transaction((binding, packets, route) => {
    if (active.get(binding.cc)?.id!==binding.id)
      throw Error('本回合期间配对已变，信件未投递 '+packets.filter(p=>p.id).map(p=>p.id).join('、'));
    return packets.map(packet=>{
      if(packet.id) {
        if(!laterReply.run(packet.text,packet.id,binding.id).changes) {
          const prior=priorReply.get(packet.id,binding.id);
          if(prior?.reply!==packet.text || !['received','returning','submitted','returned'].includes(prior.state))
            throw Error('回信 '+packet.id+' 未投递：原信不属于当前配对、未等待异步回复或已有其他答复'
              +(prior?'（'+prior.state+'）':''));
        }
        return packet.id;
      }
      const id=randomUUID();
      addMail.run(id,binding.id,packet.text,packet.mode);
      if(packet.mode==='none' && (route || inboxRoute.get(binding.id))) {
        holdLetter.run(id,route?.turn ?? null);
        set.run('unknown','配对收件箱待领取',id,'queued');
      }
      return id;
    });
  });

  // Match only the exact detached writer to suppress re-forwarding an incoming message.
  const detachedWriters = new WeakSet();
  const previous = provider.run.bind(provider);
  provider.run = async (command,opts,writer,context) => {
    if (desktopProvision && !detachedWriters.has(writer) && [createDesktop,cancelDesktop].includes(command?.trim?.())) {
      const status = await desktopProvision.handle(command.trim(),opts,writer.userId);
      return previous('【工作台配对结果】'+status+' 请只向用户转达这项结果。',opts,writer,context);
    }
    const binding = active.get(opts.sessionId);
    if (!binding || !validCC(binding) || !samePath(opts.cwd,binding.cc_cwd) ||
        !Number.isInteger(writer.userId) || detachedWriters.has(writer))
      return previous(command,opts,writer,context);
    const settings = Object.fromEntries(['model','effort','permissionMode','toolsSettings']
      .filter(k=>opts[k]!==undefined).map(k=>[k,opts[k]]));
    recordReceiver.run(binding.id,writer.userId,JSON.stringify(settings));
    const observe=observeTurn();
    const route=inboxRoute.get(binding.id);
    const needsBrief=!briefed.get({binding:binding.id});
    let direct, prompt=command, deliveryError, completed=false;
    const send = writer.send;
    function report(error) {
      send.call(writer,{kind:'error',provider:'claude',sessionId:opts.sessionId,
        content:'配对桥未投递：'+error.message});
    }
    try {
      const packet=addressed(command);
      direct=packet ? [packet] : outbound(command);
      if(direct.length) {
        const ids=enqueue(binding,direct,route);
        prompt=command+'\n\n【本机配对桥状态】'+ids.join('、')+' 已在本机登记。请简短告知用户，入队不代表对方已读。';
      }
      if(needsBrief) prompt+='\n\n'+ccBrief;
    } catch(error) {
      report(error);
      throw error;
    }
    writer.send = function(event) {
      const final=observe(event);
      if(final) completed=true;
      if(final && !direct.length) {
        try {
          const messages=outbound(final.text,{allowProse:true});
          if(messages.length) enqueue(binding,messages,route);
        } catch(error) {
          deliveryError=error;
          report(error);
        }
      }
      return send.call(this,event);
    };
    try {
      const result=await previous(prompt,opts,writer,{...context,createQuery:context.createQuery ?? query});
      if(needsBrief && completed) recordBrief.run(binding.id);
      if(deliveryError) throw deliveryError;
      return result;
    } finally { writer.send=send; }
  };

  async function ccTurn(binding,receiver,prompt) {
    let final;
    const observe=observeTurn();
    const observed=Object.create(runtime);
    observed.run = async (name,cmd,opts,writer)=>{
      detachedWriters.add(writer);
      const send=writer.send;
      writer.send=function(e) {
        final ??= observe(e);
        return send.call(this,e);
      };
      try {return await runtime.run(name,cmd,opts,writer);}
      finally {writer.send=send; detachedWriters.delete(writer);}
    };
    const result=await detached({sessionId:binding.cc,userId:receiver.user_id,content:prompt,
      options:JSON.parse(receiver.options),interruptActiveRun:false},{runtime:observed});
    return {...result,complete:!!final,finalText:final?.text};
  }

  async function deliver(binding,mail,receiver) {
    const initial = mail.state==='queued';
    const claimed = initial?'submitting':'returning';
    const toDesktop = (mail.source==='cc')===initial;
    if(!toDesktop && (!receiver || runRegistry.isProcessing(binding.cc) ||
        runtime.hasBackgroundWork(binding.cc) || await externalBusy(binding.cc))) return 'busy';
    // A rebinding and a claim are serialized by SQLite. Only the winner may send.
    if(toDesktop && initial && mail.reply_mode==='none' && nativeBacklog && !steer) {
      if(!claimNotification.immediate(binding,mail)) return;
    } else {
      const claim=toDesktop && !initial ? claimDesktopReturn.run(mail.id) : set.run(claimed,null,mail.id,mail.state);
      if(!claim.changes) return;
    }
    let invoked=false;
    try {
      if (!validCC(binding) || !validDesktop(binding)) throw Error('已绑定会话归档、项目路径改变或原生 ID 不匹配');
      const notification=initial && mail.reply_mode==='none';
      const envelope=notification ? null : wireEnvelope(binding,mail,initial,toDesktop);
      if(toDesktop) {
        // Keep the native-queue correlation ID for recovery and delivery audits.
        let prompt='Claude〔'+mail.id+'〕\n'+mail.body;
        if(!notification) {
          const replyCommand=helper+' reply --id '+mail.id+' --text <最终答复，最多2500字>。';
          let action;
          if(!initial) action='正式回信。请执行 '+helper+' confirm --id '+mail.id+' 登记收件，无需另发信件。';
          else if(mail.reply_mode==='deferred') action='异步请求。正式答复时执行 '+replyCommand;
          else action='请在本聊天回答，并执行 '+replyCommand;
          prompt='Claude 来信\n'+envelope+'\n'+action;
        }
        invoked=true;
        const turnId=initial && mail.reply_mode==='none' && steer ? await steer(binding,mail,prompt,
          cwd=>samePath(cwd,binding.desktop_cwd) && !!stillClaimed.get(mail.id,claimed) &&
            nativeBacklog(binding.desktop)===false) : null;
        if (turnId) {
          set.run('notified','Desktop 原生引导已接收；turn ID '+turnId,mail.id,claimed);
          return;
        }
        if (steer && !stillClaimed.get(mail.id,claimed)) return;
        const ack=await queue(binding.desktop,prompt);
        let state='submitted';
        if(initial && mail.reply_mode==='deferred') state='awaiting_reply';
        if(initial && mail.reply_mode==='none') state='notified';
        set.run(state,'Desktop 原生队列已接收，未确认已读。'+(ack ?? ''),mail.id,claimed);
      } else {
        let content;
        if(!initial) content='这是配对 Desktop 的正式回信，无需另发信件。';
        else if(mail.reply_mode==='none') content='Astra：'+mail.body;
        else if(mail.reply_mode==='deferred') content='配对 Desktop 异步请求。'
          +'进度只在本地显示。完成后如需正式回信，在最终答复的末行写 ASTRA {"reply_to":"'+mail.id+'","text":"最终答复"}。'
          +'也可由用户稍后输入「回复 Desktop '+mail.id+'：最终答复」。';
        else content='配对 Desktop 来信，请直接用自然语言回答对方的问题，回复由桥递送。';
        invoked=true;
        const route=inboxRoute.get(binding.id);
        if(!notification) content+='\n'+envelope;
        const needsBrief=!briefed.get({binding:binding.id});
        if(needsBrief) content+='\n\n'+ccBrief;
        const r=await ccTurn(binding,receiver,content);
        if(!r.started && r.error===busyError) {set.run(mail.state,'等待 CC 空闲',mail.id,claimed);return 'busy';}
        if(!r.started) {set.run('failed',r.error || 'CC 回合未启动',mail.id,claimed);return;}
        if(r.error || !r.complete) throw Error(r.error || 'CC 回合完成情况未确认');
        if(needsBrief) recordBrief.run(binding.id);
        if(!initial) set.run('returned','CC 回合已完成',mail.id,claimed);
        else if(mail.reply_mode==='none') {
          db.transaction(() => {
            if (!set.run('notified','CC 单向消息回合已完成',mail.id,claimed).changes) return;
            const packets=outbound(r.finalText,{allowProse:true});
            if (packets.length && packets.every(p => p.mode==='none')) enqueue(binding,packets,route);
          }).immediate();
        }
        else if(mail.reply_mode==='deferred') {
          let detail='CC 回合已完成，等待显式回信';
          try {
            const packets=outbound(r.finalText,{allowProse:true});
            if(packets.length) {
              if(packets.length!==1 || packets[0].id!==mail.id)
                throw Error('本回合只接受对原信的一封回信');
              if(!setReply.run(packets[0].text,mail.id).changes)
                throw Error('原信状态已变更');
              return;
            }
          } catch(error) {
            detail+='（'+mail.id+' 未投递：'+error.message+'）';
            notify(detail);
          }
          set.run('awaiting_reply',detail,mail.id,claimed);
        } else setReply.run(shortText(r.finalText),mail.id);
      }
    } catch(error) {
      const spawnRefused=toDesktop && String(error.syscall).startsWith('spawn') &&
        ['ENOENT','EACCES','EPERM','ENOEXEC'].includes(error.code);
      const state=!invoked || spawnRefused || (toDesktop && error.code==='NO_QUEUE_CLI')?'failed':'unknown';
      set.run(state,String(error.message).slice(0,1500),mail.id,claimed);
    }
  }
  const working=new Set();
  async function sendNotice(binding,recipient,receiver) {
    if(!validCC(binding) || !validDesktop(binding)) return;
    if(recipient==='cc') {
      if(runRegistry.isProcessing(binding.cc) || runtime.hasBackgroundWork(binding.cc) || await externalBusy(binding.cc)) return;
      if(!receiver) {
        const userId=defaultUserId();
        if(!Number.isSafeInteger(userId) || userId<1) return;
        const session=sessions.getSessionById(binding.cc);
        const options={permissionMode:'default'};
        if(session.model) options.model=session.model;
        if(session.effort) options.effort=session.effort;
        seedReceiver.run(binding.id,userId,JSON.stringify(options));
        receiver=getReceiver.get(binding.id);
      }
    }
    if(!claimNotice.run(binding.id,recipient).changes) return;
    try {
      if(recipient==='cc') {
        const result=await ccTurn(binding,receiver,pairingBrief+'\n\n'+ccBrief);
        if(!result.started && result.error===busyError) {
          finishNotice.run('queued',null,binding.id,recipient);
          return;
        }
        if(!result.started || result.error || !result.complete) throw Error(result.error || 'Claude 回合完成情况未确认');
      } else {
        await queue(binding.desktop,pairingBrief+'\n\n'+introduction(binding));
      }
      finishNotice.run('sent',null,binding.id,recipient);
      if(recipient==='desktop') setIntro.run('sent',binding.id,'queued');
    } catch(error) {
      finishNotice.run('unknown',String(error.message).slice(0,1500),binding.id,recipient);
      notify('配对分工投递未确认',binding.id,recipient,error.message);
    }
  }
  const head=db.prepare(`SELECT * FROM mail WHERE binding_id=? AND source=?
    AND NOT EXISTS(SELECT 1 FROM paired_letters WHERE mail_id=mail.id)
    AND state NOT IN ('returned','cancelled','notified','awaiting_reply')
    AND NOT (reply_mode='deferred' AND state='submitted')
    AND NOT (? AND reply_mode='deferred' AND state='received')
    AND NOT (source='desktop' AND reply_mode='deferred' AND state='received' AND ${reserved})
    ORDER BY seq LIMIT 1`);
  async function lane(binding,source) {
    const key=binding.id+'/'+source;
    if(working.has(key)) return;
    working.add(key);
    try {
      const receiver=getReceiver.get(binding.id);
      const recipient=source==='cc' ? 'desktop' : 'cc';
      const notice=getNotice.get(binding.id,recipient);
      if(notice?.state==='queued') {
        await sendNotice(binding,recipient,receiver);
        return;
      }
      if(notice?.state==='sending') return;
      const mail=head.get(binding.id,source,0);
      if(!notice && source==='cc' && receiver?.introduction==='queued' && !mail && !inboxRoute.get(binding.id) && validDesktop(binding) &&
         setIntro.run('sending',binding.id,'queued').changes) {
        try {
          await queue(binding.desktop,introduction(binding)+' 这是一次性联通说明，仅需在本聊天确认就绪。');
          setIntro.run('sent',binding.id,'sending');
        } catch(error) {setIntro.run('unknown',binding.id,'sending'); notify('Desktop 配对说明未确认',error.message);}
      } else if(mail && ['queued','received'].includes(mail.state)) {
        const result=await deliver(binding,mail,receiver);
        // A busy CC receiving a deferred answer need not hold up new mail going to Desktop.
        if(result==='busy' && source==='cc' && mail.reply_mode==='deferred' && mail.state==='received') {
          const next=head.get(binding.id,source,1);
          if(next?.state==='queued') await deliver(binding,next,receiver);
        }
      }
    } finally {working.delete(key);}
  }
  const bindings=db.prepare('SELECT * FROM bindings WHERE active=1');
  return {tick:()=>Promise.all(bindings.all().flatMap(b=>['cc','desktop'].map(source=>lane(b,source))))};
}
