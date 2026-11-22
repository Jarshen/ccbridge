from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from daily import panel
from daily.catalog import list_sessions
from daily.storage import Store


@unittest.skipUnless(sys.platform == "win32", "Windows Tk panel")
class PanelTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.args = SimpleNamespace(cc_db=directory / "cc.db", desktop_db=directory / "desktop.db",
                                    store=directory / "store.db", status_file=directory / "offline.json",
                                    cc_url="http://127.0.0.1:1")
        self.native, self.first, self.second = (str(uuid4()) for _ in range(3))
        with closing(sqlite3.connect(self.args.cc_db)) as db, db:
            db.execute("""CREATE TABLE sessions(session_id, provider_session_id, custom_name,
                       project_path, provider, isArchived)""")
            db.executemany("INSERT INTO sessions VALUES (?, ?, ?, 'C:/fixture', 'claude', 0)",
                           [("fixture-a", self.native, "Alpha"), ("fixture-b", str(uuid4()), "Beta")])
        with closing(sqlite3.connect(self.args.desktop_db)) as db, db:
            db.execute("CREATE TABLE threads(id, title, cwd, source, archived)")
            db.executemany("INSERT INTO threads VALUES (?, ?, 'C:/fixture', 'desktop', 0)",
                           [(self.first, "First"), (self.second, "Second")])
        self.confirm = self.enterContext(patch.object(panel, "confirmation", return_value=None))
        self.showerror = self.enterContext(patch.object(panel.messagebox, "showerror"))
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda _, exc, tb: self.callback_errors.append(exc)
        self.app = None
        self.addCleanup(self.close_panel)
        self.open_panel()

    def open_panel(self):
        backend = panel.Backend(self.args, list_sessions, Store)
        self.app = panel.Panel(tk.Toplevel(self.root), self.args, backend)
        self.cc, self.desktop = (self.app.pickers[k] for k in panel.KINDS)
        self.wait_for(lambda: len(self.cc.tree.get_children()) == 2
                      and len(self.desktop.tree.get_children()) == 2)

    def close_panel(self):
        if self.app:
            if self.app.timer is not None:
                self.app.root.after_cancel(self.app.timer)
            self.app.close()
            self.app = None

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        satisfied = False

        def check():
            nonlocal satisfied
            satisfied = not self.app.busy and bool(predicate())
            if satisfied or self.callback_errors or time.monotonic() >= deadline:
                self.root.quit()
            else:
                self.root.after(10, check)

        self.root.after(10, check)
        self.root.mainloop()
        if self.callback_errors:
            raise self.callback_errors[0]
        self.assertTrue(satisfied, f"Timed out waiting for panel; footer={self.app.footer['text']!r}")

    def test_filter_confirm_reload_replace_and_unbind(self):
        app = self.app
        self.assertTrue(app.save.instate(["disabled"]))
        self.assertFalse(app.ready)
        for picker, query in [(self.cc, "ALPHA"), (self.desktop, "first")]:
            search = next(w for w in picker.winfo_children()
                          if isinstance(w, ttk.Entry) and str(w["state"]) == "normal")
            search.insert(0, query)
        self.wait_for(lambda: self.cc.tree.get_children() == ("fixture-a",)
                      and self.desktop.tree.get_children() == (self.first,))
        self.cc.tree.selection_set("fixture-a")
        self.desktop.tree.selection_set(self.first)
        self.wait_for(lambda: app.save.instate(["!disabled"]))
        self.assertEqual(self.native, self.cc.fields["native_id"].get())
        self.assertEqual(self.first, self.desktop.fields["id"].get())

        store = Store(self.args.store)
        app.save.invoke()
        prompt = self.confirm.call_args.args[2]
        for identifier in ("fixture-a", self.native, self.first):
            self.assertIn(identifier, prompt)
        self.wait_for(lambda: not app.bindings)
        self.assertEqual([], store.bindings())
        self.confirm.return_value = ""
        app.save.invoke()
        self.wait_for(lambda: app.selected_binding() and app.save.instate(["disabled"]))
        original = store.bindings()[0]
        self.close_panel()
        self.open_panel()
        app = self.app
        self.assertEqual(original, app.selected_binding())
        self.assertEqual(("fixture-a",), self.cc.tree.selection())
        self.assertEqual((self.first,), self.desktop.tree.selection())
        self.assertTrue(app.remove.instate(["!disabled"]))

        self.desktop.tree.selection_set(self.second)
        self.wait_for(lambda: app.save.instate(["!disabled"]))
        self.confirm.return_value = None
        app.save.invoke()
        prompt = self.confirm.call_args.args[2]
        self.assertIn(self.first, prompt)
        self.assertIn(self.second, prompt)
        self.assertIn(f"配对 {original['id']}", prompt)
        self.wait_for(lambda: app.selected_binding() == original)
        self.assertEqual([original], store.bindings())
        self.confirm.return_value = ""
        app.save.invoke()
        self.wait_for(lambda: (app.selected_binding() or {}).get("desktop") == self.second)
        self.assertEqual([self.second], [b["desktop"] for b in store.bindings()])
        self.assertEqual([0, 1], [b["active"] for b in store.bindings(include_inactive=True)])

        app.remove.invoke()
        self.assertIn(self.second, self.confirm.call_args.args[2])
        self.wait_for(lambda: not app.bindings and app.remove.instate(["disabled"]))
        self.assertEqual([], store.bindings())

        db_path = self.args.cc_db
        self.args.cc_db = db_path.with_name("missing.db")
        app.refresh()
        self.wait_for(lambda: "cc" in app.errors)
        self.assertIn(app.errors["cc"], app.footer["text"])
        self.assertIn(app.errors["cc"], self.cc.note["text"])
        self.assertTrue(app.save.instate(["disabled"]))
        self.args.cc_db = db_path
        app.refresh()
        self.wait_for(lambda: not app.errors and app.save.instate(["!disabled"]))
        self.showerror.assert_not_called()


if __name__ == "__main__":
    unittest.main()
