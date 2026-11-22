"""SQLite contract shared by the panel, relay and Node worker.

Workers claim queued mail with a conditional UPDATE before external I/O.
Every later update must also compare the expected state and check changes:
a Desktop receipt can arrive while the worker is still awaiting the CLI ack.
Opening a Store never claims, resets or replays mail.
"""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from uuid import UUID


BINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    id INTEGER PRIMARY KEY,
    cc TEXT NOT NULL,
    native_cc TEXT NOT NULL,
    cc_title TEXT NOT NULL,
    cc_cwd TEXT NOT NULL,
    desktop TEXT NOT NULL,
    desktop_host TEXT NOT NULL,
    desktop_title TEXT NOT NULL,
    desktop_cwd TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS active_cc ON bindings(cc) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS active_native_cc ON bindings(native_cc) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS active_desktop ON bindings(desktop) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS active_desktop_host ON bindings(desktop_host) WHERE active = 1;
"""

REPLY_MODES = ("required", "none", "deferred")
MAIL_TABLE = """
CREATE TABLE {table} (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    binding_id INTEGER NOT NULL REFERENCES bindings(id),
    source TEXT NOT NULL CHECK (source IN ('cc', 'desktop')),
    body TEXT NOT NULL,
    reply_mode TEXT NOT NULL DEFAULT 'required' CHECK (reply_mode IN ('required', 'none', 'deferred')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
        ('queued', 'submitting', 'submitted', 'received', 'returning',
         'returned', 'failed', 'unknown', 'cancelled', 'notified', 'awaiting_reply')),
    reply TEXT,
    detail TEXT,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (state <> 'notified' OR reply_mode = 'none'),
    CHECK (state <> 'awaiting_reply' OR reply_mode = 'deferred'),
    CHECK (reply_mode <> 'none' OR reply IS NULL)
);
"""

INDEXES_AND_TRIGGERS = (
    "CREATE INDEX IF NOT EXISTS mail_binding_seq ON mail(binding_id, seq)",
    "CREATE INDEX IF NOT EXISTS mail_state_seq ON mail(state, seq)",
    """
CREATE TRIGGER IF NOT EXISTS binding_snapshot BEFORE UPDATE ON bindings
WHEN OLD.active <> 1 OR NEW.active <> 0 OR
     (NEW.id, NEW.cc, NEW.native_cc, NEW.cc_title, NEW.cc_cwd, NEW.desktop,
      NEW.desktop_host, NEW.desktop_title, NEW.desktop_cwd) IS NOT
     (OLD.id, OLD.cc, OLD.native_cc, OLD.cc_title, OLD.cc_cwd, OLD.desktop,
      OLD.desktop_host, OLD.desktop_title, OLD.desktop_cwd)
BEGIN
    SELECT RAISE(ABORT, 'binding snapshots can only be retired');
END;
""", """
CREATE TRIGGER IF NOT EXISTS mail_active_binding BEFORE INSERT ON mail
WHEN NOT EXISTS (SELECT 1 FROM bindings WHERE id = NEW.binding_id AND active = 1)
BEGIN
    SELECT RAISE(ABORT, 'mail requires an active binding');
END;
""", """
CREATE TRIGGER IF NOT EXISTS mail_envelope BEFORE UPDATE OF
    seq, id, binding_id, source, body, created, reply_mode ON mail
BEGIN
    SELECT RAISE(ABORT, 'mail envelope is immutable');
END;
""", """
CREATE TRIGGER IF NOT EXISTS mail_terminal BEFORE UPDATE OF state ON mail
WHEN OLD.state IN ('returned', 'cancelled', 'notified') AND NEW.state <> OLD.state
BEGIN
    SELECT RAISE(ABORT, 'mail is already closed');
