import path from 'node:path';
import {readdir,stat} from 'node:fs/promises';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

const exec=promisify(execFile);
let verified;

export async function queueCli() {
  if(verified && (await stat(verified).catch(()=>null))?.isFile()) return verified;
  const candidates=new Set();
  const root=process.env.DAILY_CODEX_BIN_DIR || (process.env.LOCALAPPDATA &&
    path.join(process.env.LOCALAPPDATA,'OpenAI','Codex','bin'));
  if(root && path.isAbsolute(root)) {
    const entries=await readdir(root,{withFileTypes:true}).catch(e=>{
      if(e.code==='ENOENT') return [];
      throw e;
    });
    const installed=await Promise.all(entries.filter(e=>e.isDirectory()).map(async e=>{
      const file=path.join(root,e.name,'codex.exe');
      const info=await stat(file).catch(()=>null);
      return info?.isFile()?{file,time:info.mtimeMs}:null;
    }));
    for(const item of installed.filter(Boolean).sort((a,b)=>b.time-a.time)) candidates.add(item.file);
  }
  for(const dir of (process.env.PATH || '').split(path.delimiter)) {
    const clean=dir.replace(/^"(.*)"$/,'$1');
    if(path.isAbsolute(clean)) candidates.add(path.join(clean,'codex.exe'));
  }
  for(const file of candidates) {
    if(!(await stat(file).catch(()=>null))?.isFile()) continue;
    try {
      const {stdout}=await exec(file,['queue','--help'],{windowsHide:true,timeout:8000});
      if(stdout.includes('--thread') && stdout.includes('--message')) {
        verified=file;
        return file;
      }
    } catch { /* Try another installed version; help never submits a message. */ }
  }
  throw Object.assign(new Error('未找到支持 queue 的 Codex CLI，请检查 Desktop 安装目录及 PATH。'),{code:'NO_QUEUE_CLI'});
}
