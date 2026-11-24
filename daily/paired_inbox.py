"""Receive one-way CC letters on automatic or strict daily bindings."""
import argparse
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid4

if __package__:
    from .zeroqueue import active, connection, identity, serve
else:
    from zeroqueue import active, connection, identity, serve


def receive(store, history_path, name, args, meta, cancelled=None):
    thread, turn = identity(meta)
    deadline = time.monotonic() + (45 if name == 'watch' else 0)
    watch_id=str(uuid4())
    cleanup_registered=False
    with closing(connection(store,'rw')) as db, closing(connection(history_path,'ro')) as history, ExitStack() as cleanup:
        while True:
            with db:
                db.execute('BEGIN IMMEDIATE')
                route=db.execute('''SELECT b.id,a.binding_id FROM bindings b
                    LEFT JOIN auto_inboxes a ON a.binding_id=b.id LEFT JOIN paired_inboxes p ON p.binding_id=b.id
                    WHERE b.active=1 AND b.desktop=? AND (a.binding_id IS NOT NULL OR p.binding_id IS NOT NULL)''',
                                 (thread,)).fetchone()
                if not route:
                    return {'status':'unavailable'}
                binding,auto=route
                if (cancelled and cancelled.is_set()) or not active(history,thread,turn):
                    if auto is not None and name=='watch':
                        db.execute('DELETE FROM turn_handoffs WHERE binding_id=? AND turn=?',(binding,turn))
                    return {'status':'unavailable'}
                if name in ('arm','watch'):
                    if auto is None:
                        db.execute('UPDATE paired_inboxes SET turn=?,listen_until=unixepoch()+60 WHERE binding_id=?',
                                   (turn,binding))
                    if name=='arm':
                        return {'status':'armed' if auto is None else 'available','thread':thread,'turn':turn}
                    if auto is not None:
                        # Accept the burst as a batch. No body leaves the database
                        # until take wins the conditional state check for its ID.
                        now=int(time.time()*1000)
                        offers=db.execute('''SELECT o.mail_id FROM burst_offers o JOIN mail m ON m.id=o.mail_id
                            WHERE o.binding_id=? AND o.turn=? AND o.expires_ms>? AND m.state='queued'
                            ORDER BY m.seq''',(binding,turn,now)).fetchall()
                        if offers:
                            db.executemany('INSERT INTO paired_letters(mail_id,target_turn) VALUES(?,?)',
                                           ((mid,turn) for mid, in offers))
                            db.executemany("UPDATE mail SET state='unknown',detail='配对收件箱待领取' WHERE id=?",offers)
                    row=db.execute('''SELECT m.id FROM paired_letters l JOIN mail m ON m.id=l.mail_id
                        WHERE m.binding_id=? AND m.state='unknown' AND l.taken_at IS NULL
                        AND (l.target_turn=? OR l.target_turn IS NULL) ORDER BY m.seq LIMIT 1''',
                                   (binding,turn)).fetchone()
                    if row:
                        db.execute('UPDATE paired_letters SET target_turn=? WHERE mail_id=? AND target_turn IS NULL',
                                   (turn,row[0]))
                        return {'status':'ready','id':row[0],'thread':thread,'turn':turn}
                    if auto is not None:
                        now=int(time.time()*1000)
                        remaining=deadline-time.monotonic()
                        if remaining<=0:
                            return {'status':'timeout'}
                        offer=db.execute('''SELECT o.mail_id FROM auto_offers o JOIN auto_inboxes a
                            ON a.binding_id=o.binding_id AND a.watch_id=o.watch_id
                            JOIN mail m ON m.id=o.mail_id WHERE o.binding_id=? AND o.turn=? AND o.watch_id=?
                            AND m.state='queued' AND a.expires_ms>?''',(binding,turn,watch_id,now)).fetchone()
                        if offer:
                            db.execute('INSERT INTO paired_letters(mail_id,target_turn) VALUES(?,?)',(offer[0],turn))
                            db.execute("UPDATE mail SET state='unknown',detail='配对收件箱待领取' WHERE id=?",(offer[0],))
                            return {'status':'ready','id':offer[0],'thread':thread,'turn':turn}
                        if cleanup_registered and not db.execute('''SELECT 1 FROM auto_inboxes
                            WHERE binding_id=? AND watch_id=? AND expires_ms>?''',(binding,watch_id,now)).fetchone():
                            return {'status':'unavailable'}
                        expires=now+min(2000,int(remaining*1000))
                        changed=db.execute('''UPDATE auto_inboxes SET turn=?,watch_id=?,expires_ms=?
                            WHERE binding_id=? AND (watch_id IS NULL OR watch_id=? OR turn<>? OR expires_ms<=?)''',
                            (turn,watch_id,expires,binding,watch_id,turn,now)).rowcount
                        if not changed:
                            return {'status':'already_listening','turn':turn}
                        if not cleanup_registered:
                            def release():
                                with db:
                                    db.execute('UPDATE auto_inboxes SET watch_id=NULL,expires_ms=0 WHERE binding_id=? AND watch_id=?',
                                               (binding,watch_id))
                            cleanup.callback(release)
                            cleanup_registered=True
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
            if cancelled:
                cancelled.wait(.4)
            else:
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
    enable.add_argument('--auto',action='store_true',help='Use native queue when no live watch is available')
    for name in ('arm','watch','take','follow'):
        sub=commands.add_parser(name)
        sub.add_argument('--turn',type=UUID,required=True)
        if name=='take':
            sub.add_argument('--id',type=UUID,required=True)
    args=parser.parse_args()
    if args.command=='serve':
        serve(args.store,args.history,receive,cancellable=True)
        return
    if args.command=='enable':
        with closing(connection(args.store,'rw')) as db,db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM bindings WHERE active=1 AND id=? AND desktop=?',
                              (args.binding,str(args.thread))).fetchone():
                raise ValueError('active binding does not match')
            if args.auto:
                db.execute('INSERT OR IGNORE INTO auto_inboxes(binding_id,hint_pending) VALUES(?,1)',(args.binding,))
                db.execute('DELETE FROM paired_inboxes WHERE binding_id=?',(args.binding,))
            else:
                db.execute('INSERT OR IGNORE INTO paired_inboxes(binding_id) VALUES(?)',(args.binding,))
                db.execute('DELETE FROM auto_inboxes WHERE binding_id=?',(args.binding,))
        print(json.dumps({'status':'enabled','binding':args.binding,'mode':'auto' if args.auto else 'strict'}))
        return
    thread=str(UUID(os.environ['CODEX_THREAD_ID']))
    if args.command=='current':
        with closing(connection(args.history,'ro')) as history:
            row=history.execute('''SELECT turn_id,status FROM thread_turns WHERE thread_id=?
                ORDER BY rollout_ordinal DESC LIMIT 1''',(thread,)).fetchone()
        print(json.dumps({'thread':thread,'turn':row[0] if row and row[1]=='inProgress' else None}))
        return
    if args.command=='follow':
        follow(args.store,args.history,thread,str(args.turn))
        return
    result=receive(args.store,args.history,args.command,{'id':str(getattr(args,'id',''))},
                   {'threadId':thread,'x-codex-turn-metadata':{'turn_id':str(args.turn)}})
    print(json.dumps(result))


def follow(store,history,thread,turn):
    """One live tool invocation consumes notifications until its native turn ends."""
    meta={'threadId':thread,'x-codex-turn-metadata':{'turn_id':turn}}
    while True:
        notice=receive(store,history,'watch',{},meta)
        if notice['status']=='timeout':
            continue
        if notice['status']=='already_listening':
            time.sleep(.4)
            continue
        if notice['status']!='ready':
            print(json.dumps(notice),flush=True)
            return
        letter=receive(store,history,'take',{'id':notice['id']},meta)
        if letter['status']=='taken':
            print(json.dumps(letter),flush=True)


if __name__=='__main__':
    main()
