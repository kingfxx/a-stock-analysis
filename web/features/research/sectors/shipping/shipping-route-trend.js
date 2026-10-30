(() => {
  'use strict';
  const el=id=>document.getElementById(id);
  const colors=['#126b68','#b45f35','#4566a0','#8a6aa8','#789448'];
  const metricLabels={revenue:'收入',volume:'箱量',unit_revenue:'单箱收入'};
  let data=null,loading=false,metric='revenue';
  function periods(rows,mode){
    if(!rows.length)return [];
    const numbers=rows.map(r=>mode==='annual'?Number(r.label):Number(r.label.slice(0,4))*4+Number(r.label.slice(-1))-1);
    const first=Math.min(...numbers),last=Math.max(...numbers),labels=[];
    for(let n=first;n<=last;n++)labels.push(mode==='annual'?String(n):`${Math.floor(n/4)}Q${n%4+1}`);
    return labels;
  }
  function render(){
    if(!data)return;
    const mode=el('route-revenue-basis').value,selected=data.metrics?.[metric]||(metric==='revenue'?{series:data.series,unit:'亿元'}:null),rows=selected?.series[mode]||[],unit=selected?.unit||'',x=periods(rows,mode);
    const chosen=[...el('route-revenue-filter').querySelectorAll('input:checked')].map(input=>input.value);
    const chart=el('route-revenue-chart');
    if(!x.length||!chosen.length){Plotly.purge(chart);chart.innerHTML='<div class="empty">'+(!x.length?`当前口径暂无${metricLabels[metric]}数据。`:'请选择要显示的航线。')+'</div>';el('route-revenue-table').textContent='暂无可显示的同比数据。';return;}
    if(chart.querySelector('.empty'))chart.innerHTML='';
    const traces=data.routes.filter(r=>chosen.includes(r.scope)).map(route=>{
      const byPeriod=new Map(rows.filter(r=>r.scope===route.scope).map(r=>[r.label,r]));
      const items=x.map(label=>byPeriod.get(label)),change=items.map(row=>row?.yoy==null?'同比未记录':`同比 ${row.yoy>=0?'+':''}${row.yoy.toFixed(2)}%`);
      return {type:'bar',name:route.name,x,y:items.map(row=>row?.value??null),customdata:change,
        text:items.map(row=>row?.yoy==null?'':`${row.yoy>=0?'+':''}${row.yoy.toFixed(1)}%`),textposition:'outside',textfont:{size:10},
        cliponaxis:false,marker:{color:colors[data.routes.findIndex(r=>r.scope===route.scope)]},
        hovertemplate:`${route.name}：%{y:,.2f} ${unit} · %{customdata}<extra></extra>`};
    });
    const tickStep=Math.max(1,Math.ceil(x.length/(chart.clientWidth<600?5:14))),tickvals=x.filter((_,index)=>index%tickStep===0);
    Plotly.react(chart,traces,{barmode:'group',hovermode:'x unified',hoverlabel:{namelength:0},uniformtext:{mode:'hide',minsize:10},
      margin:{l:64,r:20,t:30,b:70},paper_bgcolor:'white',plot_bgcolor:'white',
      legend:{orientation:'h',y:-0.25},xaxis:{type:'category',tickangle:-35,automargin:true,tickmode:'array',tickvals,ticktext:tickvals},
      yaxis:{title:{text:`${metricLabels[metric]}（${unit}）`},rangemode:'tozero',gridcolor:'#edf1f0',automargin:true}},
      {responsive:true,displaylogo:false});
    const names=data.routes.filter(r=>chosen.includes(r.scope));
    const lookup=new Map(rows.map(row=>[`${row.label}:${row.scope}`,row]));
    el('route-revenue-table').innerHTML='<table><thead><tr><th>报告期</th>'+names.map(r=>`<th>${r.name}</th>`).join('')+'</tr></thead><tbody>'+x.slice().reverse().map(label=>'<tr><td>'+label+'</td>'+names.map(r=>{const row=lookup.get(`${label}:${r.scope}`);return `<td>${row?.yoy==null?'—':`${row.yoy>=0?'+':''}${row.yoy.toFixed(2)}%`}</td>`;}).join('')+'</tr>').join('')+'</tbody></table>';
    el('route-revenue-status').textContent=metric==='unit_revenue'?'单箱收入＝所选期间美元航线收入 ÷ 箱量；单季度先分别差分，箱量不大于零留空。百分比为同比。':'来源：本地正式财报集团航线数据；单季度由相邻累计值差分，缺期留空。百分比为同比。';
  }
  async function load(){
    if(loading||data)return;loading=true;
    try{
      const response=await fetch('/api/shipping-route-revenues'),body=await response.json();
      if(!response.ok)throw Error(body.error||'读取航线收入失败');
      data=body;
      el('route-revenue-filter').innerHTML='<legend>显示航线</legend>'+data.routes.map(route=>`<label><input type="checkbox" value="${route.scope}" checked>${route.name}</label>`).join('');
      render();
    }catch(error){el('route-revenue-status').textContent=error.message;}
    finally{loading=false;}
  }
  el('route-revenue-basis').addEventListener('change',render);
  el('route-revenue-filter').addEventListener('change',render);
  document.querySelectorAll('[data-route-metric]').forEach(button=>button.addEventListener('click',()=>{
    metric=button.dataset.routeMetric;
    document.querySelectorAll('[data-route-metric]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
    render();
  }));
  const panel=el('trend-view');
  new MutationObserver(()=>{if(!panel.hidden){if(data){render();}else load();}}).observe(panel,{attributes:true,attributeFilter:['hidden']});
  if(!panel.hidden)load();
})();
