"""One-turn stdio MCP inbox for a dedicated workbench test pair."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import UUID


# The strict letter is held in the same transaction that creates it. The daily
# worker (including older versions) never submits mail in the unknown state.
SCHEMA = """
CREATE TABLE IF NOT EXISTS strict_route(binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
    turn TEXT, armed_at REAL);
CREATE TABLE IF NOT EXISTS strict_letter(id TEXT PRIMARY KEY REFERENCES mail(id), binding_id INTEGER NOT NULL,
    turn TEXT, posted_at REAL NOT NULL, taken_at REAL);
CREATE TABLE IF NOT EXISTS strict_submissions(id TEXT NOT NULL, state TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS strict_submit AFTER UPDATE OF state ON mail WHEN NEW.state IN ('submitting','returning')
    AND EXISTS(SELECT 1 FROM strict_letter WHERE id=NEW.id)
BEGIN
    INSERT INTO strict_submissions VALUES(NEW.id,NEW.state);
END;
CREATE TRIGGER IF NOT EXISTS strict_hold AFTER INSERT ON mail WHEN NEW.source='cc' AND NEW.reply_mode='none'
    AND EXISTS(SELECT 1 FROM strict_route WHERE binding_id=NEW.binding_id)
BEGIN
    INSERT INTO strict_letter(id,binding_id,turn,posted_at)
        SELECT NEW.id,binding_id,turn,(julianday('now')-2440587.5)*86400 FROM strict_route
        WHERE binding_id=NEW.binding_id;
    UPDATE mail SET state='unknown',detail='零入队测试专用：原回合待领取' WHERE id=NEW.id;
END;
"""


def connection(path, mode):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=' + mode, uri=True, timeout=2)


def install(store, binding):
    with closing(connection(store, 'rw')) as db, db:
        db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
        if not db.execute('SELECT 1 FROM bindings WHERE id=? AND active=1', (binding,)).fetchone():
            raise ValueError('missing active binding')
        db.execute('INSERT OR IGNORE INTO strict_route(binding_id) VALUES(?)', (binding,))


def identity(meta):
    nested = meta.get('x-codex-turn-metadata', {})
    if isinstance(nested, str):
        nested = json.loads(nested)
    thread = str(UUID(meta['threadId']))
    turn = str(UUID(nested['turn_id']))
    if (nested.get('thread_id', thread) != thread or meta.get('turnId', turn) != turn):
        raise ValueError('conflicting runtime identity')
    return thread, turn


def active(history, thread, turn):
    row = history.execute('''SELECT turn_id,status FROM thread_turns WHERE thread_id=?
        ORDER BY rollout_ordinal DESC LIMIT 1''', (thread,)).fetchone()
    return row == (turn, 'inProgress')


def call(store, history_file, name, args, meta):
    thread, turn = identity(meta)
    with closing(connection(store, 'rw')) as db, closing(connection(history_file, 'ro')) as history:
        with db:
            db.execute('BEGIN IMMEDIATE')
            route = db.execute('''SELECT r.binding_id,r.turn FROM strict_route r JOIN bindings b
                ON b.id=r.binding_id WHERE b.active=1 AND b.desktop=?''', (thread,)).fetchone()
            if not route or not active(history, thread, turn) or route[1] not in (None, turn):
                return {'status': 'unavailable'}
            binding = route[0]
            if name == 'watch':
                db.execute('UPDATE strict_route SET turn=?,armed_at=? WHERE binding_id=? AND turn IS NULL',
                           (turn, time.time(), binding))
            elif name == 'take':
                mail = str(UUID(args['id']))
                row = db.execute('''SELECT m.body FROM strict_letter l JOIN mail m ON m.id=l.id
                    WHERE l.id=? AND l.binding_id=? AND l.turn=? AND m.state='unknown'
                    AND l.taken_at IS NULL''', (mail, binding, turn)).fetchone()
                if row is None or not active(history, thread, turn):
                    return {'status': 'unavailable'}
                db.execute('UPDATE strict_letter SET taken_at=? WHERE id=?', (time.time(), mail))
                db.execute("UPDATE mail SET state='notified', detail=? WHERE id=?",
                           ('MCP 领取；turn ID '+turn, mail))
                return {'status': 'taken', 'id': mail, 'turn': turn, 'text': row[0]}
            else:
                raise ValueError('unknown tool')
        deadline = time.monotonic()+45
        while active(history, thread, turn):
            row = db.execute('''SELECT l.id FROM strict_letter l JOIN bindings b ON b.id=l.binding_id
                JOIN mail m ON m.id=l.id WHERE b.id=? AND b.active=1 AND l.turn=?
                AND l.taken_at IS NULL AND m.state='unknown' ORDER BY m.seq LIMIT 1''',
                             (binding, turn)).fetchone()
            if row:
                return {'status': 'ready', 'id': row[0], 'turn': turn}
            if time.monotonic() >= deadline:
                return {'status': 'timeout'}
            time.sleep(.15)
        return {'status': 'ended'}


def serve(store, history, handler=call, *, cancellable=False):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    lock=Lock()
    pending={}

    def emit(frame):
        with lock:
            print(json.dumps(frame),flush=True)

    def reply(req, stop=None):
        try:
            if not isinstance(req, dict):
                req = {}
                raise ValueError('expected an MCP request object')
            if 'id' not in req:
                return
            method = req['method']
            params = req.get('params', {})
            if method == 'initialize':
                versions = ('2024-11-05','2025-03-26','2025-06-18','2025-11-25')
                version = params.get('protocolVersion')
                result = {'protocolVersion': version if version in versions else versions[-1],
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'pairinbox','version': '1'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [
                    {'name': name, 'description': desc, 'inputSchema': {'type':'object','properties': props,
                     'required': list(props), 'additionalProperties': False}}
                    for name, desc, props in [('watch','Wait for one paired letter for this turn.',{}),
                        ('take','Claim one noticed paired letter once.',{'id':{'type':'string'}})]]}
            elif method == 'tools/call':
                args = (store, history, params['name'], params.get('arguments',{}), params['_meta'])
                value = handler(*args, cancelled=stop) if stop is not None else handler(*args)
                result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
            else:
                emit({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32601,'message':'Unknown method'}})
                return
            frame = {'result':result}
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            frame = {'error':{'code':-32603,'message':str(exc)}}
        emit({'jsonrpc':'2.0','id':req.get('id'), **frame})

    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            for raw in sys.stdin.buffer:
                try:
                    req=json.loads(raw)
                except ValueError:
                    emit({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON'}})
                    continue
                if not isinstance(req,dict):
                    emit({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}})
                    continue
                if cancellable and req.get('method')=='notifications/cancelled':
                    item=pending.get(req.get('params',{}).get('requestId'))
                    if item:
                        item[0].set()
                elif cancellable and req.get('method')=='tools/call' and 'id' in req:
                    pending={i:v for i,v in pending.items() if not v[1].done()}
                    stop=Event()
                    pending[req['id']]=(stop,pool.submit(reply,req,stop))
                else:
                    reply(req)
        finally:
            for stop,_ in pending.values():
                stop.set()



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, type=Path)
    parser.add_argument('--history', required=True, type=Path)
    options = parser.parse_args()
    serve(options.store, options.history)
