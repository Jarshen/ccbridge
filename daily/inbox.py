"""One bounded CC reply reservation per immutable Desktop request."""
import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

if __package__:
    from .storage import _uuid
else:
    from storage import _uuid


LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_leases (
    binding_id INTEGER NOT NULL REFERENCES bindings(id),
    mail_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    receipt_token TEXT NOT NULL,
    PRIMARY KEY (binding_id, mail_id)
)"""
# A reservation can precede relay.send. mail.id becomes immutable on insertion.
LEASE_ROWS = """FROM inbox_leases l JOIN bindings b ON b.id=l.binding_id
    LEFT JOIN mail m ON m.id=l.mail_id AND m.binding_id=l.binding_id AND m.source='desktop'"""
SCOPE = "l.mail_id=? AND l.binding_id=? AND b.desktop=? AND b.active=1"
LIVE = "l.expires_at > unixepoch()"


def wait_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 <= seconds <= 60:
        raise ValueError("wait must be between 0 and 60 seconds")
    return seconds


def arm(db, scope, seconds):
    if not 1 <= seconds <= 60:
        raise ValueError("lease must be between 1 and 60 seconds")
    mail_id, binding, thread = scope
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM bindings WHERE id=? AND desktop=? AND active=1",
                          (binding, thread)).fetchone():
            return {"status": "unavailable"}
        row = db.execute("""SELECT state,reply_mode FROM mail
            WHERE binding_id=? AND id=? AND source='desktop'""", (binding, mail_id)).fetchone()
        if row and (row[1] == "none" or row[0] not in ("queued", "submitting", "awaiting_reply", "received")):
            return {"status": "unavailable"}
        db.execute(LEASE_SCHEMA)
        db.execute("""INSERT INTO inbox_leases(binding_id, mail_id, expires_at, receipt_token)
            VALUES (?, ?, unixepoch()+?, lower(hex(randomblob(16))))
            ON CONFLICT(binding_id,mail_id) DO NOTHING""", (binding, mail_id, seconds))
        expires, live = db.execute(f"SELECT l.expires_at, {LIVE} {LEASE_ROWS} WHERE {SCOPE}", scope).fetchone()
    return {"status": "armed" if live else "expired", "expires_at": expires}


def read(db, scope, seconds):
    deadline = time.monotonic() + seconds
    while True:
        ready = f"{LIVE} AND m.state='received' AND m.reply IS NOT NULL"
        row = db.execute(f"""SELECT {LIVE}, m.state, m.reply_mode,
            CASE WHEN {ready} THEN m.reply END, CASE WHEN {ready} THEN l.receipt_token END
            {LEASE_ROWS} WHERE {SCOPE}""", scope).fetchone()
        if row is None:
            return {"status": "unavailable"}
        live, state, mode, reply, receipt = row
        if state == "returned":
            return {"status": "already_received"}
        if not live:
            return {"status": "expired"}
        if receipt is not None:
            return {"status": "ready", "reply": reply, "receipt": receipt}
        if mode == "none" or state not in (None, "queued", "submitting", "awaiting_reply"):
            return {"status": "unavailable"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "pending"}
        time.sleep(min(remaining, 0.2))


def accept(db, scope, receipt):
    # Only the matching read returned this token. Concurrent consumers and the
    # native queue compete on the same mail row, including at the expiry boundary.
    changed = db.execute(f"""UPDATE mail SET state='returned'
        WHERE id=? AND binding_id=? AND source='desktop' AND state='received' AND reply IS NOT NULL
        AND EXISTS (SELECT 1 FROM inbox_leases l JOIN bindings b ON b.id=l.binding_id
            WHERE l.mail_id=mail.id AND l.binding_id=mail.binding_id AND b.desktop=?
            AND b.active=1 AND {LIVE} AND l.receipt_token=?)""", (*scope, receipt)).rowcount
    return {"status": "accepted" if changed else "not_accepted"}


def hook_event(names):
    # Trust hook identity fields only. Tool input/output and transcripts are never used.
    raw = sys.stdin.buffer.read(1_048_577)
    if len(raw) > 1_048_576:
        return None
    try:
        event = json.loads(raw)
        if (event["hook_event_name"] not in names or not event.get("turn_id")
                or event.get("agent_id") or event.get("agent_type")):
            return None
        thread = _uuid(event["session_id"], "session_id")
        inherited = os.environ.get("CODEX_THREAD_ID")
        if inherited is not None and _uuid(inherited, "CODEX_THREAD_ID") != thread:
            return None
        return thread, _uuid(event["turn_id"], "turn_id"), event["hook_event_name"]
    except (KeyError, ValueError, TypeError, RecursionError):
        return None


def notice(db, thread):
    row = db.execute(f"""SELECT l.binding_id,l.mail_id {LEASE_ROWS}
        WHERE b.desktop=? AND b.active=1 AND {LIVE} AND m.state='received' AND m.reply IS NOT NULL
        ORDER BY m.seq LIMIT 1""", (thread,)).fetchone()
    if row is None:
        return {}
    binding, mail_id = row
    # Only helper-created UUIDs may appear in developer context.
    mail_id = _uuid(mail_id, "mail_id")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
            "additionalContext": f"CC reply available. Use inbox.py wait --binding {int(binding)} "
                                 f"--id {mail_id}. Before acting, use accept with the receipt."}}


def tool_boundary(db, event, path):
    thread, turn, name = event
    with db:
        db.execute("BEGIN IMMEDIATE")
        binding = db.execute("SELECT id FROM bindings WHERE active=1 AND desktop=?", (thread,)).fetchone()
        if binding is None:
            return {}
        binding = binding[0]
        db.execute("""INSERT INTO inbox_tool_turns(binding_id,turn_id,seen_at) VALUES(?,?,unixepoch())
            ON CONFLICT(binding_id) DO UPDATE SET turn_id=excluded.turn_id, seen_at=excluded.seen_at""",
                   (binding, turn))
        if name == "PreToolUse":
            return {}
        row = db.execute("""SELECT m.id,o.receipt FROM inbox_offers o JOIN mail m
            ON m.id=o.mail_id AND m.binding_id=o.binding_id
            WHERE o.binding_id=? AND o.turn_id=? AND o.expires_at>unixepoch()
            AND m.source='cc' AND m.reply_mode='none' AND m.state='queued' ORDER BY m.seq LIMIT 1""",
                         (binding, turn)).fetchone()
    if row is None:
        return notice(db, thread)
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = (f"& {quote(sys.executable)} -B {quote(Path(__file__).resolve())} --store {quote(path)} "
               f"take --binding {binding} --id {quote(_uuid(row[0], 'mail id'))} --receipt {quote(row[1])}")
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext":
            "当前配对有一封单向消息。请运行下列 PowerShell 命令取件，仅在 taken 时按原任务授权处理其正文：\n" + command}}


def take(db, scope, receipt):
    mail, binding, thread = scope
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT m.body,o.turn_id,b.cc FROM inbox_offers o
            JOIN bindings b ON b.id=o.binding_id JOIN inbox_tool_turns t ON t.binding_id=b.id AND t.turn_id=o.turn_id
            JOIN mail m ON m.id=o.mail_id AND m.binding_id=b.id
            WHERE m.id=? AND b.id=? AND b.desktop=? AND b.active=1 AND o.receipt=?
            AND o.expires_at>unixepoch() AND m.source='cc' AND m.reply_mode='none' AND m.state='queued'""",
                         (mail,binding,thread,receipt)).fetchone()
        if row is None:
            return {"status": "unavailable"}
        db.execute("UPDATE mail SET state='notified',detail=? WHERE id=? AND state='queued'",
                   ("Desktop 工具结束后取件；turn ID " + row[1],mail))
    return {"status":"taken","from":row[2],"text":row[0],"turn_id":row[1]}


