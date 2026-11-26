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
const ccBrief = '配对约定：'+pairRule+'给 Astra 发单向信时调用 mcp__daily_pair__send，返回的编号可查回执。也兼容末行 ASTRA {"text":"结果"}。';
const briefRoot = fileURLToPath(new URL('../',import.meta.url));
function readPairingBrief(binding,recipient) {
  const file=recipient==='cc' ? '配对分工-Claude.md' : '配对分工-Astra.md';
  const project=binding[recipient+'_cwd'];
  try {
    return readFileSync(path.join(project,file),'utf8').trim();
  } catch(error) {
    if(error.code!=='ENOENT') throw error;
    return readFileSync(path.join(briefRoot,file),'utf8').trim();
  }
}

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

// A model can leave prose quotation marks unescaped inside a text-only packet.
// Keep JSON escapes (including newlines) and reject ambiguous field boundaries.
export function parseOutboundPacket(raw, allowQuotedProse=false) {
  try {return JSON.parse(raw);}
  catch(error) {
    const body=allowQuotedProse && /^\{\s*"text"\s*:\s*"([\s\S]*)"\s*\}$/.exec(raw.trim());
    if(!body) throw error;
    let repaired=false;
    const escaped=body[1].replace(/\\[\s\S]|"/g,(part,offset,all)=>{
      if(part!=='"') return part;
      if(/^\s*[,}:\]]/.test(all.slice(offset+1))) throw error;
      repaired=true;
      return '\\"';
    });
    if(!repaired) throw error;
    return {text:JSON.parse('"'+escaped+'"')};
  }
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
  return lines.slice(start).map((line,index)=>{
    try {return readOutboundLine(line,allowProse);}
    catch(error) {throw Error('第 '+(index+1)+' 个信封（答复第 '+(start+index+1)+' 行）：'+error.message);}
  });
}

