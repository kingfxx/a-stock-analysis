/* Read-only saved facts; all assumptions and valuations are calculated locally. */
(() => {
  'use strict';
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = text => String(text ?? '').trim() === '' ? null : Number(text);
  const waccNumber = value => finite(value) ? Number(value.toFixed(3)).toString() : '';
  const waccDecimal = value => finite(value) ? Number(value.toFixed(3)).toString() : '—';
  const parameterError = '基准FCF或增长／折现参数缺失或无效。';
  function forecastStandard(base, growth) {
    return Array.from({length:10}, (_,i)=>base*Math.pow(1+growth/100,i+1));
  }
  function forecastThreeStage(base, stage1, stage2, stage3) {
    let previous=base;
    return Array.from({length:10},(_,i)=>{
      previous*=1+(i<3?stage1:i<7?stage2:stage3)/100;
      return previous;
    });
  }
  function discountCashFlows(fcfs, discount, terminal, shares=null, price=null) {
    if (!Array.isArray(fcfs)||fcfs.length!==10||!Array.from(fcfs).every(finite)||![discount,terminal].every(finite)) return {error:parameterError};
    if (discount<=-100||terminal<=-100) return {error:'折现率和永续增长率须大于 −100%。'};
    if (discount<=terminal) return {error:'折现率必须大于永续增长率。'};
    const r=discount/100,gt=terminal/100;
    const rows=fcfs.map((fcf,i)=>{
      const year=i+1,factor=1/Math.pow(1+r,year);
      return {year,fcf,factor,pv:fcf*factor};
    });
    const explicit=rows.reduce((sum,row)=>sum+row.pv,0);
    const terminalValue=rows[9].fcf*(1+gt)/(r-gt),terminalPV=terminalValue*rows[9].factor;
    const total=explicit+terminalPV;
    if (![explicit,terminalValue,terminalPV,total,...rows.flatMap(row=>[row.fcf,row.factor,row.pv])].every(finite)) return {error:'参数导致数值溢出，请缩小假设范围。'};
    const perShare=finite(shares)&&shares>0 ? total/shares : null;
    const priceGap=finite(perShare)&&perShare>0&&finite(price)&&price>0 ? 1-price/perShare : null;
    if ((perShare!==null&&!finite(perShare))||(priceGap!==null&&!finite(priceGap))) return {error:'每股计算数值溢出，请核对股本与参数。'};
    return {rows,explicit,terminalValue,terminalPV,total,perShare,priceGap,terminalRatio:total!==0?terminalPV/total:null};
  }
  function calculate(base, growth, discount, terminal, shares=null, price=null) {
    if (![base,growth,discount,terminal].every(finite)) return {error:parameterError};
    if (growth<=-100||discount<=0||terminal<=-100) return {error:'增长率和永续增长率须大于 −100%，折现率须大于 0%。'};
    return discountCashFlows(forecastStandard(base,growth),discount,terminal,shares,price);
  }
  function calculateGrowth(base, stage1, stage2, stage3, discount, terminal, shares=null, price=null) {
    if (![base,stage1,stage2,stage3,discount,terminal].every(finite)) return {error:parameterError};
    if ([stage1,stage2,stage3].some(g=>g<=-100)) return {error:'各阶段增长率须大于 −100%。'};
    return discountCashFlows(forecastThreeStage(base,stage1,stage2,stage3),discount,terminal,shares,price);
  }
  function wacc(values) {
    if (!['rf','beta','erp','cost','tax','weight'].every(key=>finite(values[key]))) return {error:'请完整填写六项资本成本假设，不将缺项视为零。'};
    if (values.rf < -5||values.rf>20||values.beta < -10||values.beta>10||values.erp<0||values.cost<0||values.tax<0||values.tax>100||values.weight<0||values.weight>100) return {error:'Rf须在−5%至20%；ERP和债务成本不能为负；Beta须在−10至10；税率与债务权重须在0%—100%。'};
    const equity=values.rf+values.beta*values.erp;
    const value=equity*(1-values.weight/100)+values.cost*(1-values.tax/100)*values.weight/100;
    return finite(value)&&value>0 ? {value,equity} : {error:'估算折现率须为有效正值。'};
  }
  const api={calculate,calculateGrowth,forecastStandard,forecastThreeStage,discountCashFlows,wacc};
  if (typeof module !== 'undefined' && module.exports) module.exports=api;
  if (typeof window==='undefined' || typeof document==='undefined') return;
  window.DCFValuation=api;
  const amount=value=>finite(value)?(value/1e8).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+' 亿元':'—';
  const decimal=value=>finite(value)?value.toFixed(2):'—';
  const pct=value=>finite(value)?(value*100).toFixed(1)+'%':'—';
  const source=value=>typeof value==='object'&&value!==null?JSON.stringify(value):String(value??'未记录');
  function mount() {
    const button=document.getElementById('dcf-toggle');
    if (!button) return;
    const panel=document.createElement('section');
    panel.id='dcf-panel';panel.className='dcf-panel';panel.hidden=true;panel.setAttribute('aria-label','DCF估值计算');
    button.closest('.panel').querySelector('.panel-head').after(panel);
    let payload=null,loading=false,stale=false;
    let autoWacc=null;
    const code=JSON.parse(document.getElementById('payload').textContent).code;
    const el=id=>panel.querySelector('#'+id);
    const input=(id,label,value,step='0.1')=>`<label>${label}<input id="${id}" type="number" step="${step}" value="${value}" /></label>`;
    const skeleton=()=>`<h3>DCF 估值 · 简化模式</h3>
      <p class="dcf-note">FCF＝经营活动现金流净额 − 资本开支；此口径未经FCFF／FCFE桥接。总折现值与每股参考值不等于归母股权价值或投资安全边际。</p>
      <div id="dcf-evidence"></div><div class="dcf-controls">
      <label>估值模式<select id="dcf-mode"><option value="standard">标准DCF（固定增长率）</option><option value="three_stage">三阶段成长DCF</option></select></label>
      <label>基准FCF窗口<select id="dcf-window"><option value="3">最近3完整年均值</option><option value="1">最近1完整年</option><option value="5">最近5完整年均值</option><option value="manual">手动输入</option></select></label>
      ${input('dcf-base','基准FCF（亿元）','','0.01')}<span id="dcf-standard-controls">${input('dcf-growth','10年FCF年增长率（%）',3)}</span>
      <span id="dcf-stage1-control" hidden>${input('dcf-stage1','第1—3年增长率（%）',15)}</span><span id="dcf-stage2-control" hidden>${input('dcf-stage2','第4—7年增长率（%）',8)}</span><span id="dcf-stage3-control" hidden>${input('dcf-stage3','第8—10年增长率（%）',4)}</span>${input('dcf-discount','折现率（%）',9)}${input('dcf-terminal','永续增长率（%）',2)}</div>
      <p id="dcf-mode-basis" class="dcf-note"></p><p id="dcf-base-basis" class="dcf-note"></p><div id="dcf-recommendations" class="dcf-recommendations"></div>
      <p id="dcf-status" class="dcf-warning" role="status" aria-live="polite"></p><div id="dcf-forecast-chart" class="dcf-forecast-chart" hidden aria-label="未来10年FCF预测及三个增长阶段"></div><div id="dcf-results" aria-live="polite"></div>
      <details class="dcf-details"><summary>WACC 自动估算</summary><p class="dcf-note">估值基准日：<span id="dcf-wacc-as-of">按今天自动选择</span>。自动估算优先读取本地已保存行情和财报；不补采历史行情。ERP为6%模型假设。简化FCF并非严格FCFF，使用WACC仍需核对口径。</p>
      <div class="dcf-controls"><label>Beta窗口<select id="dcf-beta-window"><option value="2">2年</option><option value="3">3年</option><option value="5" selected>5年</option></select></label><label>ERP情景<select id="dcf-erp-scenario"><option value="5">5% 假设</option><option value="6" selected>6% 假设</option><option value="7">7% 假设</option></select></label><button type="button" id="dcf-auto-wacc">自动估算</button><button type="button" id="dcf-reset-wacc" disabled>恢复自动估算值</button></div>
      <div class="dcf-controls">${input('dcf-rf','10年期国债收益率（%）','','0.001')}${input('dcf-beta','Beta','','0.001')}${input('dcf-erp','股权风险溢价（%）','','0.001')}${input('dcf-cost','税前债务成本（%）','','0.001')}${input('dcf-tax','历史有效税率（%）','','0.001')}${input('dcf-weight','债务市值权重 D/(D+E)（%）','','0.001')}</div><div id="dcf-wacc-evidence"></div><p class="dcf-note">Ke＝Rf＋Beta×ERP；WACC＝Ke×(1−债务权重)＋Kd×(1−税率)×债务权重。</p><p id="dcf-wacc-result" role="status"></p><div id="dcf-wacc-breakdown"></div><h4>Beta × ERP 敏感性（WACC）</h4><div id="dcf-wacc-sensitivity"></div><button type="button" id="dcf-apply-wacc">应用WACC到折现率</button></details>
      <details class="dcf-details"><summary>专业FCFF模式 · 数据缺口与调整</summary><div id="dcf-professional"></div></details>
      <details class="dcf-details"><summary>历史财务与来源（金额：亿元）</summary><div id="dcf-history" class="dcf-scroll"></div></details>`;
    const table=(headers,rows)=>`<div class="dcf-scroll"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    function setBase() {
      const windowValue=el('dcf-window').value,base=payload.bases?.[windowValue];
      el('dcf-base').disabled=windowValue!=='manual';
      if (windowValue==='manual') {el('dcf-base-basis').textContent='手动假设：请核对年度口径与亿元单位；保留负值和零值。';}
      else {
        el('dcf-base').value=finite(base?.value)&&base?.available!==false ? base.value/1e8 : '';
        el('dcf-base-basis').textContent=`${(base?.periods||[]).join('、')||'没有完整年度'}；${base?.reason||'经营净现金流减资本开支的年度均值'}；来源：${source(base?.source)}`;
      }
      render();
    }
    function assumptions() {return [num(el('dcf-base').value),num(el('dcf-growth').value),num(el('dcf-discount').value),num(el('dcf-terminal').value)];}
    function setMode() {
      const growth=el('dcf-mode').value==='three_stage';
      el('dcf-standard-controls').hidden=growth;
      ['stage1','stage2','stage3'].forEach(key=>el('dcf-'+key+'-control').hidden=!growth);
      el('dcf-mode-basis').textContent=growth?'三个阶段增长率仅为可编辑示例，不是对本公司的预测。第1年在基准FCF上增长一次，阶段交界逐年复利；历史恒定增长推荐仅适用于标准模式。':'标准DCF使用固定年增长率，第1年在基准FCF上增长一次。';
      panel.querySelectorAll('[data-dcf-recommend]').forEach(btn=>{if(btn.dataset.dcfRecommend==='dcf-growth')btn.disabled=growth||btn.dataset.value==='';});
      render();
    }
    function chart(result, growth, stages) {
      const target=el('dcf-forecast-chart');
      target.hidden=!growth||Boolean(result.error);
      if (target.hidden) {if(typeof Plotly!=='undefined'&&Plotly.purge)Plotly.purge(target);return;}
      if(typeof Plotly==='undefined') {target.textContent='预测图暂不可用，请查看逐年现金流明细。';return;}
      const colors=['rgba(56,196,220,.10)','rgba(244,211,94,.09)','rgba(105,169,237,.12)'];
      const phases=[{start:.5,end:3.5,mid:2,name:'高增长期',rate:stages[0]},{start:3.5,end:7.5,mid:5.5,name:'增速放缓期',rate:stages[1]},{start:7.5,end:10.5,mid:9,name:'成熟过渡期',rate:stages[2]}];
      Plotly.react(target,[{type:'scatter',mode:'lines+markers',name:'简化FCF',x:result.rows.map(row=>row.year),y:result.rows.map(row=>row.fcf/1e8),line:{color:'#38c4dc',width:3},marker:{size:6},hovertemplate:'第%{x}年<br>FCF：%{y:.2f}亿元<extra></extra>'}],{
        paper_bgcolor:'#19262d',plot_bgcolor:'#19262d',font:{color:'#edf4f4'},margin:{l:70,r:22,t:55,b:48},showlegend:false,
        xaxis:{title:{text:'预测年'},dtick:1,range:[.5,10.5],gridcolor:'#304049'},yaxis:{title:{text:'FCF（亿元）'},gridcolor:'#304049',zerolinecolor:'#9aadb3',automargin:true},
        shapes:phases.map((p,i)=>({type:'rect',xref:'x',yref:'paper',x0:p.start,x1:p.end,y0:0,y1:1,fillcolor:colors[i],line:{width:0},layer:'below'})),
        annotations:phases.map(p=>({xref:'x',yref:'paper',x:p.mid,y:1.12,text:p.name+'<br>'+decimal(p.rate)+'%',showarrow:false,font:{size:12}}))
      },{responsive:true,displaylogo:false,locale:'zh-CN'});
    }
    function render() {
      const [baseInput,g,r,gt]=assumptions(),base=baseInput===null?null:baseInput*1e8;
      const growth=el('dcf-mode').value==='three_stage',stages=['stage1','stage2','stage3'].map(key=>num(el('dcf-'+key).value));
      const market=payload.market||{};
      const compute=(offset=0,rate=r,terminal=gt)=>growth?calculateGrowth(base,...stages.map(value=>finite(value)?value+offset:null),rate,terminal,market.total_shares,market.price):calculate(base,finite(g)?g+offset:null,rate,terminal,market.total_shares,market.price);
      const result=compute();
      const blocked=payload.applicability?.blocked||payload.applicability?.financial_company;
      const warnings=[...(payload.warnings||[])];
      if(stale)warnings.push('财务依据已刷新。关闭并重新展开DCF以读取最新依据；当前结果仍基于刷新前的数据。');
      if (blocked) warnings.push('金融企业或该行业不适用标准现金流DCF，本模块不输出计算结果。');
      if (result.error) warnings.push(result.error);
      if (finite(base)&&base<=0) warnings.push(base<0?'基准FCF为负，结果只代表增长假设下的数学折现，不形成有效股权估值或价格比较。':'基准FCF为零，本模型结果为零，无法用于价格比较。');
      if (!finite(market.total_shares)||market.total_shares<=0) warnings.push('总股本缺失或无效，不能计算每股参考值。');
      if (!finite(market.price)||market.price<=0) warnings.push('当前未复权价格缺失或无效，不能计算参考价格差额。');
      if (finite(gt)&&(gt<0||gt>2)) warnings.push('永续增长率超出成熟企业0%—2%的初步参考区间，请核对成熟度和长期经济约束。');
      if (growth) {
        if (stages.some(value=>finite(value)&&value>30)) warnings.push('阶段增长率超过30%，请核对高增长持续性与基准异常年份。');
        if ((finite(stages[0])&&finite(stages[1])&&stages[1]>stages[0])||(finite(stages[1])&&finite(stages[2])&&stages[2]>stages[1])) warnings.push('阶段间增长率突然上升，请核对与成长放缓假设是否一致。');
        if (finite(gt)&&finite(stages[2])&&gt>stages[2]) warnings.push('永续增长率高于最后阶段，长期增长假设出现反升。');
        if (finite(r)&&r<=0) warnings.push('零或负折现率属于非常规假设，会显著提高远期现金流权重。');
      }
      if (result.terminalRatio>0.75) warnings.push('终值占比超过75%，结果高度依赖永续增长与折现率。');
      el('dcf-status').textContent=warnings.join(' ');
      if (blocked||result.error) {el('dcf-results').innerHTML='';chart({error:true},growth,stages);return;}
      const scenarios=[{name:'悲观',offset:-2,r:r+1,gt:gt-0.5},{name:'中性',offset:0,r,gt},{name:'乐观',offset:2,r:r-1,gt:gt+0.5}];
      const scenarioRows=scenarios.map(s=>{
        const v=compute(s.offset,s.r,s.gt);
        return [esc(s.name),`${growth?stages.map(value=>decimal(value+s.offset)).join('→'):decimal(g+s.offset)} / ${decimal(s.r)} / ${decimal(s.gt)}`,v.error?`<span title="${esc(v.error)}">不可计算</span>`:amount(v.total),v.error?'—':decimal(v.perShare),v.error?'—':pct(v.priceGap)];
      });
      const rates=[r-2,r-1,r,r+1,r+2],terminals=[gt-1,gt-0.5,gt,gt+0.5,gt+1];
      const sensitivity=rates.map(rate=>[decimal(rate)+'%',...terminals.map(t=>{
        const v=compute(0,rate,t);
        return v.error?`<span title="${esc(v.error)}">—</span>`:`<span${rate===r&&t===gt?' class="dcf-center"':''}>${decimal(v.perShare)}</span>`;
      })]);
      el('dcf-results').innerHTML=`<h4>三种假设情景</h4><p class="dcf-note">情景差异：悲观各增长率−2个百分点、折现率＋1、永续增长率−0.5；乐观相反。此为参数对照，不代表概率预测。参考价格差额＝1−当前价格／每股参考值，仅正参考值可计算。</p>
        ${table(['情景','增长 / 折现 / 永续（%）','总现金流折现值','每股参考值（元）','参考价格差额'],scenarioRows)}
        <div class="dcf-summary"><p>显性10年现值<strong>${amount(result.explicit)}</strong></p><p>永续现值<strong>${amount(result.terminalPV)}</strong></p><p>终值占比<strong>${pct(result.terminalRatio)}</strong></p></div>
        <p class="dcf-note">第10年末终值＝第10年FCF×(1＋永续增长率)/(折现率−永续增长率)：${amount(result.terminalValue)}，再折现10年。未进行EV到股权的净债务／非经营资产／少数股东调整。</p>
        <details class="dcf-details" open><summary>未来10年现金流明细</summary>${table(['预测年',...(growth?['阶段','年增长率']:[]),'FCF（亿元）','折现因子','现值（亿元）'],result.rows.map(row=>[row.year,...(growth?[row.year<=3?'高增长期':row.year<=7?'增速放缓期':'成熟过渡期',decimal(stages[row.year<=3?0:row.year<=7?1:2])+'%']:[]),amount(row.fcf),row.factor.toFixed(4),amount(row.pv)]))}</details>
        <h4>折现率 × 永续增长率敏感性（每股参考值：元）</h4><p class="dcf-note">行：折现率；列：永续增长率。增长率保持${growth?stages.map(value=>decimal(value)).join(' / '):decimal(g)}%；不满足折现率＞永续增长率的格子留空。</p>${table(['折现率 / 永续',...terminals.map(t=>decimal(t)+'%')],sensitivity)}`;
      chart(result,growth,stages);
    }
    function showEvidence() {
      const m=payload.market||{};
      el('dcf-evidence').innerHTML=`<p class="dcf-note">${esc(payload.name||'')} ${esc(payload.code||'')} · ${esc(payload.model_label||'简化DCF（非FCFF/FCFE）')} · 行业：${esc(payload.applicability?.industry||'待核对')}。${esc(payload.applicability?.basis||'')}<br>总股本：${finite(m.total_shares)?esc((m.total_shares/1e8).toFixed(4))+' 亿股':'待补'}；日期：${esc(m.shares_date||'未记录')}；来源：${esc(source(m.shares_source))}<br>未复权价格：${decimal(m.price)} 元；日期：${esc(m.price_date||'未记录')}；来源：${esc(source(m.price_source))}。数据依据更新时间：${esc(payload.updated_at||'未记录')}</p>`;
      el('dcf-recommendations').innerHTML=[['growth','增长率','dcf-growth'],['discount','折现率','dcf-discount'],['terminal','永续增长率','dcf-terminal']].map(([key,label,id])=>{
        const rec=payload.recommendations?.[key]||{};
        const status={available:'可用',assumption:'配置假设',unavailable:'缺失',limited:'样本有限',insufficient:'样本不足，使用假设',estimated:'基于历史估算',cautious:'谨慎参考'}[rec.status]||rec.status||'缺失';
        const provenance=rec.source||(key==='growth'?'已保存历史财务，年度及来源见下表':'配置假设，非自动实测资本成本／成熟度');
        const date=rec.date||(key==='growth'?payload.updated_at||'未记录':'配置假设，非实测日期');
        return `<div><strong>${esc(label)}参考：${finite(rec.value_pct)?decimal(rec.value_pct)+'%':'待补'}</strong><p>状态：${esc(status)}；${esc(rec.basis||'没有可用依据')} ${rec.range_pct?'参考区间：'+esc(rec.range_pct.join('—'))+'%。':''}<br>来源：${esc(source(provenance))}；日期：${esc(date)}</p>${(rec.metrics||[]).map(metric=>`<p>${esc(metric.label)}：可比样本${esc(metric.sample_count)}，增长中位数${decimal(metric.median_growth_pct)}%，波动${decimal(metric.volatility_pct)}%，负值年${esc(metric.negative_years)}</p>`).join('')}<button type="button" data-dcf-recommend="${id}" data-value="${finite(rec.value_pct)?rec.value_pct:''}"${finite(rec.value_pct)?'':' disabled'}>应用${esc(label)}参考</button></div>`;
      }).join('');
      el('dcf-professional').innerHTML=`<p class="dcf-note">当前不可用。必须核对CFO利息归类、税盾和非经营现金流，再用WACC折现严格FCFF。股权桥接须核实有息债务、超额现金、非经营资产、少数股东和优先索取权；不能把所有货币资金视为超额现金，缺项不能记零，租赁不能重复扣除。</p><ul>${(payload.professional?.gaps||[]).map(gap=>`<li>${esc(gap)}</li>`).join('')}</ul>`;
      el('dcf-history').innerHTML=table(['完整年度','营业收入','归母净利润','经营净现金流','资本开支','简化FCF','来源／公式'],(payload.history||[]).map(row=>[esc(row.period),amount(row.revenue),amount(row.profit),amount(row.operating_cash_flow),amount(row.capex),amount(row.free_cash_flow),`<details><summary>来源</summary><pre>${esc(source(row.sources))}</pre></details>`]));
    }
    async function load() {
      if (loading||(payload&&!stale)) return;
      loading=true;panel.innerHTML='<p class="dcf-note" role="status">正在读取已保存财务依据…</p>';
      try {
        const response=await fetch('/api/dcf?'+new URLSearchParams({code}));
        if (!response.ok) throw new Error('读取失败（HTTP '+response.status+'）');
        const data=await response.json();if (data.error) throw new Error(data.error);
        payload=data;stale=false;panel.innerHTML=skeleton();showEvidence();
        const defaults=payload.defaults||{};
        el('dcf-window').value=['1','3','5'].includes(String(defaults.base_window))?String(defaults.base_window):'3';
        for(const [id,key] of [['growth','growth_pct'],['discount','discount_pct'],['terminal','terminal_growth_pct']]) {
          if(finite(defaults[key]))el('dcf-'+id).value=defaults[key];
        }
        el('dcf-mode').value='standard';
        for(const [id,key] of [['stage1','stage1_pct'],['stage2','stage2_pct'],['stage3','stage3_pct']]) {
          if(finite(payload.growth_defaults?.[key]))el('dcf-'+id).value=payload.growth_defaults[key];
        }
        el('dcf-mode').addEventListener('change',setMode);
        el('dcf-window').addEventListener('change',setBase);
        ['base','growth','stage1','stage2','stage3','discount','terminal'].forEach(key=>el('dcf-'+key).addEventListener('input',render));
        panel.querySelectorAll('[data-dcf-recommend]').forEach(btn=>btn.addEventListener('click',()=>{el(btn.dataset.dcfRecommend).value=btn.dataset.value;render();}));
        const currentWaccValues=()=>{
          const ids={rf:'rf',beta:'beta',erp:'erp',kd:'cost',tax:'tax',weight:'weight'};
          return Object.fromEntries(Object.entries(ids).map(([key,id])=>{
            const value=num(el('dcf-'+id).value),automatic=autoWacc?.parameters?.[key]?.value;
            return [key,finite(automatic)&&value===Number(waccNumber(automatic))?automatic:value];
          }));
        };
        const renderWacc=()=>{
          const values=currentWaccValues();values.cost=values.kd;delete values.kd;
          const estimate=wacc(values);el('dcf-wacc-result').textContent=estimate.error||`Ke ${waccDecimal(estimate.equity)}%；WACC ${waccDecimal(estimate.value)}%。`;
          if(estimate.error){el('dcf-wacc-breakdown').innerHTML='';el('dcf-wacc-sensitivity').innerHTML='';return;}
          const dw=values.weight/100,afterTax=values.cost*(1-values.tax/100),equityContribution=estimate.equity*(1-dw),debtContribution=afterTax*dw;
          el('dcf-wacc-breakdown').innerHTML=`<p>Ke：${waccDecimal(estimate.equity)}% · 税后债务成本：${waccDecimal(afterTax)}% · 股权权重：${waccDecimal(100-values.weight)}% · 债务权重：${waccDecimal(values.weight)}% · 股权贡献：${waccDecimal(equityContribution)}% · 债务贡献：${waccDecimal(debtContribution)}% · WACC：${waccDecimal(estimate.value)}%</p>`;
          const betas=[values.beta-0.5,values.beta,values.beta+0.5],erps=[values.erp-1,values.erp,values.erp+1];
          el('dcf-wacc-sensitivity').innerHTML=table(['Beta / ERP',...erps.map(v=>waccDecimal(v)+'%')],betas.map(b=>[waccDecimal(b),...erps.map(e=>{const result=wacc({...values,beta:b,erp:e});return result.error?'—':waccDecimal(result.value)+'%';})]));
        };
        const showWaccEvidence=()=>{
          if(!autoWacc)return;
          const ids={rf:'rf',beta:'beta',erp:'erp',kd:'cost',tax:'tax',weight:'weight'};
          el('dcf-wacc-evidence').innerHTML=table(['参数','数值/历史参考','状态','来源、日期与方法'],Object.entries(autoWacc.parameters||{}).map(([key,p])=>{
            const input=num(el('dcf-'+ids[key]).value),overridden=finite(input)&&input!==Number(waccNumber(p.value));
            const value=finite(input)?waccDecimal(input)+(key==='beta'?'':'%'):finite(p.history_value)?'历史 '+waccDecimal(p.history_value)+'%':'不可用';
            const status=overridden?'人工覆盖':key==='tax'&&finite(p.history_value)?(p.history_status==='estimated'?'历史有效税率，自动用于WACC':'历史值异常，不能采用'):({observed:'实测',estimated:'估算',assumption:'假设',unavailable:'不可用'})[p.status]||p.status||'不可用';
            const sample=key==='beta'?`共同周收益率样本${p.sample_count??0}组；区间${p.start||'未记录'}—${p.end||'未记录'}`:null;
            return [esc(({rf:'Rf',beta:'Beta',erp:'ERP',kd:'Kd',tax:'税率',weight:'债务权重'})[key]||key),value,esc(status),esc([p.source,p.as_of_date||'未记录',sample,p.method,p.reason,p.stale?'数据超过7天，可能过期':null].filter(Boolean).join('；'))];
          }));
          if(autoWacc.warnings?.length) el('dcf-wacc-evidence').insertAdjacentHTML('beforeend',`<p class="dcf-warning">${autoWacc.warnings.map(esc).join(' ')}</p>`);
        };
        ['rf','beta','erp','cost','tax','weight'].forEach(key=>el('dcf-'+key).addEventListener('input',()=>{showWaccEvidence();renderWacc();}));
        el('dcf-erp-scenario').addEventListener('change',()=>{el('dcf-erp').value=el('dcf-erp-scenario').value;showWaccEvidence();renderWacc();});
        el('dcf-auto-wacc').addEventListener('click',async()=>{
          el('dcf-wacc-result').textContent='正在读取本地历史价格和财务数据…';
          try{
            const res=await fetch('/api/dcf/wacc?'+new URLSearchParams({code,years:el('dcf-beta-window').value}));
            if(!res.ok)throw new Error('自动估算请求失败');autoWacc=await res.json();
            el('dcf-wacc-as-of').textContent=autoWacc.as_of_date||'今天';
            const ids={rf:'rf',beta:'beta',erp:'erp',kd:'cost',tax:'tax',weight:'weight'};
            for(const [key,id] of Object.entries(ids)){const p=autoWacc.parameters?.[key];if(p&&finite(p.value))el('dcf-'+id).value=waccNumber(p.value);else if(p&&key!=='erp')el('dcf-'+id).value='';}
            if(finite(autoWacc.parameters?.erp?.value))el('dcf-erp-scenario').value=String(autoWacc.parameters.erp.value);
            showWaccEvidence();
            el('dcf-reset-wacc').disabled=false;renderWacc();
          }catch(error){el('dcf-wacc-result').textContent=error.message;}
        });
        el('dcf-reset-wacc').addEventListener('click',()=>{if(!autoWacc)return;const ids={rf:'rf',beta:'beta',erp:'erp',kd:'cost',tax:'tax',weight:'weight'};for(const [key,id] of Object.entries(ids)){const p=autoWacc.parameters?.[key];el('dcf-'+id).value=waccNumber(p?.value);}el('dcf-erp-scenario').value=String(autoWacc.parameters?.erp?.value||6);showWaccEvidence();renderWacc();});
        el('dcf-apply-wacc').addEventListener('click',()=>{
          const values=currentWaccValues();values.cost=values.kd;delete values.kd;
          const estimate=wacc(values);
          if (estimate.error) {el('dcf-wacc-result').textContent=estimate.error;return;}
          if(estimate.value<=num(el('dcf-terminal').value)){el('dcf-wacc-result').textContent='WACC必须高于当前永续增长率，暂不应用。';return;}
          el('dcf-discount').value=waccNumber(estimate.value);
          el('dcf-wacc-result').textContent=`已将WACC ${waccDecimal(estimate.value)}%（Ke ${waccDecimal(estimate.equity)}%）应用到当前DCF折现率。`;
          render();
        });
        setBase();setMode();
      } catch (error) {payload=null;panel.innerHTML=`<p class="dcf-warning" role="alert">${esc(error.message)}。关闭后重新展开可重试。</p>`;}
      finally {loading=false;}
    }
    const metricButtons=Array.from(button.closest('.panel').querySelectorAll('[data-valuation-metric]'));
    let selectedMetric=metricButtons.find(item=>item.classList.contains('active'));
    function setExpanded(expanded) {
      panel.hidden=!expanded;
      button.setAttribute('aria-expanded',String(expanded));
      button.classList.toggle('active',expanded);
      metricButtons.forEach(item=>item.classList.toggle('active',!expanded&&item===selectedMetric));
      if(expanded) {load();if(payload&&!stale)render();}
    }
    metricButtons.forEach(item=>item.addEventListener('click',()=>{
      selectedMetric=item;
      setExpanded(false);
    }));
    button.addEventListener('click',()=>setExpanded(panel.hidden));
    window.addEventListener('financial-facts-updated',event=>{
      if(event.detail?.code!==code||!payload)return;
      const status=el('dcf-status');
      if(status)status.textContent='财务依据已刷新。关闭并重新展开DCF以读取最新依据；当前结果仍基于刷新前的数据。';
      // Keep the current assumptions editable until the user reopens the module.
      stale=true;
    });
  }
  if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',mount);else mount();
})();
