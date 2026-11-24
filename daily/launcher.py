"""Windows entry point for the daily workbench. See --help for rollback."""

import argparse
from contextlib import contextmanager, closing
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

if __package__:
    from .storage import Store
else:
    from storage import Store


DAILY = Path(__file__).resolve().parent
VERSION = "1.37.9"
LOCAL = "127.0.0.1"
URL_KEYS = ("CLOUDCLI_DESKTOP_LOCAL_SERVER_URL", "CLOUDCLI_LOCAL_SERVER_URL",
            "ELECTRON_LOCAL_SERVER_URL")
PORT_KEYS = ("CLOUDCLI_DESKTOP_LOCAL_SERVER_PORT", "CLOUDCLI_SERVER_PORT", "SERVER_PORT", "PORT")


class Blocked(ValueError):
    """Can still manage bindings while host startup is blocked."""


def local_path(value, *, base=None):
    raw = os.fspath(value)
    path = Path(raw)
    if (not raw or raw.startswith(("//", "\\\\")) or "://" in raw or "\0" in raw
            or '"' in raw or (not path.is_absolute() and (base is None or path.anchor))):
        raise Blocked("需要明确的本机文件路径。请核对账户和 CloudCLI 的 DATABASE_PATH。")
    path = ((base / path) if base else path).resolve()
    if path.drive.startswith("\\\\"):
        raise Blocked("请使用本机文件路径。")
    return path


def database_path(root, home, env):
    value = env.get("DATABASE_PATH")
    if not value:
        # Match 1.37.9 load-env.js (literal values, first nonempty assignment).
        # Do not import, retain or report other dotenv variables.
        try:
            with (root / ".env").open("rb") as lines:
                for line in lines:
                    line = line.strip().removeprefix(b"\xef\xbb\xbf")
                    if line[:14].upper() == b"DATABASE_PATH=":
                        value = line.partition(b"=")[2].strip().decode("utf-8")
                        if value:
                            break
        except FileNotFoundError:
            pass
    return local_path(value, base=root) if value else home / ".cloudcli" / "auth.db"


def read_json(path):
    try:
        with path.open(encoding="utf-8-sig") as data:
            obj = json.load(data)
    except FileNotFoundError:
        return None
    if not isinstance(obj, dict):
        raise Blocked(f"无法识别 {path.name}。请核对本地服务配置后重新打开工作台。")
    return obj


def write_json(path, obj):
    f = tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False)
    tmp = Path(f.name)
    try:
        with f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def port_number(value):
    if not str(value).isascii() or not str(value).isdigit() or not 0 < int(value) <= 65535:
        raise Blocked("本地服务的端口无效。")
    return int(value)


def url_port(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "http" or url.hostname not in (LOCAL, "localhost")
                or url.username is not None or url.path not in ("", "/") or url.query or url.fragment
                or any(c.isspace() or c == "\\" for c in value)):
            raise ValueError
        return port_number(80 if url.port is None else url.port)
    except (TypeError, ValueError):
        raise Blocked("服务地址必须指向本机 HTTP 回环端口。请核对 CloudCLI 本地服务配置。") from None


def endpoint(marker, env, default, root):
    if env.get("HOST") and env["HOST"] not in (LOCAL, "localhost"):
        raise Blocked("HOST 与本机回环地址不一致。请核对 CloudCLI 的启动环境。")
    ports = {port_number(env[k]) for k in PORT_KEYS if env.get(k)}
    ports.update(url_port(env[k]) for k in URL_KEYS if env.get(k))
    if marker is not None:
        if (marker.get("host", LOCAL) not in (LOCAL, "localhost")
                or not ({"port", "url"} & marker.keys())
                or type(marker.get("pid")) is not int or not 0 < marker["pid"] < 2**32):
            raise Blocked("无法确认 local-server.json 中的本机服务与进程。请核对 CloudCLI 服务状态。")
        if "appRoot" in marker and local_path(marker["appRoot"]) != root:
            raise Blocked("本地服务标记指向另一份 CloudCLI 安装。请先确认日常服务。")
        if "port" in marker:
            ports.add(port_number(marker["port"]))
        if "url" in marker:
            ports.add(url_port(marker["url"]))
    if len(ports) > 1:
        raise Blocked("本地服务标记与环境指定的端口不一致。请在日常账户环境中重试。")
    return next(iter(ports), default)


