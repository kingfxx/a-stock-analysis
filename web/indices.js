(()=>{'use strict';
const el=id=>document.getElementById(id), labels={pe_source:'来源披露 PE',pe_static:'静态 PE',pe_dynamic:'预测动态 PE',pe_ttm:'TTM PE',pe_rolling:'滚动 PE',pb:'PB',ps:'PS',ps_ttm:'TTM PS',dividend_yield:'股息率（%）'};
const providers={csindex:'中证指数',legu:'乐咕乐股',sws:'申万宏源'}, states={running:'采集中',complete:'完成',failed:'失败',cancelled:'已停止',unknown:'未知',partial:'部分',empty:'空'};
const seriesNames={'history.rolling':'历史滚动 PE',dashboard:'每日板块估值','indicator.total_shares':'近期估值（总股本）','indicator.calculation_shares':'近期估值（计算用股本）','analysis.daily':'行业估值日报','pe.ordinary':'PE（普通）','pe.equal':'PE（等权）','pe.median':'PE（中位数）','pb.ordinary':'PB（普通）','pb.equal':'PB（等权）','pb.median':'PB（中位数）'};
const methods={source_disclosed:'来源披露口径，详细权重待核对',ordinary:'普通',equal:'等权',median:'中位数'},periods={source_disclosed:'来源披露，未另行推定',rolling:'滚动','static / rolling':'静态及滚动',pending:'待核对','LYR / TTM (source field labels)':'静态及TTM（按来源字段标识）'},shares={pending:'待核对',total_shares:'总股本',calculation_shares:'计算用股本'};
let crowdingData=null;
let industryCrowdingRequest=0,industryCrowdingController,industryCrowdingData=null,industryCrowdingSortDate=null;
let data,history=[],selectedSummary,views={},metricFamily='pe',timer,historyRevision=0,historyController,pointOverlay=false,pointData,pointCode,pointController,shownIndexCode;
const chartCache=new Map();let chartCacheBytes=0;
function clearChartCache(){chartCache.clear();chartCacheBytes=0;}
function rememberChart(key,text){const size=new TextEncoder().encode(text).length;if(size>8*1024*1024)return;chartCache.set(key,{text,size});chartCacheBytes+=size;while(chartCache.size>6||chartCacheBytes>8*1024*1024){const first=chartCache.keys().next().value;chartCacheBytes-=chartCache.get(first).size;chartCache.delete(first);}}
function options(id,items,value){const node=el(id);node.replaceChildren();for(const [v,label] of items){const option=document.createElement('option');option.value=v;option.textContent=label;node.append(option);}if(items.some(item=>item[0]===value))node.value=value;}
const metricNames={pe:'PE(TTM)',pb:'PB',dividend_yield:'股息率',crowding:'拥挤度'};
const metricLegendNames={pe_source:'市盈率 PE（来源披露）',pe_static:'市盈率 PE（静态）',pe_dynamic:'市盈率 PE（动态）',pe_ttm:'市盈率 PE（TTM）',pe_rolling:'市盈率 PE（滚动）',pb:'市净率 PB',ps:'市销率 PS',ps_ttm:'市销率 PS（TTM）',dividend_yield:'股息率'};
function renderMetricButtons(){const group=el('metric-tabs'),families=el('kind').value==='broad'?['pe','pb','crowding']:['pe','pb','dividend_yield','crowding'];if(!families.includes(metricFamily))metricFamily='pe';group.replaceChildren();for(const family of families){const button=document.createElement('button');button.type='button';button.dataset.metricFamily=family;button.textContent=metricNames[family];button.classList.toggle('active',family===metricFamily);button.setAttribute('aria-pressed',String(family===metricFamily));button.onclick=()=>{metricFamily=family;renderMetricButtons();renderSeries(true);};group.append(button);}}
function definitionFor(row){const entry=data.definitions.find(def=>def.provider===row.provider&&def.series_key===row.series_key&&def.definition_version===row.definition_version);return entry?JSON.parse(entry.definition_json):null;}
function fieldFor(row){const fields=Object.values(definitionFor(row)?.fields||{});const candidates=metricFamily==='pe'?['pe_ttm','pe_rolling','pe_source']:[metricFamily];return candidates.find(field=>fields.includes(field))||null;}
async function api(url,body){const response=await fetch(url,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;}
function renderWorkProgress(operation,result){const summary=el('progress-summary'),list=el('progress-sources'),work=result.work_progress;list.replaceChildren();
if(!operation){summary.textContent='尚未执行指数采集。';return;}
if(!work||!Number.isInteger(work.total)){summary.textContent=`该历史任务未记录总进度。${operation.status==='failed'?'任务未完成；错误原因已保存在任务记录中，已发布分段仍保留。':operation.status==='cancelled'?'任务已停止，已发布分段仍保留。':''}`;return;}
const processed=(work.success||0)+(work.skipped||0)+(work.failed||0)+(work.cancelled||0),total=work.total;
if(total===0)summary.textContent=work.phase==='planning'?'正在统计本轮采集任务…':'本轮没有需要采集的估值任务（0/0）。';
else summary.textContent=`已处理 ${processed}/${total} 项；已采集成功 ${work.success||0} 项，已跳过 ${work.skipped||0} 项，失败 ${work.failed||0} 项，已停止 ${work.cancelled||0} 项，进行中 ${work.running||0} 项，待执行 ${work.pending||0} 项。`;
summary.textContent+=' 计数单位为独立采集任务，不代表数据行数或覆盖完整性。';
const labels={csindex:'中证指数',legu:'乐咕乐股',sws:'申万宏源'},stateNames={success:'成功',skipped:'跳过',failed:'失败',cancelled:'已停止',pending:'待执行',running:'进行中'};
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
}
function renderStatus(){const operation=data.operations[0],running=operation?.status==='running';for(const id of ['full','incremental','review','audit'])el(id).disabled=running;el('cancel').disabled=!running;
let result={};try{if(operation)result=JSON.parse(operation.result_json)||{};}catch{result={};}
el('progress').textContent=!operation?'尚未执行指数采集':operation.status==='running'?'指数采集中':operation.status==='complete'?'指数采集已完成':operation.status==='cancelled'?'指数采集已停止，已完成分段保留':'指数采集未全部完成，请查看进度摘要';
renderWorkProgress(operation,result);
el('progress-toggle').textContent=el('progress-details').hidden?'查看进度':'收起进度';
el('cooldowns').textContent=data.cooldowns.map(row=>`${providers[row.source_system]||row.source_system}：${row.reason}，冷却截止（北京时间）${window.DashboardTime.format(row.cooldown_until,'未记录')}`).join('；');
clearTimeout(timer);timer=null;if(running&&!document.hidden)timer=setTimeout(()=>load(false),5000);}
const parentOf=row=>row.navigation_parent_index_code||row.parent_index_code;
function renderParents(){options('parent',data.catalog.filter(row=>row.index_type==='industry'&&row.industry_level===1).map(row=>[row.index_code,row.name]),el('parent').value);}
function updateSelectedIndex(selected){const code=selected?.index_code;el('current-index-name').textContent=selected?.name||'暂无指数';el('current-index-code').textContent=code||'—';if(shownIndexCode===code)return;shownIndexCode=code;pointController?.abort();pointController=null;pointData=null;pointCode=null;el('point-overlay-note').hidden=!pointOverlay;if(pointOverlay&&code&&metricFamily!=='crowding')loadPointOverlay(code);}
function renderCodes(){
const industry=el('kind').value==='industry',old=el('code').value;
el('parent-control').hidden=!industry;el('second-control').hidden=!industry;el('code-control').hidden=industry;
const choices=data.catalog.filter(row=>row.index_type===el('kind').value).map(row=>[row.index_code,row.name]);
const preferred=choices.some(([code])=>code===old)?old:el('kind').value==='broad'?'cn:000300':'';
options('code',choices,preferred);
if(industry){const parent=el('parent').value;options('industry-second',[['','全部二级行业'],...data.catalog.filter(row=>row.index_type==='industry'&&row.industry_level===2&&parentOf(row)===parent).map(row=>[row.index_code,row.name])],el('industry-second').value);el('code').value=el('industry-second').value||parent;}
const selected=data.catalog.find(row=>row.index_code===el('code').value);el('current-index-name').textContent=selected?.name||'暂无指数';el('current-index-code').textContent=selected?.index_code||'—';renderMetricButtons();renderSeries(old!==el('code').value);updateSelectedIndex(selected);}
function renderSeries(reset=false){
const industry=el('kind').value==='industry',old=reset?'':el('series').value;
el('series-control').hidden=industry||metricFamily==='crowding';el('industry-source').hidden=!industry||metricFamily==='crowding';
const preferred=metricFamily==='pb'?'legu|pb.ordinary':metricFamily==='pe'?'csindex|history.rolling':'';
const rows=data.coverage.filter(row=>row.index_code===el('code').value&&(!industry||row.provider==='sws'&&row.series_key==='analysis.daily')&&fieldFor(row)&&row[fieldFor(row)+'_count']>0);
const rank=row=>`${row.provider}|${row.series_key}`===preferred?0:row.series_key.endsWith('.ordinary')?1:row.series_key==='dashboard'?3:2;
rows.sort((a,b)=>rank(a)-rank(b)||b.observations-a.observations||a.series_key.localeCompare(b.series_key));
options('series',rows.map(row=>[`${row.provider}|${row.series_key}|${row.definition_version}`,`${providers[row.provider]} · ${row.series_key==='dashboard'?'每日板块 '+metricNames[metricFamily]:seriesNames[row.series_key]||row.series_key} · ${row.frequency==='monthly'?'月度':'日度'}`]),old);
loadHistory();}
async function loadHistory(){const revision=++historyRevision;historyController?.abort();historyController=new AbortController();if(metricFamily==='crowding'){loadCrowding(revision);return;}try{const code=el('code').value,choice=el('series').value;if(!code||!choice){history=[];views={};selectedSummary=null;options('metric',[]);el('definition').textContent='';el('coverage').textContent='该指数暂无可用的'+metricNames[metricFamily]+'数据';draw();return;}
const [provider,series,definition]=choice.split('|'),summary=data.coverage.find(row=>row.index_code===code&&row.provider===provider&&row.series_key===series&&row.definition_version===definition);selectedSummary=summary;history=[];views={};const field=fieldFor(summary);options('metric',field?[[field,labels[field]]]:[]);el('coverage').textContent='正在读取所选指数…';
const key=new URLSearchParams({code,provider,series,definition,metric:field}).toString();let result;
if(chartCache.has(key)){const cached=chartCache.get(key);chartCache.delete(key);chartCache.set(key,cached);result=JSON.parse(cached.text);}else{const response=await fetch('/api/indices/chart?'+key,{cache:'no-store',signal:historyController.signal});const text=await response.text();result=JSON.parse(text);if(!response.ok)throw Error(result.error||'请求失败');if(revision!==historyRevision)return;if(result.revision!==data.revision){clearChartCache();await load(true);return;}rememberChart(key,text);}
if(revision!==historyRevision)return;history=result.has_rows?[true]:[];views=result.views||{};
for(const option of el('frequency').options)option.disabled=summary.frequency==='monthly'&&['day','week'].includes(option.value);
if(summary.frequency==='monthly'&&['day','week'].includes(el('frequency').value))el('frequency').value='month';
const definitionRow=data.definitions.find(row=>row.provider===provider&&row.series_key===series&&row.definition_version===definition),def=definitionRow?JSON.parse(definitionRow.definition_json):null;
el('definition').textContent=def?`统计方法：${methods[def.statistical_method]||def.statistical_method}；利润期间：${periods[def.profit_period]||def.profit_period}；股本口径：${shares[def.share_basis]||def.share_basis}。来源字段：${Object.entries(def.fields).map(([field,target])=>field+' → '+labels[target]).join('；')}。`:'定义待补';
draw();
}catch(error){if(error.name!=='AbortError'&&revision===historyRevision){el('coverage').textContent=error.message;draw();}}}
async function loadPointOverlay(code){pointController?.abort();pointController=new AbortController();const controller=pointController;el('point-overlay-note').hidden=false;el('point-overlay-note').textContent='正在读取该指数的历史收盘点位…';try{const key='point_overlay|'+code;let result;if(chartCache.has(key)){const cached=chartCache.get(key);chartCache.delete(key);chartCache.set(key,cached);result=JSON.parse(cached.text);}else{const response=await fetch('/api/indices/price?'+new URLSearchParams({code}),{cache:'no-store',signal:controller.signal});const text=await response.text();result=JSON.parse(text);if(!response.ok)throw Error(result.error||'读取指数点位失败');if(result.revision!==data.revision){clearChartCache();await load(true);return;}rememberChart(key,text);}if(controller!==pointController||code!==el('code').value)return;pointCode=code;pointData=result.available?result:null;const node=el('point-overlay-note');node.hidden=!pointOverlay;if(!result.available)node.textContent='当前指数暂无可用的收盘点位，未叠加价格线。';else{const basis={price:'价格指数',total_return:'全收益指数',net_return:'净收益指数',unspecified:'来源未标识收益口径'}[result.return_type]||result.return_type;node.textContent=`点位来源：${providers[result.provider]||result.provider} · ${result.frequency==='monthly'?'月度':'日度'} · ${basis}；范围 ${result.first_date}—${result.last_date}，${result.observations} 条有效收盘点位。按图中展示频率取每周期最后一个真实收盘点，缺失周期留空。`;}draw();}catch(error){if(error.name!=='AbortError'&&controller===pointController){pointData=null;pointCode=code;el('point-overlay-note').hidden=!pointOverlay;el('point-overlay-note').textContent='指数点位读取失败：'+error.message;draw();}}}
function bucketKey(day,frequency){if(frequency==='month')return day.slice(0,7);if(frequency==='week'){const date=new Date(day+'T00:00:00Z');date.setUTCDate(date.getUTCDate()-((date.getUTCDay()+6)%7));return date.toISOString().slice(0,10);}return day;}
function overlayPoints(rows,frequency,target){if(!pointData||pointCode!==el('code').value)return[];const latest=new Map();for(const point of pointData.rows){const key=bucketKey(point.observation_date,frequency),prior=latest.get(key);if(!prior||point.observation_date>prior.observation_date)latest.set(key,point);}return rows.map(row=>{const observed=row[target+'_date']||row.date,point=latest.get(bucketKey(observed,frequency));return point?{date:row.date,close:point.close,actualDate:point.observation_date}:{date:row.date,close:null,actualDate:null};});}
function draw(){
if(metricFamily==='crowding'){drawCrowding();return;}
const metric=el('metric').value,years=el('range').value,view=views[metric]?.[years],yieldMetric=metric==='dividend_yield',std=el('benchmark').value==='stddev';
const automaticFrequency={3:'day',5:'week',10:'month'}[years];
const frequency=selectedSummary?.frequency==='monthly'?'month':el('frequency').value==='auto'?automaticFrequency:el('frequency').value;
const rows=view?.rows_by_frequency?.[frequency]||[],target=metric?.startsWith('pe_')?'pe':metric?.startsWith('ps')?'ps':metric;
const format=value=>value==null?'—':Number(value).toFixed(2)+(yieldMetric?'%':' 倍');
const levels=std?[view?.stddev_high,view?.mean,view?.stddev_low]:[view?.high,view?.median,view?.low];
el('current-label').textContent='最近观察 · '+metricNames[metricFamily];
el('comparison').textContent=metric==='pe_source'?'当前 PE 来自申万来源披露值，利润期间尚待核实，不能视为已确认的 TTM；历史判断沿用这一来源口径。':'指数按自身历史比较；当前口径独立计算，不混合不同来源。';el('current').textContent=format(view?.current);el('current-date').textContent=view?.current_date||'暂无有效观察';
const percentile=view?.percentile,valid=Number.isFinite(percentile)&&view?.count>0,rank=!valid?-1:percentile<=5?0:percentile<=20?1:percentile<40?2:percentile<=60?3:percentile<80?4:percentile<95?5:6;
el('position').textContent=rank<0?'待判断':(yieldMetric?['极低','低息','偏低','中值','偏高','高息','极高']:['极低','低位','偏低','中值','偏高','高位','极高'])[rank];
el('position').dataset.tone=rank<0?'':String(yieldMetric?6-rank:rank);el('position').title='历史分位按 5%/20%/40%/60%/80%/95% 分档，仅表示自身历史位置。';
el('basis').textContent=`近 ${years} 年 · `+(valid?`历史分位 ${percentile.toFixed(1)}%`:'暂无有效分位');
const sampleName={month:'月',week:'周',trading_day:'交易日'}[view?.sample_frequency]||'有效';
el('history-summary').textContent=valid?`历史：当前高于近 ${years} 年 ${percentile.toFixed(1)}% 的${sampleName}样本（${view.count} 个）；切换展示频率不改变分位。`:`历史：近 ${years} 年暂无可用的${metricNames[metricFamily]}数据。`;
if(std&&view?.count)el('history-summary').textContent+=` 均值 ${format(view.mean)}，总体标准差 ${format(view.stddev)}。`;
const colors=[yieldMetric?'#6ac9aa':'#ed8180','#f4b560',yieldMetric?'#ed8180':'#6ac9aa'];
['high','median','low'].forEach((id,i)=>el(id).textContent=format(levels[i]));
el('high-label').textContent=std?'上限（均值 +1σ）':yieldMetric?'高息（80% 分位）':'高位（80% 分位）';el('median-label').textContent=std?'均值':'中值（50% 分位）';el('low-label').textContent=std?'下限（均值 −1σ）':yieldMetric?'低息（20% 分位）':'低位（20% 分位）';
el('high-dot').style.background=colors[0];el('low-dot').style.background=colors[2];
const negative=view?.negative_pe_ranges||[];el('negative-note').textContent=negative.length?'负 PE 观察区间：'+negative.map(r=>r.start===r.end?r.start:`${r.start} 至 ${r.end}`).join('；')+'。浅色区域标示；不参与走势及分位，原始值保留。':'';
if(history.length&&selectedSummary)el('coverage').textContent=`${labels[metric]}有效范围 ${selectedSummary[metric+'_first_date']||'暂无'}—${selectedSummary[metric+'_last_date']||'暂无'}，${selectedSummary[metric+'_count']||0} 个来源有效值／${selectedSummary.observations} 条观察；目录有效期及应有名单未核实的覆盖率为未知。`;
el('methodology').textContent=`与个股页使用同一计算规则：近5年按周取最后有效观察，近3/10年在未核实完整交易日覆盖时按月取最后有效观察。分位＝小于当前值的样本数÷有效样本数；参考线为80%/50%/20%线性插值分位，或均值±总体标准差（除以N）。PE/PB/PS仅正值参与，股息率保留有效零值，缺值留空。当前按${{day:'日',week:'周',month:'月'}[frequency]}展示；月频来源不派生日/周频。判断始终按历史分位，仅表示自身历史位置。`;
const shapes=levels.flatMap((value,i)=>value==null?[]:[{type:'line',xref:'paper',x0:0,x1:1,y0:value,y1:value,line:{color:colors[i],dash:'dash',width:1.5}}]);
for(const r of negative)shapes.push({type:'rect',xref:'x',yref:'paper',x0:r.start,x1:r.end===r.start?new Date(Date.parse(r.end)+86400000).toISOString().slice(0,10):r.end,y0:0,y1:1,fillcolor:'rgba(244,211,94,.09)',line:{color:'rgba(244,211,94,.3)',width:1},layer:'below'});
const traces=[{x:rows.map(row=>row.date),y:rows.map(row=>row[target]??null),type:'scatter',mode:'lines+markers',name:metricLegendNames[metric]||labels[metric]||'指数估值',line:{color:'#66aaf0',width:2},marker:{size:4},fill:'tozeroy',fillcolor:'rgba(102,170,240,.07)',connectgaps:false,hoveron:'points',hovertemplate:(labels[metric]||'估值')+'：%{y:.2f}'+(yieldMetric?'%':' 倍')+'<extra></extra>'}];const pointRows=pointOverlay?overlayPoints(rows,frequency,target):[];if(pointRows.length)traces.push({x:pointRows.map(row=>row.date),y:pointRows.map(row=>row.close),type:'scatter',mode:'lines',name:'指数点位',yaxis:'y2',line:{color:'#f4d35e',width:2.3},connectgaps:false,hovertemplate:'指数点位：%{y:.2f} 点<extra></extra>'});el('point-overlay').setAttribute('aria-pressed',String(pointOverlay));el('point-overlay-note').hidden=!pointOverlay;if(pointOverlay&&!pointData&&!pointController&&pointCode!==el('code').value&&el('code').value)loadPointOverlay(el('code').value);Plotly.react('chart',traces,{paper_bgcolor:'#1a2429',plot_bgcolor:'#1a2429',font:{color:'#edf4f4'},uirevision:JSON.stringify([el('code').value,el('series').value,metric,years,frequency]),margin:{t:25,l:65,r:pointRows.length?68:25,b:82},hovermode:'x unified',hoverdistance:-1,spikedistance:-1,hoverlabel:{bgcolor:'#23343d',bordercolor:'#49616b',font:{color:'#edf4f4'}},legend:{orientation:'h',x:0.5,xanchor:'center',y:-0.22,yanchor:'top',font:{color:'#afc2c9',size:12}},xaxis:{type:'date',gridcolor:'#30434d',hoverformat:'%Y-%m-%d'},yaxis:{title:yieldMetric?'股息率（%）':'估值（倍）',gridcolor:'#30434d',rangemode:'tozero'},...(pointRows.length?{yaxis2:{title:{text:'指数点位',font:{color:'#f4d35e'}},tickfont:{color:'#f4d35e'},overlaying:'y',side:'right',showgrid:false,rangemode:'tozero',automargin:true}}:{}),shapes},{responsive:true,displaylogo:false});
}

async function loadCrowding(revision){
  crowdingData=null;history=[];views={};selectedSummary=null;
  for(const option of el('frequency').options)option.disabled=false;
  el('coverage').textContent='正在读取所选指数的拥挤度…';
  draw();
  try{
    const code=el('code').value;
    const response=await fetch('/api/indices/crowding?'+new URLSearchParams({code}),{cache:'no-store',signal:historyController.signal});
    const result=await response.json();if(!response.ok)throw Error(result.error||'读取失败');
    if(revision!==historyRevision)return;
    if(result.revision!==data.revision){clearChartCache();await load(true);return;}
    crowdingData=result;
    el('coverage').textContent=result.available?`拥挤度有效范围 ${result.first_date}—${result.last_date}，${result.observations} 个有效日样本；全历史覆盖率未核实。`:'该指数暂无本地成交额占比数据。';
    draw();
  }catch(error){if(error.name!=='AbortError'&&revision===historyRevision){el('coverage').textContent='拥挤度读取失败：'+error.message;draw();}}
}
function drawCrowding(){
  const {periodKey,dailyStatistics}=window.CongestionStats;
  const years=el('range').value,upper=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const lowerDate=new Date(upper+'T00:00:00Z'),month=lowerDate.getUTCMonth();lowerDate.setUTCFullYear(lowerDate.getUTCFullYear()-Number(years));if(lowerDate.getUTCMonth()!==month)lowerDate.setUTCDate(0);
  const lower=lowerDate.toISOString().slice(0,10),std=el('benchmark').value==='stddev';
  const rows=(crowdingData?.rows||[]).filter(row=>row.observation_date>=lower&&row.observation_date<=upper);
  const stats=dailyStatistics({api_history:{rows:rows.map(row=>({observation_date:row.observation_date,value:row.amount_ratio}))}},lower,upper);
  const format=value=>value==null?'—':value.toFixed(2)+'%';
  const levels=std?[stats.stddevHigh,stats.mean,stats.stddevLow]:[stats.high,stats.median,stats.low];
  el('current-label').textContent='最近有效观测 · 拥挤度';el('current').textContent=format(stats.current);el('current-date').textContent=stats.currentDate||'暂无有效观察';
  el('position').textContent=stats.count?['极低','低拥挤','偏低','适中','偏高','拥挤','极拥挤'][stats.rank]:'待判断';el('position').dataset.tone=stats.count?String(stats.rank):'';
  el('position').title='按有效日频成交额占比判断自身历史位置。';
  el('basis').textContent=stats.count?`近 ${years} 年 · 历史分位 ${stats.percentile.toFixed(1)}%`:'暂无有效分位';
  el('history-summary').textContent=stats.count?`历史：当前高于近 ${years} 年 ${stats.percentile.toFixed(1)}% 的日样本（${stats.count} 个）；切换展示频率和叠加指数不改变分位。`:`历史：近 ${years} 年暂无可用的拥挤度数据。`;
  el('comparison').textContent='按所选指数／行业的成交额占全市场比例判断自身历史位置，不代表后续指数涨跌。'+(std&&stats.count?` 均值 ${format(stats.mean)}，总体标准差 ${format(stats.stddev)}。`:'');
  const colors=['#ed8180','#f4b560','#6ac9aa'];
  ['high','median','low'].forEach((id,i)=>{el(id).textContent=format(levels[i]);el(id+'-label').textContent=std?['上限（均值 +1σ）','均值','下限（均值 −1σ）'][i]:['高拥挤（80% 分位）','中值（50% 分位）','低拥挤（20% 分位）'][i];});
  el('high-dot').style.background=colors[0];el('low-dot').style.background=colors[2];el('negative-note').textContent='';
  el('definition').textContent=crowdingData?.available?`数据来源：${providers[crowdingData.provider]||crowdingData.provider} · 日频成交额占比。`+(crowdingData.provider==='csindex'?'宽基占比为本地估算：指数成交额 ÷ 同日全 A 成交额 × 100%；优先使用 Legu，2026-07-31 至 08-27 的缺值使用已有 BigQuant 汇总补齐，来源口径可能存在差异。':'申万 bargainsumrate 来源占比原值；分母细分统计口径未进一步披露。'):'';
  el('methodology').textContent='拥挤度读取指数点位表 amount_ratio（百分数）；与大盘前5%个股集中度的定义不同。有效零值保留、缺值留空，按区间全部有效日样本计算分位，参考线为80%/50%/20%分位或均值±总体标准差。周／月展示取周期最后已记录日，缺值不以前值填充。';
  const frequency=el('frequency').value==='auto'?(years==='10'?'week':'day'):el('frequency').value,periods=new Map();
  for(const row of rows)periods.set(periodKey(row.observation_date,frequency),row);
  const shown=[...periods.values()],traces=[{x:shown.map(r=>r.observation_date),y:shown.map(r=>r.amount_ratio),type:'scatter',mode:'lines',name:'拥挤度',line:{color:'#66aaf0',width:2},connectgaps:false,hovertemplate:'拥挤度：%{y:.2f}%<extra></extra>'}];
  if(pointOverlay&&rows.some(row=>row.close!=null))traces.push({x:shown.map(r=>r.observation_date),y:shown.map(r=>r.close),type:'scatter',mode:'lines',name:'指数点位',yaxis:'y2',line:{color:'#f4d35e',width:1.8},connectgaps:false,hovertemplate:'指数点位：%{y:.2f} 点<extra></extra>'});
  el('point-overlay').setAttribute('aria-pressed',String(pointOverlay));el('point-overlay-note').hidden=!pointOverlay;el('point-overlay-note').textContent='点位使用拥挤度同一来源序列的真实收盘值，缺值留空。';
  Plotly.react('chart',traces,{paper_bgcolor:'#1a2429',plot_bgcolor:'#1a2429',font:{color:'#edf4f4'},uirevision:JSON.stringify([el('code').value,'crowding',years,frequency]),margin:{l:65,r:pointOverlay?85:25,t:20,b:76},hovermode:'x unified',legend:{orientation:'h',x:.5,xanchor:'center',y:-.2},xaxis:{type:'date',gridcolor:'#30434d',tickformat:'%Y-%m',hoverformat:'%Y-%m-%d',range:[lower,upper]},yaxis:{title:{text:'拥挤度（%）'},ticksuffix:'%',gridcolor:'#30434d'},yaxis2:{title:{text:'指数点位'},overlaying:'y',side:'right',showgrid:false,tickfont:{color:'#f4d35e'}},shapes:levels.flatMap((value,i)=>!Number.isFinite(value)?[]:[{type:'line',xref:'paper',yref:'y',x0:0,x1:1,y0:value,y1:value,line:{color:colors[i],dash:'dash',width:1.5}}]),annotations:stats.count?[]:[{text:'所选区间暂无本地拥挤度数据',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false}]},{responsive:true,displaylogo:false});
}

function industryCrowdingToday(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
function industryCrowdingStart(days){const end=industryCrowdingToday(),start=new Date(end+'T00:00:00Z');start.setUTCDate(start.getUTCDate()-Number(days)+1);return start.toISOString().slice(0,10);}
async function loadIndustryCrowding(){
  const request=++industryCrowdingRequest;industryCrowdingController?.abort();industryCrowdingController=new AbortController();
  const level=el('industry-crowding-level').value,days=el('industry-crowding-range').value,start=industryCrowdingStart(days),end=industryCrowdingToday();
  el('industry-crowding-status').textContent='正在读取本地行业行情…';
  try{
    const query=new URLSearchParams({level,start,end}),response=await fetch('/api/indices/industry-crowding?'+query,{cache:'no-store',signal:industryCrowdingController.signal}),result=await response.json();
    if(!response.ok)throw Error(result.error||'读取失败');if(request!==industryCrowdingRequest)return;
    if(data&&result.revision!==data.revision){clearChartCache();await load(true);if(request===industryCrowdingRequest)await loadIndustryCrowding();return;}
    industryCrowdingData=result;if(!result.dates.includes(industryCrowdingSortDate))industryCrowdingSortDate=result.dates[0]||null;drawIndustryCrowding();
  }catch(error){if(error.name!=='AbortError'&&request===industryCrowdingRequest){industryCrowdingData=null;el('industry-crowding-status').textContent='行业拥挤度读取失败：'+error.message;el('industry-crowding-head-row').replaceChildren();el('industry-crowding-body').replaceChildren();}}
}
function industryHeatColor(percentile){
  const stops=[[0,[39,89,92]],[30,[64,121,122]],[50,[70,87,90]],[70,[151,81,90]],[100,[216,60,80]]];
  const value=Math.max(0,Math.min(100,percentile));let left=stops[0],right=stops.at(-1);
  for(let index=1;index<stops.length;index++){if(value<=stops[index][0]){left=stops[index-1];right=stops[index];break;}}
  const ratio=(value-left[0])/(right[0]-left[0]),rgb=left[1].map((part,index)=>Math.round(part+(right[1][index]-part)*ratio));return `rgb(${rgb.join(' ')})`;
}
function drawIndustryCrowding(){
  if(!industryCrowdingData)return;
  const dates=industryCrowdingData.dates||[],industries=industryCrowdingData.industries||[],days=el('industry-crowding-range').value,level=el('industry-crowding-level').value;
  if(!dates.includes(industryCrowdingSortDate))industryCrowdingSortDate=dates[0]||null;
  const valueAt=(row,day)=>row.values.find(value=>value.date===day),sortDate=industryCrowdingSortDate;
  const sorted=[...industries].sort((a,b)=>{
    const av=sortDate?valueAt(a,sortDate)?.congestion:null,bv=sortDate?valueAt(b,sortDate)?.congestion:null;
    if(av==null&&bv==null)return a.name.localeCompare(b.name,'zh-CN');if(av==null)return 1;if(bv==null)return -1;return bv-av;
  });
  el('industry-crowding-status').textContent=dates.length?`申万${level==='1'?'一级':'二级'} · 最近 ${days} 天 · ${dates.length} 个有效交易日。按拥挤度降序，点击日期切换排序。`:`申万${level==='1'?'一级':'二级'}在所选日期范围内暂无有效拥挤度数据。`;
  const header=document.createElement('tr'),first=document.createElement('th');first.className='industry-sticky';first.scope='col';
  const firstContent=document.createElement('div');firstContent.className='industry-header-content';const firstLabel=document.createElement('span');firstLabel.textContent='行业 / 拥挤度';
  const currentButton=document.createElement('button');currentButton.id='industry-crowding-current';currentButton.className='industry-current-button';currentButton.type='button';currentButton.textContent='当期';currentButton.disabled=!dates.length;currentButton.title='回到最新有效交易日并按当天拥挤度排序';currentButton.setAttribute('aria-label',currentButton.title);currentButton.onclick=()=>{const scroller=el('industry-crowding-scroll'),reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;industryCrowdingSortDate=dates[0]||null;drawIndustryCrowding();requestAnimationFrame(()=>{scroller.scrollTo({left:0,behavior:reducedMotion?'auto':'smooth'});if(reducedMotion)scroller.scrollLeft=0;});};
  firstContent.append(firstLabel,currentButton);first.append(firstContent);header.append(first);
  dates.forEach((day,index)=>{const th=document.createElement('th');th.scope='col';const button=document.createElement('button');button.type='button';button.className='industry-date-sort';button.textContent=day.slice(5).replace('-','/');button.title=`按 ${day} 的拥挤度降序排序`;button.setAttribute('aria-label',button.title);button.setAttribute('aria-pressed',String(day===sortDate));button.onclick=()=>{industryCrowdingSortDate=day;drawIndustryCrowding();};th.append(button);header.append(th);});
  el('industry-crowding-head-row').replaceChildren(header);
  el('industry-crowding-body').replaceChildren(...sorted.map((row,index)=>{
    const tr=document.createElement('tr');if(index<5)tr.className='industry-top-five';
    const label=document.createElement('th');label.className='industry-sticky';label.scope='row';const jump=document.createElement('button');jump.type='button';jump.title=`在估值与分位查看${row.name}拥挤度`;
    const name=document.createElement('span');name.className='industry-name';name.textContent=row.name;const rank=document.createElement('span');rank.className='industry-rank';rank.textContent=index<5?`TOP ${index+1}`:`${String(index+1).padStart(2,'0')}`;jump.append(name,rank);
    jump.onclick=()=>{el('kind').value='industry';renderParents();const parent=row.industry_level===1?row.index_code:(row.navigation_parent_index_code||row.parent_index_code||'');el('parent').value=parent;options('industry-second',[['','全部二级行业'],...data.catalog.filter(item=>item.index_type==='industry'&&item.industry_level===2&&parentOf(item)===parent).map(item=>[item.index_code,item.name])],row.industry_level===2?row.index_code:'');metricFamily='crowding';renderCodes();el('valuation-panel').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});};
    label.append(jump);tr.append(label);
    dates.forEach(day=>{const td=document.createElement('td'),value=valueAt(row,day),congestion=value?.congestion,percentile=value?.turnover_percentile;
      if(congestion==null){td.className='industry-empty';td.textContent='—';td.title=`${row.name} · ${day} · 无有效拥挤度`;}else{td.textContent=`${Number(congestion).toFixed(2)}%`;const amountText=value?.amount==null?'成交额暂无有效值':`成交额 ${(Number(value.amount)/100000000).toFixed(2)} 亿元`;if(percentile!=null){td.style.backgroundColor=industryHeatColor(percentile);td.title=`${row.name} · ${day} · 拥挤度 ${Number(congestion).toFixed(2)}% · ${amountText} · 换手率历史分位 ${Number(percentile).toFixed(1)}%`;}else{td.className='industry-empty';td.title=`${row.name} · ${day} · 拥挤度 ${Number(congestion).toFixed(2)}% · ${amountText} · 换手率历史分位缺少有效值`;}}
      tr.append(td);
    });return tr;
  }));
}

async function load(refreshChoices=true){try{const status=await api('/api/indices/status');if(!refreshChoices&&data&&status.operations.some(operation=>operation.status==='running')){data.operations=status.operations;data.cooldowns=status.cooldowns;renderStatus();return;}const changed=!data||data.revision!==status.revision;if(refreshChoices||changed){const catalog=await api('/api/indices/catalog');if(!data||catalog.revision!==data.revision)clearChartCache();data={...catalog,...status,revision:catalog.revision};renderStatus();renderParents();renderCodes();}else{Object.assign(data,status);renderStatus();}}catch(error){el('progress').textContent=error.message;}}
// Refresh versions when returning to a tab; status polling never reads all history.
document.addEventListener('visibilitychange',()=>{clearTimeout(timer);timer=null;if(!document.hidden)load(false);});
window.addEventListener('focus',()=>load(false));
const notifyCollection=()=>document.dispatchEvent(new CustomEvent('index-collection-change',{detail:{source:'indices'}}));
document.addEventListener('index-collection-change',event=>{if(event.detail?.source!=='indices')load(false);});
async function collect(mode){try{await api('/api/indices/collect',{mode});notifyCollection();await load(false);}catch(error){el('progress').textContent=error.message;}}
el('full').onclick=()=>collect('full');el('incremental').onclick=()=>collect('incremental');el('review').onclick=()=>collect('review');el('audit').onclick=()=>collect('audit');el('cancel').onclick=async()=>{try{await api('/api/indices/cancel',{});el('progress').textContent='已请求停止，等待当前请求返回后保存进度';}catch(error){el('progress').textContent=error.message;}};el('point-overlay').onclick=()=>{pointOverlay=!pointOverlay;if(metricFamily==='crowding'){draw();return;}el('point-overlay').setAttribute('aria-pressed',String(pointOverlay));if(pointOverlay&&(!pointData||pointCode!==el('code').value))loadPointOverlay(el('code').value);else draw();};
el('progress-toggle').onclick=()=>{const details=el('progress-details'),expanded=details.hidden;details.hidden=!expanded;el('progress-toggle').setAttribute('aria-expanded',String(expanded));el('progress-toggle').textContent=expanded?'收起进度':'查看进度';};
el('kind').onchange=renderCodes;el('parent').onchange=()=>{el('industry-second').value='';renderCodes();};el('industry-second').onchange=renderCodes;el('code').onchange=()=>{const selected=data.catalog.find(row=>row.index_code===el('code').value);renderMetricButtons();renderSeries(true);updateSelectedIndex(selected);};el('series').onchange=loadHistory;for(const id of ['range','benchmark','frequency'])el(id).onchange=draw;el('metric').onchange=draw;
el('industry-crowding-level').onchange=loadIndustryCrowding;el('industry-crowding-range').onchange=loadIndustryCrowding;
const stock=new URLSearchParams(location.search).get('stock');for(const link of document.querySelectorAll('[data-view]')){const params=new URLSearchParams({tab:link.dataset.view});if(stock&&/^(?:sh|sz|bj)?\d{6}$/i.test(stock))params.set('code',stock);link.href='/?'+params;}
api('/api/stock-groups').then(library=>window.initStockPicker({stock_library:library,code:stock||''})).catch(error=>{el('stock-picker-message').textContent='股票列表读取失败：'+error.message;});
load();loadIndustryCrowding();
})();
