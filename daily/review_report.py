"""Store-independent formatting and read-only access to a shared review."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys


def excerpt(text):
    """Select verbatim passages. Prefer the reviewer's labelled conclusions."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines)==1:
        lines=re.split(r'(?<=[。！？])|(?<=[.!?])\s+',lines[0])
        lines=[s for s in lines if s]
    selected = []
    for pattern in (r'推荐|不推荐|建议|结论|recommendation|conclusion',
                    r'主要依据|依据|理由|原因|evidence|reason',
                    r'保留意见|限制|尚未|未验证|不足|风险|不确定|caveat|limitation|unverified'):
        heading=re.compile(r'^(?:'+pattern+r')\s*(?:[：:]|$)',re.I)
        hits=[i for i,s in enumerate(lines) if heading.match(s.strip('#* '))]
        if not hits:
            hits=[i for i,s in enumerate(lines) if re.search(pattern,s,re.I)]
        if not hits:
            continue
        i=hits[0]
        line=lines[i]
        if re.fullmatch(pattern,line.strip('#* ：:'),re.I):
            for j in range(i+1,len(lines)):
                detail=lines[j]
                if detail.startswith('#') or not detail.strip('-* ') or detail.rstrip('* ').endswith(('：',':')):
                    continue
                if j>i+1 and lines[j-1].rstrip('* ').endswith(('：',':')):
                    line+='\n'+lines[j-1]
                line+='\n'+detail
                break
        if line not in selected:
            selected.append(line)
    if not selected:
        selected = [lines[0]]
    if lines[-1] not in selected and len(selected) < 3:
        selected.append(lines[-1])
    return '原文节选\n' + '\n'.join(s[:320]+('…' if len(s)>320 else '') for s in selected)


def letter(full, request, store, binding, materials=(), summary=None):
    if not isinstance(full,str) or not full.strip():
        raise ValueError('评审正文为空')
    header = f'评审结论〔{request}〕\n'
    observed = ''
    if materials:
        labels = [f'{kind} · {Path(name).name[:85]}' for kind,name in materials[:4]]
        if len(materials)>4:
            labels.append(f'另有 {len(materials)-4} 项')
        observed = '\n工作台取证：' + '；'.join(labels)
    if not summary and len(full.strip())<=1200 and len(header+full.strip()+observed)<=2500:
        return header+full.strip()+observed
    # Use a directly runnable local lookup, including when the caller uses an isolated store.
    def quote(value):
        return "'"+str(value).replace("'","''")+"'"
    lookup = (f"python -X utf8 {quote(Path(__file__).resolve())} --store {quote(Path(store).resolve())}"
              f" --binding {binding} --id {quote(request)}")
    brief = summary.strip() if summary and summary.strip() else excerpt(full)
    if len(brief)>1200:
        brief=excerpt(brief)
    wire=header+brief+observed+'\n全文与材料：'+lookup
    if len(wire)>2500:
        wire=header+brief+observed+'\n全文与材料：review_report.py，使用原库、配对号及原信编号 '+request
    return wire


def read_report(store, binding, mail_id):
    with closing(sqlite3.connect(Path(store).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute("""SELECT r.request_id,r.conclusion_id,m.body FROM review_requests r
            JOIN mail q ON q.id=r.request_id LEFT JOIN mail m ON m.id=r.conclusion_id
            WHERE q.binding_id=? AND (r.request_id=? OR r.conclusion_id=?)""",(binding,mail_id,mail_id)).fetchone()
        if not row:
            raise ValueError('指定配对无此评审')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        doc=db.execute('SELECT full_text FROM review_reports WHERE request_id=?',(row[0],)).fetchone() if 'review_reports' in tables else None
        materials=db.execute('SELECT kind,name FROM review_materials WHERE request_id=? ORDER BY rowid',(row[0],)).fetchall() if 'review_materials' in tables else []
        return dict(request=row[0],conclusion=row[1],full_text=doc[0] if doc else None,
                    returned_letter=row[2],materials=materials)


if __name__=='__main__':
    if sys.argv[1:]==['--format']:
        print(json.dumps(letter(**json.load(sys.stdin)),ensure_ascii=False))
    else:
        parser=argparse.ArgumentParser(description='用原委托或结论编号查看本地共享评审')
        parser.add_argument('--store',required=True)
        parser.add_argument('--binding',type=int,required=True)
        parser.add_argument('--id',required=True)
        args=parser.parse_args()
        try:
            print(json.dumps(read_report(args.store,args.binding,args.id),ensure_ascii=False,indent=2))
        except (sqlite3.Error,ValueError,OSError) as exc:
            parser.exit(1,str(exc)+'\n')
