from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from daily import launcher as launch


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.daily = Path(temp.name).resolve() / "测试 workbench"
        self.daily.mkdir()
        self.enterContext(patch.object(launch, "DAILY", self.daily))
        self.env = dict(os.environ, USERPROFILE=temp.name, LOCALAPPDATA=temp.name)
        self.env["CLOUDCLI_SERVER_DIR"] = str(self.daily / "fixture-server")

    def frontend_command(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.object(sys, "argv", ["launcher.py", *args]),
              patch.object(launch, "configure", side_effect=AssertionError("unexpected host access")),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            try:
                launch.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_frontend_promotion_verification_and_rollback(self):
        stable = self.daily / "panel_stable.py"
        candidate = self.daily / "panel.py"
        stable.write_bytes(b"stable")
        candidate.write_bytes(b"reviewed")
        digest = hashlib.sha256(b"reviewed").hexdigest()
        stable_status = f"panel_stable.py SHA-256 {hashlib.sha256(b'stable').hexdigest()}\n"
        self.assertEqual(self.frontend_command("--verify-frontend"), (0, stable_status, ""))
        self.assertFalse((self.daily / "data").exists())
        launch.write_json(self.daily / "release-frontend.json", {"sha256": digest})
        self.assertEqual(self.frontend_command("--verify-frontend"), (0, f"panel.py SHA-256 {digest}\n", ""))
        self.assertFalse((self.daily / "data").exists())
        for invalid in ("", digest[:12], "0" * 64):
            self.assertEqual(self.frontend_command("--promote-frontend", invalid)[0], 1)
            self.assertFalse(launch.frontend_pin().exists())
        self.assertEqual(self.frontend_command("--promote-frontend", digest.upper())[0], 0)
        self.assertEqual(self.frontend_command("--promote-frontend", "0" * 64)[0], 1)
        self.assertEqual(self.frontend_command("--verify-frontend"), (0, f"panel.py SHA-256 {digest}\n", ""))
        self.assertEqual(self.frontend_command("--rollback-frontend")[0], 0)
        self.assertEqual(self.frontend_command("--verify-frontend"), (0, stable_status, ""))
        self.assertEqual(self.frontend_command("--promote-frontend", digest)[0], 0)

        original_stat = candidate.stat()
        candidate.write_bytes(b"modified")
        os.utime(candidate, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        for case in ("modified", "missing", "corrupt pin", "wrong schema"):
            with self.subTest(case=case):
                if case == "missing":
                    candidate.unlink()
                elif case == "corrupt pin":
                    candidate.write_bytes(b"reviewed")
                    launch.frontend_pin().write_bytes(b"{")
                elif case == "wrong schema":
                    launch.write_json(launch.frontend_pin(), {"path": "panel.py"})
                code, selected, error = self.frontend_command("--verify-frontend")
                self.assertEqual((code, selected), (1, stable_status))
                self.assertIn("Invalid frontend pin", error)

        for _ in range(2):
            self.assertEqual(self.frontend_command("--rollback-frontend")[0], 0)
            self.assertEqual(self.frontend_command("--verify-frontend"), (0, stable_status, ""))
        stable.unlink()
        self.assertEqual(self.frontend_command("--verify-frontend")[0], 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows entry point")
    def test_promoted_child_runs_verified_bytes_after_candidate_replacement(self):
        panel = self.daily / "panel.py"
        (self.daily / "helper.py").write_text("value = 'reviewed'")
        source = b"""from pathlib import Path
import sys, helper
assert __name__ == '__main__' and __file__ == sys.argv[0]
assert Path(__file__).resolve() == Path.cwd() / 'panel.py'
assert '--store' in sys.argv
Path('result').write_text(helper.value)
"""
        panel.write_bytes(source)
        launch.promote_frontend(hashlib.sha256(source).hexdigest())
        spawn = subprocess.Popen

        def replace_then_spawn(*args, **kwargs):
            panel.write_bytes(b"raise RuntimeError('unreviewed')")
            with spawn(*args, **kwargs) as child:
                self.assertEqual(child.wait(timeout=10), 0)

        c = launch.configure(True, self.env)
        with (patch.object(launch, "configure", return_value=c),
              patch.object(launch, "enable"), patch.object(subprocess, "Popen", side_effect=replace_then_spawn)):
            launch.launch(True)
        self.assertEqual((self.daily / "result").read_text(), "reviewed")

    def test_server_dotenv_precedence_and_cwd(self):
        (self.daily / ".env").write_bytes(b"SECRET=\xff\n# DATABASE_PATH=unused\n"
                                          b"DATABASE_PATH=\nDATABASE_PATH=index=1.db\nDATABASE_PATH=no\n")
        self.assertEqual(launch.database_path(self.daily, self.daily.parent, {}), self.daily / "index=1.db")
        self.assertEqual(launch.database_path(self.daily, self.daily, {"DATABASE_PATH": "override"}),
                         self.daily / "override")
        with self.assertRaises(launch.Blocked):
            launch.database_path(self.daily, self.daily, {"DATABASE_PATH": "//remote/share/db"})

    def test_local_endpoint_ambiguity(self):
        marker = {"host": launch.LOCAL, "url": "http://localhost:33001", "port": 33001, "pid": 1}
        self.assertEqual(launch.endpoint(marker, {}, 3001, self.daily), 33001)
        self.assertEqual(launch.endpoint(None, {}, 3001, self.daily), 3001)
        for bad in (dict(marker, port=3001), dict(marker, host="0.0.0.0"),
                    dict(marker, url="http://example.org:33001"), dict(marker, pid=False)):
            with self.subTest(bad=bad), self.assertRaises(launch.Blocked):
                launch.endpoint(bad, {}, 3001, self.daily)
        with self.assertRaises(launch.Blocked):
            launch.endpoint(marker, {"SERVER_PORT": "3001"}, 3001, self.daily)

    def test_acceptance_uses_only_fixture_profiles_and_indexes(self):
        c = launch.configure(True, dict(self.env, DATABASE_PATH="private", CODEX_HOME="private",
                             CLAUDE_CONFIG_DIR="private", CODEX_THREAD_ID="private", SERVER_PORT="3001"))
        launch.init_acceptance(c)
        self.assertEqual(c.home.parent, self.daily / "data/acceptance")
        self.assertTrue(c.cc.is_relative_to(c.home))
        self.assertTrue(c.desktop.is_relative_to(c.home))
        self.assertTrue(Path(c.env["CLAUDE_CONFIG_DIR"]).is_relative_to(c.home))
        self.assertTrue(Path(c.env["CODEX_HOME"]).is_relative_to(c.home))
        self.assertNotIn("CODEX_THREAD_ID", c.env)
        self.assertNotIn("SERVER_PORT", c.env)
        self.assertTrue(c.cc.is_file() and c.desktop.is_file())

    def test_ready_requires_fresh_status_and_kernel_owner(self):
        c = launch.configure(True, self.env)
        pid = os.getpid()
        status = dict(state="ready", instance="test", pid=pid, port=c.port, store=str(c.store),
                      heartbeat=time.time())
        marker = {"pid": pid}
        owners = {(launch.LOCAL, pid)}
        with patch.object(launch, "pid_alive", return_value=True):
            self.assertTrue(launch.healthy(c, marker, status, owners))
            for change in (dict(port=3001), dict(store=str(c.cc)), dict(pid=pid+1),
                           dict(heartbeat=time.time()-60), dict(state="starting")):
                self.assertFalse(launch.healthy(c, marker, dict(status, **change), owners))
            self.assertFalse(launch.healthy(c, marker, status, {(launch.LOCAL, pid+1)}))

    @unittest.skipUnless(sys.platform == "win32", "Windows entry point")
    def test_daily_panel_receives_selected_paths_during_blocker(self):
        (self.daily / "panel_stable.py").touch()
        (self.daily / "panel.py").touch()
        env = dict(USERPROFILE=str(self.daily), LOCALAPPDATA=str(self.daily),
                   DATABASE_PATH="normal-db.sqlite", CODEX_HOME=str(self.daily / "desktop"))
        with patch.dict(os.environ, env, clear=True):
            c = launch.configure(False, os.environ)
            c.marker.parent.mkdir()
            c.marker.write_text(json.dumps({"pid": os.getpid(), "port": 33333}))
            with patch.object(launch, "listeners", return_value=set()), patch.object(subprocess, "Popen") as popen:
                launch.launch(False)
        args = popen.call_args.args[0]
        self.assertEqual(args[:2], [sys.executable, self.daily / "panel_stable.py"])
        self.assertEqual(args[args.index("--cc-db")+1], c.root / "normal-db.sqlite")
        self.assertEqual(args[args.index("--desktop-db")+1], self.daily / "desktop/state_5.sqlite")
        self.assertIn(c.store, args)
        self.assertIn("http://127.0.0.1:33333", args)
        popen.assert_called_once()
        with sqlite3.connect(c.store) as db:
            self.assertEqual(db.execute("select count(*) from bindings").fetchone(), (0,))
        db.close()

    @unittest.skipUnless(sys.platform == "win32", "uses Windows process and socket APIs")
    def test_ordinary_listener_keeps_panel_available_then_rerun_starts_once(self):
        # Use Python in place of Electron and an inert socket in place of CloudCLI.
        # Exercise CreateProcess, hidden stdio, inherited mutex and kernel PID lookup.
        c = launch.configure(True, self.env)
        c.electron = Path(sys.executable)
        (c.root / "dist-server/server").mkdir(parents=True)
        (c.root / "dist-server/server/index.js").touch()
        (c.root / "package.json").write_text(json.dumps({"name":"@cloudcli-ai/cloudcli","version":"1.37.9"}))
        (self.daily / "panel_stable.py").touch()
        (self.daily / "host.mjs").write_text(FAKE_HOST, encoding="utf-8")
        launch.init_acceptance(c)
        normal = self.enterContext(socket.socket())
        normal.bind((launch.LOCAL, 0))
        normal.listen()
        port = normal.getsockname()[1]
        marker = dict(port=port, host=launch.LOCAL, pid=os.getpid())
        c.marker.write_text(json.dumps(marker))
        damaged = bytes(222)
        c.status.write_bytes(damaged)
        with patch.object(launch, "configure", return_value=c), patch.object(subprocess, "Popen") as popen:
            launch.launch(True)
        args = popen.call_args.args[0]
        self.assertEqual(args[:2], [sys.executable, self.daily / "panel_stable.py"])
        self.assertIn(f"http://127.0.0.1:{port}", args)
        blocked = launch.read_json(args[args.index("--status-file")+1])
        self.assertEqual(blocked["state"], "offline")
        self.assertIn(str(port), blocked["message"])
        self.assertTrue(c.store.is_file())
        self.assertEqual(launch.listeners(port), {(launch.LOCAL, os.getpid())})
        self.assertEqual(c.status.read_bytes(), damaged)
        normal.close()
        with self.assertRaises(launch.Blocked):
            launch.enable(c)  # marker process is still alive despite no listener
        c.marker.unlink()
        c.env["SERVER_PORT"] = str(port)
        c.port = port
        with launch.host_guard(c.home), patch.object(launch, "start_host") as duplicate:
            with self.assertRaises(launch.Blocked):
                launch.enable(c)  # another launcher has the mutex, no heartbeat yet
            duplicate.assert_not_called()
        children = []
        spawn = launch.start_host

        def tracked_spawn(*args):
            child = spawn(*args)
            children.append(child)
            return child

        with patch.object(launch, "start_host", side_effect=tracked_spawn):
            try:
                launch.enable(c)
                launch.enable(c)
                self.assertEqual(len(children), 1)
                with launch.host_guard(c.home) as guard:
                    self.assertIsNone(guard)
                info = launch.read_json(c.status)
                self.assertEqual(info["python"], str(Path(sys.executable).resolve()))
                self.assertEqual(info["desktop"], str(c.desktop))
                self.assertEqual(info["db"], str(c.cc))
                self.assertEqual(info["root"], str(c.root))
                self.assertEqual(info["cwd"], str(c.root))
                record_path = c.data / "host-owner.json"
                record = launch.read_json(record_path)
                for changed in (dict(created=0), dict(pid=os.getpid()), dict(instance="different")):
                    launch.write_json(record_path, dict(record, **changed))
                    with self.assertRaises(launch.Blocked):
                        launch.stop(c)
                    self.assertIsNone(children[0].poll())
                launch.write_json(record_path, record)
                self.assertEqual(launch.stop(c), children[0].pid)
                self.assertEqual(children[0].wait(timeout=2), 0)
                self.assertFalse(launch.listeners(port))
                self.assertEqual(launch.read_json(c.status)["state"], "offline")
            finally:
                # Only our synthetic child reads this completion signal.
                (c.data / "finish-test").touch()
                for child in children:
                    child.wait(timeout=8)
        with launch.host_guard(c.home) as guard:
            self.assertIsNotNone(guard)


FAKE_HOST = """
import json, os, socket, time
from pathlib import Path
e = os.environ
port = int(e['SERVER_PORT'])
sock = socket.socket()
sock.bind((e['HOST'], port))
sock.listen()
marker = Path(e['USERPROFILE']) / '.cloudcli/local-server.json'
marker.write_text(json.dumps(dict(port=port, host=e['HOST'], pid=os.getpid())))
status = Path(e['DAILY_STATUS_FILE'])
status.write_text(json.dumps(dict(instance='fake', heartbeat=time.time(), state='ready',
    store=e['DAILY_BRIDGE_STORE'], port=port, pid=os.getpid(), python=e['DAILY_PYTHON'],
    db=e['DATABASE_PATH'], desktop=e['DAILY_DESKTOP_DB'], root=e['DAILY_CLOUDCLI_ROOT'], cwd=os.getcwd())))
deadline = time.monotonic() + 20
while not status.with_name('finish-test').exists() and time.monotonic() < deadline:
    time.sleep(.05)
"""


if __name__ == "__main__":
    unittest.main()
