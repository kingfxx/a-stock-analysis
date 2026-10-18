(() => {
  'use strict';
  function periodKey(day,frequency) {
    if(frequency==='day')return day;
    if(frequency==='month')return day.slice(0,7);
    const date=new Date(day+'T00:00:00Z');
    date.setUTCDate(date.getUTCDate()-(date.getUTCDay()+6)%7);
    return date.toISOString().slice(0,10);
  }
  function chartSeries(channels,pointRows,frequency,pointFrequency='daily') {
    const congestion=new Map(),points=new Map(pointRows.map(row=>[row.observation_date,row.close]));
    for(const channel of ['bigquant_calculated','api_history','html_recent_table']) {
      for(const row of channels[channel]?.rows||[]) {
        if(!congestion.has(row.observation_date)||row.value!==null)congestion.set(row.observation_date,row.value);
      }
    }
    const allDates=[...new Set([...congestion.keys(),...points.keys()])].sort(),periods=new Map();
    for(const day of allDates)periods.set(periodKey(day,frequency),day);
    // A monthly source keeps its actual observation dates, never fictitious daily/weekly closes.
    if(pointFrequency==='monthly') {
      for(const row of pointRows)periods.set(periodKey(row.observation_date,frequency),row.observation_date);
    }
    const dates=[...periods.values()];
    const pointDates=pointFrequency==='monthly'?pointRows.map(row=>row.observation_date):dates;
    return {dates,congestion:dates.map(day=>congestion.get(day)??null),pointDates,points:pointDates.map(day=>points.get(day)??null),firstCongestion:[...congestion.keys()].sort()[0]};
  }
  function leguCloseOverlay(channels) {
    const byDate=new Map();
    for(const channel of ['bigquant_calculated','api_history','html_recent_table']) {
      for(const row of channels[channel]?.rows||[]) {
        if(!byDate.has(row.observation_date)||row.close!==null)byDate.set(row.observation_date,row.close??null);
      }
    }
    const rows=[...byDate].sort(([a],[b])=>a.localeCompare(b)).map(([observation_date,close])=>({observation_date,close}));
    const valid=rows.filter(row=>row.close!==null);
    return {available:valid.length>0,provider:'legu',frequency:'daily',return_type:'congestion_close',
      first_date:valid[0]?.observation_date,last_date:valid.at(-1)?.observation_date,rows};
  }
  function dailyStatistics(channels,lower='',upper='9999-12-31') {
    const byDate=new Map();
    for(const channel of ['bigquant_calculated','api_history','html_recent_table']) {
      for(const row of channels[channel]?.rows||[]) {
        if(row.observation_date>=lower&&row.observation_date<=upper&&typeof row.value==='number'&&Number.isFinite(row.value))byDate.set(row.observation_date,row.value);
      }
    }
    const rows=[...byDate].sort(([a],[b])=>a.localeCompare(b));
    if(!rows.length)return {count:0,current:null,percentile:null,rank:-1};
    const values=rows.map(([,value])=>value).sort((a,b)=>a-b),count=values.length,current=rows.at(-1)[1];
    const quantile=fraction=>{const at=(count-1)*fraction,lo=Math.floor(at),hi=Math.ceil(at);return values[lo]+(values[hi]-values[lo])*(at-lo);};
    const mean=values.reduce((sum,value)=>sum+value,0)/count;
    const stddev=Math.sqrt(values.reduce((sum,value)=>sum+(value-mean)**2,0)/count);
    const percentile=Math.round(1000*values.filter(value=>value<current).length/count)/10;
    const rank=percentile<=5?0:percentile<=20?1:percentile<40?2:percentile<=60?3:percentile<80?4:percentile<95?5:6;
    return {count,current,currentDate:rows.at(-1)[0],firstDate:rows[0][0],percentile,rank,
      high:quantile(.8),median:quantile(.5),low:quantile(.2),mean,stddev,stddevHigh:mean+stddev,stddevLow:mean-stddev};
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={periodKey,chartSeries,leguCloseOverlay,dailyStatistics};
  if(typeof window!=='undefined')window.CongestionStats={periodKey,dailyStatistics};
  if(typeof document==='undefined')return;
  const el=id=>document.getElementById('congestion-'+id);
  const leguCloseKey='legu:congestion-close';
  let data=null,controller=null,requestNumber=0,settingsDirty=false,pollTimer=null;
  const fmt=value=>value===null||value===undefined?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2,minimumFractionDigits:2});
  const time=value=>window.DashboardTime.format(value);
  const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  async function api(url,body,signal) {
    const response=await fetch(url,{cache:'no-store',signal,...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    const result=await response.json();
    if(!response.ok)throw Error(result.error||'请求失败');
    return result;
  }
  function lowerBound() {
    if(el('range').value==='all')return '';
    const [year,month,day]=today().split('-').map(Number),target=year-Number(el('range').value);
    const last=new Date(Date.UTC(target,month,0)).getUTCDate();
    return `${target}-${String(month).padStart(2,'0')}-${String(Math.min(day,last)).padStart(2,'0')}`;
  }
  function draw() {
    if(!data)return;
    const lower=lowerBound(),upper=today();
    const stats=dailyStatistics(data.channels,lower,upper),std=el('benchmark').value==='stddev';
    const span=el('range').value==='all'?'全部历史':`近 ${el('range').value} 年`;
    const percent=value=>value===null||value===undefined?'—':fmt(value*100)+'%';
    const levels=std?[stats.stddevHigh,stats.mean,stats.stddevLow]:[stats.high,stats.median,stats.low];
    const colors=['#ed8180','#f4b560','#6ac9aa'];
    el('position').textContent=stats.count?['极低','低拥挤','偏低','适中','偏高','拥挤','极拥挤'][stats.rank]:'待判断';
    el('position').dataset.tone=stats.count?String(stats.rank):'';
    el('basis').textContent=stats.count?`${span} · 历史分位 ${stats.percentile.toFixed(1)}%`:'暂无有效分位';
    el('history-summary').textContent=stats.count?`历史：当前高于${span} ${stats.percentile.toFixed(1)}% 的日样本（${stats.count} 个）。切换展示频率和叠加指数不改变分位。`:'所选时间范围暂无有效日频样本。';
    el('comparison').textContent='按所选区间的有效日频拥挤度判断自身历史位置，不代表后续指数涨跌。'+(std&&stats.count?` 均值 ${percent(stats.mean)}，总体标准差 ${percent(stats.stddev)}。`:'');
    ['high','median','low'].forEach((id,i)=>{el(id).textContent=percent(levels[i]);el(id+'-label').textContent=std?['上限（均值 +1σ）','均值','下限（均值 −1σ）'][i]:['高拥挤（80% 分位）','中值（50% 分位）','低拥挤（20% 分位）'][i];});
    el('current').textContent=percent(stats.current);
    el('date').textContent=stats.currentDate||'暂无有效观察';
    const visible=row=>row.observation_date>=lower&&row.observation_date<=upper;
    const overlay=el('index').value===leguCloseKey?leguCloseOverlay(data.channels):data.overlay;
    const pointRows=(overlay.rows||[]).filter(visible);
    const channels=Object.fromEntries(Object.entries(data.channels).map(([key,series])=>[key,{rows:series.rows.filter(visible)}]));
    const frequency=el('frequency').value==='auto'?(el('range').value==='5'?'day':'week'):el('frequency').value;
    const series=chartSeries(channels,pointRows,frequency,overlay.frequency),dates=series.dates;
    const traces=[{x:dates,y:series.congestion,type:'scatter',mode:'lines',name:'大盘拥挤度',
      line:{color:'#66aaf0',width:2},connectgaps:false,hovertemplate:'拥挤度：%{y:.2%}<extra></extra>'}];
    const pointName=el('index').selectedOptions[0]?.textContent||'指数';
    if(overlay.available&&pointRows.some(row=>row.close!==null)) {
      traces.push({x:series.pointDates,y:series.points,type:'scatter',mode:'lines',name:pointName,yaxis:'y2',
        line:{color:'#f4d35e',width:1.8},connectgaps:false,hovertemplate:pointName+'：%{y:.2f} 点<extra></extra>'});
    }
    Plotly.react(el('chart'),traces,{paper_bgcolor:'#1a2429',plot_bgcolor:'#1a2429',font:{color:'#edf4f4'},
      uirevision:JSON.stringify([el('index').value,el('range').value,frequency]),
      margin:{l:65,r:85,t:20,b:76},hovermode:'x unified',legend:{orientation:'h',x:0.5,xanchor:'center',y:-0.2},
      xaxis:{type:'date',gridcolor:'#30434d',tickformat:'%Y-%m',hoverformat:'%Y-%m-%d',range:[lower||series.firstCongestion||dates[0]||upper,upper]},
      yaxis:{title:{text:'拥挤度（%）'},tickformat:'.0%',gridcolor:'#30434d'},
      yaxis2:{title:{text:'指数点位'},overlaying:'y',side:'right',showgrid:false,tickfont:{color:'#f4d35e'}},
      shapes:levels.flatMap((value,i)=>!Number.isFinite(value)?[]:[{type:'line',xref:'paper',yref:'y',x0:0,x1:1,y0:value,y1:value,line:{color:colors[i],dash:'dash',width:1.5}}]),
      annotations:!series.firstCongestion?[{text:'暂无本地区间数据，请先获取历史',xref:'paper',yref:'paper',x:0.5,y:0.5,showarrow:false,font:{color:'#9aadb3'}}]:[]
    },{responsive:true,displaylogo:false});
    const providers={csindex:'中证指数',sina:'新浪',sws:'申万宏源',legu:'乐咕乐股'};
    const basis={unspecified:'来源未标识收益口径',price:'价格指数',total_return:'全收益指数',net_return:'净收益指数',congestion_close:'拥挤度采集附带的收盘价'};
    el('points').textContent=overlay.available?`点位：${providers[overlay.provider]||overlay.provider} · ${overlay.frequency==='daily'?'日频':'月频'} · ${basis[overlay.return_type]||overlay.return_type} · ${overlay.first_date}—${overlay.last_date}${pointRows.some(r=>r.close!==null)?'':'；本区间暂无点位'}`:'该指数暂无本地点位数据，可通过现有指数采集入口补充。';
  }
  function renderProgress(operation=data?.operations[0]) {
    let result={};try{result=JSON.parse(operation?.result_json||'{}');}catch{}
    el('status').textContent=operation?.status==='running'?(result.message||'正在采集，等待任务完成…'):
      result.message||(data.initialized?'本地数据已就绪':'请先获取／恢复历史；初始化后启用每日检查。');
    el('full').disabled=data.running||data.initialized;
    el('full').textContent=data.initialized?'历史已初始化':'获取／恢复历史';
    el('update').disabled=data.running||!data.initialized;el('cancel').disabled=!data.running;el('save').disabled=!data.initialized;
  }
  function schedulePoll() {
    clearTimeout(pollTimer);pollTimer=null;
    if(data?.running&&!document.hidden)pollTimer=setTimeout(pollProgress,10000);
  }
  async function pollProgress() {
    if(!data)return load();
    const number=requestNumber;
    try {
      const status=await api('/api/indices/status');
      if(number!==requestNumber)return;
      data.running=status.operations.some(operation=>operation.status==='running');
      renderProgress(status.operations[0]);
      if(!data.running){await load();return;}
    }catch(error){el('status').textContent='进度读取失败：'+error.message;}
    schedulePoll();
  }
  function render() {
    const selected=el('index').value;
    const indices=[...data.broad_indices];
    if(!indices.some(row=>row.index_code==='cn:000001'))indices.unshift({index_code:'cn:000001',name:'上证指数'});
    indices.splice(indices.findIndex(row=>row.index_code==='cn:000001')+1,0,{index_code:leguCloseKey,name:'上证指数-legu'});
    el('index').replaceChildren(...indices.map(row=>{const option=document.createElement('option');option.value=row.index_code;option.textContent=row.name;return option;}));
    el('index').value=indices.some(row=>row.index_code===selected)?selected:'cn:000001';
    const latest=data.latest;
    el('current').textContent=latest?fmt(latest.value*100)+'%':'—';
    el('date').textContent=latest?latest.observation_date:'尚未获取数据';
    const supplemented=data.supplemented_dates||[];
    el('coverage').textContent=`API 有值至 ${data.channels.api_history.last_value_date||'—'}；网页有值至 ${data.channels.html_recent_table.last_value_date||'—'}。共 ${data.valid_dates} 个有值日期，${data.missing_dates.length} 个已知缺值日期；全历史覆盖率未核实。`+(supplemented.length?`其中 ${supplemented.length} 日使用 BigQuant 自算补值，原始 Legu 缺值保留；对应 close 取本地中证上证日频点数，口径可能与 Legu 有差异。`:'');
    el('gaps').textContent=data.missing_dates.length?`待补日期（${data.missing_dates.length}）：${data.missing_dates.join('、')}`:'已返回日期中暂无缺值；未返回的历史日期覆盖情况未知。';
    el('conflicts').textContent=data.conflicts.length?'来源值有差异：'+data.conflicts.map(row=>`${row.date} API ${fmt(row.values.api_history*100)}% / 网页 ${fmt(row.values.html_recent_table*100)}%`).join('；'):'';
    renderProgress();
    el('check').textContent=data.last_check?`最近请求：${time(data.last_check.obtained_at)} · ${data.last_check.parse_status==='complete'?'解析成功':'未通过解析'}${data.last_check.file_path?' · 初始化归档':' · 仅数据库记录'}`:'尚无请求记录';
    if(!settingsDirty){el('enabled').checked=!!data.schedule.enabled;el('time').value=data.schedule.local_time;}
    el('recent').replaceChildren(...data.channels.html_recent_table.rows.filter(r=>r.value!==null).slice(-30).reverse().map(row=>{
      const tr=document.createElement('tr');
      for(const value of [row.observation_date,fmt(row.value*100)+'%',fmt(row.close),fmt(row.top5_amount_yi),fmt(row.total_amount_yi)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
      return tr;
    }));
    draw();
  }
  async function load() {
    const number=++requestNumber;
    if(controller)controller.abort();controller=new AbortController();
    try {
      const code=el('index').value===leguCloseKey?'cn:000001':el('index').value;
      const result=await api('/api/indices/congestion?'+new URLSearchParams({code}),null,controller.signal);
      if(number!==requestNumber)return;
      data=result;render();
    }catch(error){if(error.name!=='AbortError')el('status').textContent='读取失败：'+error.message;}
    finally {
      if(number===requestNumber) {
        schedulePoll();
      }
    }
  }
  const notifyCollection=()=>document.dispatchEvent(new CustomEvent('index-collection-change',{detail:{source:'congestion'}}));
  async function collect(mode) {
    el('full').disabled=true;el('update').disabled=true;
    try{await api('/api/indices/collect',{datasets:['market_congestion'],providers:['legu'],mode});notifyCollection();await pollProgress();}
    catch(error){el('status').textContent=error.message;el('full').disabled=!!data?.initialized;el('update').disabled=!data?.initialized;}
  }
  el('full').onclick=()=>collect('full');el('update').onclick=()=>collect('incremental');
  el('cancel').onclick=async()=>{try{await api('/api/indices/cancel',{});notifyCollection();await pollProgress();}catch(error){el('status').textContent=error.message;}};
  el('range').onchange=draw;el('index').onchange=load;
  el('frequency').onchange=draw;
  el('benchmark').onchange=draw;
  for(const id of ['enabled','time'])el(id).onchange=()=>{settingsDirty=true;};
  el('save').onclick=async()=>{
    try{await api('/api/indices/congestion/schedule',{enabled:el('enabled').checked,local_time:el('time').value});settingsDirty=false;await load();}
    catch(error){el('status').textContent=error.message;}
  };
  document.addEventListener('visibilitychange',()=>{clearTimeout(pollTimer);pollTimer=null;if(!document.hidden)pollProgress();});
  document.addEventListener('index-collection-change',event=>{if(event.detail?.source!=='congestion')pollProgress();});
  load();
})();
