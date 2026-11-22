import {randomUUID} from 'node:crypto';
import {statSync} from 'node:fs';
import path from 'node:path';

const boot = randomUUID(); // Shared by pollers in this process.
const uuid = /^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i;
const prompt = '请只回复“已就绪”。这是会话初始化，请勿使用工具、读取文件或更改任何内容。';

function windowsDirectory(raw) {
  const value = raw?.replaceAll('/', '\\').replace(/^\\\\\?\\UNC\\/i, '\\\\').replace(/^\\\\\?\\/i, '');
  if (!value || !/^(?:[a-z]:\\|\\\\(?![.?]\\)[^\\]+\\[^\\]+(?:\\|$))/i.test(value))
    throw Error('Desktop cwd must be an absolute Windows directory');
  const normalized = path.win32.normalize(value);
  return normalized.length > path.win32.parse(normalized).root.length
    ? normalized.replace(/\\$/, '') : normalized;
}

function desktopLookup(db) {
  const columns = new Set(db.pragma('table_info(threads)').map(row => row.name));
  const filters = ["id=?", "archived=0", "source IN ('desktop','vscode')"];
  if (columns.has('originator'))
    filters.push("lower(coalesce(originator,'')) IN ('','codex desktop','codex_work_desktop')");
  if (columns.has('thread_source')) {
    const userChat = "coalesce(thread_source,'') IN ('','user')";
    // Desktop can create another user-owned project chat on the user's behalf.
    const desktopCreated = "thread_source='agent_created_thread' AND lower(originator) IN ('codex desktop','codex_work_desktop')";
    filters.push(columns.has('originator') ? '('+userChat+' OR ('+desktopCreated+'))' : userChat);
  }
  for (const field of ['agent_role','agent_nickname','agent_path'])
    if (columns.has(field)) filters.push("coalesce(" + field + ",'')=''");
  const title = columns.has('name') ? "coalesce(nullif(trim(name),''), title)" : "title";
  const lookup = db.prepare('SELECT cwd,' + title + ' AS title FROM threads WHERE ' + filters.join(' AND '));
  return id => {
    const row = lookup.get(id);
    if (!row) throw Error('Desktop thread is missing, archived or ineligible');
    const cwd = windowsDirectory(row.cwd);
    if (!statSync(cwd, {throwIfNoEntry:false})?.isDirectory()) throw Error('Desktop cwd is unavailable');
    return {cwd, title: row.title?.trim() || id};
  };
}

function ownerGone(row) {
  if (row.owner_pid === process.pid) return row.owner_boot !== boot;
  try { process.kill(row.owner_pid, 0); return false; }
  catch (error) { return error.code === 'ESRCH'; }
}

