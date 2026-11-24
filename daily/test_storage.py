from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from daily.storage import Store
from daily import paired_inbox


def session(label):
    return dict(id=label, native_id=str(uuid4()), title=label, cwd=f"/example/{label}")


class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test store.sqlite3"
        self.store = Store(self.path)
        self.cc, self.desktop = session("cc"), session("desktop")
        self.thread = self.desktop["native_id"]
        self.binding = self.store.bind(self.cc, self.desktop)
        # Independent SQL connection, like the worker.
        self.worker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.worker.close)

    def incoming(self, state, reply_mode="required"):
        mail_id = str(uuid4())
        self.worker.execute("""
            INSERT INTO mail(id,binding_id,source,body,state,reply_mode) VALUES (?,?,'cc','hi',?,?)
        """, (mail_id, self.binding, state, reply_mode))
        return mail_id

    def test_plain_send_uses_active_project_hook_and_follow_reserves_turn(self):
        turn = str(uuid4())
        self.worker.execute("CREATE TABLE inbox_tool_turns(binding_id,turn_id,seen_at)")
        self.worker.execute("INSERT INTO inbox_tool_turns VALUES(?,?,unixepoch())", (self.binding, turn))
        self.store.desktop_send(self.thread, "progress", str(uuid4()), handoff_turn=turn, prefer_hook=True)
        self.assertIsNone(self.worker.execute("SELECT turn FROM turn_handoffs").fetchone())
        self.store.desktop_send(self.thread, "need a decision", str(uuid4()), handoff_turn=turn)
        self.assertEqual(self.worker.execute("SELECT turn FROM turn_handoffs").fetchone(), (turn,))

    def follow_fixture(self):
        turn = str(uuid4())
        history = self.path.with_name("history.sqlite")
        with closing(sqlite3.connect(history)) as db, db:
            db.execute("CREATE TABLE thread_turns(thread_id, turn_id, status, rollout_ordinal)")
            db.execute("INSERT INTO thread_turns VALUES(?,?,'inProgress',1)", (self.thread, turn))
        mail = self.incoming("unknown", "none")
        self.worker.execute("CREATE TABLE paired_inboxes(binding_id PRIMARY KEY,turn,listen_until)")
        self.worker.execute("CREATE TABLE paired_letters(mail_id PRIMARY KEY,target_turn,taken_at)")
        self.worker.execute("INSERT INTO paired_letters VALUES(?,?,NULL)", (mail, turn))
        self.worker.execute("UPDATE mail SET detail='配对收件箱待领取' WHERE id=?", (mail,))
        return turn, history, mail

    def test_follow_output_failure_requires_review_and_blocks_duplicate_take(self):
        turn, history, mail = self.follow_fixture()
        with patch('builtins.print', side_effect=BrokenPipeError), self.assertRaises(BrokenPipeError):
            paired_inbox.follow(self.path, history, self.thread, turn)
        receipt = self.store.receipts(history, mail)[0]
        self.assertEqual(receipt['receipt'], 'unknown')
        self.assertIn('勿重复投递', receipt['next_action'])
        meta = {'threadId': self.thread, 'x-codex-turn-metadata': {'turn_id': turn}}
        self.assertEqual(paired_inbox.receive(self.path, history, 'take', {'id': mail}, meta)['status'], 'unavailable')
        self.store.reconcile(mail, 'unknown', '核对完成')
        self.assertEqual(self.store.receipts(history, mail)[0]['receipt'], 'cancelled')

    def test_turn_ends_after_ready_before_take(self):
        turn, history, mail = self.follow_fixture()
        meta = {'threadId': self.thread, 'x-codex-turn-metadata': {'turn_id': turn}}
        notice = paired_inbox.receive(self.path, history, 'watch', {}, meta)
        self.assertEqual(notice['id'], mail)
        with closing(sqlite3.connect(history)) as db, db:
            db.execute("UPDATE thread_turns SET status='completed'")
        self.assertEqual(paired_inbox.receive(self.path, history, 'take', {'id': mail}, meta)['status'], 'unavailable')
        self.assertEqual(self.worker.execute('SELECT taken_at FROM paired_letters').fetchone(), (None,))
        self.assertEqual(self.store.receipts(history, mail)[0]['receipt'], 'registered')

    def test_follow_records_success_only_after_output_and_exits_with_turn(self):
        turn, history, mail = self.follow_fixture()
        events = []
        def output(line, *, flush):
            self.assertTrue(flush)
            event = json.loads(line)
            events.append(event['status'])
            if event['status'] == 'taken':
                self.assertEqual(self.store.receipts(history, mail)[0]['receipt'], 'unknown')
                with closing(sqlite3.connect(history)) as db, db:
                    db.execute("UPDATE thread_turns SET status='completed'")
        with patch('builtins.print', side_effect=output):
            paired_inbox.follow(self.path, history, self.thread, turn)
        self.assertEqual(events, ['taken', 'unavailable'])
        self.assertEqual(self.store.receipts(history, mail)[0]['receipt'], 'delivered')

    def test_receipt_needs_matching_thread_id_and_entire_native_body(self):
        mail=self.incoming("queued","none")
        history=self.path.with_name("thread_history_1.sqlite")
        def stage():
            return self.store.receipts(history,mail)[0]['receipt']
        self.assertEqual(stage(),'registered')
        self.worker.execute("UPDATE mail SET state='notified',detail='Desktop 原生队列已接收，未确认已读。' WHERE id=?",(mail,))
        self.assertEqual(stage(),'native_queued')
        body=self.store.messages()[0]['body']
        text=f'Claude〔{mail}〕\n'+body
        def item(s):
            return json.dumps({'type':'userMessage','content':[{'type':'text','text':s}]},ensure_ascii=False)
        with closing(sqlite3.connect(history,isolation_level=None)) as native:
            native.execute('CREATE TABLE thread_items(thread_id,turn_id,item_type,item_json)')
            native.executemany("INSERT INTO thread_items VALUES(?,?,'userMessage',?)",[
                ('other','wrong',item(text)),(self.thread,'wrong',item('quote: '+text)),
                (self.thread,'wrong',item(text+' extra'))])
            self.assertEqual(stage(),'native_queued')
            native.execute("INSERT INTO thread_items VALUES(?,?,'userMessage',?)",(self.thread,'correct',item(text+'\n')))
            self.assertEqual(stage(),'delivered')
        history.unlink()
        self.assertIn('correct',self.store.receipts(history,mail)[0]['receipt_evidence'])
        self.assertEqual(self.store.messages()[0]['state'],'notified')

    def test_explicit_cc_cli_submission_has_one_id_and_checks_binding(self):
        mid=str(uuid4())
        args=(self.binding,self.cc['id'],self.cc['native_id'],'review',mid)
        self.assertEqual(self.store.cc_send(*args),mid)
        self.assertEqual(self.store.cc_send(*args),mid)
        with self.assertRaises(ValueError):
            self.store.cc_send(self.binding,self.cc['id'],self.cc['native_id'],'different body',mid)
        with self.assertRaises(ValueError):
            self.store.cc_send(self.binding,self.cc['id'],str(uuid4()),'review',str(uuid4()))
        self.assertEqual([m['id'] for m in self.store.messages()],[mid])

    def test_migrate_legacy_mail_including_retired_routes(self):
        self.worker.executescript("""
            DROP TABLE mail;
            CREATE TABLE mail (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                binding_id INTEGER NOT NULL REFERENCES bindings(id), source TEXT NOT NULL,
                body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
                    ('queued','submitting','submitted','received','returning','returned','failed','unknown','cancelled')),
                reply TEXT, detail TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX mail_state_seq ON mail(state, seq);
            CREATE TRIGGER mail_envelope BEFORE UPDATE OF id ON mail
                BEGIN SELECT RAISE(ABORT, 'legacy envelope'); END;
        """)
        historical = str(uuid4())
        self.worker.execute("""
            INSERT INTO mail VALUES (12, ?, ?, 'cc', 'old', 'returned', 'answer', 'receipt', '2026-01-02')
        """, (historical, self.binding))
        self.worker.execute("UPDATE bindings SET active=0 WHERE id=?", (self.binding,))
        current = self.store.bind(session("replacement"), self.desktop)
        pending = str(uuid4())
        self.worker.execute("""
            INSERT INTO mail VALUES (24, ?, ?, 'desktop', 'pending', 'unknown', NULL, 'timeout', '2026-01-03')
        """, (pending, current))
        self.worker.execute("UPDATE sqlite_sequence SET seq=40 WHERE name='mail'")
        snapshot = self.store.bindings(include_inactive=True)
        old_fields = "seq,id,binding_id,source,body,state,reply,detail,created"
        old_data = self.worker.execute(f"SELECT {old_fields} FROM mail ORDER BY seq").fetchall()

        with ThreadPoolExecutor(max_workers=3) as pool:
            stores = list(pool.map(Store, [self.path] * 3))
        migrated = stores[0]
        self.assertEqual(old_data, self.worker.execute(f"SELECT {old_fields} FROM mail ORDER BY seq").fetchall())
        self.assertEqual(snapshot, migrated.bindings(include_inactive=True))
        self.assertEqual([(historical, self.cc["id"], "required"), (pending, "replacement", "required")],
                         [(m["id"], m["cc"], m["reply_mode"]) for m in migrated.messages()])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.worker.execute("UPDATE mail SET binding_id=? WHERE id=?", (current, historical))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.worker.execute("UPDATE mail SET reply_mode='deferred' WHERE id=?", (pending,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "active binding"):
            self.incoming("queued")
        self.worker.execute("INSERT INTO mail(id,binding_id,source,body) VALUES ('new',?,'cc','hi')", (current,))
        self.assertEqual((41, "required"), self.worker.execute("SELECT seq,reply_mode FROM mail WHERE id='new'").fetchone())
        self.assertEqual([], self.worker.execute("PRAGMA foreign_key_check").fetchall())

    def test_notification_rejects_replies_and_allows_retirement_after_delivery(self):
        mail_id = self.incoming("submitting", "none")
        for state in ("submitting", "unknown", "notified"):
            self.worker.execute("UPDATE mail SET state=? WHERE id=?", (state, mail_id))
            with self.assertRaisesRegex(ValueError, "notification"):
                self.store.desktop_reply(self.thread, mail_id, "unexpected")
        with self.assertRaises(sqlite3.IntegrityError):
            self.worker.execute("UPDATE mail SET reply='unexpected' WHERE id=?", (mail_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "closed"):
            self.worker.execute("UPDATE mail SET state='queued' WHERE id=?", (mail_id,))
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.store.reconcile(mail_id, "notified", "already closed")
        self.store.unbind(self.binding)
        self.assertEqual("notified", self.store.messages()[0]["state"])

    def test_deferred_reply_and_manual_closure(self):
        mail_id = self.incoming("awaiting_reply", "deferred")
        with self.assertRaisesRegex(ValueError, mail_id):
            self.store.unbind(self.binding)
        self.store.desktop_reply(self.thread, mail_id, "later")
        self.assertEqual("received", self.store.messages()[0]["state"])
        with self.assertRaisesRegex(ValueError, "state changed"):
            self.store.reconcile(mail_id, "awaiting_reply", "stale")
        self.worker.execute("UPDATE mail SET state='returned' WHERE id=?", (mail_id,))
        self.assertEqual(mail_id, self.store.desktop_reply(self.thread, mail_id, "later"))
        with self.assertRaisesRegex(ValueError, "different reply"):
            self.store.desktop_reply(self.thread, mail_id, "changed")
        closed = self.incoming("awaiting_reply", "deferred")
        self.store.reconcile(closed, "awaiting_reply", "operator closed")
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.store.desktop_reply(self.thread, closed, "too late")
        self.store.unbind(self.binding)

    def test_invalid_reply_policy_rolls_back(self):
        with self.assertRaisesRegex(ValueError, "reply_mode"):
            self.store.desktop_send(self.thread, "hello", str(uuid4()), reply_mode="invalid")
        for state, mode in (("queued", "invalid"), ("awaiting_reply", "required"), ("notified", "deferred")):
            with self.subTest(state=state, mode=mode), self.assertRaises(sqlite3.IntegrityError):
                self.incoming(state, mode)
        self.assertEqual([], self.store.messages())

    def test_change_after_operator_confirmation(self):
        newer = self.store.bind(session("different-cc"), self.desktop)
        with self.assertRaisesRegex(ValueError, "相关配对已变更"):
            self.store.bind(self.cc, self.desktop, expected_conflicts={str(self.binding)})
        self.assertEqual(newer, self.store.bindings()[0]["id"])

    def test_atomic_replacement_and_snapshot(self):
        other_cc, other_desktop = session("other-cc"), session("other-desktop")
        other = self.store.bind(other_cc, other_desktop)
        first = self.incoming("queued")
        second = self.store.desktop_send(other_desktop["native_id"], "hello", str(uuid4()))
        self.worker.execute("UPDATE mail SET state='submitting' WHERE id=?", (second,))
        with self.assertRaisesRegex(ValueError, second):
            self.store.bind(self.cc, other_desktop)
        self.assertEqual([self.binding, other], [r["id"] for r in self.store.bindings()])
        self.assertEqual("queued", self.store.messages()[0]["state"])

        self.store.reconcile(second, "submitting", "Worker stopped; operator closed the request")
        new_id = self.store.bind(self.cc, other_desktop)
        self.assertEqual([new_id], [r["id"] for r in self.store.bindings()])
        self.assertEqual([0, 0, 1], [r["active"] for r in self.store.bindings(include_inactive=True)])
        self.assertEqual((self.thread, "cancelled"), self.worker.execute("""
            SELECT b.desktop, m.state FROM mail m JOIN bindings b ON b.id=m.binding_id WHERE m.id=?
        """, (first,)).fetchone())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "closed"):
            self.worker.execute("UPDATE mail SET state='submitting' WHERE id=?", (first,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "active binding"):
            self.incoming("queued")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "snapshot"):
            self.worker.execute("UPDATE bindings SET desktop_title='changed' WHERE id=?", (new_id,))

    def test_duplicate_bind_and_native_id_validation(self):
        mail_id = self.incoming("submitting")
        self.assertEqual(self.binding, self.store.bind(dict(self.cc, title="renamed"), self.desktop))
        self.assertEqual(self.cc["title"], self.store.bindings()[0]["cc_title"])
        with self.assertRaisesRegex(ValueError, mail_id):
            self.store.bind(dict(self.cc, id="alias"), session("third"))
        for bad in (None, "", "placeholder", self.thread + "-"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "UUID"):
                self.store.bind(dict(self.cc, native_id=bad), self.desktop)
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "UUID"):
                self.store.bind(self.cc, dict(self.desktop, native_id=bad))

    def test_restart_does_not_replay_and_manual_close_checks_state(self):
        states = ("submitting", "submitted", "received", "returning", "failed", "unknown")
        letters = [(self.incoming(state), state) for state in states]
        self.worker.execute("UPDATE mail SET detail='original evidence'")
        again = Store(self.path)
        self.assertEqual(list(states), [r["state"] for r in again.messages()])
        for mail_id, state in letters:
            with self.assertRaisesRegex(ValueError, mail_id):
                again.unbind(self.binding)
            with self.assertRaisesRegex(ValueError, "state changed"):
                again.reconcile(mail_id, "failed" if state != "failed" else "unknown", "stale view")
            again.reconcile(mail_id, state, "Checked session with stopped worker")
        again.unbind(self.binding)
        for message in again.messages():
            self.assertEqual("cancelled", message["state"])
            self.assertIn("original evidence", message["detail"])
            self.assertIn("Checked session", message["detail"])

    def test_concurrent_duplicate_send_and_stale_origin(self):
        mail_id = str(uuid4())
        def send(_):
            return self.store.desktop_send(self.thread, "line 1\nline 2", mail_id)
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual([mail_id] * 12, list(pool.map(send, range(12))))
        self.assertEqual(1, len(self.store.messages()))
        with self.assertRaisesRegex(ValueError, "different"):
            self.store.desktop_send(self.thread, "changed", mail_id)
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.store.desktop_send(self.thread, "invalid id", "invalid")
        self.store.bind(session("new-cc"), self.desktop)
        with self.assertRaisesRegex(ValueError, "different"):
            send(None)
        self.assertEqual("cancelled", self.store.messages()[0]["state"])
        with self.assertRaisesRegex(ValueError, "active binding"):
            self.store.desktop_send(str(uuid4()), "unknown origin", str(uuid4()))

    def test_reply_races_ack_and_retries_after_return(self):
        other = session("other")
        self.store.bind(session("second-cc"), other)
        for initial in ("submitting", "submitted", "unknown"):
            mail_id = self.incoming(initial)
            with self.assertRaisesRegex(ValueError, "No incoming"):
                self.store.desktop_reply(other["native_id"], mail_id, "spoof")
            self.store.desktop_reply(self.thread, mail_id, "receipt")
            self.assertEqual(0, self.worker.execute("""
                UPDATE mail SET state='submitted' WHERE id=? AND state='submitting'
            """, (mail_id,)).rowcount)
            self.worker.execute("UPDATE mail SET state='returned' WHERE id=?", (mail_id,))
            self.assertEqual(mail_id, Store(self.path).desktop_reply(self.thread, mail_id, "receipt"))
            with self.assertRaisesRegex(ValueError, "different reply"):
                self.store.desktop_reply(self.thread, mail_id, "changed")
        queued = self.incoming("queued")
        with self.assertRaisesRegex(ValueError, "state queued"):
            self.store.desktop_reply(self.thread, queued, "early")
        sent = self.store.desktop_send(self.thread, "question", str(uuid4()), "required")
        with self.assertRaisesRegex(ValueError, "No incoming"):
            self.store.desktop_reply(self.thread, sent, "wrong direction")
        self.store.bind(session("replacement"), self.desktop)
        with self.assertRaisesRegex(ValueError, "No incoming"):
            self.store.desktop_reply(self.thread, mail_id, "receipt")

    def test_stale_send_instruction_cannot_reach_replacement_cc(self):
        current = self.store.bind(session("new-cc"), self.desktop)
        mail_id = str(uuid4())
        with self.assertRaisesRegex(ValueError, "no longer active"):
            self.store.desktop_send(self.thread, "question", mail_id, expected_binding=self.binding)
        self.assertEqual([], self.store.messages())
        self.store.desktop_send(self.thread, "question", mail_id, expected_binding=current)
        self.assertEqual((current,), self.worker.execute("SELECT binding_id FROM mail").fetchone())

    def test_confirm_requires_origin_reply_and_eligible_state(self):
        other = session("other")
        self.store.bind(session("second-cc"), other)
        for state in ("returning", "submitted", "unknown"):
            mail_id = self.store.desktop_send(self.thread, "question", str(uuid4()), "required")
            self.worker.execute("UPDATE mail SET state=? WHERE id=?", (state, mail_id))
            with self.assertRaisesRegex(ValueError, "stored reply"):
                self.store.desktop_confirm(self.thread, mail_id)
            self.worker.execute("UPDATE mail SET reply='answer' WHERE id=?", (mail_id,))
            with self.assertRaisesRegex(ValueError, "active thread"):
                self.store.desktop_confirm(other["native_id"], mail_id)
            self.store.desktop_confirm(self.thread, mail_id)
            self.assertEqual(mail_id, Store(self.path).desktop_confirm(self.thread, mail_id))
        for state in ("queued", "submitting", "received", "failed", "cancelled"):
            mail_id = self.store.desktop_send(self.thread, "question", str(uuid4()), "required")
            self.worker.execute("UPDATE mail SET state=?,reply='answer' WHERE id=?", (state, mail_id))
            with self.assertRaisesRegex(ValueError, f"state {state}"):
                self.store.desktop_confirm(self.thread, mail_id)
        wrong = self.incoming("returning")
        self.worker.execute("UPDATE mail SET reply='answer' WHERE id=?", (wrong,))
        with self.assertRaisesRegex(ValueError, "Desktop-origin"):
            self.store.desktop_confirm(self.thread, wrong)

    def test_confirm_after_rebind_rejects_old_returned_mail(self):
        mail_id = self.store.desktop_send(self.thread, "question", str(uuid4()), "required")
        self.worker.execute("UPDATE mail SET state='returning', reply='yes' WHERE id=?", (mail_id,))
        self.store.desktop_confirm(self.thread, mail_id)
        self.store.bind(session("replacement"), self.desktop)
        with self.assertRaisesRegex(ValueError, "active thread"):
            self.store.desktop_confirm(self.thread, mail_id)

    def test_relay_from_unrelated_directory(self):
        script = Path(__file__).with_name("relay.py").resolve()
        env = dict(os.environ, DAILY_BRIDGE_STORE=str(self.path), CODEX_THREAD_ID=self.thread)
        def relay(*args):
            return subprocess.run([sys.executable, "-B", str(script), *args], cwd=self.path.parent,
                                  env=env, capture_output=True, text=True)
        mail_id = str(uuid4())
        self.assertNotEqual(0, relay("send", "--id", mail_id, "--text", "hello").returncode)
        result = relay("--store", str(self.path), "send", "--binding", str(self.binding),
                       "--id", mail_id, "--text", "hello")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"id": mail_id}, json.loads(result.stdout))
        self.assertEqual("none", self.store.messages()[0]["reply_mode"])
        mail_id = self.store.desktop_send(self.thread, "legacy", str(uuid4()), "required")
        self.worker.execute("UPDATE mail SET state='returning',reply='answer' WHERE id=?", (mail_id,))
        self.assertEqual(0, relay("confirm", "--id", mail_id).returncode)
        incoming = self.incoming("submitting")
        env["DAILY_BRIDGE_STORE"] = str(self.path.with_suffix(".unused"))
        self.assertEqual(0, relay("--store", str(self.path), "reply", "--id", incoming, "--text", "receipt").returncode)
        self.assertFalse(self.path.with_suffix(".unused").exists())
        self.assertEqual(["queued", "returned", "received"], [r["state"] for r in self.store.messages()])
        for mode in ("none", "deferred"):
            mail_id = str(uuid4())
            send = ("--store", str(self.path), "send", "--binding", str(self.binding),
                    "--id", mail_id, "--text", "hello")
            for _ in range(2):
                result = relay(*send, "--mode", mode)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({"id": mail_id}, json.loads(result.stdout))
            self.assertEqual(0, relay(*send).returncode)
            self.assertNotEqual(0, relay(*send, "--mode", "invalid").returncode)
        self.assertEqual(["none", "required", "required", "none", "deferred"],
                         [m["reply_mode"] for m in self.store.messages()])
        del env["CODEX_THREAD_ID"]
        self.assertNotEqual(0, relay("confirm", "--id", mail_id).returncode)


if __name__ == "__main__":
    unittest.main()