def run(args):
    hook = args.command in ("hook","tool-hook")
    if hook:
        names = ("PreToolUse","PostToolUse") if args.command == "tool-hook" else ("PostToolUse",)
        event = hook_event(names)
        if event is None:
            return {}
    else:
        thread = _uuid(os.environ.get("CODEX_THREAD_ID"), "CODEX_THREAD_ID")
        if not 0 < args.binding < 2**63:
            raise ValueError("binding must be a positive SQLite integer")
        scope = (_uuid(args.id, "id"), args.binding, thread)
    path = args.store or os.environ.get("DAILY_BRIDGE_STORE") or Path(__file__).parent / "data/bridge.sqlite3"
    mode = "rw" if args.command in ("arm", "accept", "take", "tool-hook") else "ro"
    uri = Path(path).resolve().as_uri() + "?mode=" + mode
    with closing(sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)) as db:
        if args.command == "tool-hook":
            return tool_boundary(db, event, Path(path).resolve())
        if hook:
            return notice(db, event[0])
        if args.command == "arm":
            result = arm(db, scope, args.lease_seconds)
        elif args.command == "wait":
            result = read(db, scope, args.seconds)
        elif args.command == "take":
            result = take(db, scope, args.receipt)
        else:
            result = accept(db, scope, args.receipt)
        return {**result, "binding": scope[1], "id": scope[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("arm", "wait", "accept", "hook", "tool-hook", "take"):
        sub = commands.add_parser(name)
        if name not in ("hook","tool-hook"):
            sub.add_argument("--binding", type=int, required=True)
            sub.add_argument("--id", required=True)
        if name == "arm":
            sub.add_argument("--lease-seconds", type=int, default=60)
        if name == "wait":
            sub.add_argument("--seconds", type=wait_seconds, default=0)
        if name in ("accept","take"):
            sub.add_argument("--receipt", required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        if args.command not in ("hook","tool-hook"):
            parser.exit(1, f"Inbox error: {exc}\n")
        result = {}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
