/* Read-only presentation of saved MAINOP facts. Ratios are source decimals. */
(() => {
  'use strict';
  const fields = {revenue:'收入',cost:'成本',gross_profit:'毛利'};
  const shares = {revenue:'revenue_share',cost:'cost_share',gross_profit:'profit_share'};
  const dimensions = {'1':'行业','2':'产品','3':'地区'};
  const frequencies = {'12-31':'年报','06-30':'中报','03-31':'一季报','09-30':'三季报'};
  const colors = ['#38c4dc','#f4d35e','#72c9b3','#b79be8','#f09a75','#729fec','#df91ba','#9dbb73','#c9aa7c','#7cbdcb','#c4b9ee','#b7c6cf'];
  const totals = new Set(['合计','总计','主营业务合计','主营收入合计','主营业务收入合计','营业收入合计','营业成本合计','主营业务成本合计']);
  const valid = value => typeof value === 'number' && Number.isFinite(value);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => valid(value) ? value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
  const percent = value => valid(value) ? number(value*100)+'%' : '—';
  const exclusion = row => totals.has(row.item_name.trim()) ? '合计，未绘图' : /^其中\s*[:：]/.test(row.item_name.trim()) ? '其中明细，未与主项叠加' : '';
  const items = (period,dimension) => (period?.dimensions?.[dimension] || []).filter(row=>!exclusion(row));
  function growth(current, previous) {
    return valid(current) && valid(previous) && previous>0 ? (current/previous-1)*100 : null;
  }
  function periodsFor(periods, frequency, range) {
    const actual=periods.filter(p=>p.report_date.slice(5)===frequency).sort((a,b)=>a.report_date.localeCompare(b.report_date));
    if(!actual.length)return [];
    const last=Number(actual.at(-1).report_date.slice(0,4));
    const first=Math.max(Number(actual[0].report_date.slice(0,4)),last-Number(range)+1);
    // Empty calendar slots break lines; they never create synthetic facts.
    const byDate=new Map(actual.map(p=>[p.report_date,p]));
    return Array.from({length:last-first+1},(_,i)=>byDate.get(`${first+i}-${frequency}`)||{report_date:`${first+i}-${frequency}`,dimensions:{},missing:true});
  }
  function catalog(periods,dimension) {
    const result=new Map();
    for(const p of periods)for(const row of items(p,dimension))result.set(row.item_key,row);
    return [...result.values()].sort((a,b)=>a.item_key.localeCompare(b.item_key));
  }
  function series(periods, dimension, field, keys, scale=1) {
    return catalog(periods,dimension).filter(row=>keys.has(row.item_key)).map(row=>({
      key:row.item_key,name:row.item_name,
      source_names:periods.map(p=>{const found=items(p,dimension).find(r=>r.item_key===row.item_key);return found?(found.source_items||[found]).map(r=>r.item_name).join(' ＋ '):'—';}),
      methods:periods.map(p=>items(p,dimension).find(r=>r.item_key===row.item_key)?.calculated_fields?.includes(field)?(field==='gross_margin'?'自行计算：完整毛利合计÷收入合计':'同地区分项相加（全部参与项有效）'):'来源原值'),
      x:periods.map(p=>p.report_date),
      y:periods.map(p=>{const value=items(p,dimension).find(r=>r.item_key===row.item_key)?.[field];return valid(value)?value/scale:null;})
    }));
  }
  function yoySeries(periods, allPeriods, dimension, field, keys) {
    const recent=periods.filter(p=>!p.missing).slice(-3);
    const byDate=new Map(allPeriods.map(p=>[p.report_date,p]));
    const categories=catalog(periods,dimension).filter(row=>keys.has(row.item_key)&&recent.some(p=>{const previous=byDate.get(`${Number(p.report_date.slice(0,4))-1}${p.report_date.slice(4)}`);return valid(items(p,dimension).find(r=>r.item_key===row.item_key)?.[field])&&valid(items(previous,dimension).find(r=>r.item_key===row.item_key)?.[field])&&items(previous,dimension).find(r=>r.item_key===row.item_key)?.[field]>0;}));
    return recent.map(p=>{
      const previous=byDate.get(`${Number(p.report_date.slice(0,4))-1}${p.report_date.slice(4)}`);
      return {name:p.report_date,x:categories.map(r=>r.item_name),customdata:categories.map(row=>{const now=items(p,dimension).find(r=>r.item_key===row.item_key),before=items(previous,dimension).find(r=>r.item_key===row.item_key);const names=r=>r?(r.source_items||[r]).map(x=>x.item_name).join(' ＋ '):'—';return [names(now),names(before)];}),y:categories.map(row=>growth(
        items(p,dimension).find(r=>r.item_key===row.item_key)?.[field],
        items(previous,dimension).find(r=>r.item_key===row.item_key)?.[field]))};
    });
  }
  window.businessSegmentPresentation={items,growth,periodsFor,catalog,series,yoySeries,percent,esc};

  window.initBusinessSegments = state => {
    const el=id=>document.getElementById(id);
    const panel=el('business-panel');
    panel.innerHTML=`<div class="fs-head"><div><h2>主营构成</h2><p>业务规模、结构与盈利能力 · <span id="bs-company"></span></p></div><button id="bs-reload" class="refresh" type="button">重新读取本地数据</button></div>
      <div class="bs-results">
      <div class="bs-controls" aria-label="筛选条件">
        <label>分类维度<select id="bs-dimension"><option value="2">按产品</option><option value="1">按行业</option><option value="3">按地区</option></select></label>
        <label>报告口径<select id="bs-frequency"></select></label>
        <label>时间范围<select id="bs-range"><option value="10">最近10年</option><option value="5">最近5年</option><option value="3">最近3年</option></select></label>
        <label>分析指标<select id="bs-field"><option value="revenue">收入</option><option value="cost">成本</option><option value="gross_profit">毛利</option></select></label>
        <label>名称口径<select id="bs-name-mode"><option value="normalized">统一名称</option><option value="raw">原始名称</option></select></label>
        <label>金额单位<select id="bs-unit"><option value="100000000">亿元</option><option value="10000">万元</option><option value="1">元</option></select></label>
      </div>
      <p id="bs-status" class="fs-status" role="status" aria-live="polite"></p>
      <div id="bs-content" hidden>
        <div id="bs-summary" class="bs-summary"></div>
        <details class="bs-projects"><summary id="bs-project-caption">选择展示项目</summary><div id="bs-project-options"></div><p>项目选择联动规模、占比、同比与毛利率。隐藏项目不重新计算占比；地区概览保留完整地区项目。</p></details>
        <details class="bs-projects bs-mappings"><summary id="bs-mapping-caption">名称归并依据</summary><div id="bs-mappings" class="bs-table-scroll"></div></details>
        ${[['scale','分项收入趋势'],['share','收入结构变化'],['yoy','分项收入同比'],['margin','分业务毛利率'],['region','地区收入分布']].map(([key,title])=>`<section id="bs-${key}-panel" class="panel bs-panel"><div class="panel-head"><div><h3 id="bs-${key}-title">${title}</h3><p id="bs-${key}-note"></p></div>${key==='region'?'<label id="bs-region-mode-label" class="bs-region-control">地区图展示<select id="bs-region-mode"><option value="amount">金额</option><option value="share">占比</option></select></label>':''}</div><div class="bs-scroll-hint">窄屏可横向滑动查看完整图表</div><div class="bs-chart-scroll"><div id="bs-${key}" class="bs-chart" role="img" aria-label="${title}"></div></div>${key==='scale'?'<details class="bs-history"><summary>展开历史完整明细</summary><div id="bs-history" class="bs-table-scroll"></div></details>':''}</section>`).join('')}
        <details class="panel bs-explanation"><summary>来源与分析口径</summary><p id="bs-provenance"></p><p>毛利（来源称主营利润）不是净利润。单个来源项目的收入、成本、毛利、三种占比及毛利率均取原值；已确认同地区互斥业务分项汇总时，金额和占比须各分项完整才相加，毛利率以完整毛利合计÷收入合计计算，并在悬浮中标注；不以收入减成本补齐毛利，不将缺失比例显示为零，也不将占比归一化。</p><p>每次仅展示同类累计报告期，不做单季度差分或TTM。同比＝（本期÷上年同期－1）×100%；统一名称模式按展示归并身份匹配，原始模式按来源代码和名称匹配；基期非正或缺少任一期时不可比。只使用名称规范与明确别名，未确认的更名不拼接，同一期冲突整组不归并。图线在缺项处断开。项目可能含抵销或分组小计，保留负值；图表排除明确命名的整维度合计及以“其中:”或“其中：”标识的下级明细，完整表格仍保留；其他分类层级不自动猜测，不据此推算公司总收入。未展示的其他累计报告期仍保存在本地。</p><p>金额按来源元值换算所选单位；已验收样本601919、605499按人民币元解释，其他公司的币种须核对正式报告。时间范围按所选报告类型最新已存年份向前计算。</p></details>
      </div>`;
    const sidebarFilters=el('business-filter-details');
    sidebarFilters.querySelector('.business-filter-body').append(panel.querySelector('.bs-controls'));
    const initialBusinessActive=!panel.hidden;sidebarFilters.hidden=!initialBusinessActive;el('trend-tab').closest('.shell').classList.toggle('business-filters-active',initialBusinessActive);
    let data=null,rawData=null,nameView=null,dirty=true,sequence=0,controller=null,selected=new Set(),selectionDimension=null;
    const active=()=>!panel.hidden;
    const title=key=>el(`bs-${key}-title`);
    const note=key=>el(`bs-${key}-note`);
    const dateLabel=date=>date.slice(0,4)+(frequencies[date.slice(5)]||' '+date.slice(5));
    const colorMap=dimension=>new Map(catalog(data.periods,dimension).map((r,i)=>[r.item_key,colors[i%colors.length]]));
    function plot(id,traces,unit,mode='relative') {
      const target=el(id);
      target.setAttribute('aria-label',el(id+'-title').textContent);
      const hasValues=traces.some(t=>t.y.some(valid));
      if(typeof Plotly==='undefined') {target.textContent='图表组件未加载，请查看明细表。';return;}
      Plotly.react(target,hasValues?traces:[],{
        paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{family:'Microsoft YaHei UI, Arial, sans-serif',color:'#9aadb3',size:12},
        margin:{l:72,r:25,t:25,b:95},barmode:mode,hovermode:'closest',
        hoverlabel:{bgcolor:'#202e35',bordercolor:'#526b78',font:{color:'#edf4f4',size:13},namelength:-1},
        xaxis:{type:'category',gridcolor:'#304049',automargin:true},
        yaxis:{title:{text:unit},gridcolor:'#304049',zerolinecolor:'#6c808b',tickformat:',.2~f',automargin:true},
        legend:{orientation:'h',y:-0.25,x:0,font:{size:11}},
        annotations:hasValues?[]:[{text:'当前选择暂无有效数据，请调整筛选或查看明细',xref:'paper',yref:'paper',x:0.5,y:0.5,showarrow:false,font:{color:'#9aadb3'}}]
      },{responsive:true,displaylogo:false,modeBarButtonsToRemove:['select2d','lasso2d']});
    }
    function traces(periods,dimension,field,keys,scale,percentMode=false,line=false) {
      const colorsByKey=colorMap(dimension);
      return series(periods,dimension,field,keys,scale).map(s=>({
        name:esc(s.name),x:s.x.map(dateLabel),y:s.y,type:line?'scatter':'bar',mode:line?'lines+markers':undefined,
        customdata:s.source_names.map((name,i)=>{const row=items(periods[i],dimension).find(r=>r.item_key===s.key);const amount=valid(row?.[el('bs-field').value])?number(row[el('bs-field').value]/Number(el('bs-unit').value))+' '+el('bs-unit').selectedOptions[0].text:'—';return [amount,percent(row?.[shares[el('bs-field').value]])];}),
        connectgaps:false,marker:{color:colorsByKey.get(s.key)},line:{color:colorsByKey.get(s.key),width:2},
        hovertemplate:`${esc(s.name)}<br>%{x}<br>%{y:,.2f}${percentMode?'%':el('bs-unit').selectedOptions[0].text}<br>${percentMode?'金额：%{customdata[0]}':'占比：%{customdata[1]}'}<extra></extra>`
      }));
    }
    function table(periods,dimension) {
      const scale=Number(el('bs-unit').value),unit=el('bs-unit').selectedOptions[0].text;
      return `<table class="bs-table"><thead><tr><th>报告期／项目</th><th>收入（${unit}）</th><th>收入占比</th><th>成本（${unit}）</th><th>成本占比</th><th>毛利（${unit}）</th><th>毛利占比</th><th>毛利率</th></tr></thead><tbody>${[...periods].reverse().map(p=>{
        const rows=p.source_dimensions?.[dimension]||p.dimensions?.[dimension]||[];
        return `<tr class="bs-report-row"><th colspan="8">${esc(dateLabel(p.report_date))}${p.missing?' · 无本地记录':` · 修订${esc(p.revision)} · 取得于 ${esc(p.obtained_at)}`}</th></tr>`+
          (rows.length?rows.map(r=>`<tr><th>${esc(r.item_name)}${(()=>{const unified=p.source_dimensions&&(p.dimensions[dimension]||[]).find(x=>(x.source_items||[]).some(source=>source.item_key===r.item_key));return unified&&unified.item_name!==r.item_name?' <span class="bs-alias-label">→ '+esc(unified.item_name)+'</span>':'';})()}${exclusion(r)?'（'+exclusion(r)+'）':''}</th><td>${number(valid(r.revenue)?r.revenue/scale:null)}</td><td>${percent(r.revenue_share)}</td><td>${number(valid(r.cost)?r.cost/scale:null)}</td><td>${percent(r.cost_share)}</td><td>${number(valid(r.gross_profit)?r.gross_profit/scale:null)}</td><td>${percent(r.profit_share)}</td><td>${percent(r.gross_margin)}</td></tr>`).join(''):'<tr><td colspan="8">该报告期无此分类记录</td></tr>');
      }).join('')}</tbody></table>`;
    }
    function setProjects(periods,dimension,reset=false) {
      const rows=catalog(periods,dimension);
      if(reset || selectionDimension!==dimension) {
        // A bounded initial legend; explicit selection always allows every project.
        const ranked=[...rows].sort((a,b)=>Math.abs(b.revenue??0)-Math.abs(a.revenue??0));
        selected=new Set(ranked.slice(0,rows.length>10?8:rows.length).map(r=>r.item_key));
        selectionDimension=dimension;
      }
      const mapping=colorMap(dimension);
      el('bs-project-options').innerHTML='<button type="button" data-bs-selection="all">全选</button><button type="button" data-bs-selection="none">清空</button>'+rows.map((r,index)=>`<label><input type="checkbox" data-bs-project="${index}" ${selected.has(r.item_key)?'checked':''}><span style="background:${mapping.get(r.item_key)}" class="bs-swatch"></span>${esc(r.item_name)}</label>`).join('');
      el('bs-project-caption').textContent=`展示项目 ${rows.filter(r=>selected.has(r.item_key)).length} / ${rows.length} · 点击调整`;
      el('bs-project-options').querySelectorAll('[data-bs-project]').forEach(input=>input.addEventListener('change',()=>{const key=rows[Number(input.dataset.bsProject)].item_key;input.checked?selected.add(key):selected.delete(key);render();}));
      el('bs-project-options').querySelectorAll('[data-bs-selection]').forEach(button=>button.addEventListener('click',()=>{selected=new Set(button.dataset.bsSelection==='all'?rows.map(r=>r.item_key):[]);render();}));
    }
    function render(reset=false) {
      if(!data || !active())return;
      const dimension=el('bs-dimension').value,field=el('bs-field').value;
      const periods=periodsFor(data.periods,el('bs-frequency').value,el('bs-range').value);
      const actual=periods.filter(p=>!p.missing),latest=actual.at(-1);
      el('bs-content').hidden=!latest;
      if(!latest){el('bs-status').textContent='本地暂无所选报告类型的数据。可切换报告口径；本页不会自动采集。';return;}
      setProjects(periods,dimension,reset);
      renderMappings();
      const scale=Number(el('bs-unit').value),unit=el('bs-unit').selectedOptions[0].text;
      const leading=items(latest,dimension).filter(r=>valid(r.revenue)&&r.revenue>0).sort((a,b)=>b.revenue-a.revenue)[0];
      el('bs-summary').innerHTML=[['最大收入项目',leading?.item_name||'—',leading?number(leading.revenue/scale)+' '+unit:'暂无有效收入'],['该项目收入占比',percent(leading?.revenue_share),leading?.calculated_fields?.includes('revenue_share')?'分项披露占比汇总，未归一化':'来源披露占比，未重新归一化'],['该项目毛利率',percent(leading?.gross_margin),leading?.calculated_fields?.includes('gross_margin')?'同地区完整毛利合计÷收入合计':'毛利不是净利润；缺失显示 —']].map(([label,value,caption])=>`<div class="bs-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(caption)}</small></div>`).join('');
      const context=`${dimensions[dimension]}分类 · ${dateLabel(periods[0].report_date)}—${dateLabel(latest.report_date)}`;
      const missingFields=actual.flatMap(p=>items(p,dimension)).filter(r=>!valid(r[field])).length;
      el('bs-status').textContent=`当前 ${dateLabel(latest.report_date)} · ${actual.length}个实际报告期 · ${dimensions[dimension]}分类${missingFields?` · ${fields[field]}有${missingFields}处缺失，图表留空`:''}。${(data.warnings||[]).map(w=>el('bs-name-mode').value==='normalized'?w.replace('跨期分类未自动映射','未确认的分类更名不自动映射'):w).join(' ')}`;
      title('scale').textContent=`分项${fields[field]}趋势`;
      note('scale').textContent=`${context} · 金额${unit}。正负分别堆叠，抵销项保留；合计及明确“其中”明细只保留在表格，不重复叠加。缺失分项不当作零，柱高不代表公司完整合计。`;
      plot('bs-scale',traces(periods,dimension,field,selected,scale),unit);
      title('share').textContent=`${fields[field]}结构变化`;
      note('share').textContent='采用来源占比，负抵销保留，不强制合计100%；缺失、隐藏项目和分类层级差异均可能影响可见合计。';
      plot('bs-share',traces(periods,dimension,shares[field],selected,0.01,true),'%');
      title('yoy').textContent=`分项${fields[field]}同比`;
      note('yoy').textContent=`最近三个已存同类报告期，纵轴为${unit}金额；悬浮查看金额、同比变化及占比。基期非正或缺失时不可比；三期都不可比的项目隐藏。颜色区分报告期。`;
      const comparePeriods=periods.filter(p=>!p.missing).slice(-3),allByDate=new Map(data.periods.map(p=>[p.report_date,p]));
      const yoyTraces=catalog(periods,dimension).filter(row=>selected.has(row.item_key)).map((item,index)=>{
        const points=comparePeriods.map(p=>{const previous=allByDate.get(`${Number(p.report_date.slice(0,4))-1}${p.report_date.slice(4)}`);const current=items(p,dimension).find(r=>r.item_key===item.item_key);const before=items(previous,dimension).find(r=>r.item_key===item.item_key);return {period:p,current,growth:growth(current?.[field],before?.[field])};});
        if(!points.some(point=>valid(point.growth)))return null;
        return {name:esc(item.item_name),x:points.map(point=>dateLabel(point.period.report_date)),y:points.map(point=>valid(point.current?.[field])?point.current[field]/scale:null),customdata:points.map(point=>[valid(point.current?.[field])?number(point.current[field]/scale)+' '+unit:'—',valid(point.growth)?(point.growth>0?'+':'')+number(point.growth)+'%':'—',percent(point.current?.[shares[field]])]),type:'bar',marker:{color:colorMap(dimension).get(item.item_key)||colors[index%colors.length]},hovertemplate:'%{x}<br>'+esc(item.item_name)+'<br>金额：%{customdata[0]}<br>同比：%{customdata[1]}<br>占比：%{customdata[2]}<extra></extra>'};
      }).filter(Boolean);
      plot('bs-yoy',yoyTraces,unit,'group');
      note('margin').textContent=`${context} · 单个项目用来源毛利率；同地区多业务汇总用完整毛利÷收入，缺项断线。颜色与规模图中的业务一致。`;
      plot('bs-margin',traces(periods,dimension,'gross_margin',selected,0.01,true,true),'%');
      title('region').textContent=`地区${fields[field]}分布`;
      const regionShare=el('bs-region-mode').value==='share';
      note('region').textContent=`${dateLabel(latest.report_date)} · 按当前名称口径展示实际披露的地区项目；切换金额或占比，年份与范围跟随上方筛选。地区与产品、行业不可跨维度相加，缺失值留空。`;
      plot('bs-region',traces(periods,'3',regionShare?shares[field]:field,new Set(catalog(periods,'3').map(row=>row.item_key)),regionShare?0.01:scale,regionShare),regionShare?'%':unit);
      el('bs-history').innerHTML=table(periods,dimension);
      el('bs-provenance').textContent=`来源：东方财富 RPT_F10_FN_MAINOP，本地已存当前修订。最新所选报告期 ${latest.report_date}，修订 ${latest.revision}，实际取得时间 ${latest.obtained_at}。历史明细保留整维度合计及原始分类名称。`;
    }
    function applyNames() {
      data={...rawData,periods:el('bs-name-mode').value==='normalized'?nameView.periods:rawData.periods};
    }
    function renderMappings() {
      const normalized=el('bs-name-mode').value==='normalized';
      el('bs-mapping-caption').textContent=normalized?`名称归并依据 · ${nameView.mappings.length}组映射 · ${nameView.conflicts.length}处同期间冲突`:'当前使用原始名称 · 查看可用归并依据';
      el('bs-mappings').innerHTML='<table class="bs-table"><thead><tr><th>维度／统一名称</th><th>原始名称与处理依据</th></tr></thead><tbody>'+nameView.mappings.map(r=>`<tr><th>${dimensions[r.dimension]} · ${esc(r.name)}</th><td class="bs-mapping-description">${r.original_names.map(esc).join(' ／ ')}<small>${r.reasons.map(esc).join('；')}${r.aggregated?'；存在同地区分项相加，明细仍保留原值':''}</small></td></tr>`).join('')+nameView.conflicts.map(r=>`<tr><th>${dimensions[r.dimension]} · ${esc(r.name)}</th><td class="bs-mapping-description">${esc(r.report_date)}：${r.original_names.map(esc).join(' ／ ')}<small>${esc(r.reason)}</small></td></tr>`).join('')+'</tbody></table><p>规则版本 '+esc(nameView.version)+'。归并是展示口径，不证明历史经济范围完全不变；未确认的重组和更名不按相似度拼接。完整明细保留原始名称，箭头表示统一名称。</p>';
    }
    async function load() {
      const request=++sequence;controller?.abort();controller=new AbortController();dirty=false;
      el('bs-status').textContent='正在读取本地主营构成…';
      el('bs-reload').disabled=true;
      try {
        const response=await fetch('/api/business-segments?'+new URLSearchParams({code:state.code}),{cache:'no-store',signal:controller.signal});
        const fresh=await response.json();if(request!==sequence)return;
        if(!response.ok)throw new Error(fresh.error||'读取失败');
        rawData=fresh;
        nameView=window.businessSegmentNames.normalize(fresh.code,fresh.periods);
        applyNames();
        el('bs-company').textContent=`${data.name||''} ${data.code}`;
        const available=Object.keys(frequencies).filter(f=>data.periods.some(p=>p.report_date.endsWith(f)));
        const previous=el('bs-frequency').value;
        el('bs-frequency').innerHTML=available.map(f=>`<option value="${f}">${frequencies[f]}</option>`).join('');
        el('bs-frequency').value=available.includes(previous)?previous:available.includes('12-31')?'12-31':data.periods.map(p=>p.report_date).sort().at(-1)?.slice(5)||'';
        if(!data.periods.some(p=>items(p,el('bs-dimension').value).length)) {
          el('bs-dimension').value=['2','1','3'].find(d=>data.periods.some(p=>items(p,d).length))||'2';
        }
        render(true);
      } catch(error) {
        if(request!==sequence||error.name==='AbortError')return;
        dirty=true;el('bs-status').textContent=`读取失败：${error.message}。${data?'保留上次展示；':''}点击重新读取本地数据重试。`;
      } finally {if(request===sequence)el('bs-reload').disabled=false;}
    }
    el('bs-name-mode').addEventListener('change',()=>{if(rawData){applyNames();render(true);}});
    ['bs-dimension','bs-frequency','bs-range'].forEach(id=>el(id).addEventListener('change',()=>render(true)));
    ['bs-field','bs-unit','bs-region-mode'].forEach(id=>el(id).addEventListener('change',()=>render()));
    el('bs-reload').addEventListener('click',load);
    window.addEventListener('research-tab-changed',event=>{const selectedTab=event.detail.tab;sidebarFilters.hidden=selectedTab!=='business';el('trend-tab').closest('.shell').classList.toggle('business-filters-active',selectedTab==='business');if(selectedTab==='business'){if(dirty)load();else render();}});
    ['financial-facts-updated','dashboard-data-state'].forEach(name=>window.addEventListener(name,event=>{if(event.detail.code===state.code&&!event.detail.updating){dirty=true;if(active())load();}}));
    if(active())load();
  };
})();
