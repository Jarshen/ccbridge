"""Install the two bridge callbacks and open native Codex project review on Windows."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import contextmanager
from threading import Timer
from tempfile import NamedTemporaryFile


class ReviewRequired(ValueError):
    """Codex requires native review of the installed commands."""


def has_receipt_hooks(project, store):
    path = Path(project) / '.codex' / 'hooks.json'
    if not path.is_file():
        return False
    try:
        groups = json.loads(path.read_text(encoding='utf-8-sig'))['hooks']
        command = hook_command(store)
        return all(any(h.get('type') == 'command' and h.get('command') == command
                       for g in groups.get(event, []) for h in g['hooks'])
                   for event in ('PreToolUse', 'PostToolUse'))
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return False


def hook_command(store):
    python = Path(sys.executable).with_name('python.exe').resolve(strict=True)
    script = Path(__file__).with_name('inbox.py').resolve(strict=True)
    command = subprocess.list2cmdline([python.as_posix(), '-B']) + f' "{script.as_posix()}"'
    store = Path(store).resolve()
    if store != script.parent / 'data' / 'bridge.sqlite3':
        command += f' --store "{store.as_posix()}"'
    return command + ' tool-hook'


def install(project, store):
    project = Path(project).resolve(strict=True)
    folder = project / '.codex'
    path = folder / 'hooks.json'
    if folder.resolve() != folder or path.resolve() != path:
        raise ValueError('项目 hook 路径指向其他位置，请核对目录。')
    folder.mkdir(exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    config = json.loads(original.decode('utf-8-sig')) if original is not None else {}
    if not isinstance(config, dict) or not isinstance(config.setdefault('hooks', {}), dict):
        raise ValueError(f'现有 hook 配置格式无效：{path}')
    entry = {'type': 'command', 'command': hook_command(store), 'timeout': 3}
    changed = False
    for name in ('PreToolUse', 'PostToolUse'):
        groups = config['hooks'].setdefault(name, [])
        if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get('hooks'), list) for g in groups):
            raise ValueError(f'现有 {name} 配置格式无效：{path}')
        if not any(not g.get('matcher') and entry in g['hooks'] for g in groups):
            groups.append({'hooks': [entry]})
            changed = True
    if changed:
        with NamedTemporaryFile(dir=folder, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
            if (path.read_bytes() if path.exists() else None) != original:
                raise ValueError('项目 hook 配置刚刚发生变化，请重试。')
            if original is None:
                tmp_path.rename(path)  # On Windows this refuses to overwrite a concurrently created file.
            else:
                os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
    return path


def codex_cli():
    # Resolve against the workbench environment before starting a client in this project.
    cli = shutil.which('codex.exe')
    if not cli:
        root = Path(os.environ['LOCALAPPDATA']) / 'OpenAI' / 'Codex' / 'bin'
        cli = next(iter(sorted(root.glob('*/codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True)), None)
    if not cli:
        raise FileNotFoundError('配置已写入。请在该项目运行 Codex CLI，输入 /hooks 完成审核。')
    return str(Path(cli).resolve(strict=True))


def open_review(project):
    subprocess.Popen([codex_cli(), '-C', str(project)],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


@contextmanager
def config_session(project):
    # A separate stdio client only lists hooks and updates the user's config.
    # No model thread is started or resumed.
    with subprocess.Popen([codex_cli(), '-C', str(project), 'app-server', '--listen', 'stdio://'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW) as proc:
        timer = Timer(12, proc.kill)
        timer.start()
        sequence = 0
        def send(frame):
            proc.stdin.write(json.dumps(frame) + '\n')
            proc.stdin.flush()
        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            send(dict(id=sequence, method=method, params=params))
            for line in proc.stdout:
                frame = json.loads(line)
                if frame.get('id') == sequence:
                    if 'error' in frame:
                        raise ValueError(frame['error']['message'])
                    return frame['result']
            raise RuntimeError('Codex 配置服务未返回结果，请刷新配置后重试。')
        try:
            rpc('initialize', {'clientInfo': {'name': 'daily-pair-hooks', 'version': '1'},
                               'capabilities': {'experimentalApi': True}})
            send({'method': 'initialized'})
            yield rpc
        finally:
            try:
                proc.stdin.close()
                proc.wait(timeout=3)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                proc.kill()
            finally:
                timer.cancel()


def set_enabled(project, store, enabled):
    project = Path(project).resolve(strict=True)
    source = project / '.codex' / 'hooks.json'
    command = hook_command(store)
    def list_pair(rpc):
        rows = rpc('hooks/list', {'cwds': [str(project)]})['data']
        if len(rows) != 1 or rows[0]['errors'] or Path(rows[0]['cwd']).resolve() != project:
            raise ValueError('Codex 未能读取所选项目的 hook 配置。')
        hooks = [h for h in rows[0]['hooks']
                 if h['source'] == 'project' and Path(h['sourcePath']) == source
                 and h.get('handlerType') == 'command' and h.get('command') == command
                 and h['eventName'] in ('preToolUse', 'postToolUse') and not h['isManaged']]
        if len(hooks) != 2 or {h['eventName'] for h in hooks} != {'preToolUse','postToolUse'}:
            raise ReviewRequired('未找到唯一的一对工作台收件命令。请在「设置项目收件」中核对配置。')
        if any(h['trustStatus'] != 'trusted' for h in hooks):
            raise ReviewRequired('请在 Codex 的 /hooks 中审核两条收件命令。')
        return hooks
    with config_session(project) as rpc:
        cfg = rpc('config/read', {'cwd': str(project), 'includeLayers': True})
        hooks = list_pair(rpc)
        if all(h['enabled'] == enabled for h in hooks):
            return
        layer = next((l for l in cfg['layers'] if l['name']['type'] == 'user' and not l['name'].get('profile')), None)
        if layer is None:
            raise ValueError('未找到 Codex 用户配置层。')
        states = layer['config'].get('hooks', {}).get('state', {})
        edits = []
        for h in hooks:
            key = h['key']
            if states.get(key, {}).get('trusted_hash') != h['currentHash']:
                raise ReviewRequired('收件命令或信任记录已变更，请重新审核。')
            if h['enabled'] != enabled:
                edits.append({'keyPath': 'hooks.state.' + json.dumps(key, ensure_ascii=False) + '.enabled',
                              'value': enabled, 'mergeStrategy': 'replace'})
        rpc('config/batchWrite', {'filePath': layer['name']['file'], 'expectedVersion': layer['version'],
                                  'edits': edits})
    with config_session(project) as rpc:
        if any(h['enabled'] != enabled for h in list_pair(rpc)):
            raise ValueError('Codex 回读的启用状态与请求不一致，请在 /hooks 核对。')
