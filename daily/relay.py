"""Desktop helper. Invoke by absolute script path or as python -m daily.relay."""

import argparse
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import sys


def originating_turn(thread):
    history=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'thread_history_1.sqlite'
    try:
        with closing(sqlite3.connect(history.resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)) as db:
            row=db.execute('SELECT turn_id,status FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1',
                           (thread,)).fetchone()
            return row[0] if row and row[1]=='inProgress' else None
    except sqlite3.Error:
        return None

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
            sub.add_argument("--follow", action="store_true", help="Send and keep receiving CC letters in this Desktop turn")
            sub.add_argument("--mode", choices=REPLY_MODES, help=argparse.SUPPRESS)
            sub.add_argument("--wait", type=int, choices=range(1,61), metavar="1..60",
                             help=argparse.SUPPRESS)
        sub.add_argument("--id", required=True, help=help_text)
        sub.add_argument("--text", required=True, help="Message text")
    confirm = commands.add_parser("confirm", help="Confirm receipt of the reply to a Desktop request")
    confirm.add_argument("--id", required=True, help="UUID of the original Desktop request")
    lookup=commands.add_parser('receipt',help='Look up a paired letter or failed send by its ID.')
    lookup.add_argument('--id',required=True)
    for name in ('cc-send','cc-receipt'):
        sub=commands.add_parser(name,help='Explicit send or receipt from the paired standalone Claude CLI project')
        sub.add_argument('--binding',type=int,required=True)
        sub.add_argument('--native-cc',default=os.environ.get('CLAUDE_CODE_SESSION_ID'))
        sub.add_argument('--id',required=True)
        if name=='cc-send':
            source=sub.add_mutually_exclusive_group(required=True)
            source.add_argument('--text')
            source.add_argument('--text-file',type=Path,help='UTF-8 letter body')
    args = parser.parse_args()
    thread = os.environ.get("CODEX_THREAD_ID")
    from_cc=args.command in ('cc-send','cc-receipt')
    if not from_cc and not thread:
        parser.exit(1, "CODEX_THREAD_ID is required\n")
    path = args.store or os.environ.get("DAILY_BRIDGE_STORE")
    if not path:
        path = Path(__file__).resolve().parent / "data" / "bridge.sqlite3"
    try:
        store = Store(path)
        if from_cc:
            if __package__:
                from .launcher import configure,database_path
                from .catalog import list_sessions
            else:
                from launcher import configure,database_path
                from catalog import list_sessions
            native=_uuid(args.native_cc,'native CC ID')
            inherited=os.environ.get('CLAUDE_CODE_SESSION_ID')
            if inherited and _uuid(inherited,'current Claude session')!=native:
                raise ValueError('当前 Claude 会话与指定 ID 不符')
            binding=next((b for b in store.bindings() if b['id']==args.binding and b['native_cc']==native),None)
            if not binding or Path(binding['cc_cwd']).resolve()!=Path.cwd().resolve():
                raise ValueError('请从指定活动配对的 Claude 项目目录调用')
            config=configure(False,os.environ)
            index=database_path(config.root,config.home,config.env)
            if not any(s['id']==binding['cc'] and s['native_id']==native and Path(s['cwd']).resolve()==Path.cwd().resolve()
                       for s in list_sessions('cc',index,native)):
                raise ValueError('CloudCLI 中的会话或项目身份已变更')
            if args.command=='cc-send':
                body=args.text if args.text_file is None else args.text_file.read_text(encoding='utf-8-sig')
                mid=store.cc_send(binding['id'],binding['cc'],native,body,args.id)
                print(json.dumps({'id':mid,'registration':'registered'}))
                return
            thread=binding['desktop']
        if args.command in ('receipt','cc-receipt'):
            key=_uuid(args.id,'receipt ID')
            history=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'thread_history_1.sqlite'
            found=next((r for r in store.receipts(history,key) if r['desktop']==_uuid(thread,'thread ID')
                        and (not from_cc or r['binding_id']==binding['id'])),None)
            if not found:
                raise ValueError('本线程没有这个信件 ID 或错误编号的记录')
            print(json.dumps({k:found[k] for k in ('id','binding_id','receipt','receipt_evidence','state','detail')}))
            return
        if args.command == "send":
            receipt = None
            turn=originating_turn(thread) if not args.wait else None
            if args.follow:
                with closing(sqlite3.connect(path)) as db:
                    enabled=db.execute('SELECT 1 FROM auto_inboxes WHERE binding_id=?',(args.binding,)).fetchone()
                if not turn or args.wait or args.mode not in (None,'none') or not enabled:
                    raise ValueError('--follow requires an active Desktop turn and an automatic one-way binding')
            if args.wait and args.mode == "none":
                raise ValueError("通知无需回信，请移除 --wait")
            if args.wait:
                scope = (_uuid(args.id,"mail ID"),args.binding,_uuid(thread,"thread ID"))
                with closing(sqlite3.connect(path,isolation_level=None)) as db:
                    lease = inbox.arm(db,scope,args.wait)
            mail_id = store.desktop_send(thread, args.text, args.id, expected_binding=args.binding,
                                         handoff_turn=turn, prefer_hook=not args.follow,
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
            try:
                with closing(sqlite3.connect(path)) as db, db:
                    hint=db.execute('UPDATE auto_inboxes SET hint_pending=0 WHERE binding_id=? AND hint_pending=1',
                                    (args.binding,)).rowcount
            except sqlite3.Error:
                hint=False  # Mail is already committed. This hint can wait until the next send.
            if args.follow:
                if __package__:
                    from .paired_inbox import follow
                else:
                    from paired_inbox import follow
                print(json.dumps({'id':mail_id,'turn':turn}),flush=True)
                follow(Path(path),Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'thread_history_1.sqlite',
                       thread,turn)
                return
            if hint and turn:
                print('Ordinary work can continue with the project hook. If the current task depends on a reply, '
                      'run paired_inbox.py follow --turn '+turn+' in the background and notify each received body. '
                      'For the next request that needs replies, use relay.py send --follow from the start.',
                      file=sys.stderr)
        elif args.command == "reply":
            mail_id = store.desktop_reply(thread, args.id, args.text)
        else:
            mail_id = store.desktop_confirm(thread, args.id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"id": mail_id}))


if __name__ == "__main__":
    main()
