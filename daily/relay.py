"""Desktop helper. Invoke by absolute script path or as python -m daily.relay."""

import argparse
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3

if __package__:
    from .storage import REPLY_MODES, Store, _uuid
    from . import inbox
else:
    from storage import REPLY_MODES, Store, _uuid
    import inbox


def main():
    parser = argparse.ArgumentParser(description="Send or answer mail on the current Desktop thread.")
    parser.add_argument("--store", type=Path, help="SQLite file (overrides DAILY_BRIDGE_STORE)")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (("send", "UUID for this request, reused on retry"),
                               ("reply", "ID of the incoming CC message")):
        sub = commands.add_parser(command)
        if command == "send":
            sub.add_argument("--binding", type=int, required=True, help="Expected active binding ID")
            sub.add_argument("--mode", choices=REPLY_MODES, help=argparse.SUPPRESS)
            sub.add_argument("--wait", type=int, choices=range(1,61), metavar="1..60",
                             help=argparse.SUPPRESS)
        sub.add_argument("--id", required=True, help=help_text)
        sub.add_argument("--text", required=True, help="Message text")
    confirm = commands.add_parser("confirm", help="Confirm receipt of the reply to a Desktop request")
    confirm.add_argument("--id", required=True, help="UUID of the original Desktop request")
    args = parser.parse_args()
    thread = os.environ.get("CODEX_THREAD_ID")
    if not thread:
        parser.exit(1, "CODEX_THREAD_ID is required\n")
    path = args.store or os.environ.get("DAILY_BRIDGE_STORE")
    if not path:
        path = Path(__file__).resolve().parent / "data" / "bridge.sqlite3"
    try:
        store = Store(path)
        if args.command == "send":
            receipt = None
            if args.wait and args.mode == "none":
                raise ValueError("通知无需回信，请移除 --wait")
            if args.wait:
                scope = (_uuid(args.id,"mail ID"),args.binding,_uuid(thread,"thread ID"))
                with closing(sqlite3.connect(path,isolation_level=None)) as db:
                    lease = inbox.arm(db,scope,args.wait)
            mail_id = store.desktop_send(thread, args.text, args.id, expected_binding=args.binding,
                                         reply_mode=args.mode or ("required" if args.wait else None))
            if args.wait:
                if lease["status"] == "armed":
                    with closing(sqlite3.connect(path,isolation_level=None)) as db:
                        receipt = inbox.read(db,scope,args.wait)
                else:
                    receipt = lease
                if receipt.get("status") == "ready":
                    receipt["next"] = "使用同目录 inbox.py --store <同一数据库> accept --binding <binding> --id <id> --receipt <receipt>。只有 accepted 才在本轮执行；否则由原生投递处理。"
                print(json.dumps({"id":mail_id,"binding":args.binding,**receipt}))
                return
        elif args.command == "reply":
            mail_id = store.desktop_reply(thread, args.id, args.text)
        else:
            mail_id = store.desktop_confirm(thread, args.id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"id": mail_id}))


if __name__ == "__main__":
    main()