END;
""")

BINDING_FIELDS = "id, cc, native_cc, cc_title, cc_cwd, desktop, desktop_title, desktop_cwd, active"


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} requires non-empty text")
    return value


def _uuid(value, name):
    try:
        parsed = UUID(_text(value, name))
        if value.lower() not in (str(parsed), parsed.hex):
            raise ValueError
        return str(parsed)
    except ValueError:
        raise ValueError(f"{name} requires a UUID") from None


class Store:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            notices = Path(__file__).with_name("pairing-notices.sql").read_text(encoding="utf-8")
            db.executescript("BEGIN IMMEDIATE;\n" + BINDINGS_SCHEMA + notices)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(mail)")}
            if not columns:
                db.execute(MAIL_TABLE.format(table="mail"))
            elif "reply_mode" not in columns:
                # The old state CHECK requires a rebuild. Install the active-binding
                # insert trigger after copying history, which includes retired routes.
                db.execute(MAIL_TABLE.format(table="mail_new"))
                fields = "seq, id, binding_id, source, body, state, reply, detail, created"
                db.execute(f"INSERT INTO mail_new ({fields}) SELECT {fields} FROM mail")
                # Deleted messages may have used sequence numbers above MAX(seq).
                db.execute("""
                    UPDATE sqlite_sequence
                    SET seq = (SELECT max(seq) FROM sqlite_sequence WHERE name IN ('mail', 'mail_new'))
                    WHERE name='mail_new'
                """)
                db.execute("DROP TABLE mail")
                db.execute("ALTER TABLE mail_new RENAME TO mail")
            for statement in INDEXES_AND_TRIGGERS:
                db.execute(statement)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def bindings(self, *, include_inactive=False):
        """Return active bindings in creation order. History is opt-in."""
        where = "" if include_inactive else " WHERE active = 1"
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute(f"SELECT {BINDING_FIELDS} FROM bindings{where} ORDER BY id")]

    def messages(self):
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute("""
                SELECT m.id,m.state,m.body,m.reply,m.reply_mode,m.detail,m.created,m.binding_id,m.source,
                       b.cc,b.desktop,b.cc_title,b.desktop_title
                FROM mail m JOIN bindings b ON b.id=m.binding_id ORDER BY m.seq
            """)]

    @staticmethod
    def _retire(db, binding_id):
        pending = db.execute("""
            SELECT id, state FROM mail WHERE binding_id = ?
              AND state NOT IN ('queued', 'returned', 'cancelled', 'notified') ORDER BY seq LIMIT 1
        """, (binding_id,)).fetchone()
        if pending:
            raise ValueError(f"Binding {binding_id} has unresolved mail {pending['id']} "
                             f"({pending['state']}); finish or reconcile it first")
        db.execute("""
            UPDATE mail SET state = 'cancelled', detail = 'Binding retired before delivery'
            WHERE binding_id = ? AND state = 'queued'
        """, (binding_id,))
        db.execute("UPDATE bindings SET active = 0 WHERE id = ? AND active = 1", (binding_id,))

    def bind(self, cc_dict, desktop_dict, *, expected_conflicts=None):
        """Return the integer binding ID. desktop is always the native thread UUID."""
        cc = _text(cc_dict.get("id"), "cc.id")
        native_cc = _uuid(cc_dict.get("native_id"), "cc.native_id")
        host = _text(desktop_dict.get("id"), "desktop.id")
        desktop = _uuid(desktop_dict.get("native_id"), "desktop.native_id")
        keys = (cc, native_cc, desktop, host)
        with self._write() as db:
            conflicts = db.execute("""
                SELECT id, cc, native_cc, desktop, desktop_host FROM bindings
                WHERE active = 1 AND (cc = ? OR native_cc = ? OR desktop = ? OR desktop_host = ?)
            """, keys).fetchall()
            if expected_conflicts is not None and {str(r["id"]) for r in conflicts} != set(expected_conflicts):
                raise ValueError("相关配对已变更，请刷新列表并重新确认。")
            for row in conflicts:
                if tuple(row)[1:] == keys:
                    return row["id"]
                self._retire(db, row["id"])
            return db.execute("""
                INSERT INTO bindings (cc, native_cc, desktop, desktop_host,
                    cc_title, cc_cwd, desktop_title, desktop_cwd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (*keys, cc_dict["title"], cc_dict["cwd"],
                  desktop_dict["title"], desktop_dict["cwd"])).lastrowid

    def unbind(self, id):
        with self._write() as db:
            self._retire(db, id)

    @staticmethod
    def _origin(db, thread_id):
        row = db.execute("SELECT id FROM bindings WHERE active = 1 AND desktop = ?",
                         (_uuid(thread_id, "CODEX_THREAD_ID"),)).fetchone()
        if row is None:
            raise ValueError("Desktop thread has no active binding")
        return row["id"]

    def desktop_send(self, thread_id, text, request_id, reply_mode=None, *, expected_binding=None):
        """Enqueue a Desktop-origin letter once. Return its canonical request UUID."""
        request_id = _uuid(request_id, "request_id")
        _text(text, "body")
        if reply_mode is not None and reply_mode not in REPLY_MODES:
            raise ValueError("reply_mode must be required, none, or deferred")
        if len(text) > 2500:
            raise ValueError("单封信最多 2500 字，请改发文件路径和版本")
        with self._write() as db:
            binding_id = self._origin(db, thread_id)
            if expected_binding is not None and expected_binding != binding_id:
                raise ValueError("Expected binding is no longer active for this Desktop thread")
            row = db.execute("SELECT binding_id, source, body, reply_mode FROM mail WHERE id = ?",
                             (request_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO mail(id, binding_id, source, body, reply_mode) VALUES (?, ?, 'desktop', ?, ?)",
                           (request_id, binding_id, text, reply_mode or "none"))
            elif tuple(row) != (binding_id, "desktop", text, reply_mode or row["reply_mode"]):
                raise ValueError("Request ID already used for a different message, reply mode or binding")
        return request_id

    def desktop_reply(self, thread_id, mail_id, text):
        """Record a receipt once, even if the CLI submission ack is still pending."""
        _text(mail_id, "mail_id")
        _text(text, "reply")
        if len(text) > 2500:
            raise ValueError("单次回信最多 2500 字，请改发文件路径和版本")
        with self._write() as db:
            binding_id = self._origin(db, thread_id)
            row = db.execute("""
                SELECT state, reply, reply_mode FROM mail WHERE id = ? AND binding_id = ? AND source = 'cc'
            """, (mail_id, binding_id)).fetchone()
            if row is None:
                raise ValueError("No incoming CC mail with this ID for the active binding")
            if row["reply_mode"] == "none":
                raise ValueError("Cannot reply to a notification")
            if row["reply"] is not None:
                if row["reply"] != text:
                    raise ValueError("A different reply is already recorded")
            elif row["state"] in ("submitting", "submitted", "unknown", "awaiting_reply"):
                db.execute("UPDATE mail SET reply = ?, state = 'received' WHERE id = ?", (text, mail_id))
            else:
                raise ValueError(f"Cannot accept first reply in state {row['state']}")
        return mail_id

    def desktop_confirm(self, thread_id, id):
        """Confirm that the initiating Desktop thread received the CC reply."""
        mail_id = _uuid(id, "mail_id")
        with self._write() as db:
            binding_id = self._origin(db, thread_id)
            row = db.execute("""
                SELECT state, reply FROM mail WHERE id = ? AND binding_id = ? AND source = 'desktop'
            """, (mail_id, binding_id)).fetchone()
            if row is None or row["reply"] is None:
                raise ValueError("No Desktop-origin mail with a stored reply for the active thread")
            if row["state"] in ("returning", "submitted", "unknown"):
                db.execute("UPDATE mail SET state = 'returned' WHERE id = ?", (mail_id,))
            elif row["state"] != "returned":
                raise ValueError(f"Cannot confirm mail in state {row['state']}")
        return mail_id

    def reconcile(self, mail_id, expected_state, note):
        """Operator-only closure after checking the native sessions and stopping any stalled worker.

        Never resubmits. Require the observed state and preserve the prior detail.
        'cancelled' after manual closure makes no claim about external delivery.
        """
        _text(note, "operator note")
        if expected_state not in ("submitting", "submitted", "awaiting_reply", "received", "returning", "failed", "unknown"):
            raise ValueError("Reconciliation requires an unresolved state")
        with self._write() as db:
            result = db.execute("""
                UPDATE mail SET state = 'cancelled',
                    detail = COALESCE(detail || char(10), '') || ?
                WHERE id = ? AND state = ?
            """, (f"Manually closed from {expected_state}: {note}", mail_id, expected_state))
            if result.rowcount != 1:
                raise ValueError("Mail missing or state changed; refresh before reconciling")
