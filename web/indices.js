(()=>{'use strict';
const el=id=>document.getElementById(id), labels={pe_source:'来源披露 PE',pe_static:'静态 PE',pe_dynamic:'预测动态 PE',pe_ttm:'TTM PE',pe_rolling:'滚动 PE',pb:'PB',ps:'PS',ps_ttm:'TTM PS',dividend_yield:'股息率（%）'};
const providers={csindex:'中证指数',legu:'乐咕乐股',sws:'申万宏源'}, states={running:'采集中',complete:'完成',failed:'失败',cancelled:'已停止',unknown:'未知',partial:'部分',empty:'空'};
const seriesNames={'history.rolling':'历史滚动 PE',dashboard:'每日板块估值','indicator.total_shares':'近期估值（总股本）','indicator.calculation_shares':'近期估值（计算用股本）','analysis.daily':'行业估值日报','pe.ordinary':'PE（普通）','pe.equal':'PE（等权）','pe.median':'PE（中位数）','pb.ordinary':'PB（普通）','pb.equal':'PB（等权）','pb.median':'PB（中位数）'};
const methods={source_disclosed:'来源披露口径，详细权重待核对',ordinary:'普通',equal:'等权',median:'中位数'},periods={source_disclosed:'来源披露，未另行推定',rolling:'滚动','static / rolling':'静态及滚动',pending:'待核对','LYR / TTM (source field labels)':'静态及TTM（按来源字段标识）'},shares={pending:'待核对',total_shares:'总股本',calculation_shares:'计算用股本'};
let data,history=[],selectedSummary,views={},metricFamily='pe',timer,historyRevision=0,historyController;
const chartCache=new Map();let chartCacheBytes=0;
function clearChartCache(){chartCache.clear();chartCacheBytes=0;}
function rememberChart(key,text){const size=new TextEncoder().encode(text).length;if(size>8*1024*1024)return;chartCache.set(key,{text,size});chartCacheBytes+=size;while(chartCache.size>6||chartCacheBytes>8*1024*1024){const first=chartCache.keys().next().value;chartCacheBytes-=chartCache.get(first).size;chartCache.delete(first);}}
function options(id,items,value){const node=el(id);node.replaceChildren();for(const [v,label] of items){const option=document.createElement('option');option.value=v;option.textContent=label;node.append(option);}if(items.some(item=>item[0]===value))node.value=value;}
const metricNames={pe:'PE(TTM)',pb:'PB',dividend_yield:'股息率'};
function renderMetricButtons(){const group=el('metric-tabs'),families=el('kind').value==='broad'?['pe','pb']:['pe','pb','dividend_yield'];if(!families.includes(metricFamily))metricFamily='pe';group.replaceChildren();for(const family of families){const button=document.createElement('button');button.type='button';button.dataset.metricFamily=family;button.textContent=metricNames[family];button.classList.toggle('active',family===metricFamily);button.setAttribute('aria-pressed',String(family===metricFamily));button.onclick=()=>{metricFamily=family;renderMetricButtons();renderSeries(true);};group.append(button);}}
function definitionFor(row){const entry=data.definitions.find(def=>def.provider===row.provider&&def.series_key===row.series_key&&def.definition_version===row.definition_version);return entry?JSON.parse(entry.definition_json):null;}
function fieldFor(row){const fields=Object.values(definitionFor(row)?.fields||{});const candidates=metricFamily==='pe'?['pe_ttm','pe_rolling','pe_source']:[metricFamily];return candidates.find(field=>fields.includes(field))||null;}
async function api(url,body){const response=await fetch(url,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;}
function renderStatus(){const operation=data.operations[0],running=operation?.status==='running';for(const id of ['full','incremental','review','audit'])el(id).disabled=running;el('cancel').disabled=!running;
el('progress').textContent=operation?`${states[operation.status]} · ${JSON.parse(operation.result_json).message||''}${operation.error?' · '+operation.error:''}`:'尚未执行指数采集';
el('cooldowns').textContent=data.cooldowns.map(row=>`${providers[row.source_system]||row.source_system}：${row.reason}，冷却截止 ${row.cooldown_until}`).join('；');
clearTimeout(timer);timer=setTimeout(()=>{if(!document.hidden)load(false);},running?5000:30000);}
const parentOf=row=>row.navigation_parent_index_code||row.parent_index_code;
function renderParents(){options('parent',data.catalog.filter(row=>row.index_type==='industry'&&row.industry_level===1).map(row=>[row.index_code,row.name]),el('parent').value);}
function renderCodes(){
const industry=el('kind').value==='industry',old=el('code').value;
el('parent-control').hidden=!industry;el('second-control').hidden=!industry;el('code-control').hidden=industry;
const choices=data.catalog.filter(row=>row.index_type===el('kind').value).map(row=>[row.index_code,row.name]);
const preferred=choices.some(([code])=>code===old)?old:el('kind').value==='broad'?'cn:000300':'';
options('code',choices,preferred);
if(industry){const parent=el('parent').value;options('industry-second',[['','全部二级行业'],...data.catalog.filter(row=>row.index_type==='industry'&&row.industry_level===2&&parentOf(row)===parent).map(row=>[row.index_code,row.name])],el('industry-second').value);el('code').value=el('industry-second').value||parent;}
const selected=data.catalog.find(row=>row.index_code===el('code').value);el('current-index-name').textContent=selected?.name||'暂无指数';el('current-index-code').textContent=selected?.index_code||'—';renderMetricButtons();renderSeries(old!==el('code').value);}
function renderSeries(reset=false){
const industry=el('kind').value==='industry',old=reset?'':el('series').value;
el('series-control').hidden=industry;el('industry-source').hidden=!industry;
const preferred=metricFamily==='pb'?'legu|pb.ordinary':metricFamily==='pe'?'csindex|history.rolling':'';
const rows=data.coverage.filter(row=>row.index_code===el('code').value&&(!industry||row.provider==='sws'&&row.series_key==='analysis.daily')&&fieldFor(row)&&row[fieldFor(row)+'_count']>0);
const rank=row=>`${row.provider}|${row.series_key}`===preferred?0:row.series_key.endsWith('.ordinary')?1:row.series_key==='dashboard'?3:2;
rows.sort((a,b)=>rank(a)-rank(b)||b.observations-a.observations||a.series_key.localeCompare(b.series_key));
options('series',rows.map(row=>[`${row.provider}|${row.series_key}|${row.definition_version}`,`${providers[row.provider]} · ${row.series_key==='dashboard'?'每日板块 '+metricNames[metricFamily]:seriesNames[row.series_key]||row.series_key} · ${row.frequency==='monthly'?'月度':'日度'}`]),old);
loadHistory();}
async function loadHistory(){const revision=++historyRevision;historyController?.abort();historyController=new AbortController();try{const code=el('code').value,choice=el('series').value;if(!code||!choice){history=[];views={};selectedSummary=null;options('metric',[]);el('definition').textContent='';el('coverage').textContent='该指数暂无可用的'+metricNames[metricFamily]+'数据';draw();return;}
const [provider,series,definition]=choice.split('|'),summary=data.coverage.find(row=>row.index_code===code&&row.provider===provider&&row.series_key===series&&row.definition_version===definition);selectedSummary=summary;history=[];views={};const field=fieldFor(summary);options('metric',field?[[field,labels[field]]]:[]);draw();el('coverage').textContent='正在读取所选指数…';
const key=new URLSearchParams({code,provider,series,definition,metric:field}).toString();let result;
if(chartCache.has(key)){const cached=chartCache.get(key);chartCache.delete(key);chartCache.set(key,cached);result=JSON.parse(cached.text);}else{const response=await fetch('/api/indices/chart?'+key,{cache:'no-store',signal:historyController.signal});const text=await response.text();result=JSON.parse(text);if(!response.ok)throw Error(result.error||'请求失败');if(revision!==historyRevision)return;if(result.revision!==data.revision){clearChartCache();await load(true);return;}rememberChart(key,text);}
if(revision!==historyRevision)return;history=result.has_rows?[true]:[];views=result.views||{};
for(const option of el('frequency').options)option.disabled=summary.frequency==='monthly'&&['day','week'].includes(option.value);
if(summary.frequency==='monthly'&&['day','week'].includes(el('frequency').value))el('frequency').value='month';
const definitionRow=data.definitions.find(row=>row.provider===provider&&row.series_key===series&&row.definition_version===definition),def=definitionRow?JSON.parse(definitionRow.definition_json):null;
el('definition').textContent=def?`统计方法：${methods[def.statistical_method]||def.statistical_method}；利润期间：${periods[def.profit_period]||def.profit_period}；股本口径：${shares[def.share_basis]||def.share_basis}。来源字段：${Object.entries(def.fields).map(([field,target])=>field+' → '+labels[target]).join('；')}。`:'定义待补';
draw();
}catch(error){if(error.name!=='AbortError'&&revision===historyRevision)el('coverage').textContent=error.message;}}
function draw(){
const metric=el('metric').value,years=el('range').value,view=views[metric]?.[years],yieldMetric=metric==='dividend_yield',std=el('benchmark').value==='stddev';
const frequency=selectedSummary?.frequency==='monthly'?'month':el('frequency').value==='auto'?({3:'day',5:'week',10:'month'}[years]):el('frequency').value;
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
Plotly.react('chart',[{x:rows.map(row=>row.date),y:rows.map(row=>row[target]??null),customdata:rows.map(row=>row[target+'_date']||row.date),type:'scatter',mode:'lines+markers',line:{color:'#66aaf0',width:2},marker:{size:4},fill:'tozeroy',fillcolor:'rgba(102,170,240,.07)',connectgaps:false,hoveron:'points',hovertemplate:'展示日期：%{x|%Y-%m-%d}<br>实际观察日：%{customdata}<br>'+ (labels[metric]||'估值')+'：%{y:.2f}'+(yieldMetric?'%':' 倍')+'<extra></extra>'}],{paper_bgcolor:'#1a2429',plot_bgcolor:'#1a2429',font:{color:'#edf4f4'},margin:{t:25,l:65,r:25,b:45},hovermode:'x unified',hoverdistance:-1,spikedistance:-1,hoverlabel:{bgcolor:'#23343d',bordercolor:'#49616b',font:{color:'#edf4f4'}},xaxis:{gridcolor:'#30434d'},yaxis:{title:yieldMetric?'股息率（%）':'估值（倍）',gridcolor:'#30434d',rangemode:'tozero'},shapes},{responsive:true,displaylogo:false});
}
async function load(refreshChoices=true){try{const status=await api('/api/indices/status');const changed=!data||data.revision!==status.revision;if(refreshChoices||changed){const catalog=await api('/api/indices/catalog');if(!data||catalog.revision!==data.revision)clearChartCache();data={...catalog,...status,revision:catalog.revision};renderStatus();renderParents();renderCodes();}else{Object.assign(data,status);renderStatus();}}catch(error){el('progress').textContent=error.message;}}
// Refresh versions when returning to a tab; status polling never reads all history.
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load(false);});
window.addEventListener('focus',()=>load(false));
async function collect(mode){try{await api('/api/indices/collect',{mode});await load(false);}catch(error){el('progress').textContent=error.message;}}
el('full').onclick=()=>collect('full');el('incremental').onclick=()=>collect('incremental');el('review').onclick=()=>collect('review');el('audit').onclick=()=>collect('audit');el('cancel').onclick=async()=>{try{await api('/api/indices/cancel',{});el('progress').textContent='已请求停止，等待当前请求返回后保存进度';}catch(error){el('progress').textContent=error.message;}};
el('kind').onchange=renderCodes;el('parent').onchange=()=>{el('industry-second').value='';renderCodes();};el('industry-second').onchange=renderCodes;el('code').onchange=()=>{const selected=data.catalog.find(row=>row.index_code===el('code').value);el('current-index-name').textContent=selected?.name||'暂无指数';el('current-index-code').textContent=selected?.index_code||'—';renderMetricButtons();renderSeries(true);};el('series').onchange=loadHistory;for(const id of ['range','benchmark','frequency'])el(id).onchange=draw;el('metric').onchange=draw;
const stock=new URLSearchParams(location.search).get('stock');for(const link of document.querySelectorAll('[data-view]')){const params=new URLSearchParams({tab:link.dataset.view});if(stock&&/^(?:sh|sz|bj)?\d{6}$/i.test(stock))params.set('code',stock);link.href='/?'+params;}
load();
})();
