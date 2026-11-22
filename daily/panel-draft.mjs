import {execFile} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';
import {promisify} from 'node:util';
import {desktopRequests, createDesktop, cancelDesktop} from './desktop-request.mjs';

// The panel and an interactive CC command reserve the same request and use the
// same native draft, first-message validation and host-side binding transaction.
export async function fromPanel(input, {db, cloud, desktopDb, openDraft}) {
  if (!['create','cancel'].includes(input.action)) throw Error('未知的草稿操作');
  if (input.action === 'cancel' && !input.request) throw Error('缺少要取消的申请编号');
  const users = cloud.prepare('SELECT id FROM users WHERE is_active=1').all();
  if (users.length !== 1) throw Error('需要唯一的活动 CloudCLI 用户');
  const lookup = cloud.prepare(`SELECT s.* FROM sessions s WHERE s.session_id=?
    AND NOT EXISTS(SELECT 1 FROM projects p WHERE p.project_path=s.project_path AND p.isArchived=1)`);
  const bridge = desktopRequests({db,desktopDb,sessions:{getSessionById:id=>lookup.get(id)},openDraft});
  return bridge.handle(input.action === 'create' ? createDesktop : cancelDesktop,
    {sessionId:input.cc, cwd:input.cwd, permissionMode:'default'}, users[0].id,
    {native:input.native, request:input.request});
}

async function main() {
  const chunks=[];
  for await(const chunk of process.stdin) chunks.push(chunk);
  const input=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const require=createRequire(path.join(process.env.DAILY_CLOUDCLI_ROOT,'package.json'));
  const Database=require('better-sqlite3');
  const db=new Database(input.store,{fileMustExist:true});
  const readonly={readonly:true,fileMustExist:true};
  const cloud=new Database(input.cloud,readonly);
  const desktopDb=new Database(input.desktop,readonly);
  try {
    db.pragma('foreign_keys = ON');
    db.pragma('busy_timeout = 5000');
    const openDraft=url=>promisify(execFile)(input.python,
      ['-c','import os,sys;os.startfile(sys.argv[1])',url],{windowsHide:true,timeout:15000});
    const status=await fromPanel(input,{db,cloud,desktopDb,openDraft});
    console.log(JSON.stringify({status}));
  } finally {desktopDb.close();cloud.close();db.close();}
}

import {fileURLToPath} from 'node:url';
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  main().catch(error=>{console.error(error.message);process.exitCode=1;});
