/* Local company preview: this module never initializes or refreshes a stock. */
(() => {
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const colors=['#38c4dc','#b9a0f5','#f4d35e'];
  const modes={quarter:'单季度',ytd:'当年累计',annual:'年度',ttm:'TTM'};
  let dialog,data,companies=[],index=0,request=0,controller,opener,mode='ttm',outsideDown=false,lastSlots=null,lastRange='10';
  const el=id=>document.getElementById('cp-'+id);
  function mount(){
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='company-preview';
    dialog.setAttribute('aria-labelledby','cp-title');
    dialog.innerHTML=`<header class="cp-header"><div><span class="cp-eyebrow">公司快览 · 本地数据</span><h2 id="cp-title"></h2></div><div class="cp-actions"><button id="cp-full" type="button">完整个股展示</button><button id="cp-prev" type="button">上一家</button><button id="cp-next" type="button">下一家</button><button id="cp-close" type="button" aria-label="关闭公司快览">关闭 ×</button></div></header>
      <div class="cp-body"><p id="cp-status" role="status" aria-live="polite"></p>
      <section class="comparison-panel"><div class="panel-head"><div><h2 id="cp-chart-title"></h2><p id="cp-chart-subtitle"></p><div class="series-key" aria-label="固定颜色图例"><span style="--series-color:#38c4dc"><i aria-hidden="true"></i>A · 青色</span><span style="--series-color:#b9a0f5"><i aria-hidden="true"></i>B · 紫色</span><span style="--series-color:#f4d35e"><i aria-hidden="true"></i>C · 黄色</span></div></div><div id="cp-controls"></div></div>
      <p id="cp-chart-scroll-hint" class="chart-scroll-hint" hidden>向右滑动可查看其他纵轴与完整图表</p><div id="cp-display"></div></section>
      <details id="cp-details"><summary>实际日期与收入依据</summary><div id="cp-dates" class="cp-table"></div></details><details class="cp-source-notes"><summary>来源与口径说明</summary><p id="cp-coverage" class="cp-note"></p><div id="cp-methods" class="cp-note"></div><div id="cp-notes"></div><div id="cp-dividends"></div></details></div>`;
    const ids = {'bar-metric':'cp-metric-0','bar-metric-2':'cp-metric-1','line-metric':'cp-metric-2',
      'comparison-range':'cp-range','chart-scroll':'cp-chart-scroll','chart':'cp-chart',
      'trend-history-scroll':'cp-table','trend-history-head':'cp-history-head','trend-history-body':'cp-history-body',
      'trend-history-footer':'cp-history-footer','trend-history-caption':'cp-history-note'};
    document.body.append(dialog);
    el('controls').innerHTML=TrendView.controls(ids);
    el('display').innerHTML=TrendView.display(ids);
    el('metric-1').parentElement.querySelector('[data-series-mode="bar"]').setAttribute('aria-pressed','true');
    [0,1,2].forEach(i=>el('metric-'+i).replaceChildren());
    el('range').value=lastRange;
    if(lastSlots)lastSlots.forEach((slot,i)=>el('metric-'+i).parentElement.querySelectorAll('[data-series-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.seriesMode===slot.type))));
    el('close').addEventListener('click',()=>dialog.close());
    const outside=event=>{const r=dialog.getBoundingClientRect();return event.target===dialog&&(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom);};
    dialog.addEventListener('pointerdown',event=>{outsideDown=outside(event);});
    dialog.addEventListener('click',event=>{if(outsideDown&&outside(event))dialog.close();outsideDown=false;});
    el('full').addEventListener('click',()=>{
      if(window.confirm(`确认打开「${companies[index].name}」的完整个股展示？首次打开可能初始化并获取完整数据。`))window.location.assign(el('full').dataset.url);
    });
    dialog.addEventListener('close',()=>{
      lastSlots=[0,1,2].map(i=>({key:el('metric-'+i).value,type:el('metric-'+i).parentElement.querySelector('[data-series-mode][aria-pressed="true"]')?.dataset.seriesMode}));
      lastRange=el('range').value;request++;controller?.abort();data=null;Plotly.purge(el('chart'));
      unbindShapes();dialog.remove();dialog=null;opener?.focus({preventScroll:true});
    });
    el('prev').addEventListener('click',()=>{index--;load();});
    el('next').addEventListener('click',()=>{index++;load();});
    el('controls').addEventListener('change',()=>draw());
    const unbindShapes=TrendView.bindShapes(el('controls'),()=>draw());
    el('controls').addEventListener('click',event=>{
      const period=event.target.closest('[data-period]');
      const next=period?.dataset.period==='year'?'annual':period?.dataset.period;
      if(next&&next!==mode){setMode(next);load();}
    });
    el('details').addEventListener('toggle',()=>{if(el('details').open&&data)renderDates();});
  }
  function setMode(value){
    mode=modes[value]?value:'ttm';
    el('controls').querySelectorAll('[data-period]').forEach(button=>{const active=button.dataset.period===(mode==='annual'?'year':mode);button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  }
  function selected(){
    return [0,1,2].map(i=>({i,metric:data.metrics.find(m=>m.key===el('metric-'+i).value),type:el('metric-'+i).parentElement.querySelector('[data-series-mode][aria-pressed="true"]')?.dataset.seriesMode})).filter(s=>s.metric&&s.type);
  }
  function visibleRows(){
    const rows=data.rows,range=el('range').value;
    if(!rows.length||range==='all')return rows;
    const latest=rows.at(-1).period,cutoff=String(Number(latest.slice(0,4))-Number(range))+latest.slice(4);
    return rows.filter(r=>r.period>cutoff);
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
    const sourceRows=visibleRows(), slots=selected();
    const normalize = row => ({...row, ...row.values, period_end_market_cap:row.values.market_cap,
      market_date:data.market[row.period]?.cap_date, price_date:data.market[row.period]?.price_date,
      pe_date:data.market[row.period]?.pe_ttm_date, pb_date:data.market[row.period]?.pb_mrq_date});
    const allRows=data.rows.map(normalize), rows=sourceRows.map(normalize);
    const metrics=Object.fromEntries(data.metrics.map(m=>{
      const key=m.key==='market_cap'?'period_end_market_cap':m.key;
      return [key,{name:m.label,historyName:m.label+(m.unit==='亿元'?'':'（'+m.unit+'）'),unit:m.unit,scale:m.unit==='亿元'?1e8:1,
        dateKey:({market_cap:'market_date',price:'price_date',pe_ttm:'pe_date',pb_mrq:'pb_date'})[m.key]}];
    }));
    const active=slots.map(s=>{const key=s.metric.key==='market_cap'?'period_end_market_cap':s.metric.key;
      return {label:'ABC'[s.i],color:colors[s.i],kind:s.type,key,metric:metrics[key],
        values:rows.map(row=>row[key]==null?null:row[key]/metrics[key].scale)};});
    const selectedPeriod=mode==='annual'?'year':mode;
    const label=row=>selectedPeriod==='year'?row.period.slice(0,4):row.period.slice(0,4)+' Q'+Math.ceil(Number(row.period.slice(5,7))/3);
    const positions=items=>{let gap=0;return items.map((row,i)=>{if(['quarter','ytd'].includes(selectedPeriod)&&i&&row.period.slice(0,4)!==items[i-1].period.slice(0,4))gap+=.48;return i+gap;});};
    const elements={scroll:el('chart-scroll'),hint:el('chart-scroll-hint'),title:el('chart-title'),subtitle:el('chart-subtitle'),chart:el('chart'),
      head:el('history-head'),body:el('history-body'),footer:el('history-footer'),caption:el('history-note')};
    const {showQoq,comparisonText,periodOffsets}=TrendView.draw({elements,rows,allRows,active,selectedPeriod,metrics,label,positions,
      isPriceOrCap:key=>key==='price',hasMarketComparisons:key=>['price','period_end_market_cap','pe_ttm','pb_mrq','annual_dividend_yield'].includes(key),includeDisclosureDates:false,emptyDataMessage:'所选指标暂无可用数据'});
    TrendView.history({elements,rows,active,selectedPeriod,label,showQoq,comparisonText,periodOffsets,emptyDataMessage:'当前范围暂无历史数据',marketComparisonLabel:'行情／估值'});
    el('coverage').textContent=slots.map(s=>`${s.metric.label}：${rows.filter(r=>r.values[s.metric.key]!==null&&r.values[s.metric.key]!==undefined).length}/${rows.length}期`).join(' · ')+`。${data.missing_reason} 仅1期时显示单点。`;
    el('methods').innerHTML=slots.map(s=>`<p><b>${esc(s.metric.label)}</b>：${esc(s.metric.method)}</p>`).join('');
    renderDividendPlans();
    if(el('details').open)renderDates();else el('dates').replaceChildren();
  }
  async function load(){
    if(index<0||index>=companies.length)return;
    const company=companies[index],token=++request;controller?.abort();controller=new AbortController();data=null;
    el('title').textContent=company.name+' '+company.code;
    el('full').dataset.url='/?code='+encodeURIComponent(company.code)+'&tab=trend';
    el('prev').disabled=index===0;el('next').disabled=index===companies.length-1;
    el('status').textContent='正在读取本地数据…';el('controls').querySelectorAll('select,[data-series-mode]').forEach(n=>n.disabled=true);
    Plotly.purge(el('chart'));el('history-head').replaceChildren();el('history-body').replaceChildren();el('dates').replaceChildren();el('dividends').replaceChildren();el('history-note').textContent='';el('coverage').textContent='';el('methods').replaceChildren();el('notes').replaceChildren();
    try{
      const response=await fetch('/api/industry-company-preview?'+new URLSearchParams({code:company.code,mode}),{signal:controller.signal});
      const result=await response.json();if(token!==request||!dialog.open)return;
      if(!response.ok)throw new Error(result.error||'读取失败');
      data=result;
      [0,1,2].forEach(i=>{
        const select=el('metric-'+i),key=select.options.length?select.value:lastSlots?.[i]?.key??['profit','','market_cap'][i];
        select.innerHTML='<option value="">不展示</option>'+data.metrics.map(m=>`<option value="${esc(m.key)}">${esc(m.label)}</option>`).join('');
        select.value=data.metrics.some(m=>m.key===key)?key:'';
      });
      el('controls').querySelectorAll('select,[data-series-mode]').forEach(n=>n.disabled=false);
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
