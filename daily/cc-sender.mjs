import {pathToFileURL} from 'node:url';

export async function claudeSender(require) {
  const {query,createSdkMcpServer,tool}=await import(pathToFileURL(require.resolve('@anthropic-ai/claude-agent-sdk')));
  const {z}=require('zod');
  function makeSendServer(send) {
    return createSdkMcpServer({name:'daily_pair',tools:[tool('send',
      '给本会话已配对的 Astra 发一封单向信。返回工作台登记编号。工具成功后无需再写 ASTRA 信封。',
      {text:z.string()},async ({text},extra)=>{
        try {
          if(typeof extra?.requestId!=='number' && typeof extra?.requestId!=='string')
            throw Error('工具请求缺少编号');
          const id=send(text,extra.requestId);
          return {content:[{type:'text',text:'已登记 · '+id}]};
        } catch(error) {
          return {isError:true,content:[{type:'text',text:error.message}]};
        }
      })]});
  }
  return {query,makeSendServer};
}
