/* Financial analysis is a read-only view of the saved four-table source facts. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const scales = {'元':1, '万元':1e4, '亿元':1e8};
  const names = {fzb:'资产负债表',lrb:'利润表',llb:'现金流量表'};
  const amount = (item, selectedUnit='亿元') => {
    if (!item || item.value === null || item.value === undefined) return '待补';
    const value = item.unit === '元' ? item.value/scales[selectedUnit] : item.value;
    return value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}) +
      (item.unit === '元' ? ' '+selectedUnit : ' '+item.unit);
  };
  const sourceText = item => !item ? '待补' : [item.source ? '新浪 '+item.source : '',
    item.field, item.item_source ? '底层 '+item.item_source : '', item.period, item.method,
    item.formula, item.reason].filter(Boolean).join(' · ');
  const waterfallLabel = label => {
    const lines = {
      '其他营业损益（净额）':['其他营业损益','（净额）'],
      '营业外收支等净额':['营业外收支','等净额'],
      '营业净支出及损益':['营业净支出','及损益'],
      '其他净利润调整':['其他净利润','调整'],
      '期初现金及等价物':['期初现金及','现金等价物'],
      '期末现金及等价物':['期末现金及','现金等价物'],
      '经营净现金流':['经营活动','净现金流'],
      '投资净现金流':['投资活动','净现金流'],
      '筹资净现金流':['筹资活动','净现金流'],
    };
    return (lines[label] || (String(label).match(/.{1,6}/gu) || [''])).map(esc).join('<br>');
  };
  const waterfallTickFormat = (rows, divisor) => {
    let level=0,extent=0;
    for(const row of rows){level=row.measure==='absolute'?row.value/divisor:level+row.value/divisor;extent=Math.max(extent,Math.abs(level));}
    const decimals=extent>0?Math.min(20,Math.max(0,2-Math.floor(Math.log10(extent)))):0;
    return `,.${decimals}~f`;
  };
  const cashChangeText = (start,end,unit) => {
    if(start===null || end===null || start===undefined || end===undefined)return '';
    const change=end-start;
    const comparison=change===0?'期末与期初持平':`本期${change>0?'增加':'减少'} ${amount({value:Math.abs(change),unit:'元'},unit)}`+
      (start>0?`（${(Math.abs(change)/start*100).toFixed(2)}%）`:'');
    return `期初 ${amount({value:start,unit:'元'},unit)} → 期末 ${amount({value:end,unit:'元'},unit)}；${comparison}。`;
  };
  const sourceHTML = item => `<details class="fs-source"><summary>来源与口径</summary><p>${esc(sourceText(item))}</p>` +
    (item?.baseline_period ? `<p>差分基期 ${esc(item.baseline_period)} · ${esc(item.baseline_field)}</p>` : '') +
    (item?.inputs ? `<ul>${item.inputs.map(x=>`<li>${esc(x.label)}：${esc(sourceText(x))}</li>`).join('')}</ul>` : '')+'</details>';
  const shareText = (value,total,label) => `占${label}：`+(value!==null && value!==undefined && total>0 ? (value/total*100).toFixed(2)+'%' : '待补');
  const balanceShare = (item,values) => {
    const labels={assets:'总资产',liabilities:'总负债',equity:'股东权益'};
    const label=labels[item.balance_side];
    const total=label?values[item.balance_side]?.value:null;
    return {label:label || '适用合计',pct:item.value!==null && total>0 ? item.value/total*100:null,total};
  };
  // Export only the pure presentation helpers for the local regression tests.
  window.financialStatementFormat = {amount,sourceText,esc,waterfallLabel,waterfallTickFormat,cashChangeText,shareText,balanceShare};
  window.initFinancialStatements = state => {
    const el = id => document.getElementById(id);
    const panel = el('statements-panel');
    panel.innerHTML = `<div class="fs-head"><div><h2>财务分析</h2><p>资产结构、利润来源与现金去向 · <span id="fs-company"></span></p></div><button id="fs-reload" class="refresh" type="button">重新读取本地数据</button></div>
      <div class="fs-controls"><button id="fs-prev" type="button" disabled>上一期</button><label>报告期<select id="fs-period" aria-label="财务分析报告期"></select></label><button id="fs-next" type="button" disabled>下一期</button>
      <label id="fs-mode-label">利润与现金流口径<select id="fs-mode"><option value="ytd">本年累计</option><option value="quarter">单季度</option></select></label>
      <label>比较对象<select id="fs-comparison"><option value="year_end">上年末（流量用上年同期）</option><option value="yoy">上年同期</option><option value="previous">上一报告期</option></select></label>
      <label>金额单位<select id="fs-unit"><option>亿元</option><option>万元</option><option>元</option></select></label>
      <label id="fs-display-label">资产负债图<select id="fs-display"><option value="amount">金额</option><option value="share">占本侧合计比例</option></select></label></div>
      <div class="fs-subtabs" role="tablist" aria-label="财务报表">${Object.entries(names).map(([k,n])=>`<button type="button" role="tab" id="fs-tab-${k}" data-statement="${k}" aria-controls="fs-content" aria-selected="${k==='fzb'}" tabindex="${k==='fzb'?0:-1}">${n}</button>`).join('')}</div>
      <p id="fs-status" class="fs-status" role="status" aria-live="polite"></p><div id="fs-content" role="tabpanel" aria-labelledby="fs-tab-fzb">
      <div id="fs-metrics" class="fs-metrics"></div><div class="panel fs-panel"><h3 id="fs-chart-title"></h3><p id="fs-cash-change" hidden></p><p id="fs-chart-note"></p><div class="fs-chart-scroll"><div id="fs-chart" class="fs-chart" role="img"></div></div></div>
      <div id="fs-expense-panel" class="panel fs-panel" hidden><h3 id="fs-expense-title">期间费用占毛利润</h3><p id="fs-expense-note"></p><div id="fs-expense" class="fs-chart fs-secondary" role="img" aria-label="本期与比较期期间费用占毛利润柱形图"></div></div>
      <div class="panel fs-panel"><h3 id="fs-secondary-title"></h3><p id="fs-secondary-note"></p><div id="fs-secondary" class="fs-chart fs-secondary" role="img"></div></div>
      <div class="panel fs-panel"><h3>关键明细与比较</h3><p id="fs-table-note"></p><div id="fs-table" class="fs-table-scroll"></div></div>
      <details class="panel fs-panel fs-full"><summary>展开完整原始报表</summary><p>保留来源科目名称；单季度金额按同口径累计差分，现金期初取上季末余额。空白科目显示待补，不作为零。</p><div id="fs-full-table" class="fs-table-scroll"></div></details>
      <div class="panel fs-panel"><h3>三表关联观察</h3><p>比较上年同期，展示事实和差异。应收、存货余额变化只是线索，不能替代净利润到经营现金流的精确调节。</p><div id="fs-observations" class="fs-observations"></div></div>
      <div class="panel fs-panel"><h3>分析口径</h3><p id="fs-notes"></p></div></div>`;
    let data = null, kind = 'fzb', active = false, dirty = true, sequence = 0, controller = null, loading = false;
    function selectTab(selected) {
      active = selected === 'statements';
      try{sessionStorage.setItem('investment-research-tab-v1',selected);}catch{}
      for (const key of ['trend','statements','industry','maintenance']) {
        const id=key+'-tab';
        const on = key === selected; el(id).setAttribute('aria-selected',String(on)); el(id).tabIndex = on?0:-1;
        el(key+'-panel').hidden = !on;
      }
      if (active) { if (dirty && !loading) load(); else if (data) render(); }
      else window.dispatchEvent(new Event('resize'));
    }
    const researchNav=el('trend-tab').closest('[role="tablist"]');
    const compactNav=window.matchMedia('(max-width:800px)');
    const navOrientation=()=>researchNav.setAttribute('aria-orientation',compactNav.matches?'horizontal':'vertical');
    navOrientation();compactNav.addEventListener('change',navOrientation);
    ['trend','statements','industry','maintenance'].forEach(key=>el(key+'-tab').addEventListener('click',()=>{
      selectTab(key);el(key+'-panel').scrollIntoView({block:'start',behavior:'instant'});
    }));
    function tabKeys(buttons, activate) {
      buttons.forEach((button,index)=>button.addEventListener('keydown',event=>{
        let next;
        if (event.key==='ArrowRight') next=(index+1)%buttons.length;
        if (event.key==='ArrowLeft') next=(index+buttons.length-1)%buttons.length;
        if(buttons[0].closest('[role="tablist"]')?.getAttribute('aria-orientation')==='vertical'){
          if(event.key==='ArrowDown')next=(index+1)%buttons.length;
          if(event.key==='ArrowUp')next=(index+buttons.length-1)%buttons.length;
        }
        if (event.key==='Home') next=0;
        if (event.key==='End') next=buttons.length-1;
        if (next!==undefined) {event.preventDefault();activate(buttons[next]);buttons[next].focus();}
      }));
    }
    tabKeys(['trend','statements','industry','maintenance'].map(key=>el(key+'-tab')),button=>button.click());
    const subtabButtons = [...panel.querySelectorAll('[data-statement]')];
    subtabButtons.forEach(button=>button.addEventListener('click',()=>{
      kind=button.dataset.statement;
      subtabButtons.forEach(b=>{const on=b===button;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;});
      el('fs-content').setAttribute('aria-labelledby',button.id);
      render();
    }));
    tabKeys(subtabButtons,button=>button.click());
    async function load() {
      const request=++sequence; controller?.abort();controller=new AbortController();loading=true;dirty=false;
      el('fs-status').textContent = '正在读取本地财报…';
      const query=new URLSearchParams({code:state.code,mode:el('fs-mode').value,comparison:el('fs-comparison').value});
      if(el('fs-period').value)query.set('period',el('fs-period').value);
      try {
        const response=await fetch('/api/statements?'+query,{cache:'no-store',signal:controller.signal});
        const fresh=await response.json();
        if(request!==sequence)return;
        if(!response.ok)throw new Error(fresh.error || '读取失败');
        data=fresh;
        el('fs-period').innerHTML=fresh.periods.map(p=>`<option value="${esc(p)}">${esc(p)} · ${{'03-31':'一季报','06-30':'中报','09-30':'三季报','12-31':'年报'}[p.slice(5)]}</option>`).join('');
        el('fs-period').value=fresh.period || '';
        render();
      } catch(error) {
        if(request!==sequence || error.name==='AbortError')return;
        dirty=true;
        el('fs-status').textContent='读取失败：'+error.message+(data?'。当前保留 '+data.period+' 的上一份展示，点击重新读取重试。':'。点击重新读取重试。');
      } finally {if(request===sequence){loading=false;if(dirty&&active&&data && el('fs-status').textContent==='正在读取本地财报…')load();}}
    }
    ['fs-period','fs-mode','fs-comparison'].forEach(id=>el(id).addEventListener('change',load));
    ['fs-unit','fs-display'].forEach(id=>el(id).addEventListener('change',render));
    el('fs-reload').addEventListener('click',load);
    function move(offset){const select=el('fs-period'),index=select.selectedIndex+offset;if(index>=0&&index<select.options.length){select.selectedIndex=index;load();}}
    el('fs-prev').addEventListener('click',()=>move(1));el('fs-next').addEventListener('click',()=>move(-1));
    window.addEventListener('financial-facts-updated',event=>{if(event.detail.code===state.code){dirty=true;if(active)load();}});
    window.addEventListener('dashboard-data-state',event=>{if(event.detail.code===state.code&&!event.detail.updating){dirty=true;if(active)load();}});
    function plot(id,traces,extra={}) {
      if(!el(id)._fullLayout)el(id).replaceChildren();
      if(typeof Plotly==='undefined'){el(id).textContent='图表组件未加载，明细表仍可查看。';return;}
      Plotly.react(id,traces,{paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:'#d8e6e9',family:'Microsoft YaHei UI, Arial'},
        margin:{l:65,r:25,t:30,b:95},showlegend:false,hovermode:'closest',
        xaxis:{tickangle:-25,automargin:true},yaxis:{gridcolor:'#304049',zerolinecolor:'#56717e',title:{text:el('fs-unit').value},automargin:true},...extra},
        {responsive:true,displaylogo:false,locale:'zh-CN'});
    }
    function empty(id,message) {if(typeof Plotly!=='undefined')Plotly.purge(id);el(id).innerHTML=`<div class="fs-chart-empty">${esc(message)}</div>`;}
    function drawWaterfall(rows,title) {
      const unavailable=rows.filter(x=>x.value===null);
      if(unavailable.length){empty('fs-chart','瀑布图待补：'+unavailable.map(x=>x.label).join('、')+'。下方明细保留已有数值。');return;}
      const divisor=scales[el('fs-unit').value];
      const cashReference=kind==='llb'?{
        shapes:[{type:'line',xref:'paper',x0:0,x1:1,yref:'y',y0:rows[0].value/divisor,y1:rows[0].value/divisor,
          line:{color:'#f4d35e',width:1.5,dash:'dash'},layer:'below'}],
        annotations:[{xref:'paper',x:1,xanchor:'right',yref:'y',y:rows[0].value/divisor,
          text:'期初余额参考线',showarrow:false,yshift:18,font:{color:'#f4d35e',size:12},bgcolor:'#1a2227'}]
      }:{shapes:[],annotations:[]};
      plot('fs-chart',[{type:'waterfall',x:rows.map(x=>x.label),y:rows.map(x=>x.value/divisor),measure:rows.map(x=>x.measure),
        text:rows.map(x=>amount({...x,unit:'元'},el('fs-unit').value)),textposition:'outside',cliponaxis:false,
        customdata:rows.map(x=>esc(amount({value:x.value,unit:'元'},el('fs-unit').value))+'<br>'+esc(shareText(x.value,
          kind==='llb'?data.values.cash_start.value:data.values[data.financial_company?'revenue':'total_revenue'].value,
          kind==='llb'?'期初现金及等价物':data.financial_company?'营业收入':'营业总收入'))),
        hovertemplate:'%{x}<br>%{customdata}<extra></extra>',
        connector:{line:{color:'#68818b',dash:'dot'}},increasing:{marker:{color:'#64c6b0'}},decreasing:{marker:{color:'#d99183'}},totals:{marker:{color:'#38a9c0'}}}],
        {...cashReference,xaxis:{type:'category',tickmode:'array',tickvals:rows.map(x=>x.label),ticktext:rows.map(x=>waterfallLabel(x.label)),
          tickangle:0,automargin:true,categoryorder:'array',categoryarray:rows.map(x=>x.label)},
        yaxis:{title:{text:el('fs-unit').value},gridcolor:'#304049',zerolinecolor:'#56717e',zerolinewidth:1.5,
          rangemode:'tozero',tickformat:waterfallTickFormat(rows,divisor),exponentformat:'none',automargin:true},margin:{l:75,r:25,t:35,b:75}});
      el('fs-chart').setAttribute('aria-label',title);
    }
    function table(rows,comparison,denominator) {
      const prior=new Map(comparison.map(x=>[x.item_source+':'+x.field,x]));
      const currentUnit=el('fs-unit').value;
      return `<table class="fs-table"><thead><tr><th>科目</th><th>本期</th><th>占比</th><th>比较期</th><th>变动额</th><th>来源 / 计算</th></tr></thead><tbody>`+
        rows.map(x=>{const old=prior.get(x.item_source+':'+x.field),ratio=kind==='fzb'?balanceShare(x,data.values):null;
          const share=x.unit==='元'&&x.value!==null?(kind==='fzb'?(ratio.pct===null?'—':ratio.pct.toFixed(2)+'%'):
            denominator>0?(x.value/denominator*100).toFixed(2)+'%':'—'):'—';
          const diff=x.value!==null&&old?.value!==null&&old?.value!==undefined&&old.unit===x.unit?amount({...x,value:x.value-old.value},currentUnit):'—';
          return `<tr><td>${esc(x.label)}</td><td class="${x.value===null?'fs-missing':''}" title="${esc(x.reason || '')}">${esc(amount(x,currentUnit))}</td><td>${share}</td><td>${esc(amount(old,currentUnit))}</td><td>${esc(diff)}</td><td>${sourceHTML(x)}</td></tr>`;}).join('')+'</tbody></table>';
    }
    function balanceChart(section) {
      const valid=section.items.filter(x=>x.value!==null);
      const share=el('fs-display').value==='share';
      if(!valid.length){empty('fs-chart','缺少适用资产负债数据，请先查看明细或刷新财务数据。');return;}
      const plotted=valid.map(x=>share?balanceShare(x,data.values).pct:x.value/scales[el('fs-unit').value]);
      plot('fs-chart',[{type:'bar',x:valid.map(x=>x.label),y:plotted,
        marker:{color:valid.map(x=>x.balance_side==='liabilities'?'#d99183':x.balance_side==='equity'?'#64c6b0':'#48b3d1')},
        text:plotted.map(x=>x===null?'':x.toFixed(2)+(share?'%':'')),textposition:'outside',cliponaxis:false,
        customdata:valid.map(x=>{const ratio=balanceShare(x,data.values);return esc(amount(x,el('fs-unit').value))+'<br>'+esc(shareText(x.value,ratio.total,ratio.label));}),
        hovertemplate:'%{x}<br>%{customdata}<extra></extra>'}],
        {yaxis:{title:{text:share?'占对应合计 %':el('fs-unit').value},gridcolor:'#304049',automargin:true},margin:{l:65,r:25,t:35,b:110}});
      const codes=['TOTCURRASSET','TOTALNONCASSETS','TOTALCURRLIAB','TOTALNONCLIAB','RIGHAGGR'];
      if(data.financial_company){
        const metrics=['assets','liabilities','equity'].map(key=>data.values[key]);
        if(metrics.some(x=>x.value===null)){empty('fs-secondary','结构条待补：资产、负债或权益合计缺少适用值。');return;}
        plot('fs-secondary',metrics.map((x,i)=>({type:'bar',orientation:'h',name:x.label,y:[i===0?'资产合计':'负债与权益'],
          x:[x.value/scales[el('fs-unit').value]],marker:{color:['#48b3d1','#d99183','#64c6b0'][i]},hovertemplate:esc(x.label)+'<br>'+esc(amount(x,el('fs-unit').value))+'<br>'+esc(shareText(x.value,data.values.assets.value,i===0?'总资产':'资金来源合计'))+'<extra></extra>'})),
          {barmode:'stack',showlegend:true,legend:{orientation:'h',y:-.3},xaxis:{title:{text:el('fs-unit').value},gridcolor:'#304049'},yaxis:{},margin:{l:110,r:20,t:25,b:65}});
        return;
      }
      const values=codes.map(code=>section.full_items.find(x=>x.field===code));
      if(values.some(x=>!x||x.value===null)){empty('fs-secondary','结构条待补：流动／非流动资产、流动／非流动负债或权益合计缺少适用值。');return;}
      plot('fs-secondary',values.map((x,i)=>({type:'bar',orientation:'h',name:x.label,
        y:[i<2?'资产构成':'负债与权益'],x:[x.value/scales[el('fs-unit').value]],marker:{color:['#48b3d1','#317e9a','#d99183','#ad7167','#64c6b0'][i]},
        hovertemplate:esc(x.label)+'<br>'+esc(amount(x,el('fs-unit').value))+'<br>'+esc(shareText(x.value,data.values.assets.value,i<2?'总资产':'资金来源合计'))+'<extra></extra>'})),{barmode:'stack',showlegend:true,
          legend:{orientation:'h',y:-.3},xaxis:{title:{text:el('fs-unit').value},gridcolor:'#304049'},yaxis:{},margin:{l:110,r:20,t:25,b:65}});
    }
    function secondaryFlows(section) {
      const fields=kind==='lrb'? (data.financial_company?['NETINTEINCO','NETPOUNINCO','NETINVINCO','MANAEXPE']:['BIZCOST','BIZTAX','SALESEXPE','MANAEXPE','DEVEEXPE','FINEXPE']):['BIZCASHINFL','BIZCASHOUTF','INVCASHINFL','INVCASHOUTF','FINCASHINFL','FINCASHOUTF'];
      const prior=new Map(section.comparison_items.map(x=>[x.field,x]));
      const rows=fields.map(code=>section.full_items.find(x=>x.field===code)).filter(Boolean);
      if(!rows.some(x=>x.value!==null)){empty('fs-secondary','暂无适用明细。');return;}
      const revenue=data.values.revenue.value,oldRevenue=data.baseline_values.revenue.value;
      const useRatio=kind==='lrb'&&!data.financial_company;
      plot('fs-secondary',[[false,'本期','#48b3d1'],[true,section.baseline,'#657b8b']].map(([old,name,color])=>({type:'bar',name,x:rows.map(x=>x.label),
        y:rows.map(x=>{const value=(old?prior.get(x.field):x)?.value,r=old?oldRevenue:revenue;return value===null||value===undefined||(useRatio&&!(r>0))?null:value/(useRatio?r/100:scales[el('fs-unit').value]);}),marker:{color},
        customdata:rows.map(x=>{const item=old?prior.get(x.field):x,values=old?data.baseline_values:data.values;
          return esc(amount(item,el('fs-unit').value))+'<br>'+esc(shareText(item?.value,kind==='llb'?values.cash_start.value:values.revenue.value,kind==='llb'?'期初现金及等价物':'营业收入'));}),
        hovertemplate:'%{x}<br>'+esc(name)+'：%{customdata}<extra></extra>'})),{showlegend:true,barmode:'group',legend:{orientation:'h',y:1.15},
        yaxis:{title:{text:useRatio?'占营业收入 %':el('fs-unit').value},gridcolor:'#304049',automargin:true}});
    }
    function expenseChart(section) {
      const analysis=section.expense_analysis;
      if(!analysis){empty('fs-expense','费用分析资料待补，请重新读取本地数据。');return;}
      const current=analysis.current,previous=analysis.comparison;
      const keys=['sales_expense','management_expense','rd_expense','financial_expense','period_expenses'];
      const ratioMode=current.gross_profit.value>0;
      const ratio=(item,group)=>item.value!==null&&group.gross_profit.value>0?item.value/group.gross_profit.value*100:null;
      el('fs-expense-title').textContent=ratioMode?'期间费用占毛利润':'期间费用金额';
      el('fs-expense-note').textContent=`本期毛利润 ${amount(current.gross_profit,el('fs-unit').value)}；比较期毛利润 ${amount(previous.gross_profit,el('fs-unit').value)}。毛利润＝营业收入－营业成本。`+
        (ratioMode?'各期费用除以各期毛利润；比较期毛利润非正或缺失时不绘制其比例。':'本期毛利润非正或缺失，改为金额展示，不计算其比例。')+
        '合计为销售、管理、研发及财务费用之和；缺项则合计待补。财务费用负值表示净收益，不一定全部来自利息收入。';
      const missing=keys.filter(key=>current[key].value===null).map(key=>current[key].label);
      if(missing.length)el('fs-expense-note').textContent+=' 本期待补：'+missing.join('、')+'。';
      const oldMissing=keys.filter(key=>previous[key].value===null).map(key=>previous[key].label);
      if(oldMissing.length)el('fs-expense-note').textContent+=' 比较期待补：'+oldMissing.join('、')+'。';
      const traces=[[current,'本期 '+data.period,'#48b3d1'],[previous,'比较期 '+section.baseline,'#657b8b']].map(([group,name,color])=>({
        type:'bar',name,x:keys.map(key=>current[key].label),
        y:keys.map(key=>ratioMode?ratio(group[key],group):group[key].value===null?null:group[key].value/scales[el('fs-unit').value]),
        marker:{color},text:keys.map(key=>{const value=ratioMode?ratio(group[key],group):group[key].value;
          return value===null?'待补':ratioMode?value.toFixed(2)+'%':(value/scales[el('fs-unit').value]).toFixed(2);}),
        textposition:'outside',cliponaxis:false,
        customdata:keys.map(key=>{const item=group[key],pct=ratio(item,group),other=ratio((group===current?previous:current)[key],group===current?previous:current);
          const difference=pct!==null&&other!==null?(group===current?pct-other:other-pct):null;
          return esc(amount(item,el('fs-unit').value))+'<br>'+esc(shareText(item.value,group.gross_profit.value,'毛利润'))+
            (difference!==null?'<br>本期较比较期：'+(difference>=0?'+':'')+difference.toFixed(2)+' 个百分点':'')+
            (key==='financial_expense'&&item.value<0?'<br>负值表示净收益':'');}),
        hovertemplate:'%{x}<br>'+esc(name)+'<br>%{customdata}<extra></extra>'}));
      plot('fs-expense',traces,{barmode:'group',showlegend:true,legend:{orientation:'h',y:1.15},
        xaxis:{tickangle:0,tickmode:'array',tickvals:keys.map(key=>current[key].label),ticktext:['销售<br>费用','管理<br>费用','研发<br>费用','财务<br>费用','期间费用<br>合计'],automargin:true},
        shapes:[{type:'line',xref:'paper',yref:'paper',x0:.8,x1:.8,y0:0,y1:1,line:{color:'#304049',width:1,dash:'dot'}}],
        yaxis:{title:{text:ratioMode?'占毛利润 %':el('fs-unit').value},gridcolor:'#304049',zerolinecolor:'#8da8b3',zerolinewidth:1.5,automargin:true},
        margin:{l:75,r:25,t:50,b:65}});
    }
    function render() {
      if(!data)return;
      const index=data.periods.indexOf(data.period);
      el('fs-prev').disabled=index<0||index===data.periods.length-1;el('fs-next').disabled=index<=0;
      el('fs-company').textContent=state.code+' '+(data.name || '');
      el('fs-display-label').hidden=kind!=='fzb';
      if(!data.period){el('fs-content').hidden=true;el('fs-status').textContent='本地尚未保存三表。点击页面上方“刷新数据”获取当前股票财报。';return;}
      el('fs-content').hidden=false;
      const section=data.sections[kind],unit=el('fs-unit').value;
      el('fs-expense-panel').hidden=kind!=='lrb'||data.financial_company;
      const periodText=kind==='fzb'?'期末余额':data.mode==='quarter'?'单季度': '1—'+Number(data.period.slice(5,7))+' 月累计';
      const baselineText=kind==='fzb'?'期末余额':data.mode==='quarter'?'单季度':'1—'+Number(section.baseline.slice(5,7))+' 月累计';
      el('fs-status').textContent=`${data.period} · ${periodText} · 比较期 ${section.baseline}（${baselineText}） · ${section.scope || '合并范围待确认'} · ${section.currency || '币种待确认'}${data.financial_company?' · 金融企业口径':''}。`+section.warnings.join(' ');
      const metricKeys=kind==='fzb'?['assets','leverage','liabilities','cash']:kind==='lrb'?
        (data.financial_company?['revenue','net_profit','parent_profit','deducted_profit']:['revenue','gross_margin','parent_profit','deducted_profit']):
        (data.financial_company?['cfo','cfi','cff','cash_increase']:['cfo','cash_profit','capex','cash_after_capex']);
      el('fs-metrics').innerHTML=metricKeys.map(key=>{const x=data.values[key],old=data.baseline_values[key];
        return `<div class="fs-metric"><span class="fs-label">${esc(x.label)}</span><strong>${esc(amount(x,unit))}</strong><small>比较期：${esc(amount(old,unit))}${x.reason?'<br>'+esc(x.reason):''}</small>${sourceHTML(x)}</div>`;}).join('');
      const titles={fzb:'资产放在哪里，资金从哪里来',lrb:'从收入到合并净利润',llb:'现金为什么增加或减少'};
      el('fs-chart-title').textContent=titles[kind];
      const cashChange=kind==='llb'?cashChangeText(data.values.cash_start.value,data.values.cash_end.value,unit):'';
      el('fs-cash-change').hidden=!cashChange;
      el('fs-cash-change').textContent=cashChange;
      el('fs-chart-note').textContent=kind==='fzb'?'蓝色为资产，橙色为负债，绿色为权益。资产占总资产，负债占总负债，权益占股东权益。分项可能含母子科目，不直接加总；待补项目不绘制为零。':kind==='lrb'?
        '按报告总额展示利润形成；其他营业损益净额为营业利润减营业总收入加营业总成本，展开明细查看各项收益及减值。':'期初现金及等价物＋三类活动净现金流＋汇率影响＝期末现金及等价物。货币资金与现金及等价物分别展示。';
      el('fs-secondary-title').textContent=kind==='fzb'?'资产 = 负债 + 权益':kind==='lrb'?(data.financial_company?'金融业务关键收入与费用':'成本与费用占营业收入比例'):'三类活动的现金流入与流出';
      el('fs-secondary-note').textContent=kind==='fzb'?(data.financial_company?'金融企业按资产、负债与权益总额展示，不套用工业企业流动结构。':'流动与非流动结构；资产和负债＋权益使用同一金额坐标。'):kind==='lrb'?'对照同一期间窗口；财务费用保留原始正负号，不重复加入利息费用子项目。':'同时展示流入与流出小计，避免净额掩盖现金进出规模；负值保留来源符号。';
      if(active){if(kind==='fzb')balanceChart(section);else{drawWaterfall(data.charts[kind],titles[kind]);secondaryFlows(section);if(kind==='lrb'&&!data.financial_company)expenseChart(section);}}
      const denominator=kind==='fzb'?data.values.assets.value:kind==='lrb'?data.values.revenue.value:null;
      el('fs-table-note').textContent=`本期 ${data.period}，比较期 ${section.baseline}；${kind==='fzb'?'资产占总资产、负债占总负债、权益占股东权益':kind==='lrb'?'占比以营业收入为分母':'现金流量不统一计算占比'}。金额单位 ${unit}，每股指标单列单位。`;
      el('fs-table').innerHTML=table(section.items,section.comparison_items,denominator);
      el('fs-full-table').innerHTML=table(section.full_items,section.comparison_items,denominator);
      const obs=new Map(data.observations.map(x=>[x.key,x]));
      const grow=key=>{const x=obs.get(key);return x.growth_pct===null?'同比待补':(x.growth_pct>=0?'+':'')+x.growth_pct.toFixed(2)+'%';};
      el('fs-observations').innerHTML=[['收入与应收',`营业收入 ${grow('revenue')}；应收账款 ${grow('receivables')}`,'应收增长快于收入时，进一步核对回款、账龄与销售条件。'],
        ['收入与存货',`营业收入 ${grow('revenue')}；存货 ${grow('inventory')}`,'存货增长需结合产销、备货与减值准备解释。'],
        ['利润与现金',`合并净利润 ${grow('net_profit')}；经营净现金 ${grow('cfo')}`,data.financial_company?'金融企业经营现金受贷款、存款等影响，应结合业务结构解释。':'结合经营占款与购建支出；利润基期非正时不展示增长百分比。']].map(([title,text,note])=>`<div class="fs-observation"><strong>${title}</strong><p>${esc(text)}</p><p>${esc(note)}</p></div>`).join('');
      el('fs-notes').textContent='金额优先核对同口径关键指标，缺少适用值时回退三表。累计与单季度分别计算；余额科目不做季度差分。缺失字段、空值均不当作零，有息债务仅在组成科目完整时计算；一年内到期非流动负债可能包含非有息项目。货币资金可能含受限资金，不等于可用现金。经营净现金减购建支出不等于严格 FCFF／FCFE；投资收益等不自动认定为非经常性损益。'+(data.financial_company?'金融企业不套用工业企业毛利率及自由现金流指标，完整科目以原报表为准。':'');
    }
    try{const selected=sessionStorage.getItem('investment-research-tab-v1');if(['statements','industry','maintenance'].includes(selected))selectTab(selected);}catch{}
  };
})();
