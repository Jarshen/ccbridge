from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from .storage import Store
from .zeroqueue import active, call, identity, install


class StrictInboxTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.store, self.history = base/'bridge', base/'history'
        self.thread,self.turn,self.mail = (str(uuid4()) for _ in range(3))
        cc = str(uuid4())
        self.binding = Store(self.store).bind(
            {'id':cc,'native_id':cc,'title':'CC','cwd':str(base)},
            {'id':self.thread,'native_id':self.thread,'title':'Desktop','cwd':str(base)})
        install(self.store,self.binding)
        self.meta = {'threadId':self.thread,'x-codex-turn-metadata':{'turn_id':self.turn}}
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute('CREATE TABLE thread_turns(thread_id,turn_id,status,rollout_ordinal)')
            db.execute("INSERT INTO thread_turns VALUES(?,?,'inProgress',0)",(self.thread,self.turn))
        with closing(sqlite3.connect(self.store)) as db, db:
            db.execute('UPDATE strict_route SET turn=?',(self.turn,))
            db.execute("INSERT INTO mail(id,binding_id,source,body,reply_mode) VALUES(?,?,'cc','feed1234','none')",
                       (self.mail,self.binding))

    def take(self, meta=None):
        return call(self.store,self.history,'take',{'id':self.mail},meta or self.meta)

    def test_concurrent_claim_and_lost_ack(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.take()['status'],range(4)))
        self.assertEqual(results.count('taken'),1)
        self.assertEqual(self.take(),{'status':'unavailable'})
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM strict_submissions').fetchone(),(0,))

    def test_wrong_thread_ended_turn_and_new_turn(self):
        self.assertEqual(self.take({**self.meta,'threadId':str(uuid4())}),{'status':'unavailable'})
        new = str(uuid4())
        with closing(sqlite3.connect(self.history)) as db,db:
            db.execute("UPDATE thread_turns SET status='completed'")
            db.execute("INSERT INTO thread_turns VALUES(?,?,'inProgress',1)",(self.thread,new))
            self.assertFalse(active(db,self.thread,self.turn))
        self.assertEqual(self.take(),{'status':'unavailable'})
        self.assertEqual(self.take({'threadId':self.thread,'x-codex-turn-metadata':{'turn_id':new}}),
                         {'status':'unavailable'})
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(db.execute('SELECT state,taken_at FROM mail JOIN strict_letter USING(id)').fetchone(),
                             ('unknown',None))

    def test_conflicting_runtime_metadata(self):
        with self.assertRaises(ValueError):
            identity({**self.meta,'turnId':str(uuid4())})

    def test_submission_counter_positive_control(self):
        with closing(sqlite3.connect(self.store)) as db, db:
            # Exercise the audit trigger only, against a disposable DB.
            db.execute("UPDATE mail SET state='submitting' WHERE id=?",(self.mail,))
            self.assertEqual(db.execute('SELECT id,state FROM strict_submissions').fetchall(),
                             [(self.mail,'submitting')])
