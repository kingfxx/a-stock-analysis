/* Shared collector progress: counts remain distinct from rows and coverage. */
(()=>{'use strict';
const labels={sina:'新浪财经',tencent:'腾讯行情',baidu:'百度股市通',csindex:'中证指数',legu:'乐咕乐股',sws:'申万宏源',eastmoney:'东方财富',sse:'上海航运交易所',mot:'交通运输部',user_excel:'本地研究资料',cninfo:'巨潮资讯',local:'本地'};
function renderDataCounts(summary,list,result){
if(result.provider_counts?.length){
 summary.textContent+=' 来源写入：'+result.provider_counts.map(counts=>`${labels[counts.provider]||counts.provider} ${Number.isInteger(counts.written)?`${counts.unrecorded_segments?'已知 ':''}${counts.written} 条${counts.unrecorded_segments?'（部分未记录）':''}`:'未记录写入数'}`).join('；')+'。';
}
for(const counts of result.data_counts||[]){
 const item=document.createElement('li');
 const number=key=>Number.isInteger(counts[key])?counts[key]:'未记录';
 item.className='collection-data-counts';
 const title=`${labels[counts.provider]||counts.provider} · ${counts.label||'数据'}`;
 if(!Number.isInteger(counts.written)){
  item.textContent=`${title}：实际写入数量未记录；${Number.isInteger(counts.returned)?`返回／解析 ${counts.returned} 条`:'解析数量未记录'}。`;
 }else{
  const details=Number.isInteger(counts.inserted)&&Number.isInteger(counts.revised)?`（新增 ${counts.inserted} 条、修订 ${counts.revised} 条${Number.isInteger(counts.enriched)?'、补全 '+counts.enriched+' 条':''}）`:'（新增／修订未分别记录）';
  item.textContent=`${title}：已记录写入 ${counts.written} 条${details}；未变化 ${number('unchanged')} 条；返回／解析 ${number('returned')} 条。`;
 }
 if(counts.reused_segments)item.textContent+=` 复用／跳过 ${counts.reused_segments} 个分段，这些分段未重复写入。`;
 if(counts.unrecorded_segments&&Number.isInteger(counts.written))item.textContent+=` 另有 ${counts.unrecorded_segments} 个分段未记录写入数量。`;
 list.append(item);
 if(counts.empty_returns>0){
  const warning=document.createElement('li');warning.className='collection-progress-warning';
  warning.textContent=`注意：${labels[counts.provider]||counts.provider}有 ${counts.empty_returns} 个分段来源返回 0 条（${counts.empty_start||'日期未记录'}—${counts.empty_end||'日期未记录'}），这些分段未更新数据；执行成功不表示数据已刷新。`;
  list.append(warning);summary.textContent+=` 注意：${labels[counts.provider]||counts.provider}有 ${counts.empty_returns} 个分段返回 0 条，未更新数据。`;
 }
}
}
function renderStocks(list,result){
 const reasons={incremental:'增量',full:'全量',pagination:'分页',historical_rebuild:'历史重建',anchor_check:'旧日期核验',identity:'身份辅助',legacy_unknown:'历史未记录'};
 if(result.request_reasons?.length){const li=document.createElement('li');li.textContent='请求用途：'+result.request_reasons.map(r=>`${labels[r.provider]||r.provider} ${reasons[r.reason]||r.reason} ${r.requests}次`).join('；')+'。实际请求只计一次，共享请求不按股票重复累加。';list.append(li);}
 if(!result.stock_results?.length)return;
 const li=document.createElement('li'),details=document.createElement('details'),title=document.createElement('summary');
 title.textContent='逐股刷新结果（整组统计由各股实际提交汇总）'+(result.stock_results_truncated?` · 另有${result.stock_results_truncated}只，请按需查看任务详情`:'');details.append(title);
 const stocks=new Map();for(const work of result.stock_results){if(!stocks.has(work.code))stocks.set(work.code,[]);stocks.get(work.code).push(work);}
 const names={financial:'财务',business_segments:'主营',announcements:'公告',prices:'价格',valuation:'估值',dividends:'分红',shareholders:'股东人数',financing:'融资融券'};
 for(const [code,works] of stocks){const p=document.createElement('p');p.textContent=code+' · '+works.map(w=>{
   const status=({complete:'成功',failed:'失败',cancelled:'已停止',pending:'待执行',running:'进行中',reused:'复用'})[w.status]||w.status;
   const entries=w.data_counts||[],known=entries.filter(e=>Number.isInteger(e.published));
   const total=key=>{const values=entries.map(e=>e[key]).filter(Number.isInteger);return values.length?values.reduce((a,b)=>a+b,0):'未记录';};
   const seconds=w.started_at&&w.finished_at?Math.max(0,(Date.parse(w.finished_at)-Date.parse(w.started_at))/1000).toFixed(1):null;
   const updated=w.finished_at?new Date(w.finished_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'未结束';
   return `${names[w.dataset]||w.dataset} ${status}（写入${known.length?total('published'):'未记录'}，新增${total('inserted')}，修订${total('revised')}；结束${updated}${seconds===null?'':'，历时'+seconds+'秒'}）${w.error?'：'+w.error:''}`;
 }).join('；');details.append(p);}
 li.append(details);list.append(li);
}
function renderElapsed(summary,operation){
 const start=Date.parse(operation.started_at||''),end=Date.parse(operation.finished_at||'');
 if(!Number.isFinite(start))return;
 const seconds=Math.max(0,((Number.isFinite(end)?end:Date.now())-start)/1000),whole=Math.floor(seconds),hours=Math.floor(whole/3600),minutes=Math.floor((whole%3600)/60),remainder=whole%60;
 const value=hours?`${hours}小时${minutes}分${remainder}秒`:minutes?`${minutes}分${remainder}秒`:`${remainder}秒`;
 summary.textContent+=` ${Number.isFinite(end)?'本次总耗时':'已运行'}：${value}。`;
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
 renderDataCounts(summary,list,result);renderStocks(list,result);renderElapsed(summary,operation);return;
}
const processed=(work.success||0)+(work.skipped||0)+(work.failed||0)+(work.cancelled||0),total=work.total;
if(total===0)summary.textContent=work.phase==='planning'?'正在统计本轮采集任务…':'本轮没有需要采集的任务（0/0）。';
else summary.textContent=`已处理 ${processed}/${total} 项；已采集成功 ${work.success||0} 项，已跳过 ${work.skipped||0} 项，失败 ${work.failed||0} 项，已停止 ${work.cancelled||0} 项，进行中 ${work.running||0} 项，待执行 ${work.pending||0} 项。`;
summary.textContent+=` 计数单位为${work.unit||'独立采集任务'}，不代表数据行数或覆盖完整性。`;
if(Number.isInteger(result.network_requests))summary.textContent+=` HTTP请求 ${result.network_requests} 次（含已记录重试）。`;
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
renderDataCounts(summary,list,result);renderStocks(list,result);renderElapsed(summary,operation);
}
window.CollectionProgress={render};
})();
