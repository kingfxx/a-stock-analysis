/* Local company preview: this module never initializes or refreshes a stock. */
(() => {
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const colors=['#38c4dc','#b9a0f5','#f4d35e'];
  const modes={quarter:'单季度',ytd:'当年累计',annual:'年度',ttm:'TTM'};
  let dialog,data,companies=[],index=0,request=0,controller,opener,mode='ttm',outsideDown=false;
  const el=id=>document.getElementById('cp-'+id);
  function mount(){
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='company-preview';
    dialog.setAttribute('aria-labelledby','cp-title');
    dialog.innerHTML=`<header class="cp-header"><div><span class="cp-eyebrow">公司快览 · 本地数据</span><h2 id="cp-title"></h2></div><div class="cp-actions"><button id="cp-full" type="button">完整个股展示</button><button id="cp-prev" type="button">上一家</button><button id="cp-next" type="button">下一家</button><button id="cp-close" type="button" aria-label="关闭公司快览">关闭 ×</button></div></header>
      <div class="cp-body"><p id="cp-status" role="status" aria-live="polite"></p><div id="cp-controls" class="cp-controls">
      ${['A','B','C'].map((slot,i)=>`<div class="metric-choice"><div class="metric-choice-head"><label class="metric-choice-name" for="cp-metric-${i}">指标 ${slot}</label><div id="cp-type-${i}" class="metric-mode-group" role="group" aria-label="指标 ${slot} 表现形式">${[['bar','柱形'],['scatter','折线']].map(([type,label])=>`<button type="button" class="metric-mode" data-cp-slot="${i}" data-cp-type="${type}" aria-pressed="${type===(i===2?'scatter':'bar')}" aria-controls="cp-chart">${label}</button>`).join('')}</div></div><select id="cp-metric-${i}" aria-label="指标 ${slot}"></select></div>`).join('')}
      <div id="cp-mode" class="period" role="group" aria-label="财务口径">${Object.entries(modes).map(([key,label])=>`<button type="button" data-cp-mode="${key}" aria-pressed="false">${label}</button>`).join('')}</div><label class="cp-range-control"><span class="metric-choice-name">时间范围</span><select id="cp-range"><option value="5">最近5年</option><option value="10" selected>最近10年</option><option value="all">全部</option></select></label></div>
      <div class="cp-chart-scroll" role="region" aria-label="公司财务趋势，窄屏可横向滚动" tabindex="0"><div id="cp-chart" class="cp-chart"></div></div><p class="cp-mobile-note">图表可左右滑动查看</p>
      <section class="cp-history" aria-labelledby="cp-history-title"><div class="cp-history-heading"><h3 id="cp-history-title">历史数据</h3><span id="cp-history-note"></span></div><div id="cp-table" class="cp-history-scroll trend-history-scroll" tabindex="0" role="region" aria-label="历史数据，报告期从新到旧，可横向滚动"></div></section>
      <details id="cp-details"><summary>实际日期与收入依据</summary><div id="cp-dates" class="cp-table"></div></details><details class="cp-source-notes"><summary>来源与口径说明</summary><p id="cp-coverage" class="cp-note"></p><div id="cp-methods" class="cp-note"></div><div id="cp-notes"></div><div id="cp-dividends"></div></details></div>`;
    document.body.append(dialog);
    el('close').addEventListener('click',()=>dialog.close());
    const outside=event=>{const r=dialog.getBoundingClientRect();return event.target===dialog&&(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom);};
    dialog.addEventListener('pointerdown',event=>{outsideDown=outside(event);});
    dialog.addEventListener('click',event=>{if(outsideDown&&outside(event))dialog.close();outsideDown=false;});
    el('full').addEventListener('click',()=>{
      if(window.confirm(`确认打开「${companies[index].name}」的完整个股展示？首次打开可能初始化并获取完整数据。`))window.location.assign(el('full').dataset.url);
    });
    dialog.addEventListener('close',()=>{request++;controller?.abort();data=null;Plotly.purge(el('chart'));opener?.focus({preventScroll:true});});
    el('prev').addEventListener('click',()=>{index--;load();});
    el('next').addEventListener('click',()=>{index++;load();});
    el('controls').addEventListener('change',()=>draw());
    el('controls').addEventListener('click',event=>{
      const shape=event.target.closest('[data-cp-type]');
      if(shape){const active=shape.getAttribute('aria-pressed')==='true';el('type-'+shape.dataset.cpSlot).querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button===shape&&!active)));draw();return;}
      const period=event.target.closest('[data-cp-mode]');
      if(period&&period.dataset.cpMode!==mode){setMode(period.dataset.cpMode);load();}
    });
    el('details').addEventListener('toggle',()=>{if(el('details').open&&data)renderDates();});
  }
  function setMode(value){
    mode=modes[value]?value:'ttm';
    el('mode').querySelectorAll('button').forEach(button=>{const active=button.dataset.cpMode===mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  }
  function selected(){
    return [0,1,2].map(i=>({i,metric:data.metrics.find(m=>m.key===el('metric-'+i).value),type:el('type-'+i).querySelector('[aria-pressed="true"]')?.dataset.cpType})).filter(s=>s.metric&&s.type);
  }
  function visibleRows(){
    const rows=data.rows,range=el('range').value;
    if(!rows.length||range==='all')return rows;
    const latest=rows.at(-1).period,cutoff=String(Number(latest.slice(0,4))-Number(range))+latest.slice(4);
    return rows.filter(r=>r.period>cutoff);
  }
  function renderTable(){
    const slots=selected(),rows=[...visibleRows()].reverse(),all=new Map(data.rows.map(r=>[r.period,r]));
    const showQoq=key=>mode!=='ytd'||['market_cap','price','pe_ttm','pb_mrq','annual_dividend_yield'].includes(key);
    const change=(row,key,months)=>{
      const date=new Date(row.period+'T00:00:00Z');date.setUTCDate(1);date.setUTCMonth(date.getUTCMonth()-months);
      const target=new Date(Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+1,0)).toISOString().slice(0,10);
      const current=row.values[key],previous=all.get(target)?.values[key];
      if(current==null||previous==null)return '<span title="当前值或比较期缺失">—</span>';
      if(previous<=0)return '<span title="基期非正，不计算百分比">—</span>';
      const value=(current/previous-1)*100;
      return `<span class="${value>0?'positive':value<0?'negative':''}">${value.toFixed(1)}%</span>`;
    };
    const label=period=>mode==='annual'?period.slice(0,4):period.slice(0,4)+' Q'+Math.ceil(Number(period.slice(5,7))/3);
    el('history-note').textContent=`口径：${modes[mode]} · 金额：亿元 · ${mode==='ytd'?'财务仅同比，行情／估值另列环比':'同比／环比并排'} · 时间由新到旧`;
    el('table').innerHTML=slots.length&&rows.length?`<table class="trend-history-table"><thead><tr><th scope="col">指标 / 单位（金额：亿元）</th>${rows.map((r,i)=>`<th scope="col"${i===0?' class="trend-history-latest"':''}>${esc(label(r.period))}${i===0?'<small>最新一期</small>':''}</th>`).join('')}</tr></thead><tbody>${slots.map(({metric})=>`<tr><th scope="row"><div class="trend-history-metric-name">${esc(metric.label)}${metric.unit==='亿元'?'':'（'+esc(metric.unit)+'）'}</div><div class="trend-history-row-labels">${showQoq(metric.key)?'同比 / 环比':'同比'}</div></th>${rows.map((r,i)=>{
      const value=r.values[metric.key],text=value==null?'—':(value/(metric.unit==='亿元'?1e8:1)).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
      return `<td${i===0?' class="trend-history-latest"':''}><strong>${esc(text)}</strong><div class="trend-history-change">${change(r,metric.key,12)}${showQoq(metric.key)?'<span class="trend-history-separator">/</span>'+change(r,metric.key,mode==='annual'?12:3):''}</div></td>`;
    }).join('')}</tr>`).join('')}</tbody></table>`:`<p class="cp-note">${slots.length?'当前范围暂无历史数据':'请选择至少一个指标'}</p>`;
  }
  function renderDates(){
    const rows=[...visibleRows()].reverse();
    el('dates').innerHTML=`<table><thead><tr><th>报告期</th><th>市值观察日</th><th>实际股价日</th><th>PE观察日</th><th>PB观察日</th><th>利润率收入依据</th></tr></thead><tbody>${rows.map(r=>{
      const market=data.market[r.period]||{},basis=data.sources[r.period]?.methods?.margin_revenue;
      return `<tr><th>${esc(r.period)}</th><td>${esc(market.cap_date||'—')}</td><td>${esc(market.price_date||'—')}</td><td>${esc(market.pe_ttm_date||'—')}</td><td>${esc(market.pb_mrq_date||'—')}</td><td>${basis==='TOTAL_OPERATE_INCOME'?'营业总收入':basis==='OPERATE_INCOME'?'营业收入（缺失时留空）':'—'}</td></tr>`;
    }).join('')}</tbody></table>`;
  }
  function renderDividendPlans(){
    if(!selected().some(s=>s.metric.key==='annual_dividend_yield')){el('dividends').replaceChildren();return;}
    const years=[...new Set(visibleRows().map(r=>String(Number(r.period.slice(0,4))-1)))].sort().reverse();
    el('dividends').innerHTML=`<h4>年度分红方案合计（税前元／股）</h4><p>本季使用上一自然年归属方案。未提供方案的Q1／Q3未计入合计；中报、年报和四期报告覆盖须完整。合计仍可能缺特别分红；含送转或无法解析时留空。</p><div class="cp-table"><table><thead><tr><th>归属年</th><th>每股合计</th><th>已存组成方案</th><th>校验说明</th></tr></thead><tbody>${years.map(year=>{
      const info=data.dividend_years?.[year],cash=info?.total_per_share;
      const plans=(info?.plans||[]).filter(p=>p.assigndscrpt).map(p=>`${esc(p.report_date)}：${esc(p.assigndscrpt)}`).join('<br>');
      return `<tr><th>${esc(year)}</th><td>${cash==null?'—':esc(cash.toLocaleString('zh-CN',{maximumFractionDigits:6}))}</td><td class="cp-plan-description">${plans||'—'}</td><td class="cp-plan-description">${esc(info?.reason||'缺少该年度方案')}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function draw(){
    if(!data||!dialog.open)return;
    const rows=visibleRows(),slots=selected(),traces=[],layout={
      paper_bgcolor:'#192328',plot_bgcolor:'#192328',font:{color:'#dce8ed'},
      margin:{t:65,l:85,r:slots.length===3?145:85,b:85},height:530,barmode:'group',
      hovermode:'x unified',legend:{orientation:'h',y:1.13,x:0},
      xaxis:{type:'category',domain:[0,slots.length===3?.88:1],tickangle:-55,gridcolor:'#304149',nticks:20},
      annotations:[]
    };
    slots.forEach(({i,metric,type},position)=>{
      const axis=position?'y'+(position+1):'y',axisKey=position?'yaxis'+(position+1):'yaxis';
      const values=rows.map(r=>r.values[metric.key]===null||r.values[metric.key]===undefined?null:r.values[metric.key]/(metric.unit==='亿元'?1e8:1));
      const count=values.filter(v=>v!==null).length;
      traces.push({x:rows.map(r=>r.period),y:values,type,mode:count===1?'markers':'lines+markers',
        name:`${'ABC'[i]} ${type==='bar'?'柱形':'折线'} · ${metric.label}`,yaxis:axis,offsetgroup:String(i),connectgaps:false,
        marker:{color:colors[i],size:5},line:{color:colors[i],width:2.5},
        hovertemplate:`${esc(metric.label)}：%{y:,.2f} ${esc(metric.unit)}<extra></extra>`});
      layout[axisKey]={title:{text:metric.label+'（'+metric.unit+'）',font:{color:colors[i],size:12}},
        tickfont:{color:colors[i]},gridcolor:'#304149',showgrid:position===0,zerolinecolor:'#52616a',automargin:true,rangemode:'tozero',
        ...(position?{overlaying:'y',side:'right'}:{}),...(position===2?{anchor:'free',position:1}:{})};
    });
    if(traces.length>1){
      // Match the main chart: reserve the same positive/negative fraction on every axis.
      const extents=traces.map(trace=>{
        const values=trace.y.filter(Number.isFinite),positive=Math.max(0,...values),negative=Math.max(0,...values.map(v=>-v));
        return {positive,negative,scale:Math.max(positive,negative)||1};
      });
      const positiveFraction=Math.max(...extents.map(e=>e.positive/e.scale));
      const negativeFraction=Math.max(...extents.map(e=>e.negative/e.scale));
      extents.forEach((extent,index)=>{
        const axis=layout[index?'yaxis'+(index+1):'yaxis'];
        axis.range=[-extent.scale*negativeFraction*1.12,extent.scale*(positiveFraction||(negativeFraction?0:1))*1.12];
        axis.autorange=false;
      });
    }
    if(!traces.length||traces.every(t=>t.y.every(v=>v===null)))layout.annotations=[{text:slots.length?'所选指标暂无可用数据':'请选择至少一个指标',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false}];
    Plotly.react(el('chart'),traces,layout,{responsive:true,displayModeBar:false});
    el('coverage').textContent=slots.map(s=>`${s.metric.label}：${rows.filter(r=>r.values[s.metric.key]!==null&&r.values[s.metric.key]!==undefined).length}/${rows.length}期`).join(' · ')+`。${data.missing_reason} 仅1期时显示单点。`;
    el('methods').innerHTML=slots.map(s=>`<p><b>${esc(s.metric.label)}</b>：${esc(s.metric.method)}</p>`).join('');
    renderDividendPlans();
    renderTable();
    if(el('details').open)renderDates();else el('dates').replaceChildren();
  }
  async function load(){
    if(index<0||index>=companies.length)return;
    const company=companies[index],token=++request;controller?.abort();controller=new AbortController();data=null;
    el('title').textContent=company.name+' '+company.code;
    el('full').dataset.url='/?code='+encodeURIComponent(company.code)+'&tab=trend';
    el('prev').disabled=index===0;el('next').disabled=index===companies.length-1;
    el('status').textContent='正在读取本地数据…';el('controls').querySelectorAll('select,[data-cp-type]').forEach(n=>n.disabled=true);
    Plotly.purge(el('chart'));el('table').replaceChildren();el('dates').replaceChildren();el('dividends').replaceChildren();el('history-note').textContent='';el('coverage').textContent='';el('methods').replaceChildren();el('notes').replaceChildren();
    try{
      const response=await fetch('/api/industry-company-preview?'+new URLSearchParams({code:company.code,mode}),{signal:controller.signal});
      const result=await response.json();if(token!==request||!dialog.open)return;
      if(!response.ok)throw new Error(result.error||'读取失败');
      data=result;
      [0,1,2].forEach(i=>{
        const select=el('metric-'+i),key=select.options.length?select.value:['profit','','market_cap'][i];
        select.innerHTML='<option value="">不展示</option>'+data.metrics.map(m=>`<option value="${esc(m.key)}">${esc(m.label)}</option>`).join('');
        select.value=data.metrics.some(m=>m.key===key)?key:'';
      });
      el('controls').querySelectorAll('select,[data-cp-type]').forEach(n=>n.disabled=false);
      const financial=data.financial_periods,market=data.market_periods;
      el('status').textContent=`财务 ${financial.length?financial[0]+' — '+financial.at(-1):'暂无'} · 季末行情 ${market.length?market[0]+' — '+market.at(-1):'暂无'} · ${index+1}/${companies.length} 家`+(data.financial_company?' · 已隐藏金融企业不适用指标':'');
      el('notes').innerHTML=data.notes.map(note=>`<p>${esc(note)}</p>`).join('');draw();
    }catch(error){if(token===request&&error.name!=='AbortError')el('status').textContent='读取失败：'+error.message;}
  }
  window.openIndustryCompanyPreview=(code,list,initialMode,trigger)=>{
    mount();companies=list.map(c=>({code:c.code,name:c.name}));index=companies.findIndex(c=>c.code===code);if(index<0)return;
    opener=trigger||document.activeElement;setMode(initialMode);
    if(!dialog.open)dialog.showModal();el('close').focus();load();
  };
})();