function readOutboundLine(line,allowProse) {
  const prefix=line.startsWith('ASTRA-REPLY')?'ASTRA-REPLY':'ASTRA';
  let packet;
  try {packet=parseOutboundPacket(line.slice(prefix.length),allowProse && prefix==='ASTRA');}
  catch(error) {throw Error('JSON 解析失败：'+error.message);}
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
  return {mode:'none',text:shortText(packet?.text)};
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

export function installWorker({db, desktopDb, sessions, provider, runtime, runRegistry, detached, makeSendServer,
    query, python, storePath, desktopProvision, queue = nativeQueue, steer, nativeBacklog, handoff,
    externalBusy = async () => false, defaultUserId = () => null, notify = console.error}) {
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  db.exec(readFileSync(new URL('./pairing-notices.sql',import.meta.url),'utf8'));
  db.exec(readFileSync(new URL('./auto-delivery.sql',import.meta.url),'utf8'));
  db.exec(readFileSync(new URL('./receipts.sql',import.meta.url),'utf8'));
  db.exec(`CREATE TRIGGER IF NOT EXISTS delivery_failure_feedback AFTER UPDATE OF state ON mail
    WHEN OLD.state IN ('submitting','returning') AND NEW.state IN ('failed','unknown') BEGIN
      INSERT OR IGNORE INTO send_feedback(id,binding_id,sender,related_id,reason)
      VALUES('delivery:'||NEW.id||':'||OLD.state,NEW.binding_id,
        CASE WHEN OLD.state='submitting' THEN NEW.source WHEN NEW.source='cc' THEN 'desktop' ELSE 'cc' END,NEW.id,
        CASE WHEN NEW.state='unknown' THEN '提交结果待核对，请勿重发。' ELSE '投递失败。' END||coalesce(NEW.detail,''));
    END;`);
  db.exec("UPDATE send_feedback SET state='unknown',result='服务在提醒期间退出，请核对原会话' WHERE state='sending'");
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
  db.exec(`CREATE TABLE IF NOT EXISTS cc_output_receipts(
    binding_id INTEGER NOT NULL REFERENCES bindings(id), source_key TEXT NOT NULL,
    digest TEXT NOT NULL, ids TEXT NOT NULL, PRIMARY KEY(binding_id,source_key))`);
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
  const reviewById=db.prepare('SELECT * FROM review_requests WHERE request_id=?');
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
    ' --id <新UUID> --text <正文>。需要结论的评审加 --review；答复对方的评审加 --review-to <原信 UUID>。重试沿用原编号。'+pairRule;

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
  const autoInbox=db.prepare('SELECT 1 FROM auto_inboxes WHERE binding_id=?');
  const listener=db.prepare('SELECT turn FROM auto_inboxes WHERE binding_id=? AND watch_id IS NOT NULL AND expires_ms>?');
  const hasAutoOffer=db.prepare('SELECT 1 FROM auto_offers WHERE mail_id=? UNION ALL SELECT 1 FROM burst_offers WHERE mail_id=? LIMIT 1');
  const rejectWrongTurn=db.prepare('DELETE FROM auto_offers WHERE mail_id=? AND turn<>?');
  const rejectWrongBurst=db.prepare('DELETE FROM burst_offers WHERE mail_id=? AND turn<>?');
  function senderRoute(id) {
    return autoInbox.get(id) ? listener.get(id,Date.now()) : inboxRoute.get(id);
  }
  const holdLetter=db.prepare('INSERT INTO paired_letters(mail_id,target_turn) VALUES(?,?)');
  const addFeedback=db.prepare(`INSERT INTO send_feedback(id,binding_id,sender,reason,related_id,correction,state)
    VALUES(?,?,'cc',?,?,?,?)`);
  function rejectPacket(binding,error,{related=null,correct=false,shown=false,output=''}={}) {
    const preview=output.split(/\r?\n/).find(line=>/^ASTRA(?:-REPLY)?\s/.test(line));
    const reason=String(error.message).slice(0,1200)+(preview?'\n出站片段：'+preview.slice(0,120):'');
    const id=randomUUID();
    addFeedback.run(id,binding.id,reason,related,Number(correct),shown?'shown':'queued');
    return '未发出（未登记信件） · '+reason+' · 错误编号 '+id;
  }
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
      let body=packet.text;
      if (packet.review) body='评审委托〔'+id+'〕\n'+body+'\n请用 relay.py send --review-to '+id+' 回传简明结论。';
      if (packet.reviewTo) body='评审结论〔'+packet.reviewTo+'〕\n'+body;
      addMail.run(id,binding.id,shortText(body),packet.mode);
      if (packet.review) db.prepare('INSERT INTO review_requests(request_id) VALUES(?)').run(id);
      if (packet.reviewTo && !db.prepare('UPDATE review_requests SET conclusion_id=? WHERE request_id=?').run(id,packet.reviewTo).changes)
        throw Error('未知评审委托');
      if(packet.mode==='none') {
        // The trigger offers only to the watch present at insertion. The
        // receiver checks native turn status before accepting the offer.
        if(autoInbox.get(binding.id)) {
          if(route?.turn) {
            rejectWrongTurn.run(id,route.turn);
            rejectWrongBurst.run(id,route.turn);
          }
        } else if(route || inboxRoute.get(binding.id)) {
          holdLetter.run(id,route?.turn ?? null);
          set.run('unknown','配对收件箱待领取',id,'queued');
        }
      }
      return id;
    });
  });

  function registerFinal(binding,result,route,related=null) {
    if(result.sdkHandled) return;
    try {
      const packets=outbound(result.finalText,{allowProse:true});
      if(related && packets.some(p=>p.mode!=='none')) throw Error('单向来信回合只能另发单向信');
      if(packets.length) return enqueue(binding,packets,route);
    } catch(error) {
      notify(rejectPacket(binding,error,{related,correct:true,output:result.finalText}));
    }
  }

  // Match only the exact detached writer to suppress re-forwarding an incoming message.
  const detachedWriters = new WeakMap();
  const lookupOutput=db.prepare('SELECT digest,ids FROM cc_output_receipts WHERE binding_id=? AND source_key=?');
  const saveOutput=db.prepare('INSERT INTO cc_output_receipts VALUES(?,?,?,?)');
  const registerOutput=db.transaction((binding,key,packets,route)=>{
    const digest=createHash('sha256').update(JSON.stringify(packets)).digest('hex');
    const prior=lookupOutput.get(binding.id,key);
    if(prior) {
      if(prior.digest!==digest) throw Error('重复的来源编号对应不同的信件');
      return JSON.parse(prior.ids);
    }
    const ids=enqueue(binding,packets,route);
    saveOutput.run(binding.id,key,digest,JSON.stringify(ids));
    return ids;
  });

  // A CloudCLI run can contain several SDK turns when the user steers CC.
  // Their individual successful results arrive before the single UI completion.
  function sendingContext(context,binding,scope,writer) {
    let messageId, blocks=new Map(), skip=scope.skipFirst, closed=false;
    const sentByTool=new Set();
    function finish(text) {
      if(!messageId || closed) return;
      closed=true;
      scope.handled=true;
      if(skip) {skip=false;return;}
      let ids;
      try {
        const packets=outbound(text,{allowProse:true}).filter(p=>p.id || p.mode!=='none' || !sentByTool.has(p.text));
        if(scope.related && packets.some(p=>p.mode!=='none')) throw Error('单向来信回合只能另发单向信');
        if(packets.length) ids=registerOutput.immediate(binding,'assistant:'+messageId,packets,scope.route);
      } catch(error) {
        notify(rejectPacket(binding,error,{related:scope.related,correct:true,output:text}));
      }
      if(ids) {
        try {writer.send({kind:'status',provider:'claude',sessionId:binding.cc,text:'已登记 · '+ids.join('、')});}
        catch {notify('信件已登记，界面状态未确认',ids.join('、'));}
      }
    }
    const wrapped={...context};
    if(scope.reviewId && context.normalizeMessage) wrapped.normalizeMessage=(raw,sid)=>{
      if((!raw.session_id || raw.session_id===binding.native_cc) && !raw.parent_tool_use_id) {
        // A later local question can share the detached provider run. Only an
        // explicitly addressed conclusion may leave that mixed conversation.
        const parts=raw.message?.content;
        const userInput=typeof parts==='string' ? parts : Array.isArray(parts) ?
          parts.filter(p=>p.type==='text').map(p=>p.text).join('\n') : '';
        if(raw.type==='attachment' || (raw.type==='user' && userInput && userInput!==scope.reviewPrompt))
          scope.mixedInput=true;
      }
      return context.normalizeMessage(raw,sid);
    };
    if(!scope.toolOnly && context.normalizeMessage) wrapped.normalizeMessage=(raw,sid)=>{
      const events=context.normalizeMessage(raw,sid);
      if(scope.done || raw.session_id!==binding.native_cc || raw.parent_tool_use_id || raw.parentToolUseId) return events;
      if(raw.type==='assistant' && raw.message?.id && !raw.isReplay && !raw.error &&
          raw.message.model!=='<synthetic>' && raw.message.role==='assistant') {
        if(messageId!==raw.message.id) {
          messageId=raw.message.id;blocks=new Map();closed=false;scope.handled=false;
        }
        if(raw.uuid && Array.isArray(raw.message.content))
          blocks.set(raw.uuid,raw.message.content.filter(p=>p.type==='text').map(p=>p.text).join('\n'));
        if(raw.message.stop_reason==='end_turn') finish([...blocks.values()].filter(Boolean).join('\n'));
      } else if(raw.type==='result') {
        if(raw.subtype==='success' && !raw.is_error && typeof raw.result==='string') finish(raw.result);
        else scope.handled=true;
        sentByTool.clear();
      }
      return events;
    };
    const create=context.createQuery ?? query;
    if(makeSendServer && create) wrapped.createQuery=request=>{
      const instance=randomUUID();
      const server=makeSendServer((body,requestId,review=false,reviewTo=null)=>{
        if(scope.done || !validCC(binding) || active.get(binding.cc)?.id!==binding.id)
          throw Error('配对或本次发件会话已结束');
        if(skip) throw Error('原信已登记，使用原信回执');
        const text=shortText(body);
        if (review && reviewTo) throw Error('请选择发起评审或回传结论');
        if(scope.reviewId && reviewTo && reviewTo!==scope.reviewId && !scope.mixedInput)
          throw Error('本轮评审结论须使用当前委托编号 '+scope.reviewId);
        const packet={text,mode:'none'};
        if (review) packet.review=true;
        if (reviewTo || (scope.reviewId && !scope.mixedInput && !review)) packet.reviewTo=reviewTo || scope.reviewId;
        const ids=registerOutput.immediate(binding,'tool:'+instance+':'+requestId,[packet],senderRoute(binding.id));
        sentByTool.add(text);
        return ids[0];
      });
      if(request.options?.mcpServers?.daily_pair) return create(request);
      return create({...request,options:{...request.options,mcpServers:{...request.options?.mcpServers,daily_pair:server}}});
    };
    return wrapped;
  }
  const previous = provider.run.bind(provider);
  provider.run = async (command,opts,writer,context) => {
    const check=handoff?.claimReadback(command,opts,writer);
    if (check) {
      const observe=observeTurn(), send=writer.send;
      writer.send=function(event) {
        const final=observe(event);
        if (final) check.finish(final.text);
        return send.call(this,event);
      };
      try {return await previous(check.prompt,opts,writer,context);}
      finally {writer.send=send;}
    }
    handoff?.changedSession(opts.sessionId);
    if (desktopProvision && !detachedWriters.has(writer) && [createDesktop,cancelDesktop].includes(command?.trim?.())) {
      const status = await desktopProvision.handle(command.trim(),opts,writer.userId);
      return previous('【工作台配对结果】'+status+' 请只向用户转达这项结果。',opts,writer,context);
    }
    const binding = active.get(opts.sessionId);
    if (!binding) {
      const owner=handoff?.retiredOwner(opts.sessionId);
      if (owner) return previous('【工作台】此会话原任务已移交至配对 #'+owner+'。保留历史供查询，后续工作请到接管方继续。\n'+command,opts,writer,context);
    }
    if (!binding || !validCC(binding) || !samePath(opts.cwd,binding.cc_cwd) || !Number.isInteger(writer.userId))
      return previous(command,opts,writer,context);
    if(detachedWriters.has(writer)) {
      return previous(command,opts,writer,sendingContext(context,binding,detachedWriters.get(writer),writer));
    }
    const settings = Object.fromEntries(['model','effort','permissionMode','toolsSettings']
      .filter(k=>opts[k]!==undefined).map(k=>[k,opts[k]]));
    recordReceiver.run(binding.id,writer.userId,JSON.stringify(settings));
    const observe=observeTurn();
    const route=senderRoute(binding.id);
    const needsBrief=!briefed.get({binding:binding.id});
    let direct, prompt=command, deliveryError, completed=false;
    const send = writer.send;
    function report(error,modelOutput='') {
      send.call(writer,{kind:'error',provider:'claude',sessionId:opts.sessionId,
        content:rejectPacket(binding,error,{correct:!!modelOutput,shown:!modelOutput,output:modelOutput||command})});
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
    const scope={route,skipFirst:!!direct.length};
    writer.send = function(event) {
      if(event.kind==='complete' && !event.parentToolUseId) scope.done=true;
      const final=observe(event);
      if(final) completed=true;
      if(final && !direct.length && !scope.handled) {
        try {
          const messages=outbound(final.text,{allowProse:true});
          if(messages.length) {
            const ids=enqueue(binding,messages,route);
            try {send.call(this,{kind:'status',provider:'claude',sessionId:binding.cc,text:'已登记 · '+ids.join('、')});}
            catch {notify('信件已登记，界面状态未确认',ids.join('、'));}
          }
        } catch(error) {
          deliveryError=error;
          report(error,final.text);
        }
      }
      return send.call(this,event);
    };
    try {
      const result=await previous(prompt,opts,writer,sendingContext({...context,createQuery:context.createQuery ?? query},binding,scope,writer));
      if(needsBrief && completed) recordBrief.run(binding.id);
      if(deliveryError) throw deliveryError;
      return result;
    } finally { scope.done=true;writer.send=send;handoff?.changedSession(opts.sessionId); }
  };

  async function ccTurn(binding,receiver,prompt,scope={toolOnly:true}) {
    let final,showStatus;
    const observe=observeTurn();
    const observed=Object.create(runtime);
    observed.run = async (name,cmd,opts,writer)=>{
      detachedWriters.set(writer,scope);
      const send=writer.send;
      showStatus=text=>send.call(writer,{kind:'status',provider:'claude',sessionId:binding.cc,text});
      writer.send=function(e) {
        if(scope && e.kind==='complete' && !e.parentToolUseId) scope.done=true;
        final ??= observe(e);
        return send.call(this,e);
      };
      try {return await runtime.run(name,cmd,opts,writer);}
      finally {if(scope) scope.done=true;writer.send=send; detachedWriters.delete(writer);}
    };
    const result=await detached({sessionId:binding.cc,userId:receiver.user_id,content:prompt,
      options:JSON.parse(receiver.options),interruptActiveRun:false},{runtime:observed});
    return {...result,complete:!!final,finalText:final?.text,sdkHandled:scope?.handled,registered(ids) {
      if(!ids?.length) return;
      try {showStatus?.('已登记 · '+ids.join('、'));}
      catch {notify('信件已登记，界面状态未确认',ids.join('、'));}
    }};
  }

  async function deliver(binding,mail,receiver) {
    const initial = mail.state==='queued';
    const claimed = initial?'submitting':'returning';
    const toDesktop = (mail.source==='cc')===initial;
    if(!toDesktop && (!receiver || runRegistry.isProcessing(binding.cc) ||
        runtime.hasBackgroundWork(binding.cc) || await externalBusy(binding.cc))) return 'busy';
    // A rebinding and a claim are serialized by SQLite. Only the winner may send.
    const auto=toDesktop && initial && mail.reply_mode==='none' && !!autoInbox.get(binding.id);
    // The original watch/handoff wins if the arrival matched it. Otherwise a
    // trusted project hook can receive a letter even on an automatic binding.
    if(toDesktop && initial && mail.reply_mode==='none' && nativeBacklog && !steer &&
        !(auto && hasAutoOffer.get(mail.id,mail.id))) {
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
        const turnId=!auto && initial && mail.reply_mode==='none' && steer ? await steer(binding,mail,prompt,
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
        const route=senderRoute(binding.id);
        const review=initial && reviewById.get(mail.id);
        if(review) content+='\n【共享评审 '+mail.id+'】本回合的最终答复将回传 Astra。请只写可共享的推荐、依据和保留意见，控制在 2400 字内。也可调用 send 的 review_to 显式提交结论，普通通知和工具日志无需发送。';
        if(!notification) content+='\n'+envelope;
        const needsBrief=!briefed.get({binding:binding.id});
        if(needsBrief) content+='\n\n'+ccBrief;
        const scope=review ? {route,reviewId:mail.id,reviewPrompt:content,toolOnly:true} : notification ? {route,related:mail.id} : {toolOnly:true};
        const r=await ccTurn(binding,receiver,content,scope);
        if(!r.started && r.error===busyError) {set.run(mail.state,'等待 CC 空闲',mail.id,claimed);return 'busy';}
        if(!r.started) {set.run('failed',r.error || 'CC 回合未启动',mail.id,claimed);return;}
        if(r.error || !r.complete) throw Error(r.error || 'CC 回合完成情况未确认');
        if(needsBrief) recordBrief.run(binding.id);
        if(!initial) set.run('returned','CC 回合已完成',mail.id,claimed);
        else if(mail.reply_mode==='none') {
          const ids=db.transaction(() => {
            if (!set.run('notified','CC 单向消息回合已完成',mail.id,claimed).changes) return;
            if(review) {
              const latest=reviewById.get(mail.id);
              if(latest.conclusion_id || latest.closed_reason || scope.mixedInput) return;
              try {
                const packets=outbound(r.finalText,{allowProse:true});
                if(packets.length>1 || packets[0]?.id) throw Error('请用 review_to 回传一份结论');
                const answer=shortText(packets[0]?.text ?? r.finalText);
                return enqueue(binding,[{text:answer,mode:'none',reviewTo:mail.id}],route);
              } catch(error) {
                notify(rejectPacket(binding,error,{related:mail.id,shown:true}));
                return;
              }
            }
            return registerFinal(binding,r,route,mail.id);
          }).immediate();
          r.registered(ids);
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
            rejectPacket(binding,error,{related:mail.id,correct:true,output:r.finalText});
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
  const pendingFeedback=db.prepare("SELECT * FROM send_feedback WHERE binding_id=? AND sender=? AND state='queued' ORDER BY rowid LIMIT 1");
  const feedbackState=db.prepare('UPDATE send_feedback SET state=?,result=? WHERE id=? AND state=?');
  async function sendFeedback(binding,receipt,receiver) {
    if(!validCC(binding) || !validDesktop(binding)) return;
    if(receipt.correction && receipt.related_id) {
      const original=db.prepare('SELECT state,reply_mode FROM mail WHERE id=? AND binding_id=?')
        .get(receipt.related_id,binding.id);
      if(!original || original.state==='cancelled' ||
          (original.reply_mode==='deferred' && original.state!=='awaiting_reply')) {
        feedbackState.run('shown','原信已关闭或已有答复，免去修正提醒',receipt.id,'queued');
        return;
      }
    }
    if(receipt.sender==='cc' && (!receiver || runRegistry.isProcessing(binding.cc) ||
        runtime.hasBackgroundWork(binding.cc) || await externalBusy(binding.cc))) return;
    if(!feedbackState.run('sending',null,receipt.id,'queued').changes) return;
    const transport=receipt.id.startsWith('delivery:');
    const header=transport?'信件 '+receipt.related_id:'未发出：上一批信封没有登记';
    const instruction=receipt.correction?'请核对这次失败的信封。若任务仍有效，可修正后在末行重新发出；已成功登记的信无需重发。':'请核对原记录。';
    const prompt='【工作台失败回执】'+header+'。'+receipt.reason+
      (!transport && receipt.related_id?'\n关联原信 '+receipt.related_id:'')+'\n'+instruction;
    try {
      if(receipt.sender==='desktop') {
        await queue(binding.desktop,prompt);
        feedbackState.run('shown','失败提醒已入队',receipt.id,'sending');
        return;
      }
      const route=senderRoute(binding.id);
      const r=await ccTurn(binding,receiver,prompt);
      if(!r.started && r.error===busyError) {feedbackState.run('queued',null,receipt.id,'sending');return;}
      if(!r.started || r.error || !r.complete) throw Error(r.error || '失败提醒回执不明');
      db.transaction(()=>{
        let result='发件方已收到失败提醒';
        if(receipt.correction) {
          try {
            const packets=outbound(r.finalText,{allowProse:true});
            if(packets.length) {
              if(receipt.related_id) {
                const original=db.prepare('SELECT reply_mode,state FROM mail WHERE id=? AND binding_id=?').get(receipt.related_id,binding.id);
                if(original?.reply_mode==='deferred' && (original.state!=='awaiting_reply' ||
                   packets.length!==1 || packets[0].id!==receipt.related_id)) throw Error('原异步信状态或回信编号不匹配');
                if(original?.reply_mode==='none' && packets.some(p=>p.mode!=='none')) throw Error('本次只接受独立单向信');
              }
              // A human may already have repaired this batch while the model was busy.
              const registered=db.prepare(`SELECT id FROM mail WHERE binding_id=? AND source='cc'
                AND created>=? AND body=? AND reply_mode=? ORDER BY seq`);
              const recent=new Map();
              const ids=db.transaction(()=>packets.map(packet=>{
                if(!packet.id) {
                  const key=JSON.stringify([packet.mode,packet.text]);
                  if(!recent.has(key)) recent.set(key,registered.all(binding.id,receipt.created,packet.text,packet.mode));
                  const prior=recent.get(key).shift();
                  if(prior) return prior.id;
                }
                return enqueue(binding,[packet],route)[0];
              })).immediate();
              result='修正后已登记 · '+ids.join('、');
            }
          } catch(error) {result='修正仍未发出 · '+error.message;notify(result);}
        }
        feedbackState.run('shown',result,receipt.id,'sending');
      }).immediate();
    } catch(error) {feedbackState.run('unknown',String(error.message),receipt.id,'sending');}
  }
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
    const notes=handoff ? '\n仅在方向、反馈或证据发生变化时，代用户用 '+quotePS(fileURLToPath(new URL('./handoff.py',import.meta.url)))+
      ' --store '+quotePS(storePath)+' show --binding '+binding.id+' --side '+recipient+
      ' 查看有效方向，note --help 可查增量记录方式。保留用户原话、局部认可和样品版本。方向改变要给搭档发一次短消息。' : '';
    const brief=readPairingBrief(binding,recipient)+notes+(handoff?.notice(binding.id) || '');
    if(!claimNotice.run(binding.id,recipient).changes) return;
    try {
      if(recipient==='cc') {
        const route=senderRoute(binding.id);
        const result=await ccTurn(binding,receiver,[brief,ccBrief].filter(Boolean).join('\n\n'),{route});
        if(!result.started && result.error===busyError) {
          finishNotice.run('queued',null,binding.id,recipient);
          return;
        }
        if(!result.started || result.error || !result.complete) throw Error(result.error || 'Claude 回合完成情况未确认');
        const ids=db.transaction(()=>{
          if(!finishNotice.run('sent',null,binding.id,recipient).changes) return;
          return registerFinal(binding,result,route);
        }).immediate();
        result.registered(ids);
        return;
      } else {
        const listen=autoInbox.get(binding.id) ? '\n平时继续做手头的事，项目收件 hook 会在工具结束时提示取件。若本回合的下一步依赖 Claude 的回复，发信时用 relay.py send --follow，在后台读取同一进程的后续输出并 notify，直到回合结束。' : '';
        await queue(binding.desktop,[brief,introduction(binding)+listen].filter(Boolean).join('\n\n'));
        if(listen) db.prepare('UPDATE auto_inboxes SET hint_pending=0 WHERE binding_id=?').run(binding.id);
      }
      finishNotice.run('sent',null,binding.id,recipient);
      setIntro.run('sent',binding.id,'queued');
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
      const feedback=pendingFeedback.get(binding.id,recipient);
      if(feedback && (!mail || !['queued','received'].includes(mail.state))) {
        await sendFeedback(binding,feedback,receiver);
        return;
      }
      if(!notice && source==='cc' && receiver?.introduction==='queued' && !mail && !autoInbox.get(binding.id) && !inboxRoute.get(binding.id) && validDesktop(binding) &&
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
