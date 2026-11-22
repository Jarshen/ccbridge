import {randomUUID} from 'node:crypto';
import {statSync} from 'node:fs';
import path from 'node:path';

export const createDesktop = '新建 Desktop 配对';
export const cancelDesktop = '取消 Desktop 配对申请';
const firstMessage = id => '工作台配对请求 ' + id + '。请简短回复就绪。';
const normal = cwd => path.win32.normalize(cwd.replace(/^\\\\\?\\UNC\\/i,'\\\\').replace(/^\\\\\?\\/,'')).toLowerCase();

export function desktopRequests({db, desktopDb, sessions, openDraft}) {
  db.exec(`CREATE TABLE IF NOT EXISTS desktop_requests(
    id TEXT PRIMARY KEY, cc TEXT NOT NULL, native_cc TEXT NOT NULL, cwd TEXT NOT NULL,
    user_id INTEGER NOT NULL, options TEXT NOT NULL, created INTEGER NOT NULL,
    state TEXT NOT NULL, detail TEXT, desktop TEXT, binding_id INTEGER REFERENCES bindings(id));
    CREATE UNIQUE INDEX IF NOT EXISTS pending_desktop_request ON desktop_requests(cc) WHERE state='pending'`);
  const pending = db.prepare("SELECT * FROM desktop_requests WHERE cc=? AND state='pending'");
  const conflict = db.prepare('SELECT id FROM bindings WHERE active=1 AND (cc=? OR native_cc=? OR desktop=? OR desktop_host=?)');
  const finish = db.prepare("UPDATE desktop_requests SET state=?,detail=? WHERE id=? AND state='pending'");
  const columns = new Set(desktopDb.pragma('table_info(threads)').map(row => row.name));
  const supported = ['id','title','cwd','first_user_message','originator','created_at','source','archived']
    .every(column => columns.has(column));
  const extra = ['agent_role','agent_path','agent_nickname'].filter(c => columns.has(c))
    .map(c => ` AND coalesce(${c},'')=''`).join('') +
    (columns.has('thread_source') ? " AND coalesce(thread_source,'user') IN ('user','')" : '');
  const match = supported && desktopDb.prepare(`SELECT id,title,cwd FROM threads WHERE
    first_user_message=? AND created_at>=? AND archived=0 AND source IN ('vscode','desktop')
    AND lower(originator) IN ('codex desktop','codex_work_desktop') ${extra}`);

  function source(cc,cwd) {
    const row = sessions.getSessionById(cc);
    if (row?.session_id !== cc || row.provider !== 'claude' || row.isArchived ||
        !row.provider_session_id || !row.project_path || normal(row.project_path) !== normal(cwd))
      throw Error('请在已有原生 ID 的 Claude 聊天及其项目目录中发起配对');
    return row;
  }
  const reserve = db.transaction((command,opts,userId,expected) => {
    const cc = opts.sessionId, row = source(cc,opts.cwd);
    if (expected && (!expected.native || expected.native !== row.provider_session_id))
      throw Error('Claude 原生身份已变更，请刷新后重新选择');
    const prior = pending.get(cc);
    if (expected?.request && prior?.id !== expected.request)
      throw Error('待处理申请已变更，请刷新后重新选择');
    if (command === cancelDesktop) {
      if (prior) finish.run('cancelled','用户取消',prior.id);
      return {status:prior ? '已取消申请' : '没有待处理申请'};
    }
    const bound = conflict.get(cc,row.provider_session_id,null,null);
    if (bound) return {status:'已有配对 #' + bound.id};
    if (prior) return {status:'请在已打开的 Desktop 草稿发送首条消息；若已关闭草稿，请取消申请后重新发起。'};
    if (!supported) throw Error('Desktop 索引缺少核对首条消息或客户端身份所需的字段');
    if (!Number.isSafeInteger(userId) || userId<1 || !path.win32.isAbsolute(opts.cwd) ||
        !statSync(opts.cwd,{throwIfNoEntry:false})?.isDirectory()) throw Error('无效的会话用户或目录');
    const id = randomUUID();
    const options = Object.fromEntries(['permissionMode','model','effort','toolsSettings']
      .filter(k => opts[k] !== undefined).map(k => [k,opts[k]]));
    db.prepare(`INSERT INTO desktop_requests(id,cc,native_cc,cwd,user_id,options,created,state)
      VALUES(?,?,?,?,?,?,unixepoch(),'pending')`).run(id,cc,row.provider_session_id,opts.cwd,userId,JSON.stringify(options));
    return {id};
  });
  const bind = db.transaction(request => {
    if (pending.get(request.cc)?.id !== request.id) return;
    const matches = match.all(firstMessage(request.id),request.created)
      .filter(r => normal(r.cwd) === normal(request.cwd));
    if (!matches.length) return;
    if (matches.length !== 1) throw Error('多条 Desktop 首信匹配，请人工核对');
    const thread = matches[0], cc = source(request.cc,request.cwd);
    if (cc.provider_session_id !== request.native_cc ||
        conflict.get(request.cc,request.native_cc,thread.id,thread.id)) throw Error('会话身份或配对已变更');
    const binding = db.prepare(`INSERT INTO bindings(cc,native_cc,cc_title,cc_cwd,desktop,desktop_host,desktop_title,desktop_cwd)
      VALUES(?,?,?,?,?,?,?,?)`).run(request.cc,request.native_cc,cc.custom_name || request.cc,request.cwd,
        thread.id,thread.id,thread.title || thread.id,thread.cwd).lastInsertRowid;
    db.prepare('INSERT INTO receiver(binding_id,user_id,options) VALUES(?,?,?)').run(binding,request.user_id,request.options);
    db.prepare("UPDATE desktop_requests SET state='bound',desktop=?,binding_id=? WHERE id=?").run(thread.id,binding,request.id);
  });
  return {
    async handle(command,opts,userId,expected) {
      const result = reserve.immediate(command,opts,userId,expected);
      if (result.status) return result.status;
      const url = new URL('codex://threads/new');
      url.searchParams.set('path',opts.cwd);
      url.searchParams.set('prompt',firstMessage(result.id));
      try { await openDraft(url.href); }
      catch (error) { finish.run('failed',error.message,result.id); throw error; }
      db.prepare("UPDATE desktop_requests SET detail=? WHERE id=? AND state='pending'")
        .run('已请求打开 Desktop 草稿',result.id);
      return '请在 Desktop 草稿发送预填的首条消息，工作台将自动配对。';
    },
    tick() {
      for (const row of db.prepare("SELECT * FROM desktop_requests WHERE state='pending'").all()) {
        if (!supported) continue;
        try { bind.immediate(row); }
        catch (error) {
          if (String(error.code).startsWith('SQLITE_BUSY') || String(error.code).startsWith('SQLITE_LOCKED')) throw error;
          finish.run('failed',error.message,row.id);
        }
      }
    }
  };
}
