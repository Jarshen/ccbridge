"""Local decision notes and explicit single-side session handoff."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

if __package__:
    from .catalog import list_sessions
else:
    from catalog import list_sessions


SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_links(binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),root INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_notes(
 rev INTEGER PRIMARY KEY, root INTEGER NOT NULL, topic TEXT NOT NULL, kind TEXT NOT NULL,
 audience TEXT NOT NULL, body TEXT NOT NULL, evidence TEXT NOT NULL, binding_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_files(rev INTEGER PRIMARY KEY REFERENCES continuity_notes(rev),path TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_sources(rev INTEGER PRIMARY KEY REFERENCES continuity_notes(rev),source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_cc_progress(
 rev INTEGER PRIMARY KEY REFERENCES continuity_notes(rev),
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','included','referenced','superseded')));
CREATE INDEX IF NOT EXISTS continuity_pending_progress ON continuity_cc_progress(rev) WHERE state='pending';
CREATE TABLE IF NOT EXISTS continuity_focus(
 rev INTEGER PRIMARY KEY REFERENCES continuity_notes(rev),adopted TEXT NOT NULL,closed TEXT NOT NULL,
 next_step TEXT NOT NULL,keep_revs TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_checks(ticket TEXT NOT NULL REFERENCES continuity_tickets(id),literal TEXT NOT NULL,
 PRIMARY KEY(ticket,literal));
CREATE TABLE IF NOT EXISTS continuity_stage(ticket TEXT PRIMARY KEY REFERENCES continuity_tickets(id), boot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_tickets(
 id TEXT PRIMARY KEY, binding_id INTEGER NOT NULL REFERENCES bindings(id), side TEXT NOT NULL,
 candidate TEXT NOT NULL, native TEXT NOT NULL, title TEXT NOT NULL, root INTEGER NOT NULL,
 revision INTEGER NOT NULL, mail_seq INTEGER NOT NULL, packet TEXT NOT NULL, state TEXT NOT NULL,
 readback TEXT, detail TEXT, boot TEXT, expires INTEGER, successor INTEGER REFERENCES bindings(id),
 prepared_at INTEGER NOT NULL DEFAULT (unixepoch()));
"""


def active(db, binding):
    row = db.execute("SELECT * FROM bindings WHERE id=? AND active=1", (binding,)).fetchone()
    if not row:
        raise ValueError("原配对已改变，请核对当前执行归属")
    root = db.execute("SELECT root FROM continuity_links WHERE binding_id=?", (binding,)).fetchone()
    return row, root[0] if root else binding


def notes(db, root, side, history=False, through=None):
    latest="" if history else """AND n.rev IN (
          SELECT max(v.rev) FROM continuity_notes v LEFT JOIN continuity_sources a ON a.rev=v.rev
          WHERE v.root=? GROUP BY v.topic,a.source,CASE WHEN a.source IS NOT NULL THEN v.kind END)"""
    return [dict(r) for r in db.execute("""
        SELECT n.rev,topic,kind,s.source,audience,body,evidence,f.path,f.digest FROM continuity_notes n
        LEFT JOIN continuity_files f ON f.rev=n.rev
        LEFT JOIN continuity_sources s ON s.rev=n.rev
        WHERE root=? AND n.kind<>'交接首页' """+latest+"""
        AND (audience='both' OR audience=?) AND (? IS NULL OR n.rev<=?) ORDER BY n.rev""",
        (*((root,side) if history else (root,root,side)),through,through))]


def overview(db, root, side):
    current=notes(db,root,side)
    focus=db.execute("""SELECT f.*,n.evidence FROM continuity_focus f JOIN continuity_notes n USING(rev)
                         WHERE n.root=? ORDER BY rev DESC LIMIT 1""",(root,)).fetchone()
    if not focus:
        return dict(notes=current)
    keep=set(json.loads(focus["keep_revs"]))
    return dict(overview={k:focus[k] for k in ("adopted","closed","next_step","evidence","rev")},
                notes=[n for n in current if n["rev"] in keep or n["rev"]>focus["rev"]])


def revision(db, root):
    return db.execute("SELECT coalesce(max(rev),0) FROM continuity_notes WHERE root=?", (root,)).fetchone()[0]


