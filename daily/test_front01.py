"""FRONT-01 acceptance checks through the candidate's Tk controls, with no live bridge."""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import call, patch
from uuid import uuid4

from daily import panel
from daily.provision import get_request, request_for_desktop
from daily.storage import Store


def widgets(parent, kind):
    for child in parent.winfo_children():
        if child.winfo_viewable() and isinstance(child, kind):
            yield child
        yield from widgets(child, kind)


def rows(tree, parent=""):
    for item in tree.get_children(parent):
        # Inspect only columns displayed to the operator.
        columns = tree["displaycolumns"]
        if columns == ("#all",):
            columns = tree["columns"]
        values = [tree.set(item, col) for col in columns]
        if "tree" in str(tree["show"]):
            values.append(tree.item(item, "text"))
        yield item, " ".join(values)
        yield from rows(tree, item)


@unittest.skipUnless(sys.platform == "win32", "Windows Tk workbench")
class Front01Tests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = folder / "bridge.sqlite3"
        self.store = Store(self.db)
        self.cc = dict(id="host CC", native_id=str(uuid4()), title="Fixture Claude", cwd=str(folder))
        self.desktop = dict(id=str(uuid4()), title="Fixture paired", cwd=str(folder))
        self.free = dict(id=str(uuid4()), title="Fixture available", cwd=str(folder))
        for thread in (self.desktop, self.free):
            thread["native_id"] = thread["id"]
        self.binding = self.store.bind(self.cc, self.desktop)
        self.catalog = {"cc": [self.cc], "desktop": [self.desktop, self.free]}

        def list_sessions(kind, path, query=""):
            self.assertEqual(Path(path), folder / kind)
            return [r for r in self.catalog[kind] if query.casefold() in " ".join(r.values()).casefold()]

        self.enterContext(patch("daily.catalog.list_sessions", side_effect=list_sessions))
        self.status = folder / "status.json"
        self.write_heartbeat("ready")
        self.enterContext(patch.object(sys, "argv", ["panel.py", "--store", str(self.db),
            "--cc-db", str(folder / "cc"), "--desktop-db", str(folder / "desktop"),
            "--status-file", str(self.status), "--cc-url", "http://127.0.0.1:1"]))
        self.confirm = self.enterContext(patch.object(panel, "confirmation", return_value=None))
        self.browser = self.enterContext(patch("webbrowser.open", return_value=True))
        self.desktop_open = self.enterContext(patch("os.startfile"))
        self.dialogs = [self.enterContext(patch("tkinter.messagebox." + name))
                        for name in ("showerror", "showwarning", "showinfo")]
        self.root = tk.Tk()
        self.addCleanup(self.close)
        self.exceptions = []
        self.root.report_callback_exception = lambda _, exc, tb: self.exceptions.append(exc)

    def start(self, *, home=False):
        # Run the real argument parsing and backend wiring, retaining control of the event loop.
        with patch.object(tk, "Tk", return_value=self.root), patch.object(self.root, "mainloop"):
            panel.main()
        self.wait(lambda: self.button_if_visible("打开 Desktop"), "pairing home did not appear")
        if not home:
            self.tab(r"会话配对")
            self.wait(lambda: self.find_row(self.free["title"]), "unpaired fixture thread missing")

    def button_if_visible(self, label):
        return next((w for w in widgets(self.root, ttk.Button) if str(w["text"]) == label), None)


    def close(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        close = self.root.protocol("WM_DELETE_WINDOW")
        if close:
            self.root.tk.call(close)
        else:
            self.root.destroy()

    def wait(self, check, reason):
        deadline = time.monotonic() + 3
        while True:
            self.root.update()
            if self.exceptions:
                raise self.exceptions[0]
            result = check()
            if result:
                return result
            if time.monotonic() >= deadline:
                self.fail(reason)
            time.sleep(.01)

    def tab(self, pattern):
        for book in widgets(self.root, ttk.Notebook):
            for tab in book.tabs():
                if re.search(pattern, book.tab(tab, "text"), re.I):
                    book.select(tab)
                    self.root.update()
                    return

    def find_row(self, text):
        for tree in widgets(self.root, ttk.Treeview):
            for item, visible in rows(tree):
                if text in visible:
                    return tree, item

    def select(self, text):
        tree, item = self.wait(lambda: self.find_row(text), f"No displayed row for {text!r}")
        tree.see(item)
        tree.selection_set(item)
        self.root.update()
        if self.exceptions:
            raise self.exceptions[0]
        return tree, item

    def button(self, pattern):
        for button in widgets(self.root, (ttk.Button, tk.Button)):
            if re.search(pattern, str(button["text"]), re.I):
                return button
        self.fail(f"No visible action matching {pattern!r} after selecting the fixture")

    def feedback(self):
        parts = [str(w["text"]) for w in widgets(self.root, (ttk.Label, tk.Label))]
        parts.extend(w.get() for w in widgets(self.root, (ttk.Entry, tk.Entry)))
        parts.extend(w.get("1.0", "end") for w in widgets(self.root, tk.Text))
        for tree in widgets(self.root, ttk.Treeview):
            parts.extend(text for _, text in rows(tree))
        return "\n".join(parts)

    def write_heartbeat(self, message):
        self.status.write_text(json.dumps(dict(heartbeat=time.time(), state="ready", pid=1,
                                               port=1, instance="fixture", message=message)), encoding="utf8")

    def refresh_and_wait(self):
        # A fresh, visible heartbeat proves the async snapshot actually reached the UI,
        # even if rendering moved the operator to a different page.
        marker = str(uuid4())
        self.write_heartbeat(marker)
        self.button(r"刷新|refresh").invoke()
        self.wait(lambda: marker in self.feedback(), "Refresh did not render the new fixture heartbeat")

    def seed_mail(self, mode, state, body, detail=None):
        # Insert historical observations directly. No worker, send or reply operation runs.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""INSERT INTO mail(id,binding_id,source,reply_mode,state,body,detail)
                          VALUES (?,?,'desktop',?,?,?,?)""",
                       (str(uuid4()), self.binding, mode, state, body, detail))

    def test_project_receipt_setup_uses_selected_desktop_and_merges_once(self):
        self.start(home=True)
        project = Path(self.desktop["cwd"])
        file = project / ".codex" / "hooks.json"
        file.parent.mkdir()
        other = {"hooks": [{"type": "command", "command": "echo keep"}]}
        file.write_text(json.dumps({"hooks": {"PreToolUse": [other]}}), encoding="utf8")
        self.confirm.return_value = ""
        with patch.object(panel.project_hooks, "set_enabled", side_effect=panel.project_hooks.ReviewRequired), \
             patch.object(panel.project_hooks, "open_review") as review:
            for count in (1, 2):
                self.button("设置项目收件").invoke()
                self.wait(lambda: review.call_count == count and self.dialogs[2].call_count == count,
                          "setup did not reach native review")
            review.assert_called_with(project)
        hooks = json.loads(file.read_text(encoding="utf8"))["hooks"]
        self.assertEqual(hooks["PreToolUse"], [other, *hooks["PostToolUse"]])
        self.assertEqual(len(hooks["PostToolUse"]), 1)
        self.assertIn("inbox.py", hooks["PostToolUse"][0]["hooks"][0]["command"])

    def test_last_unbind_offers_optional_scoped_disable_and_rechecks_on_click(self):
        project = Path(self.desktop["cwd"])
        panel.project_hooks.install(project, self.db)
        backend = panel.Backend(type('Args', (), {'store': self.db})(), None, Store)
        backend.store = self.store
        other_cc = dict(self.cc, id="second CC", native_id=str(uuid4()))
        other = dict(self.free)
        self.store.bind(other_cc, other)
        first, second = backend.bindings()
        self.assertIsNone(backend.unbind(first))
        self.assertEqual(backend.unbind(second), project.resolve())
        with patch.object(panel.project_hooks, 'set_enabled') as toggle:
            backend.disable_unused_hooks(project)
            toggle.assert_called_once_with(project, self.db, False)
            self.store.bind(other_cc, other)
            with self.assertRaisesRegex(ValueError, "活动配对"):
                backend.disable_unused_hooks(project)
            self.assertEqual(toggle.call_count, 1)

    def test_unbind_button_offers_disable_and_cancel_keeps_hooks(self):
        project = Path(self.desktop["cwd"])
        panel.project_hooks.install(project, self.db)
        self.start()
        self.select(self.desktop["title"])
        self.confirm.side_effect = ["", None]
        with patch.object(panel.project_hooks, 'set_enabled') as toggle:
            self.button("解除配对").invoke()
            self.wait(lambda: self.confirm.call_count == 2, "last pair did not offer to disable hooks")
            self.assertEqual(self.confirm.call_args.args[1], "停用本目录收件")
            toggle.assert_not_called()

    def test_find_failed_send_by_error_reference_and_show_reason(self):
        ref=str(uuid4())
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute("""INSERT INTO send_feedback(id,binding_id,sender,reason,state)
                VALUES(?,?,'cc','第 2 行 JSON 解析失败','shown')""",(ref,self.binding))
        self.start()
        self.tab(r"消息|mail")
        with patch.object(panel.simpledialog,'askstring',return_value=ref):
            self.button(r"按编号查找").invoke()
        text='\n'.join(w.get('1.0','end') for w in widgets(self.root,tk.Text))
        self.assertIn('未发出',text)
        self.assertIn(ref,text)
        self.assertIn('第 2 行',text)
        self.assertIn('错误编号',text)

    def test_modes_are_visible_on_the_corresponding_messages(self):
        expectations = [
            ("required", "Letter A", r"required|(?<!无)(?<!不)需(?:要)?(?:本轮)?(?:回复|答复|回信)|本轮答复"),
            ("none", "Letter B", r"\bnone\b|通知|无需回"),
            ("deferred", "Letter C", r"deferred|稍后|异步|延[迟后]"),
        ]
        for mode, body, _ in expectations:
            self.seed_mail(mode, "queued", body)
        self.start()
        self.tab(r"消息|mail")
        for mode, body, pattern in expectations:
            with self.subTest(mode=mode):
                tree, item = self.select(body)
                row = dict(rows(tree))[item]
                self.assertRegex(row, pattern, "The message list must identify this letter's reply mode")
                for other, _, wrong in expectations:
                    if other != mode:
                        self.assertNotRegex(row, wrong)

    def test_pending_and_unknown_are_distinct_and_unknown_can_be_reviewed(self):
        self.seed_mail("required", "queued", "Letter A")
        self.seed_mail("required", "unknown", "Letter B", "Fixture transport interrupted")
        self.start()
        self.tab(r"消息|mail")
        tree, item = self.select("Letter A")
        self.assertRegex(dict(rows(tree))[item], r"已登记 · 待投递|排队|待处理|queued|pending")
        resolve = r"核销|已处理|reconcile"
        self.assertTrue(self.button(resolve).instate(["disabled"]))
        tree, item = self.select("Letter B")
        self.assertRegex(dict(rows(tree))[item], r"待核对|未知|不明|unknown")
        self.assertTrue(any("Fixture transport interrupted" in w.get("1.0", "end")
                            for w in widgets(self.root, tk.Text)) or
                        "Fixture transport interrupted" in dict(rows(tree))[item])
        self.confirm.return_value = "Checked fixture"
        self.button(resolve).invoke()
        self.wait(lambda: self.store.messages()[1]["state"] == "cancelled", "unknown was not closed")
        pending, reviewed = self.store.messages()
        self.assertEqual(pending["state"], "queued")
        self.assertIn("Checked fixture", reviewed["detail"])

    def test_notified_and_awaiting_reply_explain_different_outcomes(self):
        self.seed_mail("none", "notified", "Letter A")
        self.seed_mail("deferred", "awaiting_reply", "Letter B")
        self.start()
        self.tab(r"消息|mail")
        for body, outcome, may_close in [
            ("Letter A", r"已通知|通知已投递|已送达|notice delivered", False),
            ("Letter B", r"等[待候].*(?:复|回)|待(?:回复|答复)|awaiting reply", True),
        ]:
            with self.subTest(body=body):
                tree, item = self.select(body)
                detail = [w.get("1.0", "end") for w in widgets(self.root, tk.Text)]
                visible = [dict(rows(tree))[item]] + [text for text in detail if body in text]
                self.assertRegex("\n".join(visible), outcome)
                self.assertEqual(self.button(r"核销|已处理|reconcile").instate(["!disabled"]),
                                 may_close)

    def test_selected_desktop_can_request_one_new_cc(self):
        self.start()
        self.tab(r"新建|创建|provision|create")
        self.select(self.free["title"])
        create = self.button(r"新建|创建|provision|create")
        self.assertFalse(create.instate(["disabled"]))
        # Cancel once, then confirm. Both interactions go through the actual control.
        create.invoke()
        self.assertTrue(self.confirm.called, "Creation must identify the target before queuing")
        self.assertIn(self.free["id"], str(self.confirm.call_args))

        def requests():
            with closing(sqlite3.connect(self.db)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='provision_requests'").fetchone():
                    return db.execute("SELECT desktop,title,state FROM provision_requests").fetchall()
                return []

        self.assertEqual(requests(), [])
        self.confirm.return_value = ""
        self.button(r"新建|创建|provision|create").invoke()
        self.assertFalse(self.dialogs[0].called, str(self.dialogs[0].call_args))
        request, = self.wait(requests, "No durable creation request after confirmation")
        self.assertEqual((request[0], request[2]), (self.free["id"], "queued"))
        self.assertTrue(request[1].strip())
        self.assertEqual([b["id"] for b in self.store.bindings()], [self.binding])

    def test_refresh_preserves_edited_creation_title(self):
        self.start()
        self.tab(r"新建|创建|provision|create")
        self.select(self.free["title"])
        fields = [w for label in widgets(self.root, (ttk.Label, tk.Label))
                  if re.search(r"标题|title", str(label["text"]), re.I)
                  for w in widgets(label.master, (ttk.Entry, tk.Entry)) if str(w["state"]) == "normal"]
        self.assertTrue(fields, "No editable session title for the selected Desktop")
        fields[0].delete(0, "end")
        fields[0].insert(0, "My new Claude")
        other = str(uuid4())
        self.catalog["desktop"].append(dict(self.free, id=other, native_id=other, title="New fixture thread"))
        self.button(r"刷新|refresh").invoke()
        self.wait(lambda: self.find_row("New fixture thread"), "Catalog refresh did not finish")
        self.assertIn("My new Claude", [w.get() for w in widgets(self.root, (ttk.Entry, tk.Entry))],
                      "Refresh discarded the title being edited")

    def check_open_request(self, state, expected):
        req_id = str(uuid4())
        request_for_desktop(self.db, self.free["id"], req_id, "Fixture request")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE provision_requests SET state=? WHERE id=?", (state, req_id))
        self.start()
        self.tab(r"新建|创建|provision|create")
        if self.find_row(self.free["title"]):
            self.select(self.free["title"])

        def feedback():
            dialogs = [str(call.args) for dialog in self.dialogs for call in dialog.call_args_list]
            return "\n".join([self.feedback(), *dialogs])

        # Accept persistent feedback or an explanation in response to the operator's action.
        if not re.search(expected, feedback(), re.I):
            self.confirm.return_value = ""
            for btn in widgets(self.root, (ttk.Button, tk.Button)):
                if re.search(r"请求状态|查询|检查进度|新建|创建|check|create", str(btn["text"]), re.I):
                    btn.invoke()
                    break
        self.wait(lambda: re.search(expected, feedback(), re.I),
                  f"Operator cannot distinguish an existing {state} request. {feedback()}")
        self.assertEqual(get_request(self.db, req_id)["state"], state)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM provision_requests").fetchone(), (1,))
        self.assertEqual([b["id"] for b in self.store.bindings()], [self.binding])

    def test_existing_queued_creation_is_identifiable(self):
        self.check_open_request("queued", r"排队|队列|待处理|等待创建|queued|pending")

    def test_unknown_creation_is_identifiable_without_duplicate(self):
        self.check_open_request("unknown", r"待核对|待核实|未知|不明|不确定|unknown")

    def test_selected_request_tracks_queued_starting_unknown(self):
        req = request_for_desktop(self.db, self.free["id"], str(uuid4()), "Fixture request")
        self.start()
        self.tab(r"新建|创建|provision|create")
        self.select(self.free["title"])
        queued = r"排队|队列|待处理|等待创建|queued|pending"
        starting = r"启动|初始化|starting"
        unknown = r"待核对|待核实|未知|不明|不确定|unknown"
        self.assertRegex(self.feedback(), queued)

        for state, expected in (("starting", starting), ("unknown", unknown)):
            with self.subTest(state=state):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute("UPDATE provision_requests SET state=? WHERE id=?", (state, req["id"]))
                self.refresh_and_wait()
                selected = self.find_row(self.free["title"])
                self.assertIsNotNone(selected, "Refresh moved away from the selected Desktop")
                tree, item = selected
                self.assertIn(item, tree.selection(), "Refresh cleared the Desktop selection")
                self.assertRegex(self.feedback(), expected)

    def complete_fixture_request(self, req, desktop):
        cc = dict(self.cc, id=req["cc"], native_id=str(uuid4()), title=req["title"])
        binding_id = self.store.bind(cc, desktop)
        self.catalog["cc"].append(cc)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""UPDATE provision_requests SET state='bound', native_cc=?, binding_id=?
                          WHERE id=?""", (cc["native_id"], binding_id, req["id"]))
        return cc

    def test_newly_bound_request_focuses_its_pair_once(self):
        old_id = str(uuid4())
        old_desktop = dict(self.desktop, id=old_id, native_id=old_id, title="Previously provisioned Desktop")
        self.catalog["desktop"].append(old_desktop)
        old = request_for_desktop(self.db, old_id, str(uuid4()), "Earlier Claude")
        self.complete_fixture_request(old, old_desktop)
        self.start()
        self.tab(r"新建|创建|provision|create")
        self.select(self.free["title"])
        self.confirm.return_value = ""
        self.button(r"新建|创建|provision|create").invoke()

        def queued_request():
            with closing(sqlite3.connect(self.db)) as db:
                row = db.execute("SELECT id FROM provision_requests WHERE desktop=? AND state='queued'",
                                 (self.free["id"],)).fetchone()
            return get_request(self.db, row[0]) if row else None

        current = self.wait(queued_request, "The selected Desktop's creation request was not queued")
        self.refresh_and_wait()
        self.tab(r"新建|创建|provision|create")
        cc = self.complete_fixture_request(current, self.free)
        self.refresh_and_wait()

        with self.subTest(step="focus the newly completed binding"):
            # The inspect action identifies the active binding even when other pairs
            # also appear in the catalog, or the new CC and Desktop share a title.
            self.dialogs[2].reset_mock()
            self.button(r"查看配对|inspect.*pair|view.*pair").invoke()
            inspected = str(self.dialogs[2].call_args)
            for key in (cc["id"], cc["native_id"], self.free["native_id"]):
                self.assertIn(key, inspected, "The pairing page has the wrong binding selected")

        with self.subTest(step="focused pair opens the new endpoints"):
            self.button(r"浏览器|打开.*(?:CC|Claude)|open.*(?:CC|Claude)").invoke()
            self.button(r"(?:Desktop.*打开|打开.*Desktop|open.*Desktop)").invoke()
            self.assertEqual((self.browser.call_args, self.desktop_open.call_args),
                             (call("http://127.0.0.1:1/session/" + cc["id"]),
                              call("codex://threads/" + self.free["native_id"])),
                             "Open actions must follow the binding shown by Inspect")

        self.tab(r"消息|mail")
        book = next(widgets(self.root, ttk.Notebook))
        messages = book.select()
        self.assertRegex(book.tab(messages, "text"), r"消息|mail")
        for index in range(2):
            with self.subTest(step="keep operator on Messages", refresh=index + 1):
                self.refresh_and_wait()
                self.assertEqual(book.select(), messages, "A bound request took focus again")

    def test_waiting_inbox_letter_is_not_offered_for_manual_reconciliation(self):
        self.seed_mail("none", "unknown", "等待本回合领取", "配对收件箱待领取")
        self.start()
        self.tab("消息记录")
        tree, item = self.select("等待本回合领取")
        self.assertIn("待会话领取", dict(rows(tree))[item])
        self.assertTrue(self.button("标记为已处理").instate(["disabled"]))
        self.assertFalse(any("查看待核对消息" in str(w["text"]) for w in widgets(self.root, ttk.Button)))

    def test_reverse_tab_shows_sent_draft_requirement_and_observed_binding(self):
        cc = dict(self.cc, id="unpaired", native_id=str(uuid4()), title="Claude 草稿来源")
        self.catalog["cc"].append(cc)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE desktop_requests(id, cc, native_cc, cwd, state, detail, desktop, binding_id)")
        self.start()
        self.tab("新建配对")
        next(w for w in widgets(self.root, ttk.Radiobutton) if "Claude → Desktop" == w["text"]).invoke()
        self.root.update()
        self.select(cc["title"])
        self.confirm.return_value = ""
        with patch("daily.provision.desktop_draft", return_value="已打开") as launch:
            self.button("打开 Desktop 草稿").invoke()
            self.wait(lambda: launch.called, "reverse control did not dispatch")
            self.assertEqual(launch.call_args.args[1:], (cc, "create", None))
        rid = str(uuid4())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO desktop_requests VALUES (?,?,?,?,?,?,?,?)",
                       (rid,cc["id"],cc["native_id"],cc["cwd"],"pending",None,None,None))
        self.refresh_and_wait()
        self.assertIn("原样发送预填首信", self.feedback())
        self.assertTrue(self.button("打开 Desktop 草稿").instate(["disabled"]))
        with patch("daily.provision.desktop_draft", return_value="已取消") as cancel:
            self.button("取消申请").invoke()
            self.wait(lambda: cancel.called, "cancel did not dispatch")
            self.assertEqual(cancel.call_args.args[1:], (cc, "cancel", rid))
        bid = self.store.bind(cc,self.free)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE desktop_requests SET state='bound',binding_id=? WHERE id=?", (bid,rid))
        self.refresh_and_wait()
        self.assertIn(f"已绑定 · 配对 {bid}", self.feedback())
        self.assertTrue(self.button("取消申请").instate(["disabled"]))

    def test_home_issue_targets_exact_letter_and_updates_after_reconciliation(self):
        self.seed_mail("required", "unknown", "需要核对的投递", "传输中断")
        second_cc = dict(self.cc, id="other", native_id=str(uuid4()))
        self.store.bind(second_cc, self.free)
        self.start(home=True)
        self.root.geometry("390x740")
        self.wait(lambda: self.button_if_visible("打开 Desktop").instate(["!disabled"]),
                  "home did not load")
        self.button(r"查看待核对消息").invoke()
        self.wait(lambda: self.find_row("需要核对的投递"), "issue did not open")
        self.assertIn("传输中断", self.feedback())
        self.confirm.return_value = "已核实"
        self.button(r"标记为已处理").invoke()
        self.wait(lambda: self.store.messages()[0]["state"] == "cancelled", "issue remained open")
        self.wait(lambda: not any("查看待核对消息" in str(b["text"])
                  for b in widgets(self.root, ttk.Button)), "issue banner remained visible")
        self.tab("配对首页")
        self.button(r"打开 Desktop").invoke()
        self.button(r"打开 Claude").invoke()
        self.desktop_open.assert_called_once_with("codex://threads/" + self.desktop["id"])
        self.browser.assert_called_once_with("http://127.0.0.1:1/session/host%20CC")

    def test_open_existing_pair_uses_host_cc_and_native_desktop(self):
        self.start()
        self.select(self.desktop["title"])
        self.button(r"浏览器|打开.*(?:CC|Claude)|open.*(?:CC|Claude)").invoke()
        self.button(r"(?:Desktop.*打开|打开.*Desktop|open.*Desktop)").invoke()
        self.browser.assert_called_once_with("http://127.0.0.1:1/session/host%20CC")
        self.desktop_open.assert_called_once_with("codex://threads/" + self.desktop["native_id"])


if __name__ == "__main__":
    unittest.main()
