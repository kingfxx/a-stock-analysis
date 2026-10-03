/* Industry reads never refresh data or generate an analysis. */
(() => {
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const amount=v=>v===null||v===undefined?'待补':(v/1e8).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const pct=v=>v===null||v===undefined?'待补':`${v>=0?'+':''}${v.toFixed(2)}%`;
  const coverage=(n,total)=>`${n}/${total} · ${total?(n/total*100).toFixed(1):'0'}%`;
  const marketDisplay=series=>({mode:series.length>1?'lines+markers':'markers',
    label:series.length===1?'总市值快照（仅1期）':'季度总市值（已覆盖）',
    note:series.length>1?`市值已有 ${series.length} 个季度点（${series[0].quarter}—${series.at(-1).quarter}），更早季度待补。`:
      series.length===1?'市值仅有 1 个季度快照，图中为橙色点，尚不能形成趋势线；历史季度待补。':'尚无市值快照，图中只展示营收；市值历史待补。'});
  window.industryMarketDisplay=marketDisplay;
  window.initIndustry=state=>{
    const el=id=>document.getElementById(id);
    const panel=el('industry-panel');
    panel.innerHTML=`<div class="sw-head"><div><h2>申万行业</h2><p>三级成分向上汇总 · 营收景气与季度市值 · 当前股票与本地分析对照</p></div></div>
      <p id="sw-status" class="sw-status" role="status" aria-live="polite"></p><p id="sw-path" class="sw-path"></p>
      <p class="sw-alert" id="sw-scope">试运行仅补齐锂电池、乳品。行业更新独立于个股刷新；查看页面读取已保存的行业快照。</p>
      <div class="sw-controls sw-updates"><label>更新报告期<select id="sw-update-period"></select></label><button type="button" id="sw-refresh-financial">更新行业财务</button>
      <label>市值季度<select id="sw-update-quarter"></select></label><button type="button" id="sw-refresh-cap">补齐季度市值</button>
      <label class="sw-toggle"><input id="sw-recheck-cap" type="checkbox">核对已有市值修订</label></div>
      <p id="sw-job-status" class="sw-status" role="status" aria-live="polite">财务仅更新指定报告期；市值默认补缺，固定季末交易日。</p>
      <div class="sw-controls"><label>一级行业<select id="sw-first"><option value="">全部一级</option></select></label><label>二级行业<select id="sw-second"><option value="">全部二级</option></select></label><label>三级行业<select id="sw-third"><option value="">全部三级</option></select></label>
      <label>排行层级<select id="sw-level"><option value="1">一级</option><option value="2">二级</option><option value="3" selected>三级</option></select></label>
      <label>营收口径<select id="sw-mode"><option value="ttm">滚动十二个月 TTM</option><option value="quarter">单季度</option><option value="ytd">本年累计</option><option value="annual">全年</option></select></label>
      <label>报告期<select id="sw-period"></select></label><label class="sw-toggle"><input id="sw-covered" type="checkbox" checked>仅看同比覆盖 ≥95%</label></div>
      <div id="sw-metrics" class="fs-metrics"></div>
      <section class="panel fs-panel"><h3 id="sw-title">行业趋势</h3><p id="sw-chart-note"></p><div id="sw-chart" class="sw-chart"></div><div id="sw-market"></div></section>
      <section class="panel fs-panel"><h3>营收同比排行</h3><p id="sw-rank-note"></p><div id="sw-ranking" class="sw-scroll"></div></section>
      <section class="panel fs-panel"><h3>与已保存的股票及分析对照</h3><p id="sw-compare-note"></p><div id="sw-saved" class="sw-scroll"></div></section>
      <details class="panel fs-panel sw-notes"><summary>数据来源与汇总口径</summary><div id="sw-notes"></div></details>`;
    let data=null,seq=0,controller=null,loaded=false,selected=null,parent=null,period=null,pollTimer=null;
    const completed=[];const today=new Date(),todayText=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
    for(let year=today.getFullYear()-3;year<=today.getFullYear();year++)for(let q=1;q<=4;q++){
      const month=q*3,last=new Date(year,month,0).getDate(),end=`${year}-${String(month).padStart(2,'0')}-${last}`;
      if(end<todayText)completed.unshift({period:end,quarter:`${year}Q${q}`});
    }
    el('sw-update-period').innerHTML=completed.map(r=>`<option value="${r.period}">${r.period}</option>`).join('');
    el('sw-update-quarter').innerHTML=completed.map(r=>`<option value="${r.quarter}">${r.quarter}</option>`).join('');
    let updateDefaultsSet=false;
    async function load(){
      const request=++seq;controller?.abort();controller=new AbortController();
      el('sw-status').textContent='正在读取本地行业数据…';
      const query=new URLSearchParams({code:state.code,level:el('sw-level').value,mode:el('sw-mode').value});
      if(selected)query.set('industry',selected);if(parent)query.set('parent',parent);if(period)query.set('period',period);
      try{
        const response=await fetch('/api/industry?'+query,{cache:'no-store',signal:controller.signal});const result=await response.json();
        if(!response.ok)throw new Error(result.error||'读取失败');if(request!==seq)return;
        data=result;loaded=true;render();
      }catch(e){if(e.name!=='AbortError'&&request===seq)el('sw-status').textContent=e.message;}
    }
    function choices(id,items,value,label){el(id).innerHTML=`<option value="">全部${label}</option>`+items.map(r=>`<option value="${esc(r.code)}">${esc(r.name)} · ${esc(r.code)}</option>`).join('');el(id).value=value||'';}
    function render(){
      const history=data.manifest?.scope==='all_market_history';
      const allMarket=history||data.manifest?.scope==='all_market_one_year';
      el('sw-scope').textContent=history?'已补采约十年沪深股票营收、归母利润和季度市值，按当前名单回溯，缺失留空；不包含已退市股票。行业任务独立于个股刷新，页面更新按钮仍仅用于锂电池、乳品试点。':allMarket?'已采集最近一年沪深市场财务与季度市值，按当前名单回溯。TTM、同比所需的更早基期仍待补；可切换本年累计查看。行业任务独立于个股刷新，页面更新按钮仍仅用于锂电池、乳品试点。':'试运行仅补齐锂电池、乳品。行业更新独立于个股刷新；查看页面读取已保存的行业快照。';
      el('sw-refresh-financial').textContent=allMarket?'更新试点财务':'更新行业财务';
      el('sw-refresh-cap').textContent=allMarket?'补齐试点市值':'补齐季度市值';
      el('sw-notes').innerHTML=data.notes.map(n=>`<p>${esc(n)}</p>`).join('')+`<p>官方来源：<a href="https://www.swsresearch.com/swindex/pdf/SwClass2021/SwClassCode_2021.xls" target="_blank" rel="noopener">申万行业分类</a> · <a href="https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls" target="_blank" rel="noopener">股票分类及变更</a>；营收：东方财富 RPT_DMSK_FN_INCOME / 本地新浪；总市值：${allMarket?'东方财富 RPT_VALUEANALYSIS_DET（元），保留原试点来源版本':'腾讯行情字段 45'}。</p>`;
      if(data.empty){el('sw-status').textContent='尚未初始化行业分类及历史，请先导入试运行来源。';return;}
      if(!updateDefaultsSet){el('sw-update-period').value=data.period||completed[0]?.period;updateDefaultsSet=true;}
      const map=new Map(data.catalog.map(r=>[r.code,r]));let path=[],node=data.selected;
      while(node){path.unshift(node);node=map.get(node.parent_code);}
      choices('sw-first',data.catalog.filter(r=>r.level===1),path[0]?.code,'一级');
      choices('sw-second',data.catalog.filter(r=>r.level===2&&(!path[0]||r.parent_code===path[0].code)),path[1]?.code,'二级');
      choices('sw-third',data.catalog.filter(r=>r.level===3&&(!path[1]||r.parent_code===path[1].code)),path[2]?.code,'三级');
      el('sw-period').innerHTML=data.periods.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');el('sw-period').value=data.period||'';
      el('sw-path').textContent=`当前股票 ${state.code}：`+(data.stock_path.length?data.stock_path.map(r=>r.name).join(' → '):'分类待补');
      el('sw-status').textContent=`申万 2021 · ${data.catalog.filter(r=>r.level===1).length} / ${data.catalog.filter(r=>r.level===2).length} / ${data.catalog.filter(r=>r.level===3).length} 个行业 · 分类覆盖 ${data.classified_count}/${data.universe_count} · 采集 ${new Date(data.obtained_at).toLocaleString('zh-CN')}`;
      if(!data.selected)return;
      const metric=data.series.find(r=>r.period===data.period),market=data.market_series.at(-1),marketStyle=marketDisplay(data.market_series);
      const sameIndustry=data.stock_path.some(r=>r.code===data.selected.code);
      const stockGrowth=sameIndustry?data.stock_metrics?.revenue_yoy:null;
      const difference=stockGrowth!==null&&stockGrowth!==undefined&&metric?.revenue_yoy!==null&&metric?.revenue_yoy!==undefined?stockGrowth-metric.revenue_yoy:null;
      el('sw-metrics').innerHTML=[['营收合计（亿元）',amount(metric?.revenue_known),metric?`有效 ${coverage(metric.revenue_count,metric.expected_count)}${metric.revenue===null?' · 已覆盖合计':' · 完整合计'}`:'数据待补'],
        ['营收同比',pct(metric?.revenue_yoy),metric?`同一批公司 ${coverage(metric.revenue_matched_count,metric.expected_count)}`:'数据待补'],
        ['最新季度总市值（亿元）',amount(market?.known_cap),market?`${market.quarter} · ${market.trade_date} · ${coverage(market.known_count,market.expected_count)}${market.total_cap===null?' · 已覆盖合计':''}`:'本行业尚未采集市值'],
        ['当前股票相对行业',difference===null?'待补':`${difference>=0?'+':''}${difference.toFixed(2)} 个百分点`,sameIndustry?`股票 ${pct(stockGrowth)} · 行业 ${pct(metric?.revenue_yoy)}`:'当前股票不属于所选行业']].map(([title,value,note])=>`<div class="fs-metric"><span class="fs-label">${esc(title)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`).join('');
      el('sw-title').textContent=path.map(r=>r.name).join(' → ')+' · 营收与季度市值';
      el('sw-chart-note').textContent=`蓝柱：${el('sw-mode').selectedOptions[0].textContent}营收，按报告期；${data.market_series.length>1?'橙线':'橙点'}：季度总市值，按实际市值日期。${marketStyle.note} 两轴均为亿元，尺度独立。营收按当前成分回溯；市值按各季度保存的成员口径，覆盖不全为已覆盖合计。`;
      const valid=data.series.filter(r=>r.revenue_known!==null);
      if(valid.length||market){
        Plotly.react('sw-chart',[{type:'bar',name:'营收合计（已覆盖）',x:data.series.map(r=>r.period),y:data.series.map(r=>r.revenue_known===null?null:r.revenue_known/1e8),marker:{color:'#39aacf'},customdata:data.series.map(r=>coverage(r.revenue_count,r.expected_count)),hovertemplate:'报告期 %{x}<br>营收 %{y:,.2f} 亿元<br>覆盖 %{customdata}<extra></extra>'},
          {type:'scatter',mode:marketStyle.mode,name:marketStyle.label,x:data.market_series.map(r=>r.trade_date),y:data.market_series.map(r=>r.known_cap/1e8),yaxis:'y2',line:{color:'#ec995f',width:3},marker:{size:data.market_series.length===1?12:9},customdata:data.market_series.map(r=>`${r.quarter} · ${coverage(r.known_count,r.expected_count)}${r.provisional?' · 季度暂存':''}`),hovertemplate:'市值日期 %{x}<br>总市值 %{y:,.2f} 亿元<br>%{customdata}<extra></extra>'}],
          {paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:'#b8c9d0'},margin:{l:75,r:85,t:25,b:70},legend:{orientation:'h',y:-.2},xaxis:{type:'date',tickformat:'%Y-%m',gridcolor:'#27333a'},yaxis:{title:{text:'营收 · 亿元'},gridcolor:'#27333a',rangemode:'tozero'},yaxis2:{title:{text:'总市值 · 亿元'},overlaying:'y',side:'right',showgrid:false,rangemode:'tozero'},annotations:data.market_series.length===1?[{xref:'x',yref:'y2',x:market.trade_date,y:market.known_cap/1e8,text:'仅1期市值快照<br>历史待补',showarrow:true,arrowhead:2,ax:-65,ay:35,font:{color:'#ec995f',size:12}}]:[]},{responsive:true,displayModeBar:false});
      }else{Plotly.purge('sw-chart');el('sw-chart').innerHTML='<p class="sw-empty">该行业尚无可汇总的财务或市值。</p>';}
      el('sw-market').innerHTML=`<p class="sw-note">${esc(marketStyle.note)}</p>`+(data.market_series.length?`<p class="sw-note">${data.market_series.map(r=>`${esc(r.quarter)}：截至 ${esc(r.trade_date)}，${amount(r.known_cap)} 亿元${r.total_cap===null?'（覆盖不全）':''}${r.provisional?' · 季度暂存':''} · ${r.composition==='quarter_end_classification_current_universe'?'季末分类／当前股票范围':'当前成分回填'}`).join('；')}</p>`:'');
      const rows=el('sw-covered').checked?data.ranking.filter(r=>r.rank_eligible):data.ranking;
      el('sw-rank-note').textContent=`${data.period} · 按营收同比排序，符合覆盖条件的行业优先。当前显示 ${rows.length}/${data.ranking.length} 个；${history?'历史已补采，仍按同一批公司计算同比；覆盖不足的行业标记待补。':allMarket?'最近一年已批量采集，同比及 TTM 基期覆盖不足的行业仍标记待补。':'试运行覆盖两个三级行业，其余数据待补。'}`;
      el('sw-ranking').innerHTML=rows.length?`<table class="sw-table"><thead><tr><th>行业</th><th>营收合计（亿元）</th><th>营收同比</th><th>归母利润同比</th><th>同比可比覆盖</th></tr></thead><tbody>${rows.map(r=>`<tr aria-current="${r.code===data.selected.code}"><td><button type="button" data-industry="${esc(r.code)}">${esc(r.name)}</button></td><td>${amount(r.revenue_known)}${r.revenue===null?' *':''}</td><td>${pct(r.revenue_yoy)}</td><td>${pct(r.parent_profit_yoy)}</td><td>${coverage(r.revenue_matched_count,r.expected_count)}${r.rank_eligible?'':' · 待补'}</td></tr>`).join('')}</tbody></table>`:'<p class="sw-empty">此层级没有同比覆盖达到 95% 的行业。可取消覆盖筛选查看样本。</p>';
      el('sw-compare-note').textContent=`仅比较所选行业内已保存股票，财务使用相同报告期与口径。历史分析展示原摘要和日期，未重新生成。${sameIndustry?' 当前股票营收来源：'+(data.stock_provenance.revenue?.report_type||data.stock_provenance.revenue?.source||'待补')+' / '+(data.stock_provenance.revenue?.field||'待补'):''}`;
      el('sw-saved').innerHTML=data.saved_stocks.length?`<table class="sw-table"><thead><tr><th>已保存股票</th><th>营收同比</th><th>归母利润同比</th><th>最近分析</th><th>历史摘要</th></tr></thead><tbody>${data.saved_stocks.map(s=>`<tr><td><a href="/?code=${esc(s.code)}">${esc(s.code)} ${esc(s.name)}</a></td><td>${pct(s.metrics?.revenue_yoy)}</td><td>${pct(s.metrics?.parent_profit_yoy)}</td><td>${s.analysis_date?esc(new Date(s.analysis_date).toLocaleDateString('zh-CN')):'尚无分析'}</td><td class="sw-summary">${esc(s.analysis_summary||'—')}</td></tr>`).join('')}</tbody></table>`:'<p class="sw-empty">本地还没有所选行业的股票。</p>';
    }
    ['first','second','third'].forEach((name,index)=>el('sw-'+name).addEventListener('change',()=>{
      selected=el('sw-'+name).value||null;el('sw-level').value=String(index+1);
      parent=index===2?el('sw-second').value||null:index===1?el('sw-first').value||null:null;load();
    }));
    el('sw-level').addEventListener('change',()=>{selected=null;parent=null;load();});
    el('sw-mode').addEventListener('change',()=>{period=null;load();});
    el('sw-period').addEventListener('change',()=>{period=el('sw-period').value||null;load();});
    el('sw-covered').addEventListener('change',()=>{if(data)render();});
    el('sw-ranking').addEventListener('click',e=>{const button=e.target.closest('[data-industry]');if(button){selected=button.dataset.industry;load();}});
    async function poll(){
      try{const response=await fetch('/api/industry/status',{cache:'no-store'});const status=await response.json();if(!response.ok)throw new Error(status.error||'读取刷新状态失败');
        el('sw-job-status').textContent=status.message+(status.error?'：'+status.error:'');busy(status.running);
        if(status.running)pollTimer=setTimeout(poll,1500);else if(!status.error)await load();
      }catch(e){el('sw-job-status').textContent=e.message;busy(false);}
    }
    function busy(value){['sw-refresh-financial','sw-refresh-cap','sw-update-period','sw-update-quarter','sw-recheck-cap'].forEach(id=>{el(id).disabled=value;});}
    async function update(action,target,recheck=false){
      busy(true);
      try{const response=await fetch('/api/industry/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,target,recheck})});const result=await response.json();if(!response.ok)throw new Error(result.error||'刷新失败');clearTimeout(pollTimer);poll();}
      catch(e){el('sw-job-status').textContent=e.message;busy(false);}
    }
    el('sw-refresh-financial').addEventListener('click',()=>update('financial_period',el('sw-update-period').value));
    el('sw-refresh-cap').addEventListener('click',()=>update('cap_quarter',el('sw-update-quarter').value,el('sw-recheck-cap').checked));
    el('industry-tab').addEventListener('click',()=>{if(!loaded)load();else{render();window.dispatchEvent(new Event('resize'));}});
    if(!panel.hidden)load();
  };
})();
