// Count only matched tool calls and successful results from this review round.
// A player command returns process information; its frames are not model input.
export function observeReviewMaterials(record) {
  const pending=new Map();
  const launch=/^(?:cd\s+"[^"$`\r\n]+"\s*&&\s*)?start\s+""\s+"([^"$`\r\n]+\.(?:mp4|mov|webm|mkv|avi))"\s*$/i;
  return raw=>{
    if(raw.isReplay || raw.error || !Array.isArray(raw.message?.content)) return;
    for(const p of raw.message.content) {
      if(raw.type==='assistant' && p.type==='tool_use' && p.id) {
        if(p.name==='Read' && typeof p.input?.file_path==='string')
          pending.set(p.id,{read:p.input.file_path});
        else if(p.name==='Bash') {
          const match=launch.exec(String(p.input?.command || '').trim());
          if(match) pending.set(p.id,{player:match[1]});
        }
      }
      if(raw.type!=='user' || p.type!=='tool_result') continue;
      const tool=pending.get(p.tool_use_id);
      pending.delete(p.tool_use_id);
      if(!tool || p.is_error) continue;
      if(tool.player) record('播放器启动命令成功',tool.player);
      else {
        const returned=typeof p.content==='string' ? [{type:'text',text:p.content}] : p.content;
        for(const item of Array.isArray(returned) ? returned : []) {
          if(item.type==='text' && /^\s*\d+\t[^\n]+/m.test(item.text ?? ''))
            record('已返回文件文本',tool.read);
          if(!item.source?.data || item.source.type!=='base64') continue;
          if(item.type==='image') record('已返回静帧图像',tool.read);
          if(item.type==='video') record('已返回视频数据',tool.read);
        }
      }
    }
  };
}
