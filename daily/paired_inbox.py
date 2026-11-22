"""Receive one-way CC letters on explicitly enabled daily bindings."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import time
from uuid import UUID

if __package__:
    from .zeroqueue import active, connection, identity, serve
else:
    from zeroqueue import active, connection, identity, serve


def receive(store, history_path, name, args, meta):
    thread, turn = identity(meta)
    deadline = time.monotonic() + (45 if name == 'watch' else 0)
    with closing(connection(store,'rw')) as db, closing(connection(history_path,'ro')) as history:
        while True:
            with db:
                db.execute('BEGIN IMMEDIATE')
                route=db.execute('''SELECT b.id FROM bindings b JOIN paired_inboxes p ON p.binding_id=b.id
                    WHERE b.active=1 AND b.desktop=?''',(thread,)).fetchone()
                if not route or not active(history,thread,turn):
                    return {'status':'unavailable'}
                binding=route[0]
                if name in ('arm','watch'):
                    db.execute('UPDATE paired_inboxes SET turn=?,listen_until=unixepoch()+60 WHERE binding_id=?',
                               (turn,binding))
                    if name=='arm':
                        return {'status':'armed','thread':thread,'turn':turn}
                    row=db.execute('''SELECT m.id FROM paired_letters l JOIN mail m ON m.id=l.mail_id
                        WHERE m.binding_id=? AND m.state='unknown' AND l.taken_at IS NULL
                        AND (l.target_turn=? OR l.target_turn IS NULL) ORDER BY m.seq LIMIT 1''',
                                   (binding,turn)).fetchone()
                    if row:
                        db.execute('UPDATE paired_letters SET target_turn=? WHERE mail_id=? AND target_turn IS NULL',
                                   (turn,row[0]))
                        return {'status':'ready','id':row[0],'thread':thread,'turn':turn}
                elif name=='take':
                    mail=str(UUID(args['id']))
                    row=db.execute('''SELECT m.body FROM paired_letters l JOIN mail m ON m.id=l.mail_id
                        WHERE l.mail_id=? AND m.binding_id=? AND l.target_turn=? AND m.state='unknown'
                        AND l.taken_at IS NULL''',(mail,binding,turn)).fetchone()
                    if not row or not active(history,thread,turn):
                        return {'status':'unavailable'}
                    db.execute('UPDATE paired_letters SET taken_at=? WHERE mail_id=?',(time.time(),mail))
                    db.execute("UPDATE mail SET state='notified',detail=? WHERE id=?",
                               ('同回合收件；turn ID '+turn,mail))
                    return {'status':'taken','id':mail,'thread':thread,'turn':turn,'text':row[0]}
                else:
                    raise ValueError('unknown operation')
            if time.monotonic()>=deadline:
                return {'status':'timeout'}
            time.sleep(.4)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,default=Path(__file__).parent/'data/bridge.sqlite3')
    parser.add_argument('--history',type=Path,default=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'thread_history_1.sqlite')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('serve')
    commands.add_parser('current')
    enable=commands.add_parser('enable')
    enable.add_argument('--binding',type=int,required=True)
    enable.add_argument('--thread',type=UUID,required=True)
    for name in ('arm','watch','take'):
        sub=commands.add_parser(name)
        sub.add_argument('--turn',type=UUID,required=True)
        if name=='take':
            sub.add_argument('--id',type=UUID,required=True)
    args=parser.parse_args()
    if args.command=='serve':
        serve(args.store,args.history,receive)
        return
    if args.command=='enable':
        with closing(connection(args.store,'rw')) as db,db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM bindings WHERE active=1 AND id=? AND desktop=?',
                              (args.binding,str(args.thread))).fetchone():
                raise ValueError('active binding does not match')
            db.execute('INSERT OR IGNORE INTO paired_inboxes(binding_id) VALUES(?)',(args.binding,))
        print(json.dumps({'status':'enabled','binding':args.binding}))
        return
    thread=str(UUID(os.environ['CODEX_THREAD_ID']))
    if args.command=='current':
        with closing(connection(args.history,'ro')) as history:
            row=history.execute('''SELECT turn_id,status FROM thread_turns WHERE thread_id=?
                ORDER BY rollout_ordinal DESC LIMIT 1''',(thread,)).fetchone()
        print(json.dumps({'thread':thread,'turn':row[0] if row and row[1]=='inProgress' else None}))
        return
    result=receive(args.store,args.history,args.command,{'id':str(getattr(args,'id',''))},
                   {'threadId':thread,'x-codex-turn-metadata':{'turn_id':str(args.turn)}})
    print(json.dumps(result))


if __name__=='__main__':
    main()