// Install after the mail worker has initialized receiver, using the already-open
// native database connection. Importing this module never starts a service.
export function installProvisioner({db, desktopDb, sessionsDb, userDb, cloudDb, detached, runtime}) {
  const desktop = desktopLookup(desktopDb);
  const users = cloudDb.prepare('SELECT count(*) AS n, min(id) AS id FROM users WHERE is_active=1');
  function activeUser() {
    const {n, id} = users.get();
    if (n !== 1 || !Number.isSafeInteger(id) || id < 1 || userDb.getUserById(id)?.id !== id)
      throw Error('Provisioning requires exactly one validated active CloudCLI user');
    return id;
  }
  const conflict = db.prepare('SELECT 1 FROM bindings WHERE active=1 AND (desktop=? OR desktop_host=? OR cc=? OR native_cc=?)');
  function ensureFree(r, native = null) {
    if (conflict.get(r.desktop, r.desktop, r.cc, native)) throw Error('An active binding already exists');
  }
  const exists = db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provision_requests'");
  let initialized = false;
  function initialize() {
    if (initialized) return true;
    if (!exists.get()) return false; // Python owns the queue schema and may enqueue after host startup.
    db.transaction(() => {
      for (const row of db.prepare("SELECT id,owner_pid,owner_boot FROM provision_requests WHERE state IN ('creating','starting')").all()) {
        if (ownerGone(row)) db.prepare("UPDATE provision_requests SET state='unknown',detail=? WHERE id=?")
          .run('Host exited during creation. Inspect the recorded CloudCLI app ID before proceeding.', row.id);
      }
    }).immediate();
    initialized = true;
    return true;
  }
  const claim = db.transaction(() => {
    const r = db.prepare("SELECT * FROM provision_requests WHERE state='queued' ORDER BY rowid LIMIT 1").get();
    if (r) db.prepare("UPDATE provision_requests SET state='creating',owner_pid=?,owner_boot=? WHERE id=?")
      .run(process.pid, boot, r.id);
    return r;
  });

  async function processRequest(r) {
    let failureState = 'failed', native = null;
    try {
      const origin = desktop(r.desktop);
      const userId = activeUser();
      function session() {
        const row = sessionsDb.getSessionById(r.cc);
        if (row?.session_id !== r.cc || row.provider !== 'claude' || row.isArchived ||
            windowsDirectory(row.project_path).toLowerCase() !== origin.cwd.toLowerCase())
          throw Error('CloudCLI session identity or project changed');
        return row;
      }
      // A panel binding and this synchronous native insert must see one another.
      // The separate claim transaction already committed the intent if this process dies.
      db.transaction(() => {
        ensureFree(r);
        if (sessionsDb.getSessionById(r.cc)) throw Error('Allocated CloudCLI app ID already exists');
        failureState = 'unknown';
        sessionsDb.createAppSession(r.cc, 'claude', origin.cwd, r.title);
        if (session().provider_session_id !== null) throw Error('New CloudCLI session is already mapped');
        db.prepare("UPDATE provision_requests SET state='starting' WHERE id=?").run(r.id);
      }).immediate();

      let complete = false, receiver;
      const observed = Object.create(runtime);
      observed.run = async (provider, cmd, opts, writer) => {
        ensureFree(r);
        if (activeUser() !== userId || writer.userId !== userId || opts.permissionMode !== 'default' ||
            provider !== 'claude' || opts.sessionId !== r.cc || session().provider_session_id !== null ||
            windowsDirectory(opts.cwd).toLowerCase() !== origin.cwd.toLowerCase() ||
            desktop(r.desktop).cwd.toLowerCase() !== origin.cwd.toLowerCase())
          throw Error('First turn identity, permissions or directory changed');
        const send = writer.send;
        receiver = {userId:writer.userId, options:{permissionMode:opts.permissionMode}};
        writer.send = function(event) {
          if (event.kind === 'complete') complete = event.exitCode === 0 && !event.aborted;
          return send.call(this, event);
        };
        try { return await runtime.run(provider, cmd, opts, writer); }
        finally {
          writer.send = send;
          const announced = writer.getSessionId();
          if (uuid.test(announced)) native = announced;
        }
      };
      const result = await detached({sessionId:r.cc, userId, content:prompt,
        options:{permissionMode:'default'}, interruptActiveRun:false}, {runtime:observed});
      const cc = session();
      if (!result?.started || result.error || !complete || !native || cc.provider_session_id !== native)
        throw Error('Successful first turn and native Claude ID could not be confirmed');

      failureState = 'failed';
      db.transaction(() => {
        const current = desktop(r.desktop);
        if (current.cwd.toLowerCase() !== origin.cwd.toLowerCase() || activeUser() !== userId ||
            session().provider_session_id !== native) throw Error('Session or user changed during the first turn');
        ensureFree(r, native);
        const binding = db.prepare('INSERT INTO bindings(cc,native_cc,cc_title,cc_cwd,desktop,desktop_host,desktop_title,desktop_cwd) VALUES(?,?,?,?,?,?,?,?)')
          .run(r.cc, native, cc.custom_name || r.title, origin.cwd, r.desktop, r.desktop, current.title, current.cwd);
        db.prepare('INSERT INTO receiver(binding_id,user_id,options) VALUES(?,?,?)')
          .run(binding.lastInsertRowid, receiver.userId, JSON.stringify(receiver.options));
        db.prepare("UPDATE provision_requests SET state='bound',binding_id=?,native_cc=? WHERE id=?")
          .run(binding.lastInsertRowid, native, r.id);
      }).immediate();
    } catch (error) {
      db.prepare("UPDATE provision_requests SET state=?,detail=?,native_cc=? WHERE id=? AND state IN ('creating','starting')")
        .run(failureState, String(error.message).slice(0,500), native, r.id);
    }
  }
  return {async tick() {
    if (!initialize()) return;
    const request = claim.immediate();
    if (request) await processRequest(request);
  }};
}
