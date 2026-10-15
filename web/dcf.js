/* Read-only saved facts; all assumptions and valuations are calculated locally. */
(() => {
  'use strict';
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = text => String(text ?? '').trim() === '' ? null : Number(text);
  function calculate(base, growth, discount, terminal, shares=null, price=null) {
    if (![base,growth,discount,terminal].every(finite)) return {error:'基准FCF或增长／折现参数缺失或无效。'};
    if (growth <= -100 || discount <= 0 || terminal <= -100) return {error:'增长率和永续增长率须大于 −100%，折现率须大于 0%。'};
    if (discount <= terminal) return {error:'折现率必须大于永续增长率。'};
    const g=growth/100,r=discount/100,gt=terminal/100;
    const rows=Array.from({length:10}, (_,i) => {
      const year=i+1,fcf=base*Math.pow(1+g,year),factor=1/Math.pow(1+r,year);
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
  function wacc(values) {
    if (!['rf','beta','erp','cost','tax','weight'].every(key=>finite(values[key]))) return {error:'请完整填写六项资本成本假设，不将缺项视为零。'};
    if (values.rf<0||values.beta<0||values.erp<0||values.cost<0||values.tax<0||values.tax>100||values.weight<0||values.weight>100) return {error:'收益率、Beta、ERP和债务成本不能为负；税率与债务权重须在 0%—100%。'};
    const equity=values.rf+values.beta*values.erp;
    const value=equity*(1-values.weight/100)+values.cost*(1-values.tax/100)*values.weight/100;
    return finite(value)&&value>0 ? {value,equity} : {error:'估算折现率须为有效正值。'};
  }
  const api={calculate,wacc};
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
    const code=JSON.parse(document.getElementById('payload').textContent).code;
    const el=id=>panel.querySelector('#'+id);
    const input=(id,label,value,step='0.1')=>`<label>${label}<input id="${id}" type="number" step="${step}" value="${value}" /></label>`;
    const skeleton=()=>`<h3>DCF 估值 · 简化模式</h3>
      <p class="dcf-note">FCF＝经营活动现金流净额 − 资本开支；此口径未经FCFF／FCFE桥接。总折现值与每股参考值不等于归母股权价值或投资安全边际。</p>
      <div id="dcf-evidence"></div><div class="dcf-controls">
      <label>基准FCF窗口<select id="dcf-window"><option value="3">最近3完整年均值</option><option value="1">最近1完整年</option><option value="5">最近5完整年均值</option><option value="manual">手动输入</option></select></label>
      ${input('dcf-base','基准FCF（亿元）','','0.01')}${input('dcf-growth','10年FCF年增长率（%）',3)}${input('dcf-discount','折现率（%）',9)}${input('dcf-terminal','永续增长率（%）',2)}</div>
      <p id="dcf-base-basis" class="dcf-note"></p><div id="dcf-recommendations" class="dcf-recommendations"></div>
      <p id="dcf-status" class="dcf-warning" role="status" aria-live="polite"></p><div id="dcf-results" aria-live="polite"></div>
      <details class="dcf-details"><summary>WACC 人工假设估算器</summary><p class="dcf-note">系统暂无可靠自动资本成本来源。以下输入均为人工假设，输入来源及日期后才可应用；简化FCF应用WACC也不能使其成为专业FCFF。</p>
      <div class="dcf-controls">${input('dcf-rf','10年期国债收益率（%）','')}${input('dcf-beta','Beta','','0.01')}${input('dcf-erp','股权风险溢价（%）','')}${input('dcf-cost','税前债务成本（%）','')}${input('dcf-tax','有效税率（%）','')}${input('dcf-weight','债务市值权重 D/(D+E)（%）','')}
      <label>各项来源／估算依据<input id="dcf-wacc-source" type="text" placeholder="注明国债、Beta、ERP及资本结构依据" /></label><label>假设依据日期<input id="dcf-wacc-date" type="date" /></label></div>
      <p class="dcf-note">Ke＝Rf＋Beta×ERP；WACC＝Ke×(1−债务权重)＋Kd×(1−税率)×债务权重。</p><p id="dcf-wacc-result" role="status"></p><button type="button" id="dcf-apply-wacc">估算并应用折现率</button></details>
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
    function render() {
      const [baseInput,g,r,gt]=assumptions(),base=baseInput===null?null:baseInput*1e8;
      const market=payload.market||{},result=calculate(base,g,r,gt,market.total_shares,market.price);
      const blocked=payload.applicability?.blocked||payload.applicability?.financial_company;
      const warnings=[...(payload.warnings||[])];
      if(stale)warnings.push('财务依据已刷新。关闭并重新展开DCF以读取最新依据；当前结果仍基于刷新前的数据。');
      if (blocked) warnings.push('金融企业或该行业不适用标准现金流DCF，本模块不输出计算结果。');
      if (result.error) warnings.push(result.error);
      if (finite(base)&&base<=0) warnings.push(base<0?'基准FCF为负，结果只代表恒定增长假设下的数学折现，不形成有效股权估值或价格比较。':'基准FCF为零，本模型结果为零，无法用于价格比较。');
      if (!finite(market.total_shares)||market.total_shares<=0) warnings.push('总股本缺失或无效，不能计算每股参考值。');
      if (!finite(market.price)||market.price<=0) warnings.push('当前未复权价格缺失或无效，不能计算参考价格差额。');
      if (finite(gt)&&(gt<0||gt>2)) warnings.push('永续增长率超出成熟企业0%—2%的初步参考区间，请核对成熟度和长期经济约束。');
      if (result.terminalRatio>0.75) warnings.push('终值占比超过75%，结果高度依赖永续增长与折现率。');
      el('dcf-status').textContent=warnings.join(' ');
      if (blocked||result.error) {el('dcf-results').innerHTML='';return;}
      const scenarios=[{name:'悲观',g:g-2,r:r+1,gt:gt-0.5},{name:'中性',g,r,gt},{name:'乐观',g:g+2,r:r-1,gt:gt+0.5}];
      const scenarioRows=scenarios.map(s=>{
        const v=calculate(base,s.g,s.r,s.gt,market.total_shares,market.price);
        return [esc(s.name),`${decimal(s.g)} / ${decimal(s.r)} / ${decimal(s.gt)}`,v.error?`<span title="${esc(v.error)}">不可计算</span>`:amount(v.total),v.error?'—':decimal(v.perShare),v.error?'—':pct(v.priceGap)];
      });
      const rates=[r-2,r-1,r,r+1,r+2],terminals=[gt-1,gt-0.5,gt,gt+0.5,gt+1];
      const sensitivity=rates.map(rate=>[decimal(rate)+'%',...terminals.map(t=>{
        const v=calculate(base,g,rate,t,market.total_shares,market.price);
        return v.error?`<span title="${esc(v.error)}">—</span>`:`<span${rate===r&&t===gt?' class="dcf-center"':''}>${decimal(v.perShare)}</span>`;
      })]);
      el('dcf-results').innerHTML=`<h4>三种假设情景</h4><p class="dcf-note">情景差异：悲观增长率−2个百分点、折现率＋1、永续增长率−0.5；乐观相反。此为参数对照，不代表概率预测。参考价格差额＝1−当前价格／每股参考值，仅正参考值可计算。</p>
        ${table(['情景','增长 / 折现 / 永续（%）','总现金流折现值','每股参考值（元）','参考价格差额'],scenarioRows)}
        <div class="dcf-summary"><p>显性10年现值<strong>${amount(result.explicit)}</strong></p><p>永续现值<strong>${amount(result.terminalPV)}</strong></p><p>终值占比<strong>${pct(result.terminalRatio)}</strong></p></div>
        <p class="dcf-note">第10年末终值＝第10年FCF×(1＋永续增长率)/(折现率−永续增长率)：${amount(result.terminalValue)}，再折现10年。未进行EV到股权的净债务／非经营资产／少数股东调整。</p>
        <details class="dcf-details" open><summary>未来10年现金流明细</summary>${table(['预测年','FCF（亿元）','折现因子','现值（亿元）'],result.rows.map(row=>[row.year,amount(row.fcf),row.factor.toFixed(4),amount(row.pv)]))}</details>
        <h4>折现率 × 永续增长率敏感性（每股参考值：元）</h4><p class="dcf-note">行：折现率；列：永续增长率。增长率保持${decimal(g)}%；不满足折现率＞永续增长率的格子留空。</p>${table(['折现率 / 永续',...terminals.map(t=>decimal(t)+'%')],sensitivity)}`;
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
        el('dcf-window').addEventListener('change',setBase);
        ['base','growth','discount','terminal'].forEach(key=>el('dcf-'+key).addEventListener('input',render));
        panel.querySelectorAll('[data-dcf-recommend]').forEach(btn=>btn.addEventListener('click',()=>{el(btn.dataset.dcfRecommend).value=btn.dataset.value;render();}));
        el('dcf-apply-wacc').addEventListener('click',()=>{
          const values=Object.fromEntries(['rf','beta','erp','cost','tax','weight'].map(key=>[key,num(el('dcf-'+key).value)]));
          const estimate=wacc(values),basis=el('dcf-wacc-source').value.trim(),date=el('dcf-wacc-date').value;
          if (estimate.error||!basis||!/^\d{4}-\d{2}-\d{2}$/.test(date)) {el('dcf-wacc-result').textContent=estimate.error||'请填写人工假设的来源依据和日期。';return;}
          el('dcf-discount').value=estimate.value.toFixed(4);
          el('dcf-wacc-result').textContent=`已应用人工WACC假设 ${estimate.value.toFixed(4)}%（Ke ${decimal(estimate.equity)}%）；${basis}；依据日期 ${date}。`;
          render();
        });
        setBase();
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
      if(expanded)load();
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
