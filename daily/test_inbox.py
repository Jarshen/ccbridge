import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from daily import inbox
from daily.storage import Store


# Independent connection and the worker owner's required atomic return claim.
NATIVE_CLAIM = """UPDATE mail SET state='returning' WHERE id=? AND source='desktop'
    AND state='received' AND NOT EXISTS (SELECT 1 FROM inbox_leases l
    WHERE l.binding_id=mail.binding_id AND l.mail_id=mail.id AND l.expires_at>unixepoch())"""


class InboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test #1.sqlite3"
        self.store = Store(self.path)

        def session():
            return dict(id=str(uuid4()), native_id=str(uuid4()), title="fixture", cwd=temp.name)

        self.desktop = session()
        self.thread = self.desktop["native_id"]
        self.binding = self.store.bind(session(), self.desktop)
        self.id = str(uuid4())
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute(inbox.LEASE_SCHEMA)
        self.scope = self.id, self.binding, self.thread

    def cli(self, cmd, *args, event=None, thread=True):
        flags = [] if cmd == "hook" else ["--binding", str(self.binding), "--id", self.id]
        env = dict(os.environ, DAILY_BRIDGE_STORE=str(self.path))
        env.pop("CODEX_THREAD_ID", None)
        if thread:
            env["CODEX_THREAD_ID"] = self.thread if thread is True else thread
        return subprocess.run([sys.executable, "-B", str(Path(inbox.__file__).resolve()),
                               cmd, *flags, *args], cwd=self.path.parent, env=env,
                              input=json.dumps(event), capture_output=True, text=True, timeout=6)

    def send(self):
        self.store.desktop_send(self.thread, "private original", self.id, "deferred",
                                expected_binding=self.binding)

    def reply(self):
        self.db.execute("UPDATE mail SET state='received', reply='private CC answer' WHERE id=?", (self.id,))

    def test_reserve_before_send_real_wait_and_conditional_receipt(self):
        armed = json.loads(self.cli("arm").stdout)
        self.assertEqual("armed", armed["status"])
        self.assertNotIn("receipt", armed)
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM mail").fetchone()[0])
        self.send()
        self.assertEqual(armed, json.loads(self.cli("arm", "--lease-seconds", "1").stdout))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting = pool.submit(self.cli, "wait", "--seconds", "3")
            time.sleep(0.35)
            self.assertFalse(waiting.done())
            self.reply()
            self.assertEqual(0, self.db.execute(NATIVE_CLAIM, (self.id,)).rowcount)
            seen = json.loads(waiting.result().stdout)
        self.assertEqual("private CC answer", seen["reply"])
        self.assertNotIn("private original", str(seen))
        self.assertEqual("received", self.db.execute("SELECT state FROM mail").fetchone()[0])
        self.assertEqual("not_accepted", json.loads(self.cli("accept", "--receipt", "wrong").stdout)["status"])
        for expected in ("accepted", "not_accepted"):
            self.assertEqual(expected, json.loads(self.cli("accept", "--receipt", seen["receipt"]).stdout)["status"])
        self.assertEqual(0, self.db.execute(NATIVE_CLAIM, (self.id,)).rowcount)
        self.assertEqual("already_received", json.loads(self.cli("wait").stdout)["status"])

    def test_preview_then_expiry_hands_off_without_ack_or_double_job(self):
        inbox.arm(self.db, self.scope, 60)
        self.send()
        self.reply()
        seen = inbox.read(self.db, self.scope, 0)
        # Force the exact equality boundary using the same database clock as the worker.
        self.db.execute("UPDATE inbox_leases SET expires_at=unixepoch()")
        self.assertEqual({}, inbox.notice(self.db, self.thread))
        self.assertEqual("expired", inbox.arm(self.db, self.scope, 60)["status"])
        self.assertEqual({"status": "expired"}, inbox.read(self.db, self.scope, 0))
        self.assertEqual({"status": "not_accepted"}, inbox.accept(self.db, self.scope, seen["receipt"]))
        self.assertEqual(1, self.db.execute(NATIVE_CLAIM, (self.id,)).rowcount)
        self.assertEqual(0, self.db.execute(NATIVE_CLAIM, (self.id,)).rowcount)
        self.assertEqual("unavailable", inbox.arm(self.db, self.scope, 60)["status"])

    def test_racing_late_arm_and_stale_native_selection_have_one_owner(self):
        self.send()
        self.reply()
        gate = threading.Barrier(2)

        def native():
            with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                self.assertEqual("received", db.execute("SELECT state FROM mail").fetchone()[0])
                gate.wait(timeout=2)
                return db.execute(NATIVE_CLAIM, (self.id,)).rowcount

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(native)
            gate.wait(timeout=2)
            result = inbox.arm(self.db, self.scope, 60)
            claimed = future.result()
        self.assertEqual("unavailable" if claimed else "armed", result["status"])
        if claimed:
            self.assertNotIn("reply", inbox.read(self.db, self.scope, 0))
        else:
            seen = inbox.read(self.db, self.scope, 0)
            self.assertEqual({"status": "accepted"}, inbox.accept(self.db, self.scope, seen["receipt"]))

    def test_receipt_is_specific_to_mail_and_concurrent_accepts_have_one_winner(self):
        inbox.arm(self.db, self.scope, 60)
        self.send()
        self.reply()
        token = inbox.read(self.db, self.scope, 0)["receipt"]
        other = str(uuid4()), self.binding, self.thread
        inbox.arm(self.db, other, 60)
        self.store.desktop_send(self.thread, "another request", other[0], "required", expected_binding=self.binding)
        self.db.execute("UPDATE mail SET state='received',reply='another answer' WHERE id=?", (other[0],))
        self.assertEqual({"status": "not_accepted"}, inbox.accept(self.db, other, token))
        gate = threading.Barrier(2)

        def accept_once():
            with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                gate.wait(timeout=2)
                return inbox.accept(db, self.scope, token)["status"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = [pool.submit(accept_once) for _ in range(2)]
            self.assertCountEqual(["accepted", "not_accepted"], [claim.result() for claim in claims])
        self.assertEqual(0, self.db.execute(NATIVE_CLAIM, (self.id,)).rowcount)
        self.assertEqual("ready", inbox.read(self.db, other, 0)["status"])

    def test_wrong_scope_token_and_retired_binding(self):
        inbox.arm(self.db, self.scope, 60)
        self.send()
        self.reply()
        token = inbox.read(self.db, self.scope, 0)["receipt"]
        for scope in ((str(uuid4()), self.binding, self.thread), (self.id, self.binding+1, self.thread),
                      (self.id, self.binding, str(uuid4()))):
            self.assertEqual({"status": "unavailable"}, inbox.read(self.db, scope, 0))
            self.assertEqual({"status": "not_accepted"}, inbox.accept(self.db, scope, token))
        cc_id = str(uuid4())
        cc_scope = cc_id, *self.scope[1:]
        inbox.arm(self.db, cc_scope, 60)
        self.db.execute("""INSERT INTO mail(id,binding_id,source,body,state,reply)
                           VALUES (?,?,'cc','private new job','received','private answer')""", (cc_id, self.binding))
        self.assertNotIn("reply", inbox.read(self.db, cc_scope, 0))
        self.assertNotEqual(0, self.cli("wait", thread=False).returncode)
        self.db.execute("UPDATE mail SET state='returned'")
        self.store.unbind(self.binding)
        self.assertEqual({"status": "unavailable"}, inbox.read(self.db, self.scope, 0))
        self.assertEqual({"status": "not_accepted"}, inbox.accept(self.db, self.scope, token))
        self.assertEqual({}, inbox.notice(self.db, self.thread))

    def test_any_subsequent_tool_can_signal_only_scoped_metadata(self):
        self.cli("arm").check_returncode()
        self.send()
        event = {"session_id": self.thread, "turn_id": str(uuid4()), "hook_event_name": "PostToolUse",
                 "tool_name": "apply_patch", "tool_response": "unrelated private tool output"}
        self.assertEqual({}, json.loads(self.cli("hook", event=event).stdout))
        self.reply()
        result = self.cli("hook", event=event, thread=False)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.id, context)
        self.assertNotIn("private", context)
        self.assertNotIn(inbox.read(self.db, self.scope, 0)["receipt"], context)
        self.assertEqual("", result.stderr)
        for override in ({"session_id": str(uuid4())}, {"hook_event_name": "Stop"},
                         {"agent_id": str(uuid4())}, {"turn_id": None}):
            self.assertEqual({}, json.loads(self.cli("hook", event=event | override).stdout))
        self.assertEqual({}, json.loads(self.cli("hook", event=event, thread=str(uuid4())).stdout))
        self.assertEqual("received", self.db.execute("SELECT state FROM mail").fetchone()[0])

    def test_bounded_wait_expires_and_native_claims_without_any_reader(self):
        self.cli("arm", "--lease-seconds", "1").check_returncode()
        self.send()
        started = time.monotonic()
        self.assertEqual("expired", json.loads(self.cli("wait", "--seconds", "3").stdout)["status"])
        self.assertLess(time.monotonic() - started, 2.5)
        self.reply()
        self.assertEqual(1, self.db.execute(NATIVE_CLAIM, (self.id,)).rowcount)

    def test_wait_deadline_and_missing_store(self):
        self.cli("arm").check_returncode()
        started = time.monotonic()
        self.assertEqual("pending", json.loads(self.cli("wait", "--seconds", "0.15").stdout)["status"])
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        for invalid in ("nan", "inf", "-1", "61"):
            self.assertNotEqual(0, self.cli("wait", "--seconds", invalid).returncode)
        self.path = self.path.with_name("missing.sqlite3")
        self.assertNotEqual(0, self.cli("arm").returncode)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
