"""Project transport evidence into sender-facing receipts without advancing the mail state."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def matches(item_json, mail):
    try:
        item = json.loads(item_json)
        parts = item['content']
        if item['type'] != 'userMessage' or len(parts) != 1 or parts[0]['type'] != 'text':
            return False
        text = parts[0]['text']
        if mail['reply_mode'] == 'none':
            expected = f"Claude〔{mail['id']}〕\n{mail['body']}"
            # Desktop may append a line ending when it imports a queued user message.
            if text in (expected, expected+'\n', expected+'\r\n'):
                return True
        header, _, rest = text.partition('\n')
        if header != 'Claude 来信' and not header.startswith('CloudCLI 配对来信。'):
            return False
        packet = json.loads(rest.split('\n', 1)[0])
        return packet == dict(kind='message', id=mail['id'], reply_to=None, reply_mode=mail['reply_mode'],
                              text=mail['body'], **{'from':mail['cc'], 'to':mail['desktop']})
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def enrich(store, history_path, rows, only_id=None):
    with closing(store._connect()) as db:
        saved = dict(db.execute('SELECT mail_id,turn_id FROM delivery_evidence'))
        has_inbox = db.execute("SELECT 1 FROM sqlite_master WHERE name='paired_letters'").fetchone()
        claims = dict(db.execute("""SELECT l.mail_id,l.target_turn FROM paired_letters l
            JOIN mail m ON m.id=l.mail_id WHERE l.taken_at IS NOT NULL
            AND coalesce(m.detail,'') NOT LIKE '监听输出待核对；%'""")) if has_inbox else {}
        missing = [m for m in rows if m['source']=='cc' and m['id'] not in saved and m['id'] not in claims
                   and m['state'] not in ('queued','failed','cancelled') and m['detail']!='配对收件箱待领取']
        discovered = []
        if missing and Path(history_path).is_file():
            try:
                with closing(sqlite3.connect(Path(history_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)) as native:
                    for m in missing:
                        candidates = native.execute("""SELECT turn_id,item_json FROM thread_items
                            WHERE thread_id=? AND item_type='userMessage' AND instr(item_json,?)>0""",
                            (m['desktop'],m['id']))
                        turn = next((turn for turn,item in candidates if matches(item,m)), None)
                        if turn:
                            saved[m['id']] = turn
                            discovered.append((m['id'],turn))
            except sqlite3.OperationalError:
                pass  # An unavailable history provides no new evidence.
        if discovered:
            with db:
                db.executemany('INSERT OR IGNORE INTO delivery_evidence(mail_id,turn_id) VALUES(?,?)',discovered)
        for m in rows:
            detail, state = m['detail'] or '', m['state']
            stage, evidence = 'registered','桥已登记信件'
            if m['id'] in saved:
                stage,evidence='delivered','Desktop 原生用户消息已核对信件 ID 和完整正文 · 回合 '+saved[m['id']]
            elif m['id'] in claims:
                stage,evidence='delivered','同回合首次领取 · '+claims[m['id']]
            elif (m['source']=='desktop' and (detail.startswith('CC 单向消息回合已完成') or
                  state=='awaiting_reply' or m['reply'] is not None)):
                stage,evidence='delivered','Claude 回合完成或已返回答复'
            elif m['source']=='cc' and (m['reply'] is not None or detail.startswith(('Desktop 工具结束后取件','MCP 领取；'))):
                stage,evidence='delivered','目标已领取或已对原信答复'
            elif m['source']=='cc' and detail.startswith('Desktop 原生队列已接收'):
                stage,evidence='native_queued','Desktop 原生队列已有提交回执'
            elif state in ('failed','cancelled') or (state=='unknown' and detail!='配对收件箱待领取'):
                stage,evidence=state,detail
            elif state in ('submitting','returning'):
                evidence='正在投递，等待回执'
            elif detail=='配对收件箱待领取':
                evidence='已进入同回合收件箱，等候领取'
            m.update(receipt=stage,receipt_evidence=evidence)
        feedback = db.execute("""SELECT f.*,b.cc,b.desktop,b.cc_title,b.desktop_title FROM send_feedback f
            JOIN bindings b ON b.id=f.binding_id WHERE f.id NOT LIKE 'delivery:%'
            AND (? IS NULL OR f.id=?) ORDER BY f.rowid""",(only_id,only_id))
        for f in feedback:
            resolved=(f['result'] or '').startswith(('修正后已登记','原信已关闭或已有答复'))
            rows.append(dict(id=f['id'],binding_id=f['binding_id'],source=f['sender'],state='resolved' if resolved else 'not_sent',
                             receipt='not_sent',receipt_evidence=f['reason'],body='发信失败 · '+f['reason'],
                             detail=f['result'] or {'queued':'等待提醒发件方','sending':'正在提醒发件方',
                             'shown':'已提示发件方','unknown':'提醒结果待核对'}.get(f['state'],f['state']),
                             reply=None,reply_mode='none',created=f['created'],cc=f['cc'],desktop=f['desktop'],
                             cc_title=f['cc_title'],desktop_title=f['desktop_title']))
    for row in rows:
        row['next_action'] = receipt_action(row)
    return sorted(rows,key=lambda m:m['created'])


def receipt_action(row):
    stage = row['receipt']
    if stage == 'not_sent':
        if row['state'] == 'resolved':
            return '查看处理详情中的修正结果。'
        return '按错误编号查看原因和修正进度；确认仍未登记后再修正发件。'
    if stage == 'delivered':
        return '在收件会话中核对任务结果。'
    if stage == 'native_queued':
        return '等待 Desktop 处理，稍后用原编号查询。'
    if stage == 'unknown' or row['state'] in ('submitting', 'submitted', 'returning'):
        return '用原编号核对接收方会话，再决定如何处理；请勿重复投递。'
    if stage == 'failed':
        return '先核对双方会话及失败详情；确认原信未送达后再处理。'
    if stage == 'cancelled':
        return '该记录已关闭，可在处理详情查看原因。'
    return '保留当前编号，稍后查询投递进度。'
