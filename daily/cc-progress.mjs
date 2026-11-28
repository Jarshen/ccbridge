// Progress rides an existing Claude turn. Merely recording it never creates mail.
export function ccProgress(db, lookupCommand) {
  const installed=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuity_cc_progress'");
  let pending, mark;
  function snapshot(binding) {
    if(!installed.get()) return null;
    pending ??= db.prepare(`SELECT n.rev,substr(n.topic,1,80) topic,n.kind,s.source,substr(n.body,1,260) body,
      length(n.body)>260 OR length(n.topic)>80 AS clipped, count(*) OVER () AS total,min(n.rev) OVER () AS first
      FROM continuity_cc_progress p JOIN continuity_notes n USING(rev)
      LEFT JOIN continuity_sources s USING(rev)
      WHERE p.state='pending' AND n.audience IN ('cc','both') AND n.root=coalesce(
        (SELECT root FROM continuity_links WHERE binding_id=@id),@id)
      AND EXISTS (SELECT 1 FROM bindings WHERE id=@id AND active=1 AND native_cc=@native)
      ORDER BY n.rev DESC LIMIT 5`);
    const rows=pending.all({id:binding.id,native:binding.native_cc});
    if(!rows.length) return null;
    const {first,total,rev:last}=rows[0];
    const included=[];
    const lines=['【已记录的项目进度 · 仅供本轮参考，以本次请求为准】'];
    for(const r of rows.reverse()) {
      if(!r.clipped) included.push(r.rev);
      lines.push('#'+r.rev+' '+[r.source,r.kind,r.topic].filter(Boolean).join(' · ')+'：'+r.body+(r.clipped?'…':''));
    }
    if(total>rows.length || rows.some(r=>r.clipped))
      lines.push('共有 '+total+' 条；按需查阅省略内容和原始依据：'+lookupCommand(binding.id)+' --since '+first+' --through '+last);
    return {text:lines.join('\n'),included,last};
  }
  const complete=db.transaction((binding,batch)=>{
    const active=db.prepare('SELECT id FROM bindings WHERE id=? AND active=1 AND native_cc=?').get(binding.id,binding.native_cc);
    if(!active) return;
    mark ??= db.prepare(`UPDATE continuity_cc_progress SET state=CASE WHEN rev IN
      (SELECT value FROM json_each(?)) THEN 'included' ELSE 'referenced' END
      WHERE state='pending' AND rev<=? AND rev IN (SELECT rev FROM continuity_notes WHERE root=coalesce(
      (SELECT root FROM continuity_links WHERE binding_id=?),?))`);
    mark.run(JSON.stringify(batch.included),batch.last,binding.id,binding.id);
  });
  return {snapshot,complete:(binding,batch)=>{if(batch) complete.immediate(binding,batch);}};
}
