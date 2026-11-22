import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily.catalog import default_paths, list_sessions


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "索引 #100%.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)

    def test_cc_identity_archives_paths_and_search(self):
        self.db.executescript("""
            CREATE TABLE sessions(session_id, provider_session_id, custom_name,
                project_path, provider, isArchived, updated_at, jsonl_path);
            CREATE TABLE projects(project_path, isArchived);
            INSERT INTO projects VALUES ('C:/hidden', 1);
        """)
        rows = [
            ("app", "native", "Straße 100%_", r"\\?\UNC\lab\共享\work", "claude", 0),
            ("old", "old", None, "E:/work", "claude", 0),
            ("pending", None, "", "E:/work", "claude", 0),
            ("other", "x", "", "E:/work", "codex", 0),
            ("archived", "x", "", "E:/work", "claude", 1),
            ("hidden-project", "x", "", "C:/hidden", "claude", 0),
            ("linux", "x", "", "/home/user", "claude", 0),
            ("relative", "x", "", "C:work", "claude", 0),
            ("device", "x", "", r"\\.\pipe\name", "claude", 0),
        ]
        self.db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 0, NULL)", rows)
        self.db.commit()
        expected = {"id": "app", "native_id": "native", "title": "Straße 100%_",
                    "cwd": r"\\lab\共享\work"}
        for term in ("app", "NATIVE", "strasse", "%_", "//lab/共享"):
            with self.subTest(term=term):
                self.assertEqual(list_sessions("cc", self.path, term), [expected])
        self.assertEqual(list_sessions("cc", self.path), [
            expected, {"id": "old", "native_id": "old", "title": "old", "cwd": r"E:\work"}])

    def test_desktop_provenance_and_public_title(self):
        self.db.execute("""CREATE TABLE threads(id, name, title, cwd, source,
            originator, thread_source, archived, agent_role, first_user_message, preview)""")
        for i, (source, origin, thread, archived, role) in enumerate([
            ("vscode", "Codex Desktop", "user", 0, None),
            ("vscode", "codex_work_desktop", "user", 0, None),
            ("vscode", None, None, 0, None),
            ("exec", "Codex Desktop", "user", 0, None),
            ("cli", None, "user", 0, None),
            ("vscode", "codex_vscode", "user", 0, None),
            ("vscode", "Codex Desktop", "guardian_review", 0, None),
            ("vscode", "Codex Desktop", "subagent", 0, None),
            ("vscode", "Codex Desktop", "agent_created_thread", 0, None),
            ("vscode", "Codex Desktop", "user", 1, None),
            ("vscode", None, None, 0, "worker"),
        ]):
            self.db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (str(i), "Display", "Earlier title", r"\\?\C:\工程",
                             source, origin, thread, archived, role, "private", "private"))
        self.db.commit()
        connect = sqlite3.connect

        def guarded_connect(database, **kwargs):
            conn = connect(database, **kwargs)

            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ and column in ("first_user_message", "preview"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn

        with patch("daily.catalog.sqlite3.connect", guarded_connect):
            self.assertEqual(list_sessions("desktop", self.path), [
                {"id": i, "native_id": i, "title": "Display", "cwd": r"C:\工程"}
                for i in ("0", "1", "2")])

    def test_old_schema_and_old_match_in_live_wal(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE threads(id, title, cwd, source, archived, updated_at)")
        self.db.executemany("INSERT INTO threads VALUES (?, ?, 'D:/lab', 'vscode', 0, ?)",
                            ((str(i), "old needle" if i == 0 else "recent", i)
                             for i in range(5001)))
        self.db.commit()
        self.assertEqual(list_sessions("desktop", self.path, "needle"), [
            {"id": "0", "native_id": "0", "cwd": r"D:\lab", "title": "old needle"}])
        rows = list_sessions("desktop", self.path)
        self.assertEqual(len(rows), 5001)
        self.assertEqual(rows[0]["id"], "5000")

    def test_defaults_and_missing_database(self):
        self.assertEqual(default_paths(), {"cc": Path.home() / ".cloudcli/auth.db",
                                           "desktop": Path.home() / ".codex/state_5.sqlite"})
        missing = self.path.with_name("missing.sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            list_sessions("cc", missing)
        with self.assertRaises(ValueError):
            list_sessions("unknown", missing)
        self.assertFalse(missing.exists())