def prune_progress(db,root):
    pending=db.execute("""SELECT p.rev FROM continuity_cc_progress p JOIN continuity_notes n USING(rev)
                         WHERE n.root=? AND p.state='pending'""",(root,)).fetchall()
    if pending:
        visible={n["rev"] for n in overview(db,root,"cc")["notes"]}
        db.executemany("UPDATE continuity_cc_progress SET state='superseded' WHERE rev=?",
                       [(r[0],) for r in pending if r[0] not in visible])


def run(args):
    store = Path(args.store).resolve(strict=True)
    with closing(sqlite3.connect(store.as_uri()+"?mode=rw", uri=True, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        if args.command in ("status", "request", "cancel"):
            ticket = db.execute("SELECT * FROM continuity_tickets WHERE id=?", (args.ticket,)).fetchone()
            if not ticket:
                raise ValueError("未知交接编号")
            if args.command == "status":
                return dict(ticket)
            if args.command == "cancel":
                db.execute("UPDATE continuity_tickets SET state='cancelled' WHERE id=? AND successor IS NULL",
                           (args.ticket,))
            elif ticket["state"] not in ("requested", "committed"):
                active(db, ticket["binding_id"])
                if ticket["side"] == "cc" and not ticket["readback"]:
                    raise ValueError("候选 Claude 尚无原生核对答复")
                # The host publishes its own instance in its normal heartbeat.
                status = json.loads(Path(args.status_file or store.with_name("status.json")).read_text(encoding="utf8"))
                if (status["state"] != "ready" or Path(status["store"]).resolve() != store
                        or ticket["state"] not in ("prepared", "reviewed", "blocked")):
                    raise ValueError("桥或交接状态不允许接管")
                db.execute("""UPDATE continuity_tickets SET state='requested',boot=?,expires=unixepoch()+30,
                              detail=NULL WHERE id=?""", (status["instance"], args.ticket))
            return dict(db.execute("SELECT * FROM continuity_tickets WHERE id=?", (args.ticket,)).fetchone())
        if args.command=="show" and args.history:
            row=db.execute("""SELECT coalesce(l.root,b.id) FROM bindings b LEFT JOIN continuity_links l
                              ON l.binding_id=b.id WHERE b.id=?""",(args.binding,)).fetchone()
            if not row:
                raise ValueError("未知配对")
            root=row[0]
            limit=revision(db,root) if args.through is None else args.through
            focuses=db.execute("""SELECT f.rev,adopted,closed,next_step,evidence FROM continuity_focus f
                JOIN continuity_notes n USING(rev) WHERE root=? AND f.rev<=? ORDER BY f.rev""",(root,limit))
            return dict(binding=args.binding,through=limit,notes=notes(db,root,args.side,True,limit),
                        overviews=[dict(r) for r in focuses])
        route, root = active(db, args.binding)
        if args.command=="close-review":
            if not args.reason.strip():
                raise ValueError("请记录该评审如何处置")
            changed=db.execute("""UPDATE review_requests SET closed_reason=? WHERE request_id=?
                AND conclusion_id IS NULL AND closed_reason IS NULL
                AND EXISTS(SELECT 1 FROM mail WHERE id=request_id AND binding_id=?)""",
                (args.reason,args.request_id,args.binding)).rowcount
            if not changed:
                raise ValueError("该配对没有对应的待回传评审")
            return dict(request=args.request_id,closed_reason=args.reason)
        rev = revision(db, root)
        if args.command == "show":
            if args.progress:
                if args.side!="cc":
                    raise ValueError("延后带入的是 Claude 侧共享进度，请使用 --side cc")
                rows=db.execute("""SELECT n.rev,n.topic,n.kind,s.source,n.body,n.evidence,f.path,f.digest,p.state
                    FROM continuity_cc_progress p JOIN continuity_notes n USING(rev)
                    LEFT JOIN continuity_sources s USING(rev) LEFT JOIN continuity_files f USING(rev)
                    WHERE n.root=? AND n.audience IN ('cc','both') AND n.rev BETWEEN ? AND ? ORDER BY n.rev""",
                    (root,args.since,rev if args.through is None else args.through))
                return dict(binding=args.binding,revision=rev,progress=[dict(r) for r in rows])
            data=overview(db,root,args.side)
            return dict(binding=args.binding,revision=rev,**data)
        if args.command=="focus":
            if rev!=args.base:
                raise ValueError(f"方向已更新到 {rev}，请先核对")
            if not all(v.strip() for v in (args.adopted,args.closed,args.next_step,args.evidence)):
                raise ValueError("首页需要现行方案、已结案内容、下一步及裁决依据")
            eligible={n["rev"] for side in ("cc","desktop") for n in notes(db,root,side)}
            if not set(args.keep)<=eligible:
                raise ValueError("保留项须为当前配对仍有效的记录版本")
            entry=db.execute("""INSERT INTO continuity_notes(root,topic,kind,audience,body,evidence,binding_id)
                VALUES(?,'交接首页','交接首页','both',?,?,?)""",(root,args.adopted,args.evidence,args.binding))
            db.execute("INSERT INTO continuity_focus VALUES(?,?,?,?,?)",(entry.lastrowid,args.adopted,
                       args.closed,args.next_step,json.dumps(args.keep)))
            prune_progress(db,root)
            return dict(revision=entry.lastrowid,overview=overview(db,root,"cc")["overview"])
        if args.command == "note":
            if args.base != rev:
                raise ValueError(f"方向已更新到 {rev}，请先 show 再核对迟到结果")
            if not all(s.strip() for s in (args.topic, args.text, args.evidence)):
                raise ValueError("需要事项、实际变化与可回查的依据")
            if args.next_cc and args.audience=="desktop":
                raise ValueError("仅 Astra 可见的记录不能带入 Claude 回合")
            source=args.source
            if args.kind=="用户授权":
                source=source or "用户"
            if source and (args.kind.startswith("用户") and source!="用户"
                           or args.kind in ("方案建议","模型推测","体验观察") and source=="用户"):
                raise ValueError("决定类型与来源不一致，请按原话或证据核对")
            if not source and args.kind in ("方案建议","体验观察"):
                raise ValueError("请用 --source 指明判断来自 Opus 或 Astra")
            row = db.execute("""INSERT INTO continuity_notes(root,topic,kind,audience,body,evidence,binding_id)
                    VALUES(?,?,?,?,?,?,?)""", (root,args.topic,args.kind,args.audience,args.text,
                                                 args.evidence,args.binding))
            if source:
                db.execute("INSERT INTO continuity_sources VALUES(?,?)",(row.lastrowid,source))
            if args.file:
                path = args.file.resolve(strict=True)
                with path.open('rb') as src:
                    digest = hashlib.file_digest(src,'sha256').hexdigest()
                db.execute("INSERT INTO continuity_files VALUES(?,?,?)",(row.lastrowid,str(path),digest))
            if args.next_cc:
                db.execute("INSERT INTO continuity_cc_progress(rev) VALUES(?)",(row.lastrowid,))
            prune_progress(db,root)
            if args.next_cc:
                message="已记入共享状态，等待下次 Claude 业务回合"
                try:
                    pulse=json.loads(store.with_name("status.json").read_text(encoding="utf8"))
                    if (isinstance(pulse,dict) and pulse.get("state")=="ready"
                            and Path(pulse.get("store") or "").resolve()==store and not pulse.get("ccProgress")):
                        message+="；此库最近的就绪记录来自旧版宿主，正常重启新版工作台后生效"
                except (OSError,TypeError,ValueError):
                    pass  # The durable status remains pending even when the host is offline.
                return dict(revision=row.lastrowid,progress="pending",message=message)
            return dict(revision=row.lastrowid)
        candidate = next((r for r in list_sessions(args.side, args.index) if r["id"] == args.candidate), None)
        if not candidate or Path(candidate["cwd"]) != Path(route[args.side+"_cwd"]):
            raise ValueError("候选不在原端项目或原生索引无法确认")
        if db.execute("""SELECT 1 FROM bindings WHERE cc=? OR native_cc=? OR desktop=? OR desktop_host=?""",
                      (candidate["id"],candidate["native_id"],candidate["native_id"],candidate["id"])).fetchone():
            raise ValueError("候选曾有配对，请使用新会话")
        current = overview(db, root, args.side)
        if not (current.get("overview") or current["notes"]) or not args.work.strip():
            raise ValueError("请先记录有效方向，并交代未完操作及其执行归属")
        status = json.loads(Path(args.status_file or store.with_name("status.json")).read_text(encoding="utf8"))
        if status["state"]!="ready" or Path(status["store"]).resolve()!=store or not status.get("instance"):
            raise ValueError("请在目标桥就绪后准备交接")
        key = str(uuid4())
        packet = dict(ticket=key, role=args.side, project=route[args.side+"_cwd"], revision=rev,
                      previous_binding=args.binding, partner=route["desktop" if args.side=="cc" else "cc"],
                      **current, work=args.work,
                      history=("python -X utf8 '"+str(Path(__file__).resolve()).replace("'","''")
                               +"' --store '"+str(store).replace("'","''")+"' show --binding "
                               +f"{args.binding} --side {args.side} --history --through {rev}"),
                      instruction="请回查关键原始样品或实际文件，说明交付、约束、局部认可、未决点、反证和下一步。接管成功后才执行移交任务。最后单列：接续核对 "+key)
        seq = db.execute("SELECT coalesce(max(seq),0) FROM mail WHERE binding_id=?", (args.binding,)).fetchone()[0]
        db.execute("""INSERT INTO continuity_tickets(id,binding_id,side,candidate,native,title,root,revision,
                      mail_seq,packet,state) VALUES(?,?,?,?,?,?,?,?,?,?,'prepared')""",
                   (key,args.binding,args.side,candidate["id"],candidate["native_id"],candidate["title"],
                    root,rev,seq,json.dumps(packet,ensure_ascii=False)))
        for literal in set(args.expect):
            if not literal.strip():
                raise ValueError("核对值不能为空")
            db.execute("INSERT INTO continuity_checks VALUES(?,?)",(key,literal))
        db.execute("INSERT INTO continuity_stage VALUES(?,?)",(key,status["instance"]))
        return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=os.environ.get("DAILY_BRIDGE_STORE") or Path(__file__).parent/"data/bridge.sqlite3")
    commands = parser.add_subparsers(dest="command",required=True)
    for name in ("show","note","prepare","focus"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--binding",type=int,required=True)
        if name == "focus":
            cmd.add_argument("--base",type=int,required=True)
            for field in ("adopted","closed","next-step","evidence"):
                cmd.add_argument("--"+field,required=True)
            cmd.add_argument("--keep",type=int,action="append",default=[],help="继续生效的约束或证据的版本号，可重复")
        elif name == "note":
            cmd.add_argument("--base",type=int,required=True)
            cmd.add_argument("--topic",required=True)
            cmd.add_argument("--kind",choices=["用户原话","用户授权","用户局部认可","用户整体验收","方案建议","体验观察","模型推测","实测事实","未决"],required=True)
            cmd.add_argument("--source",choices=["用户","Opus","Astra"],help="原始决定、建议或测量的来源，转述位置写入 evidence")
            cmd.add_argument("--audience",choices=["both","cc","desktop"],default="both")
            cmd.add_argument("--text",required=True)
            cmd.add_argument("--evidence",required=True)
            cmd.add_argument("--file",type=Path,help="固定本地样品版本；换新时复核 SHA-256")
            cmd.add_argument("--next-cc",action="store_true",help="只记状态，在下次 Claude 业务回合带入")
        else:
            cmd.add_argument("--side",choices=["cc","desktop"],required=True)
            if name=="show":
                view=cmd.add_mutually_exclusive_group()
                view.add_argument("--history",action="store_true",help="展开角色可见的历史记录及原始依据")
                view.add_argument("--progress",action="store_true",help="查看延后带入的进度及状态")
                cmd.add_argument("--since",type=int,default=0,help="进度查询的起始版本（含）")
                cmd.add_argument("--through",type=int,help="展开历史截至此版本")
        if name == "prepare":
            cmd.add_argument("--index",type=Path,required=True)
            cmd.add_argument("--status-file",type=Path)
            cmd.add_argument("--candidate",required=True)
            cmd.add_argument("--work",required=True)
            cmd.add_argument("--expect",action="append",default=[],help="可选的客观样品标识，要求候选答复含此值；不放入交接包")
    close=commands.add_parser("close-review",help="核对后关闭尚无结论的评审，保留原信及处置理由")
    close.add_argument("--binding",required=True,type=int)
    close.add_argument("--request-id",required=True)
    close.add_argument("--reason",required=True)
    for name in ("request","status","cancel"):
        cmd = commands.add_parser(name)
        cmd.add_argument("ticket")
        if name == "request":
            cmd.add_argument("--status-file",type=Path)
    try:
        print(json.dumps(run(parser.parse_args()),ensure_ascii=False,indent=2))
    except (ValueError,OSError,KeyError,sqlite3.Error) as error:
        parser.exit(1,str(error)+"\n")


if __name__=="__main__":
    main()
