/* Industry reads never refresh data or generate an analysis. */
(() => {
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const amount=v=>v===null||v===undefined?'待补':(v/1e8).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const pct=v=>v===null||v===undefined?'待补':`${v>=0?'+':''}${v.toFixed(2)}%`;
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
      <p class="sw-alert" id="sw-scope">全市场行业更新独立于个股刷新；每次只处理所选报告期或季度，查看页面只读本地快照。</p>
      <div class="sw-controls sw-updates"><label>更新报告期<select id="sw-update-period"></select></label><button type="button" id="sw-refresh-financial">更新行业财务</button>
      <label>市值季度<select id="sw-update-quarter"></select></label><button type="button" id="sw-refresh-cap">补齐季度市值</button>
      <label class="sw-toggle"><input id="sw-recheck-cap" type="checkbox">核对已有市值修订</label></div>
      <p id="sw-job-status" class="sw-status" role="status" aria-live="polite">财务仅更新指定报告期；市值默认补缺，固定季末交易日。</p>
      <div class="sw-controls"><label>一级行业<select id="sw-first"><option value="">全部一级</option></select></label><label>二级行业<select id="sw-second"><option value="">全部二级</option></select></label><label>三级行业<select id="sw-third"><option value="">全部三级</option></select></label>
      <label>排行层级<select id="sw-level"><option value="1">一级</option><option value="2">二级</option><option value="3" selected>三级</option></select></label>
      <label>财务口径<select id="sw-mode"><option value="ttm">滚动十二个月 TTM</option><option value="quarter">单季度</option><option value="ytd">本年累计</option><option value="annual">全年</option></select></label>
      <label>报告期<select id="sw-period"></select></label><label class="sw-toggle"><input id="sw-covered" type="checkbox" checked>仅看同比覆盖 ≥95%</label></div>
      <div id="sw-metrics" class="fs-metrics"></div>
      <section class="panel fs-panel"><h3 id="sw-title">行业趋势</h3><p id="sw-chart-note"></p><div id="sw-chart" class="sw-chart"></div><div id="sw-market"></div></section>
      <section class="panel fs-panel"><div class="sw-company-tabs" role="tablist" aria-label="行业与公司"><button type="button" id="sw-companies-tab" role="tab" aria-selected="true" aria-controls="sw-companies-panel">行业内公司 <small id="sw-company-total"></small></button><button type="button" id="sw-rank-tab" role="tab" aria-selected="false" aria-controls="sw-rank-panel" tabindex="-1">行业排行</button></div><div id="sw-rank-panel" role="tabpanel" aria-labelledby="sw-rank-tab" hidden><p id="sw-rank-note" class="sw-note"></p><div id="sw-ranking" class="sw-company-scroll"></div></div><div id="sw-companies-panel" role="tabpanel" aria-labelledby="sw-companies-tab"><div class="sw-company-info"><div id="sw-company-context"></div><input id="sw-company-search" type="search" aria-label="搜索公司名称或代码" placeholder="搜索公司名称 / 代码"></div><div id="sw-companies" class="sw-company-scroll"></div><div class="sw-company-footer"><span id="sw-company-count"></span><span>金额单位：亿元 · 点击金额或同比表头排序 · 缺失值排在末尾</span></div></div></section>
      <section class="panel fs-panel"><h3>与已保存的股票及分析对照</h3><p id="sw-compare-note"></p><div id="sw-saved" class="sw-scroll"></div></section>
      <details class="panel fs-panel sw-notes"><summary>数据来源与汇总口径</summary><div id="sw-notes"></div></details>`;
    let data=null,seq=0,controller=null,loaded=false,selected=null,parent=null,period=null,pollTimer=null;
    let companySort='revenue',companyDirection=-1;
    let rankingSort='revenue_yoy',rankingDirection=-1;
    const companyFields=[['revenue','营业收入金额'],['revenue_yoy','营业收入同比'],['parent_profit','归母利润金额'],['parent_profit_yoy','归母利润同比'],['total_cap','市值金额'],['cap_yoy','市值同比']];
    const companyValue=(company,key)=>key==='total_cap'||key==='cap_yoy'?company[key]:company.metrics?.[key.endsWith('_yoy')?key:key+'_known'];
    function renderRanking(){
      const rankValue=(row,key)=>row[key==='revenue'||key==='parent_profit'?key+'_known':key];
      const rows=(el('sw-covered').checked?data.ranking.filter(r=>r.rank_eligible):[...data.ranking]).sort((a,b)=>{
        const av=rankValue(a,rankingSort),bv=rankValue(b,rankingSort);
        if(av===null||av===undefined)return bv===null||bv===undefined?a.code.localeCompare(b.code):1;
        if(bv===null||bv===undefined)return -1;
        return (av-bv)*rankingDirection||a.code.localeCompare(b.code);
      });
      el('sw-rank-note').textContent=`${data.period} · ${el('sw-mode').selectedOptions[0].textContent} · 市值为对应${data.mode==='annual'?'年末':'季末'}快照 · 显示 ${rows.length}/${data.ranking.length} 个行业 · ${companyFields.find(([key])=>key===rankingSort)[1]}${rankingDirection===-1?'从高到低':'从低到高'}。金额单位：亿元，点击表头排序。`;
      el('sw-ranking').innerHTML=`<table class="sw-company-table"><thead><tr><th class="sw-company-name" scope="col" rowspan="2">行业</th><th colspan="2" scope="colgroup">营业收入</th><th colspan="2" scope="colgroup">归母净利润</th><th colspan="2" scope="colgroup">总市值</th></tr><tr>${companyFields.map(([key,label],i)=>`<th scope="col" class="${i%2===0?'sw-company-divider':''}" aria-sort="${rankingSort===key?(rankingDirection===-1?'descending':'ascending'):'none'}"><button type="button" data-ranking-sort="${key}" aria-label="按行业${label}排序" class="${rankingSort===key?'sw-company-active':''}">${i%2===0?'金额（亿元）':'同比'} <span>${rankingSort===key?(rankingDirection===-1?'↓':'↑'):'↕'}</span></button></th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(r=>`<tr class="${r.code===data.selected.code?'sw-company-current':''}"><td class="sw-company-name" title="营收同比可比 ${r.revenue_matched_count} 家"><button type="button" data-industry="${esc(r.code)}">${esc(r.name)}</button><small>${esc(r.code)}</small></td>${companyFields.map(([key],i)=>{const value=rankValue(r,key),missing=value===null||value===undefined;return `<td class="${i%2===0?'sw-company-divider sw-company-amount':missing?'':value>=0?'sw-up':'sw-down'}">${esc(i%2===0?amount(value):missing?'不可比':pct(value))}</td>`;}).join('')}</tr>`).join(''):'<tr><td colspan="7" class="sw-empty">此层级没有符合同比覆盖条件的行业，可取消覆盖筛选查看。</td></tr>'}</tbody></table>`;
    }
    function renderCompanies(){
      const query=el('sw-company-search').value.trim().toLowerCase();
      const companies=(data.companies||[]).filter(c=>c.name.toLowerCase().includes(query)||c.code.includes(query)).sort((a,b)=>{
        const av=companyValue(a,companySort),bv=companyValue(b,companySort);
        if(av===null||av===undefined)return bv===null||bv===undefined?a.code.localeCompare(b.code):1;
        if(bv===null||bv===undefined)return -1;
        return (av-bv)*companyDirection||a.code.localeCompare(b.code);
      });
      el('sw-companies').innerHTML=`<table class="sw-company-table"><thead><tr><th class="sw-company-name" scope="col" rowspan="2">公司</th><th colspan="2" scope="colgroup">营业收入</th><th colspan="2" scope="colgroup">归母净利润</th><th colspan="2" scope="colgroup">总市值</th></tr><tr>${companyFields.map(([key,label],i)=>`<th scope="col" class="${i%2===0?'sw-company-divider':''}" aria-sort="${companySort===key?(companyDirection===-1?'descending':'ascending'):'none'}"><button type="button" data-company-sort="${key}" aria-label="按${label}排序" class="${companySort===key?'sw-company-active':''}">${i%2===0?'金额（亿元）':'同比'} <span>${companySort===key?(companyDirection===-1?'↓':'↑'):'↕'}</span></button></th>`).join('')}</tr></thead><tbody>${companies.length?companies.map(c=>`<tr class="${c.code===state.code?'sw-company-current':''}"><td class="sw-company-name"><a href="/?code=${esc(c.code)}">${esc(c.name)}</a>${c.code===state.code?'<span class="sw-current-tag">当前股票</span>':''}<small>${esc(c.code)}</small></td>${companyFields.map(([key],i)=>{const value=companyValue(c,key),missing=value===null||value===undefined;return `<td class="${i%2===0?'sw-company-divider sw-company-amount':missing?'':value>=0?'sw-up':'sw-down'}"${key==='total_cap'?` title="市值日期 ${esc(c.cap_trade_date||'待补')}"`:''}>${esc(i%2===0?amount(value):missing?'不可比':pct(value))}</td>`;}).join('')}</tr>`).join(''):'<tr><td colspan="7" class="sw-empty">没有匹配的公司</td></tr>'}</tbody></table>`;
      el('sw-company-total').textContent=`${(data.companies||[]).length} 家`;
      el('sw-company-count').textContent=`显示 ${companies.length} 家公司 · ${companyFields.find(([key])=>key===companySort)[1]}${companyDirection===-1?'从高到低':'从低到高'}`;
    }
    const completed=[];const today=new Date(),todayText=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
    for(let year=today.getFullYear()-10;year<=today.getFullYear();year++)for(let q=1;q<=4;q++){
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
      const history=data.manifest?.scope==='all_market_history'||data.manifest?.scope==='all_market_update';
      const allMarket=history||data.manifest?.scope==='all_market_one_year';
      el('sw-scope').textContent='全市场更新使用已导入的沪深公司及申万分类名单，不包含北交所及已退市公司。财务批量核对所选报告期；市值按固定季末交易日补缺，勾选核对修订后重核已有值。任务独立于个股刷新，查看页面不采集。';
      el('sw-refresh-financial').textContent='更新全市场财务';
      el('sw-refresh-cap').textContent='补齐全市场市值';
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
      const annual=data.mode==='annual';
      const quarterPeriod=r=>`${r.quarter.slice(0,4)}-${['03-31','06-30','09-30','12-31'][Number(r.quarter.slice(-1))-1]}`;
      const revenueByPeriod=new Map(data.series.map(r=>[r.period,r]));
      const marketSeries=data.market_series.filter(r=>!annual||(r.quarter.endsWith('Q4')&&revenueByPeriod.has(quarterPeriod(r))));
      const marketByPeriod=new Map(marketSeries.map(r=>[quarterPeriod(r),r]));
      const metric=revenueByPeriod.get(data.period),market=marketSeries.at(-1),marketStyle=marketDisplay(marketSeries);
      const capSummary=data.market_summary;
      if(annual){
        marketStyle.label='年末总市值（已覆盖）';
        marketStyle.note=marketSeries.length?`市值已有 ${marketSeries.length} 个年末点（${marketSeries[0].quarter.slice(0,4)}—${market.quarter.slice(0,4)}），使用各年末最后交易日。`:'尚无对应年度的年末市值，历史待补。';
      }
      const hoverAmount=v=>v===null||v===undefined?'待补':`${amount(v)} 亿元`;
      const hoverData=period=>{
        const revenue=revenueByPeriod.get(period),cap=marketByPeriod.get(period);
        return [hoverAmount(revenue?.revenue_known),revenue?.revenue_count||0,hoverAmount(cap?.known_cap),cap?.known_count||0,cap?.trade_date||'待补'];
      };
      const hoverTemplate='报告期 %{x|%Y-%m}<br>营收 %{customdata[0]} · 覆盖 %{customdata[1]} 家<br>总市值 %{customdata[2]} · 覆盖 %{customdata[3]} 家<br>市值日期 %{customdata[4]}<extra></extra>';
      const sameIndustry=data.stock_path.some(r=>r.code===data.selected.code);
      const growthText=v=>v===null||v===undefined?'不可比':pct(v);
      const growthClass=v=>v===null||v===undefined?'':v>=0?'sw-up':'sw-down';
      const summaryCard=(title,value,growth,notes)=>`<article class="fs-metric sw-summary-card"><div class="sw-summary-heading">${esc(title)} <span>亿元</span></div><div class="sw-summary-values"><strong>${esc(amount(value))}</strong><div class="sw-summary-growth"><span>同比</span><b class="${growthClass(growth)}">${esc(growthText(growth))}</b></div></div><div class="sw-summary-bottom">${notes.map(n=>`<div>${esc(n)}</div>`).join('')}</div></article>`;
      const comparisonRow=(label,field)=>{
        const stock=sameIndustry?data.stock_metrics?.[field+'_yoy']:null,industry=metric?.[field+'_yoy'];
        const comparable=stock!==null&&stock!==undefined&&industry!==null&&industry!==undefined;
        const difference=comparable?stock-industry:null;
        return `<div class="sw-comparison-row"><div class="sw-comparison-value"><span>${label}</span><strong class="${growthClass(difference)}">${sameIndustry?(comparable?`${difference>=0?'+':''}${difference.toFixed(2)}<i>个百分点</i>`:'不可比'):'不适用'}</strong></div><small>${sameIndustry?`股票 ${esc(growthText(stock))} · 行业 ${esc(growthText(industry))}`:'当前股票不属于所选行业'}</small></div>`;
      };
      el('sw-metrics').innerHTML=summaryCard('营收合计',metric?.revenue_known,metric?.revenue_yoy,[`营收覆盖 ${metric?.revenue_count||0} 家`,`同比可比 ${metric?.revenue_matched_count||0} 家`])+
        summaryCard('归母利润合计',metric?.parent_profit_known,metric?.parent_profit_yoy,[`利润覆盖 ${metric?.parent_profit_count||0} 家`,`同比可比 ${metric?.parent_profit_matched_count||0} 家`])+
        summaryCard(annual?'年末市值合计':'季末市值合计',capSummary?.known_cap,capSummary?.yoy,[`${capSummary?.quarter||'所选季度'} · ${capSummary?.trade_date||'日期待补'} · 覆盖 ${capSummary?.known_count||0} 家`,`去年同季可比 ${capSummary?.matched_count||0} 家`])+
        `<article class="fs-metric sw-summary-card sw-summary-comparison"><div class="sw-summary-heading">当前股票相对行业</div>${comparisonRow('营收同比','revenue')}${comparisonRow('归母利润同比','parent_profit')}</article>`;
      el('sw-title').textContent=path.map(r=>r.name).join(' → ')+(annual?' · 营收与年末市值':' · 营收与季度市值');
      el('sw-chart-note').textContent=`蓝柱：${el('sw-mode').selectedOptions[0].textContent}营收；${marketSeries.length>1?'橙线':'橙点'}：${annual?'年末':'季度'}总市值。按${annual?'年度':'季度'}末对齐，实际市值交易日期见悬浮提示。${marketStyle.note} 两轴均为亿元，尺度独立。营收按当前成分回溯；市值按各季度保存的成员口径，覆盖不全为已覆盖合计。`;
      const valid=data.series.filter(r=>r.revenue_known!==null);
      if(valid.length||market){
        Plotly.react('sw-chart',[{type:'bar',name:'营收合计（已覆盖）',x:data.series.map(r=>r.period),y:data.series.map(r=>r.revenue_known===null?null:r.revenue_known/1e8),marker:{color:'#39aacf'},customdata:data.series.map(r=>hoverData(r.period)),hovertemplate:hoverTemplate},
          {type:'scatter',mode:marketStyle.mode,name:marketStyle.label,x:marketSeries.map(quarterPeriod),y:marketSeries.map(r=>r.known_cap/1e8),yaxis:'y2',line:{color:'#ec995f',width:3},marker:{size:marketSeries.length===1?12:9},customdata:marketSeries.map(r=>hoverData(quarterPeriod(r))),hovertemplate:hoverTemplate}],
          {paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:'#b8c9d0'},hovermode:'closest',margin:{l:75,r:85,t:25,b:70},legend:{orientation:'h',y:-.2},xaxis:{type:'date',tickformat:'%Y-%m',gridcolor:'#27333a'},yaxis:{title:{text:'营收 · 亿元'},gridcolor:'#27333a',rangemode:'tozero'},yaxis2:{title:{text:'总市值 · 亿元'},overlaying:'y',side:'right',showgrid:false,rangemode:'tozero'},annotations:marketSeries.length===1?[{xref:'x',yref:'y2',x:quarterPeriod(market),y:market.known_cap/1e8,text:'仅1期市值快照<br>历史待补',showarrow:true,arrowhead:2,ax:-65,ay:35,font:{color:'#ec995f',size:12}}]:[]},{responsive:true,displayModeBar:false});
      }else{Plotly.purge('sw-chart');el('sw-chart').innerHTML='<p class="sw-empty">该行业尚无可汇总的财务或市值。</p>';}
      const historyCaps=new Map((data.market_history||[]).filter(r=>!annual||r.quarter.endsWith('Q4')).map(r=>[quarterPeriod(r),r]));
      const latestFinancialPeriod=data.series.filter(r=>r.revenue_known!==null&&r.revenue_known!==undefined||r.parent_profit_known!==null&&r.parent_profit_known!==undefined).map(r=>r.period).sort().at(-1);
      const historyPeriods=latestFinancialPeriod?[...new Set([...revenueByPeriod.keys(),...historyCaps.keys()])].filter(p=>p<=latestFinancialPeriod).sort().reverse():[];
      const financialLabel=el('sw-mode').selectedOptions[0].textContent;
      const historyRows=[
        {label:'营业收入',scope:financialLabel,value:p=>revenueByPeriod.get(p)?.revenue_known,growth:p=>revenueByPeriod.get(p)?.revenue_yoy,count:p=>revenueByPeriod.get(p)?.revenue_count},
        {label:'归母净利润',scope:financialLabel,value:p=>revenueByPeriod.get(p)?.parent_profit_known,growth:p=>revenueByPeriod.get(p)?.parent_profit_yoy,count:p=>revenueByPeriod.get(p)?.parent_profit_count},
        {label:'总市值',scope:annual?'年末快照':'季末快照',value:p=>historyCaps.get(p)?.known_cap,growth:p=>historyCaps.get(p)?.yoy,count:p=>historyCaps.get(p)?.known_count,cap:true}
      ];
      el('sw-market').innerHTML=`<div class="sw-history-heading"><h4>行业历史数据</h4><span>金额：亿元 · 同比见金额下方 · 时间由新到旧</span></div>`+(historyPeriods.length?
        `<div class="sw-history-scroll" tabindex="0" role="region" aria-label="行业历史数据，可横向滚动"><table class="sw-history-table"><thead><tr><th scope="col">指标 / 亿元</th>${historyPeriods.map((p,i)=>`<th scope="col" class="${i===0?'sw-history-latest':''}">${esc(annual?p.slice(0,4):`${p.slice(0,4)}Q${Number(p.slice(5,7))/3}`)}${i===0?'<small>最新一期</small>':''}</th>`).join('')}</tr></thead><tbody>${historyRows.map(row=>`<tr><th scope="row">${esc(row.label)}<span>${esc(row.scope)}</span></th>${historyPeriods.map((p,i)=>{const value=row.value(p),growth=row.growth(p),title=`覆盖 ${row.count(p)||0} 家${row.cap?' · 市值日期 '+(historyCaps.get(p)?.trade_date||'待补'):''}`;return `<td class="${i===0?'sw-history-latest':''}" title="${esc(title)}"><strong>${esc(amount(value))}</strong><div class="sw-history-yoy ${growthClass(growth)}"><small>同比</small>${esc(growthText(growth))}</div></td>`;}).join('')}</tr>`).join('')}</tbody></table></div><p class="sw-history-footer">营收与利润按所选财务口径；市值为对应${annual?'年末':'季末'}快照。同比使用两期可比公司，缺失金额待补；悬停金额可查看覆盖家数${annual?'及年末':'及季末'}市值日期。</p>`:
        '<p class="sw-empty">暂无行业历史数据。</p>');
      renderRanking();
      el('sw-company-context').innerHTML=`<strong>${esc(path.map(r=>r.name).join(' → '))}</strong><small>财报期 ${esc(data.period)} · ${esc(financialLabel)} · 市值 ${esc(capSummary?.quarter||'待补')} ${annual?'年末':'季末'}</small>`;
      renderCompanies();
      el('sw-compare-note').textContent=`仅比较所选行业内已保存股票，财务使用相同报告期与口径。历史分析展示原摘要和日期，未重新生成。${sameIndustry?' 当前股票营收来源：'+(data.stock_provenance.revenue?.report_type||data.stock_provenance.revenue?.source||'待补')+' / '+(data.stock_provenance.revenue?.field||'待补'):''}`;
      el('sw-saved').innerHTML=data.saved_stocks.length?`<table class="sw-table"><thead><tr><th>已保存股票</th><th>营收同比</th><th>归母利润同比</th><th>最近分析</th><th>历史摘要</th></tr></thead><tbody>${data.saved_stocks.map(s=>`<tr><td><a href="/?code=${esc(s.code)}">${esc(s.code)} ${esc(s.name)}</a></td><td>${pct(s.metrics?.revenue_yoy)}</td><td>${pct(s.metrics?.parent_profit_yoy)}</td><td>${s.analysis_date?esc(new Date(s.analysis_date).toLocaleDateString('zh-CN')):'尚无分析'}</td><td class="sw-summary">${esc(s.analysis_summary||'—')}</td></tr>`).join('')}</tbody></table>`:'<p class="sw-empty">本地还没有所选行业的股票。</p>';
    }
    ['first','second','third'].forEach((name,index)=>el('sw-'+name).addEventListener('change',()=>{
      selected=el('sw-'+name).value||null;el('sw-level').value=String(index+1);
      parent=index===2?el('sw-second').value||null:index===1?el('sw-first').value||null:null;load();
    }));
    el('sw-level').addEventListener('change',()=>{selected=null;parent=null;load();});
    el('sw-mode').addEventListener('change',()=>{
      period=el('sw-period').value||null;
      if(el('sw-mode').value==='annual'&&period&&!period.endsWith('12-31')){
        period=data.periods.filter(p=>p.endsWith('12-31')&&p<=period).at(-1)||null;
      }
      load();
    });
    el('sw-period').addEventListener('change',()=>{period=el('sw-period').value||null;load();});
    el('sw-covered').addEventListener('change',()=>{if(data)render();});
    el('sw-ranking').addEventListener('click',e=>{
      const sort=e.target.closest('[data-ranking-sort]');
      if(sort){rankingDirection=sort.dataset.rankingSort===rankingSort?-rankingDirection:-1;rankingSort=sort.dataset.rankingSort;renderRanking();return;}
      const button=e.target.closest('[data-industry]');if(button){selected=button.dataset.industry;load();}
    });
    function companyTab(name){
      ['rank','companies'].forEach(item=>{const active=item===name;el(`sw-${item}-tab`).setAttribute('aria-selected',String(active));el(`sw-${item}-tab`).tabIndex=active?0:-1;el(`sw-${item}-panel`).hidden=!active;});
    }
    ['rank','companies'].forEach(name=>{
      el(`sw-${name}-tab`).addEventListener('click',()=>companyTab(name));
      el(`sw-${name}-tab`).addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const target=e.key==='Home'?'companies':e.key==='End'?'rank':name==='rank'?'companies':'rank';companyTab(target);el(`sw-${target}-tab`).focus();});
    });
    el('sw-company-search').addEventListener('input',()=>{if(data)renderCompanies();});
    el('sw-companies').addEventListener('click',e=>{const button=e.target.closest('[data-company-sort]');if(!button)return;companyDirection=button.dataset.companySort===companySort?-companyDirection:-1;companySort=button.dataset.companySort;renderCompanies();});
    async function poll(){
      try{const response=await fetch('/api/industry/status',{cache:'no-store'});const status=await response.json();if(!response.ok)throw new Error(status.error||'读取刷新状态失败');
        el('sw-job-status').textContent=status.message+(status.error?'：'+status.error:'');busy(status.running);
        if(status.running)pollTimer=setTimeout(poll,1500);else if(!status.error)await load();
      }catch(e){el('sw-job-status').textContent=e.message;busy(false);}
    }
    function busy(value){['sw-refresh-financial','sw-refresh-cap','sw-update-period','sw-update-quarter','sw-recheck-cap'].forEach(id=>{el(id).disabled=value;});}
    async function update(action,target,recheck=false){
      const scope=`已导入沪深全市场名单（${data?.universe_count||'约 5000'} 家），与当前所选行业无关`;
      const details=action==='financial_period'
        ?`更新全市场财务？\n\n报告期：${target}\n范围：${scope}\n批量核对该期营收、归母利润及来源修订，只保存新增或变化的指标。`
        :`更新全市场季度市值？\n\n季度：${target}\n范围：${scope}；已有季度使用固定季末成员名单。\n方式：${recheck?'重新核对该季已有市值及修订':'只补缺失市值，已有值跳过'}。`;
      if(!window.confirm(details+'\n\n任务可能需要数分钟，串行限频并间歇休息。不会刷新个股或其他报告期。\n确认后开始，取消则不执行。'))return;
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

