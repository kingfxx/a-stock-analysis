/* Shared collector progress: counts remain distinct from rows and coverage. */
(()=>{'use strict';
const labels={csindex:'中证指数',legu:'乐咕乐股',sws:'申万宏源',eastmoney:'东方财富',sse:'上海航运交易所',mot:'交通运输部',user_excel:'本地研究资料',cninfo:'巨潮资讯',local:'本地'};
function renderDataCounts(summary,list,result){
for(const counts of result.data_counts||[]){
 const item=document.createElement('li');
 const number=key=>Number.isInteger(counts[key])?counts[key]:'未记录';
 item.className='collection-data-counts';
 item.textContent=`${labels[counts.provider]||counts.provider} · ${counts.label||'数据'}：已记录写入 ${number('written')} 条（新增 ${number('inserted')} 条、修订 ${number('revised')} 条${Number.isInteger(counts.enriched)?'、补全 '+number('enriched')+' 条':''}）；未变化 ${number('unchanged')} 条；返回／解析 ${number('returned')} 条。`;
 if(counts.reused_segments)item.textContent+=` 复用／跳过 ${counts.reused_segments} 个分段，这些分段未重复写入。`;
 if(counts.unrecorded_segments)item.textContent+=` 另有 ${counts.unrecorded_segments} 个分段未记录写入数量。`;
 list.append(item);
 if(counts.empty_returns>0){
  const warning=document.createElement('li');warning.className='collection-progress-warning';
  warning.textContent=`注意：${labels[counts.provider]||counts.provider}有 ${counts.empty_returns} 个分段来源返回 0 条（${counts.empty_start||'日期未记录'}—${counts.empty_end||'日期未记录'}），这些分段未更新数据；执行成功不表示数据已刷新。`;
  list.append(warning);summary.textContent+=` 注意：${labels[counts.provider]||counts.provider}有 ${counts.empty_returns} 个分段返回 0 条，未更新数据。`;
 }
}
}
function render({summary,list},operation,result={}){const work=result.work_progress;list.replaceChildren();
if(!operation){summary.textContent='尚未执行采集。';return;}
if(!work||!Number.isInteger(work.total)){
 const progress=result.collection_progress;
 summary.textContent=progress?.message||result.message||'该历史任务未记录总进度。';
 if(progress?.interfaces){for(const [key,counts] of Object.entries(progress.interfaces)){const item=document.createElement('li');item.textContent=`${({income:'利润表',balance:'资产负债表',cashflow:'现金流量表',performance:'业绩指标'})[key]||key}：${counts.status||'待执行'} · 页 ${counts.page??'未记录'}/${counts.pages??'未记录'} · 数据 ${counts.records??'未记录'}/${counts.total??'未记录'} 行`;list.append(item);}}
 else if(progress){const item=document.createElement('li');item.textContent=`阶段：${progress.phase||'未记录'} · 页 ${progress.page??'未记录'}/${progress.pages??'未记录'} · 数据 ${progress.records??'未记录'}/${progress.total??'未记录'} 行`;list.append(item);}
 if(operation.status==='failed')summary.textContent+=' 任务未完成；已发布分段保留，详细诊断见运行记录。';
 else if(operation.status==='cancelled')summary.textContent+=' 已停止；已完成分段保留。';
 renderDataCounts(summary,list,result);return;
}
const processed=(work.success||0)+(work.skipped||0)+(work.failed||0)+(work.cancelled||0),total=work.total;
if(total===0)summary.textContent=work.phase==='planning'?'正在统计本轮采集任务…':'本轮没有需要采集的任务（0/0）。';
else summary.textContent=`已处理 ${processed}/${total} 项；已采集成功 ${work.success||0} 项，已跳过 ${work.skipped||0} 项，失败 ${work.failed||0} 项，已停止 ${work.cancelled||0} 项，进行中 ${work.running||0} 项，待执行 ${work.pending||0} 项。`;
summary.textContent+=' 计数单位为独立采集任务，不代表数据行数或覆盖完整性。';
for(const [provider,counts] of Object.entries(work.providers||{})){const item=document.createElement('li'),done=(counts.success||0)+(counts.skipped||0)+(counts.failed||0)+(counts.cancelled||0);let text=`${labels[provider]||provider}：已处理 ${done}/${counts.total} 项（成功 ${counts.success||0}，跳过 ${counts.skipped||0}，失败 ${counts.failed||0}，已停止 ${counts.cancelled||0}，进行中 ${counts.running||0}，待执行 ${counts.pending||0}）`;
if(counts.current)text+=`；当前：${counts.current}`;item.textContent=text;list.append(item);}
const current=work.current;if(operation.status==='running'&&current&&current.message&&!Object.values(work.providers||{}).some(item=>item.current)){
const item=document.createElement('li');item.textContent=`${labels[current.provider]||current.provider}：${current.message}`;list.append(item);}
if(operation.status==='failed'){
const failedProviders=Object.entries(work.providers||{}).filter(([,counts])=>(counts.failed||0)>0).map(([provider])=>labels[provider]||provider);
let errorInfo=result.errors||operation.error||{};if(typeof errorInfo==='string'){try{errorInfo=JSON.parse(errorInfo);}catch{errorInfo={operation:errorInfo};}}
const recordedProviders=errorInfo&&typeof errorInfo==='object'?Object.keys(errorInfo).filter(key=>labels[key]):[];
const sources=[...new Set([...failedProviders,...recordedProviders])];
summary.textContent+=sources.length?` 错误摘要：${sources.join('、')}来源有任务失败；详细诊断已保存在任务记录中。`:' 错误摘要：采集未能完成，详细诊断已保存在任务记录中。';}
else if(operation.status==='cancelled')summary.textContent+=' 已停止；已完成分段保留。';
renderDataCounts(summary,list,result);
}
window.CollectionProgress={render};
})();
