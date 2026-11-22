"""Durable Desktop -> Claude requests. The GUI supplies a human-selected Desktop ID."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

if __package__:
    from .storage import Store, _uuid
else:
    from storage import Store, _uuid


TABLE = """
CREATE TABLE IF NOT EXISTS provision_requests (
    id TEXT PRIMARY KEY,
    desktop TEXT NOT NULL,
    title TEXT NOT NULL,
    cc TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'queued' CHECK
        (state IN ('queued','creating','starting','bound','failed','unknown')),
    native_cc TEXT,
    binding_id INTEGER REFERENCES bindings(id),
    detail TEXT,
    owner_pid INTEGER,
    owner_boot TEXT
)
"""
PUBLIC = "id, desktop, title, state, cc, native_cc, binding_id, detail"

def list_requests(store_path):
    """Latest creation attempt per Desktop, for restoring panel progress after reopening."""
    path = Path(store_path).resolve()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provision_requests'").fetchone():
            return []
        rows = db.execute(f"""SELECT {PUBLIC} FROM provision_requests
            WHERE rowid IN (SELECT max(rowid) FROM provision_requests GROUP BY desktop)
            ORDER BY rowid DESC""")
        return [dict(row) for row in rows]


def list_desktop_drafts(store_path):
    path = Path(store_path).resolve()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_requests'").fetchone():
            return []
        rows = db.execute("""SELECT id, cc, native_cc, cwd, state, detail, desktop, binding_id
            FROM desktop_requests WHERE rowid IN (SELECT max(rowid) FROM desktop_requests GROUP BY cc)""")
        return [dict(row) for row in rows]


def desktop_draft(args, cc, action, request_id=None):
    electron = os.environ.get("DAILY_PANEL_ELECTRON")
    if not electron or not os.environ.get("DAILY_CLOUDCLI_ROOT"):
        raise ValueError("请从双会话工作台启动入口打开面板")
    payload = dict(action=action, cc=cc["id"], native=cc["native_id"], cwd=cc["cwd"],
                   store=str(Path(args.store).resolve()), cloud=str(Path(args.cc_db).resolve()),
                   desktop=str(Path(args.desktop_db).resolve()), python=sys.executable, request=request_id)
    result = subprocess.run([electron, str(Path(__file__).with_name("panel-draft.mjs"))],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, ELECTRON_RUN_AS_NODE="1"), creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=25)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "草稿操作失败，请刷新查看申请状态")
    return json.loads(result.stdout)["status"]


def get_request(store_path, id):
    """Read one request snapshot, including its original result after unbinding."""
    request_id = _uuid(id, "request id")
    path = Path(store_path).resolve()
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provision_requests'").fetchone():
            return None
        row = db.execute(f"SELECT {PUBLIC} FROM provision_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None


def request_for_desktop(store_path, desktop_id, request_id, title):
    """Queue once. Reuse the same UUID and title to obtain the current result."""
    desktop = _uuid(desktop_id, "desktop id")
    request_id = _uuid(request_id, "request id")
    if not isinstance(title, str) or not title.strip() or "\0" in title:
        raise ValueError("A nonempty session title is required")
    title = title.strip()
    store = Store(store_path)
    with store._write() as db:
        db.execute(TABLE)
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS provision_open_desktop
                      ON provision_requests(desktop)
                      WHERE state IN ('queued','creating','starting','unknown')""")
        row = db.execute(f"SELECT {PUBLIC} FROM provision_requests WHERE id=?", (request_id,)).fetchone()
        if row:
            if (row["desktop"], row["title"]) != (desktop, title):
                raise ValueError("Request ID already used for another thread or title")
            return dict(row)
        if db.execute("SELECT 1 FROM bindings WHERE active=1 AND (desktop=? OR desktop_host=?)",
                      (desktop, desktop)).fetchone():
            raise ValueError("Desktop already has an active binding")
        if db.execute("""SELECT 1 FROM provision_requests WHERE desktop=?
                         AND state IN ('queued','creating','starting','unknown')""", (desktop,)).fetchone():
            raise ValueError("Desktop already has an unresolved creation request")
        row = db.execute(f"""INSERT INTO provision_requests(id,desktop,title,cc) VALUES(?,?,?,?)
                             RETURNING {PUBLIC}""", (request_id, desktop, title, str(uuid4()))).fetchone()
        return dict(row)


def main():
    parser = argparse.ArgumentParser(description="Create and bind Claude to the calling Desktop thread")
    parser.add_argument("--store", default=os.environ.get("DAILY_BRIDGE_STORE") or
                        Path(__file__).with_name("data") / "bridge.sqlite3")
    create = parser.add_subparsers(dest="command", required=True).add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--title", required=True)
    args = parser.parse_args()
    try:
        result = request_for_desktop(args.store, os.environ.get("CODEX_THREAD_ID"), args.id, args.title)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"CODEX_THREAD_ID / provision: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