@dataclass
class Config:
    home: Path
    data: Path
    root: Path
    electron: Path
    cc: Path
    desktop: Path
    env: dict
    acceptance: bool
    port: int

    @property
    def store(self):
        return self.data / "bridge.sqlite3"

    @property
    def status(self):
        return self.data / "status.json"

    @property
    def marker(self):
        return self.home / ".cloudcli" / "local-server.json"


def configure(acceptance, env):
    account = local_path(env["USERPROFILE"])
    install = local_path(env.get("CLOUDCLI_SERVER_DIR") or account / ".cloudcli" / "server")
    root = install / VERSION
    electron = local_path(env["LOCALAPPDATA"]) / "Programs/@cloudcli-aicloudcli-desktop/CloudCLI.exe"
    data = DAILY / "data"
    home = account
    if acceptance:
        data = data / "acceptance"
        home = data / "home"
        installed_codex_bin = local_path(env["LOCALAPPDATA"]) / "OpenAI/Codex/bin"
        # Do not forward account profile overrides, tokens or session identity.
        env = {k: v for k, v in env.items() if k.upper() in
               {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
                "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "NUMBER_OF_PROCESSORS"}}
        env.update(DAILY_CODEX_BIN_DIR=str(installed_codex_bin),
                   APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"),
                   TEMP=str(data / "tmp"), TMP=str(data / "tmp"), WORKSPACES_ROOT=str(data / "projects"))
    else:
        env = dict(env)
    codex = local_path(env.get("CODEX_HOME") or home / ".codex")
    claude = local_path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    env.update(USERPROFILE=str(home), HOME=str(home), CODEX_HOME=str(codex), CLAUDE_CONFIG_DIR=str(claude))
    cc = home / ".cloudcli/auth.db"
    return Config(home, data, root, electron, cc, codex / "state_5.sqlite", env,
                  acceptance, 33180 if acceptance else 3001)


def init_acceptance(c):
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "WORKSPACES_ROOT"):
        Path(c.env[key]).mkdir(parents=True, exist_ok=True)
    # An existing empty DB prevents CloudCLI's automatic legacy auth.db import.
    c.cc.parent.mkdir(parents=True, exist_ok=True)
    with c.cc.open("ab"):
        pass
    c.desktop.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(c.desktop)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS threads
            (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, source TEXT, archived INTEGER)""")


def kernel_function(name, args, result):
    fn = getattr(ctypes.WinDLL("kernel32", use_last_error=True), name)
    fn.argtypes, fn.restype = args, result
    return fn


def close_handle(handle):
    kernel_function("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)(handle)


@contextmanager
def process_handle(pid, rights):
    handle = kernel_function("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                             wintypes.HANDLE)(rights, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield handle
    finally:
        close_handle(handle)


def creation_time(handle):
    times = [wintypes.FILETIME() for _ in range(4)]
    get_times = kernel_function("GetProcessTimes",
        [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL)
    if not get_times(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime


@contextmanager
def host_guard(home):
    # Object existence is the guard. The host inherits its handle, so even a
    # launcher crash between CreateProcess and the first heartbeat is covered.
    identity = hashlib.sha256(os.path.normcase(str(home)).encode()).hexdigest()
    create = kernel_function("CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR],
                             wintypes.HANDLE)
    ctypes.set_last_error(0)
    handle = create(None, False, "Global\\CloudCLI.Daily." + identity)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    exists = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    try:
        yield None if exists else handle
    finally:
        close_handle(handle)


def pid_alive(pid):
    if type(pid) is not int or not 0 < pid < 2**32:
        raise Blocked("服务记录中的 PID 无效。请核对后台进程。")
    handle = kernel_function("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                             wintypes.HANDLE)(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        if ctypes.get_last_error() == 87:  # no such process
            return False
        raise Blocked(f"无法确认 PID {pid} 的状态。请在任务管理器中核对后台进程。")
    try:
        result = kernel_function("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD],
                                 wintypes.DWORD)(handle, 0)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258
    finally:
        close_handle(handle)


def listener_table(family):
    get_table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    size = wintypes.DWORD()
    while True:
        table = ctypes.create_string_buffer(size.value)
        rc = get_table(table, ctypes.byref(size), False, family, 3, 0)
        if rc == 0:
            return table
        if rc != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(rc)


def listeners(port):
    # Windows TCP{,6}ROW_OWNER_PID listener tables. Any listener counts as
    # occupied; reuse requires exactly the bridge's own IPv4 loopback socket.
    owners = set()
    for family, layout in ((socket.AF_INET, "<4x4sI8xI"), (socket.AF_INET6, "<16s4xI28xI")):
        table = listener_table(family)
        count, = struct.unpack_from("<I", table)
        for i in range(count):
            addr, local_port, pid = struct.unpack_from(layout, table, 4 + i * struct.calcsize(layout))
            if socket.ntohs(local_port & 0xffff) == port:
                owners.add((socket.inet_ntop(family, addr), pid))
    return owners


def healthy(c, marker, status, owners):
    if not status or not marker:
        return False
    try:
        pid = status["pid"]
        return (status["state"] == "ready" and bool(status["instance"])
                and -1 <= time.time() - float(status["heartbeat"]) <= 15
                and type(pid) is int and pid == marker["pid"] and status["port"] == c.port
                and local_path(status["store"]) == c.store and owners == {(LOCAL, pid)}
                and pid_alive(pid))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def start_host(c, handle):
    if not (DAILY / "host.mjs").is_file():
        raise Blocked("缺少 daily/host.mjs。请补齐工作台文件后重新启动。")
    if not c.electron.is_file() or not (c.root / "dist-server/server/index.js").is_file():
        raise Blocked("未找到已安装的 CloudCLI Desktop 和 1.37.9 服务包。")
    package = read_json(c.root / "package.json") or {}
    if (package.get("name"), package.get("version")) != ("@cloudcli-ai/cloudcli", VERSION):
        raise Blocked("服务包不是已支持的 CloudCLI 1.37.9。")
    for index in (c.cc, c.desktop):
        if not index.is_file():
            raise Blocked(f"未找到日常会话索引 {index}。请核对账户与数据库位置后重试。")
    python = Path(sys.executable).resolve()
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    if not python.is_file():
        raise Blocked("未找到当前解释器的 python.exe。")
    env = dict(c.env, ELECTRON_RUN_AS_NODE="1", DAILY_CLOUDCLI_ROOT=str(c.root),
               DAILY_BRIDGE_STORE=str(c.store), DAILY_STATUS_FILE=str(c.status),
               DAILY_DESKTOP_DB=str(c.desktop), DAILY_PYTHON=str(python), HOST=LOCAL,
               SERVER_PORT=str(c.port), DATABASE_PATH=str(c.cc), NODE_ENV="production")
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": [handle]}
    with (c.data / "host.log").open("ab") as log:
        os.set_handle_inheritable(handle, True)
        try:
            return subprocess.Popen([c.electron, DAILY / "host.mjs"], cwd=c.root, env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            os.set_handle_inheritable(handle, False)


def enable(c):
    with host_guard(c.home) as handle:
        marker = read_json(c.marker)
        c.port = endpoint(marker, c.env, c.port, c.root)
        status = read_json(c.status)
        owners = listeners(c.port)
        if healthy(c, marker, status, owners):
            return
        if owners or (marker and pid_alive(marker["pid"])):
            raise Blocked(f"本地端口 {c.port} 或标记进程仍在使用。可先保存配对。"
                          "请等当前工作结束，正常退出原服务，再次运行同一启动命令。")
        if not c.acceptance and c.port != 3001 and listeners(3001):
            raise Blocked("默认端口 3001 已有监听，无法确认是否为日常服务。请核对后再次运行工作台。")
        if handle is None or (status and pid_alive(status.get("pid"))):
            raise Blocked("另一个启动器或服务进程仍在运行。请稍候再打开工作台，并核对 host.log。")
        # Also catches reserved ports and sockets bound but not yet listening.
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((LOCAL, c.port))
        except OSError as exc:
            raise Blocked(f"无法独占本地端口 {c.port}。请核对端口占用或保留状态，再次运行工作台。") from exc
        child = start_host(c, handle)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise Blocked(f"带桥服务已退出（{child.returncode}）。请检查 {c.data / 'host.log'} 后重试。")
            try:
                current = read_json(c.marker)
                pulse = read_json(c.status)
                if (current and endpoint(current, c.env, c.port, c.root) == c.port
                        and current.get("pid") == child.pid
                        and healthy(c, current, pulse, listeners(c.port))):
                    # Mint a stop receipt only for a child we actually spawned.
                    # Popen still holds its kernel process handle across this lookup.
                    with process_handle(child.pid, 0x1000) as proc:  # QUERY_LIMITED_INFORMATION
                        receipt = stop_receipt(c, pulse, proc)
                    write_json(c.data / "host-owner.json", receipt)
                    return
            except (ValueError, OSError):
                pass  # marker/status can be mid-write during startup
            time.sleep(0.2)
        raise Blocked(f"尚未确认端口 {c.port} 的带桥心跳。请查看 {c.data / 'host.log'}，稍后重开工作台。")


def stop_receipt(c, pulse, proc):
    return dict(pid=pulse["pid"], instance=pulse["instance"], created=creation_time(proc),
                port=c.port, store=str(c.store), cc=str(c.cc), desktop=str(c.desktop),
                home=str(c.home), executable=str(c.electron), script=str(DAILY / "host.mjs"))


def stop(c):
    marker = read_json(c.marker)
    c.port = endpoint(marker, c.env, c.port, c.root)
    pulse = read_json(c.status)
    if not healthy(c, marker, pulse, listeners(c.port)):
        raise Blocked("无法确认当前监听属于本工作台的健康带桥服务。已保留进程。")
    pid = pulse["pid"]
    # Pin the process object throughout verification and termination. A reused
    # numeric PID can never redirect TerminateProcess to a different object.
    with host_guard(c.home), process_handle(pid, 0x101001) as proc:
        # SYNCHRONIZE | QUERY_LIMITED_INFORMATION | TERMINATE
        receipt = read_json(c.data / "host-owner.json")
        if receipt != stop_receipt(c, pulse, proc):
            raise Blocked("启动凭据与当前进程、实例或索引不匹配。已保留进程。")
        terminate = kernel_function("TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
        if not terminate(proc, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        wait = kernel_function("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD)
        if wait(proc, 5000) != 0:
            raise Blocked(f"已请求结束 PID {pid}，尚未确认退出。请检查进程状态。")
        write_json(c.status, dict(pulse, state="offline", heartbeat=time.time(), message="带桥服务已手动停止"))
    return pid


def blocker_status(c, message):
    # Never replace a host heartbeat or let a mismatched live one enable the UI.
    target = c.data / "launcher-blocked.json"
    write_json(target, dict(state="offline", message=message, heartbeat=time.time(), pid=os.getpid(),
                            port=c.port, instance="启动器", store=str(c.store)))
    return target


def same_file(a, b):
    return a == b or (a.exists() and b.exists() and a.samefile(b))


def frontend_pin():
    return DAILY / "data" / "frontend-pin.json"


def promote_frontend(expected):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise Blocked("Expected a full SHA-256 digest from QA.")
    digest = hashlib.sha256((DAILY / "panel.py").read_bytes()).hexdigest()
    if digest != expected.lower():
        raise Blocked("panel.py does not match the supplied SHA-256.")
    pin = frontend_pin()
    pin.parent.mkdir(parents=True, exist_ok=True)
    write_json(pin, {"sha256": digest})
    return digest


def select_frontend():
    """Return the script, its approved bytes if promoted, and any pin error."""
    stable = DAILY / "panel_stable.py"
    problem = None
    try:
        pin = read_json(frontend_pin())
        if pin is None:
            pin = read_json(DAILY / "release-frontend.json")
        if pin is not None and pin != {"frontend": "stable"}:
            candidate = DAILY / "panel.py"
            source = candidate.read_bytes()
            if pin != {"sha256": hashlib.sha256(source).hexdigest()}:
                raise Blocked("panel.py does not match its activation record.")
            return candidate, source, None
    except (OSError, ValueError, RecursionError) as exc:
        problem = str(exc)
    if not stable.is_file():
        raise FileNotFoundError(stable)
    return stable, None, problem


# Execute the exact bytes verified by the parent. Supply normal script identity
# and arguments, with cwd set to DAILY for sibling imports.
RUN_VERIFIED = (
    "import sys\n"
    "del sys.argv[0]\n"
    "__file__ = sys.argv[0]\n"
    "exec(compile(sys.stdin.buffer.read(), __file__, 'exec'))\n"
)


def launch(acceptance):
    panel, source, pin_error = select_frontend()
    c = configure(acceptance, os.environ)
    status = c.status
    problem = None
    if not acceptance:
        try:
            c.cc = database_path(c.root, c.home, c.env)
        except (OSError, ValueError) as exc:
            # A directory makes the read-only catalog query fail visibly. Do
            # not silently offer sessions from a guessed production database.
            c.cc = c.data
            problem = exc
    if any(same_file(c.store, index) for index in (c.cc, c.desktop)):
        raise Blocked("桥存储与会话索引指向同一文件。请核对数据库路径。")
    Store(c.store)
    try:
        if problem:
            raise problem
        if acceptance:
            init_acceptance(c)
        elif same_file(c.cc, c.desktop):
            raise Blocked("CloudCLI 与 Desktop 索引指向同一文件。请核对数据库路径。")
        elif os.environ.get("HOME") and local_path(os.environ["HOME"]) != c.home:
            raise Blocked("HOME 与 USERPROFILE 指向不同账户目录。请从日常 Windows 账户打开工作台。")
        enable(c)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        status = blocker_status(c, str(exc))
    with (c.data / "panel.log").open("ab") as log:
        if pin_error:
            log.write(f"Using panel_stable.py: {pin_error}\n".encode("utf-8"))
            log.flush()
        args = [panel, "--cc-db", c.cc, "--desktop-db", c.desktop, "--store", c.store,
                "--cc-url", f"http://{LOCAL}:{c.port}", "--status-file", status]
        panel_env = dict(c.env, DAILY_CLOUDCLI_ROOT=str(c.root), DAILY_PANEL_ELECTRON=str(c.electron))
        options = dict(cwd=DAILY, env=panel_env, stdout=log, stderr=subprocess.STDOUT,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if source is None:
            subprocess.Popen([sys.executable, *args], stdin=subprocess.DEVNULL, **options)
        else:
            with tempfile.TemporaryFile() as verified:
                verified.write(source)
                verified.seek(0)
                subprocess.Popen([sys.executable, "-c", RUN_VERIFIED, *args], stdin=verified, **options)


def doctor(project):
    """Probe the installed runtime without starting the bridge or changing a profile."""
    c = configure(False, os.environ)
    checks = []

    def check(label, ok, detail):
        checks.append(bool(ok))
        print(f"[{'OK' if ok else '缺失'}] {label}  {detail}")

    try:
        import tkinter
        check('Python / Tk', True, f'{sys.version.split()[0]} / Tk {tkinter.TkVersion}')
    except ImportError:
        check('Python / Tk', False, '请为当前 Python 安装 Tcl/Tk。')
    check('CloudCLI Desktop', c.electron.is_file(), c.electron)
    package = read_json(c.root / 'package.json') or {}
    server_ok = (package.get('name') == '@cloudcli-ai/cloudcli' and package.get('version') == VERSION
                 and (c.root / 'dist-server/server/index.js').is_file())
    check('CloudCLI 服务包', server_ok, f'{VERSION} · {c.root}')
    cc = database_path(c.root, c.home, c.env)
    check('Claude 会话索引', cc.is_file(), cc)
    check('Desktop 会话索引', c.desktop.is_file(), c.desktop)
    panel, _, problem = select_frontend()
    check('工作台面板', not problem, problem or panel.name)
    if c.electron.is_file():
        # Run the same queue --help capability check used by the worker.
        try:
            probe = subprocess.run([c.electron, '--input-type=module', '-e',
                "import {queueCli} from './codex-cli.mjs';console.log(await queueCli())"],
                cwd=DAILY, env=dict(c.env, ELECTRON_RUN_AS_NODE='1'), capture_output=True,
                encoding='utf8', errors='replace', timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            check('Codex queue', probe.returncode == 0,
                  probe.stdout.strip() if probe.returncode == 0 else '检查是否已安装支持 queue 的 Codex CLI。')
        except (OSError, subprocess.TimeoutExpired) as exc:
            check('Codex queue', False, str(exc))
    claude = shutil.which('claude.exe')
    if claude:
        try:
            result = subprocess.run([claude, 'agents', '--json'], capture_output=True,
                                    encoding='utf8', errors='replace', timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            sessions = json.loads(result.stdout)
            if not isinstance(sessions, list):
                raise ValueError('未知的会话清单格式')
            count = sum(isinstance(s, dict) and s.get('kind') == 'interactive' for s in sessions)
            print(f'[可选] 独立 Claude CLI  {claude} · 发现 {count} 个交互终端')
        except (ValueError, OSError, subprocess.SubprocessError):
            print(f'[可选] 独立 Claude CLI  {claude} · 当前无法列出交互终端')
    else:
        print('[可选] 独立 Claude CLI  未发现；CloudCLI 配对仍可使用。')
    if project is not None:
        if __package__:
            from .project_hooks import has_receipt_hooks
        else:
            from project_hooks import has_receipt_hooks
        configured = has_receipt_hooks(project, c.store)
        print(f"[可选] 项目收件  {project.resolve()} · "
              + ('已配置，请在 /hooks 核对信任状态。' if configured else '可在面板选择「设置项目收件」。'))
    return all(checks)


def main():
    parser = argparse.ArgumentParser(description="Windows 双会话工作台",
        epilog="端口占用时可先管理配对。待当前工作结束后正常退出 CloudCLI 服务，再次运行同一入口。"
               "回滚：完成工作后运行同模式 --stop，再使用原 CloudCLI 入口。"
               "也可保存工作并正常重启 Windows。关闭面板只关闭界面。")
    parser.add_argument("--acceptance", action="store_true",
                        help="使用 daily/data/acceptance 下的独立账户、索引和桥存储（默认端口 33180）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--doctor", action="store_true", help="只读检查 Windows 工作台运行依赖。")
    parser.add_argument("--project", type=Path, help="自检时检查指定项目的收件 hook 配置。")
    action.add_argument("--stop", action="store_true",
                        help="核实启动凭据后直接结束该模式的带桥进程。请先完成所有运行中的工作。")
    action.add_argument("--promote-frontend", metavar="QA_SHA256",
                        help="Activate the exact QA-reviewed panel.py for future launches in both modes.")
    action.add_argument("--rollback-frontend", action="store_true",
                        help="Restore panel_stable.py for future launches in both modes.")
    action.add_argument("--verify-frontend", action="store_true",
                        help="Print the selected frontend and SHA-256. Exit 1 for an unusable pin or file.")
    args = parser.parse_args()
    try:
        if args.project is not None and not args.doctor:
            parser.error('--project 需要配合 --doctor')
        if args.doctor:
            if not doctor(args.project):
                parser.exit(1)
        elif args.promote_frontend is not None:
            digest = promote_frontend(args.promote_frontend)
            print(f"Activated panel.py SHA-256 {digest}")
        elif args.rollback_frontend:
            pin = frontend_pin()
            pin.parent.mkdir(parents=True, exist_ok=True)
            write_json(pin, {"frontend": "stable"})
            print("Selected panel_stable.py")
        elif args.verify_frontend:
            panel, source, problem = select_frontend()
            digest = hashlib.sha256(panel.read_bytes() if source is None else source).hexdigest()
            print(f"{panel.name} SHA-256 {digest}")
            if problem:
                parser.exit(1, f"Invalid frontend pin: {problem}\n")
        elif sys.platform != "win32":
            parser.exit(1, "需要 Windows。\n")
        elif args.stop:
            c = configure(args.acceptance, os.environ)
            if not args.acceptance:
                c.cc = database_path(c.root, c.home, c.env)
            print(f"已结束经核实的带桥服务 PID {stop(c)}。")
        else:
            launch(args.acceptance)
    except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
        parser.exit(1, f"工作台启动失败：{exc}\n")


if __name__ == "__main__":
    main()
