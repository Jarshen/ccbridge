import {pathToFileURL} from 'node:url';

export async function claudeSender(require) {
  const {query,createSdkMcpServer,tool}=await import(pathToFileURL(require.resolve('@anthropic-ai/claude-agent-sdk')));
  const {z}=require('zod');
  function makeSendServer(send) {
    return createSdkMcpServer({name:'daily_pair',tools:[tool('send',
      '给 Astra 发信。需要 Astra 评审并回传时设置 review；回传已有评审用 review_to 填原信编号。工具成功后无需再写 ASTRA 信封。',
      {text:z.string(),review:z.boolean().optional(),review_to:z.string().uuid().optional()},async ({text,review,review_to},extra)=>{
        try {
          if(typeof extra?.requestId!=='number' && typeof extra?.requestId!=='string')
            throw Error('工具请求缺少编号');
          const id=send(text,extra.requestId,review,review_to);
          return {content:[{type:'text',text:'已登记 · '+id}]};
        } catch(error) {
          return {isError:true,content:[{type:'text',text:error.message}]};
        }
      })]});
  }
  return {query,makeSendServer};
}
