/* Local facts plus explicitly requested, memory-only consensus. Rates are decimal fractions; amounts are CNY yuan. */
(() => {
  'use strict';
  const finite = value => !['number','string'].includes(typeof value) || typeof value==='string'&&!value.trim() || !Number.isFinite(Number(value)) ? null : Number(value);
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  function quantile(values, fraction) {
    const sorted=[...values].sort((a,b)=>a-b), position=(sorted.length-1)*fraction;
    return sorted[Math.floor(position)]+(sorted[Math.ceil(position)]-sorted[Math.floor(position)])*(position-Math.floor(position));
  }
  function profit_basis(reports, year, mode='A', window=3, method='median', scenario='neutral') {
    const by=Object.fromEntries(reports.map(r=>[r.period,r]));
    const periods=Array.from({length:window},(_,i)=>`${year-window+i}-12-31`);
    const current=[...reports].sort((a,b)=>b.period.localeCompare(a.period)).find(r=>r.period.startsWith(String(year))&&finite(r.profit)!==null);
    let realized=null, samples;
    if(mode==='A') {
      if(!current)return {value:null,samples:[],periods,realized:null,reason:'当年缺少已披露归母利润，请选B/C或手填利润'};
      realized=current.profit;
      if(current.period.endsWith('12-31'))return {value:realized,samples:[],periods:[current.period],realized,reason:'全年已披露，使用实际值'};
      samples=periods.map(p=>{
        const a=by[p]?.profit,b=by[p.slice(0,4)+current.period.slice(4)]?.profit;
        return finite(a)!==null&&finite(b)!==null?a-b:null;
      });
    } else samples=periods.map(p=>by[p]?.profit??null);
    if(!samples.every(v=>finite(v)!==null))return {value:null,samples,periods,realized,reason:'连续历史窗口缺年或缺同口径利润，未跳年'};
    const fraction={pessimistic:.25,neutral:.5,optimistic:.75}[scenario];
    const base=scenario==='neutral'&&method==='mean'?samples.reduce((a,b)=>a+b,0)/samples.length:quantile(samples,fraction);
    return {value:finite(base+(realized||0)),samples,periods,realized,
      reason:(mode==='A'?'累计已实现＋历史剩余利润':'历史全年正常化利润')+(scenario==='neutral'&&method==='mean'?'均值':`${fraction*100}%分位`)};
  }
  function build_history(reports, events, year, edits={}, confirmations={}, exclude_special=true) {
    const annual=Object.fromEntries(reports.filter(r=>r.period.endsWith('12-31')).map(r=>[Number(r.period.slice(0,4)),r]));
    return Array.from({length:6},(_,index)=>{
      const y=year-5+index, report=annual[y]||{}, group=events.filter(e=>e.report_period?.slice(0,4)===String(y));
      let cash=0,dps=0,missing=false,pending=false,unknown=false,excluded=0,active=0;
      for(const event of group) {
        const savedEdit=edits[event.id]||{},edit=String(savedEdit.note||'').trim()?savedEdit:{},kind=edit.kind||'unknown';
        if(event.state==='cancelled')continue;
        if(event.state!=='paid'){pending=true;continue;}
        unknown=unknown||kind==='unknown';
        if(exclude_special&&kind==='special'){excluded++;continue;}
        active++;
        const amount=finite(has(edit,'cash')?edit.cash:event.cash);
        if(amount===null||amount<0||event.conflict)missing=true;else cash+=amount;
        if(finite(event.dps)===null)missing=true;else dps+=event.dps;
      }
      const confirmation=confirmations[String(y)]||{},confirmed=Boolean(confirmation.complete&&String(confirmation.note||'').trim());
      const zero=confirmed&&confirmation.zero&&!active&&!pending;
      const known=(active>0||excluded>0||zero)&&!missing,total=known?finite(cash):null;
      const profit=finite(report.profit),fcf=finite(report.fcf),complete=Boolean(known&&confirmed&&!pending&&(!exclude_special||!unknown));
      return {year:y,profit,fcf,cash:total,dps:known?finite(dps):null,payout_ratio:total!==null&&profit!==null&&profit>0?finite(total/profit):null,
        coverage:fcf!==null&&total!==null&&total>0?finite(fcf/total):null,complete,pending,unknown,excluded,
        state:zero?'已确认不分红':complete?'已确认完整':pending?'仍待实施/核实':known?'已实施估算，覆盖未核实':'缺失',note:confirmation.note||'',sources:report.sources||{}};
    });
  }
  function payout_basis(history, year, window) {
    const rows=Array.from({length:window},(_,i)=>history.find(r=>r.year===year-window+i)||{});
    if(!rows.every(r=>!r.pending&&finite(r.cash)!==null&&r.cash>=0&&finite(r.profit)!==null))return null;
    const profit=finite(rows.reduce((a,r)=>a+r.profit,0)),cash=finite(rows.reduce((a,r)=>a+r.cash,0));
    return profit!==null&&profit>0&&cash!==null?finite(cash/profit):null;
  }
  function calculate_scenario(years, parameters, price) {
    let previous=finite(parameters.base_profit);price=finite(price);
    const rows=years.map((year,index)=>{
      const entry=parameters.rows[index],growth=finite(entry.growth_rate),ratio=finite(entry.payout_ratio),shares=finite(entry.shares),warnings=[];
      let profit=parameters.mode==='C'||index>0 ? previous!==null&&growth!==null&&growth>=-1?finite(previous*(1+growth)):null:finite(parameters.first_profit);
      if(has(entry,'profit'))profit=finite(entry.profit);
      if(profit===null)warnings.push('利润或增长基准无效');
      const implied=profit!==null&&previous!==null&&previous>0?finite(profit/previous-1):null;
      let cash=has(entry,'cash')?finite(entry.cash):profit!==null&&profit>0&&ratio!==null&&ratio>=0?finite(profit*ratio):null;
      if(cash!==null&&cash<0)cash=null;
      const actual_ratio=cash!==null&&profit!==null&&profit>0?finite(cash/profit):null;
      if(cash===null)warnings.push('分红不可用；亏损时请手填非负现金金额');
      if(actual_ratio!==null&&actual_ratio>1)warnings.push('分红超过归母净利润');
      if(shares===null||shares<=0)warnings.push('总股本须为正数');
      const dps=cash!==null&&shares!==null&&shares>0?finite(cash/shares):null;
      const yield_rate=dps!==null&&price!==null&&price>0?finite(dps/price):null;
      if(cash!==null&&shares!==null&&shares>0&&dps===null)warnings.push('每股金额溢出，请核对数量级');
      if(price===null||price<=0)warnings.push('未复权股价须为正数');
      previous=profit;
      return {year,profit,growth_rate:implied,payout_ratio:actual_ratio,cash,shares,dps,yield_rate,warnings};
    });
    const cumulative=rows.every(r=>r.dps!==null)?finite(rows.reduce((a,r)=>a+r.dps,0)):null;
    return {rows,cumulative_dps:cumulative,cumulative_yield:cumulative!==null&&price!==null&&price>0?finite(cumulative/price):null};
  }
  function target_price(dps, rate) {return finite(dps)!==null&&dps>0&&finite(rate)!==null&&rate>0?finite(dps/rate):null;}
  function scenario_factors(reports,year,mode='A',window=3,method='median') {
    mode=mode==='C'?'A':mode;
    let center=profit_basis(reports,year,mode,window,method);
    if(center.value===null&&mode==='A'){mode='B';center=profit_basis(reports,year,mode,window,method);}
    return Object.fromEntries([['pessimistic',.8],['optimistic',1.2]].map(([s,fallback])=>{
      const edge=profit_basis(reports,year,mode,window,method,s);
      const value=center.value!==null&&center.value!==0&&edge.value!==null?finite(1+(edge.value-center.value)/Math.abs(center.value)):null;
      const historical=value!==null&&(s==='pessimistic'?value<1:value>1);
      return [s,{profit_factor:historical?value:fallback,payout_factor:1,shares_factor:1,
        source:historical?`${window}年历史利润25%/75%分位相对中性基准的差异`:'历史样本不足或无方向差异，0.8/1.2为可调整的情景假设，非公司预测'}];
    }));
  }
  function derive_scenario(years,neutral_parameters,price,factors) {
    const neutral=calculate_scenario(years,neutral_parameters,price),p=finite(factors.profit_factor),r=finite(factors.payout_factor),s=finite(factors.shares_factor);
    const rows=neutral.rows.map((original,i)=>{
      const profit=original.profit,row={profit:profit!==null&&p!==null?finite(profit+Math.abs(profit)*(p-1)):null,growth_rate:0,
        payout_ratio:original.payout_ratio!==null&&r!==null&&r>=0?finite(original.payout_ratio*r):null,
        shares:original.shares!==null&&original.shares>0&&s!==null&&s>0?finite(original.shares*s):null};
      if(has(neutral_parameters.rows[i],'cash'))row.cash=original.cash!==null&&p!==null&&p>=0&&r!==null&&r>=0?finite(original.cash*p*r):null;
      return row;
    });
    const result=calculate_scenario(years,{mode:'linked',base_profit:neutral_parameters.base_profit,rows},price);
    for(const row of result.rows)row.warnings=row.warnings.map(w=>w==='分红不可用；亏损时请手填非负现金金额'?'派生分红不可用，请核对中性现金金额及情景系数':w);
    return result;
  }
  const api={finite,quantile,profit_basis,build_history,payout_basis,calculate_scenario,target_price,scenario_factors,derive_scenario};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  if(typeof document==='undefined')return;
  window.DividendForecast=api;

  const names={pessimistic:'悲观',neutral:'中性',optimistic:'乐观'},scenarioKeys=Object.keys(names);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=(v,d=2)=>finite(v)===null?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
  const amount=v=>finite(v)===null?'—':num(v/1e8);
  const pct=v=>finite(v)===null?'—':num(v*100)+'%';
  const table=(head,rows)=>`<div class="df-scroll"><table><thead><tr>${head.map(v=>`<th>${v}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${v}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  function mount() {
    const root=document.getElementById('dividend-forecast'),body=document.getElementById('dividend-forecast-body');
    if(!root||!body)return;
    let code=JSON.parse(document.getElementById('payload').textContent).code,payload=null,state=null,controller=null,serial=0,timer=null,storageError=false,loadedAt=null,savedAt=null,dirty=false,loadingCode=null;
    let consensus=null,consensusError='',consensusLoading=false,consensusController=null,consensusSerial=0;
    const el=id=>root.querySelector('#df-'+id);
    const key=()=>`dividend-forecast:v1:${code}:${payload.years[0]}`;
    const defaults=()=>({layout_version:2,window:3,method:'median',payoutMode:'auto',exclude:false,edits:{},confirmations:{},factors:{pessimistic:{},optimistic:{}},scenarios:Object.fromEntries(scenarioKeys.map(s=>[s,{mode:'A',rows:[{},{},{}]}])),targetScenario:'neutral',targetYear:0,targetRate:4});
    function restore() {
      const fresh=defaults();
      try {
        const saved=JSON.parse(localStorage.getItem(key())||'null');
        if(saved&&saved.version===1&&saved.parameters&&typeof saved.parameters==='object') {
          savedAt=saved.saved_at;
          Object.assign(fresh,saved.parameters);
          fresh.scenarios=Object.fromEntries(scenarioKeys.map(s=>{
            const candidate=saved.parameters.scenarios?.[s];
            return [s,candidate&&Array.isArray(candidate.rows)&&candidate.rows.length===3?candidate:{mode:'A',rows:[{},{},{}]}];
          }));
          if(saved.parameters.layout_version!==2) {
            const previous=Object.fromEntries(['pessimistic','optimistic'].filter(s=>{
              const c=fresh.scenarios[s];return c.mode!=='A'||c.rows.some(r=>Object.keys(r||{}).length)||['base','adjustment','remaining','note'].some(k=>has(c,k));
            }).map(s=>[s,fresh.scenarios[s]]));
            if(Object.keys(previous).length)fresh.legacy_scenarios=previous;
            for(const s of ['pessimistic','optimistic'])fresh.scenarios[s]={mode:'A',rows:[{},{},{}]};
            fresh.layout_version=2;
          }
        } else {
          // Carry only annotations whose event identity still exists. Annual
          // assumptions and completeness confirmations must be reconsidered.
          const prior=JSON.parse(localStorage.getItem(`dividend-forecast:v1:${code}:${payload.years[0]-1}`)||'null');
          const ids=new Set(payload.events.map(e=>e.id));
          if(prior?.version===1&&prior.parameters?.edits)fresh.edits=Object.fromEntries(Object.entries(prior.parameters.edits).filter(([id])=>ids.has(id)));
        }
      } catch {storageError=true;}
      for(const field of ['edits','confirmations'])if(!fresh[field]||typeof fresh[field]!=='object')fresh[field]={};
      if(!fresh.factors||typeof fresh.factors!=='object')fresh.factors={};
      for(const s of ['pessimistic','optimistic'])if(!fresh.factors[s]||typeof fresh.factors[s]!=='object')fresh.factors[s]={};
      if(![3,5].includes(fresh.window))fresh.window=3;
      for(const s of scenarioKeys)if(!['A','B','C'].includes(fresh.scenarios[s].mode))fresh.scenarios[s].mode='A';
      for(const s of scenarioKeys)fresh.scenarios[s].rows=fresh.scenarios[s].rows.map(r=>r&&typeof r==='object'?r:{});
      if(!scenarioKeys.includes(fresh.targetScenario))fresh.targetScenario='neutral';
      if(![0,1,2].includes(Number(fresh.targetYear)))fresh.targetYear=0;
      state=fresh;
    }
    function persist() {
      try {savedAt=new Date().toISOString();localStorage.setItem(key(),JSON.stringify({version:1,saved_at:savedAt,parameters:{...state,scenarios:{...state.scenarios,neutral:{...state.scenarios.neutral,mode:state.scenarios.neutral.mode==='D'?'A':state.scenarios.neutral.mode}}}}));}
      catch {storageError=true;}
    }
    function history() {return build_history(payload.reports,payload.events,payload.years[0],state.edits,state.confirmations,state.exclude);}
    function ratioDefault(hist) {
      if(state.payoutMode==='manual'||state.payoutMode==='policy')return null;
      if(state.payoutMode==='auto')return payout_basis(hist,payload.years[0],3)??payout_basis(hist,payload.years[0],1);
      return payout_basis(hist,payload.years[0],Number(state.payoutMode));
    }
    function scenarioParameters(s,hist) {
      const configured=state.scenarios[s],mode=configured.mode;
      const basis=profit_basis(payload.reports,payload.years[0],mode==='C'?'A':mode,state.window,state.method,s);
      const prior=payload.reports.find(r=>r.period===`${payload.years[0]-1}-12-31`)?.profit??null;
      const base=has(configured,'base')?configured.base:prior;
      const adjustment=has(configured,'adjustment')?finite(configured.adjustment):0;
      const remaining=has(configured,'remaining')?finite(configured.remaining):0;
      const first=basis.value!==null&&adjustment!==null&&adjustment>=-1&&remaining!==null?finite((basis.value+remaining)*(1+adjustment)):null;
      const ratio=ratioDefault(hist);
      return {basis,mode,base_profit:base,first_profit:first,
        rows:configured.rows.map((r,i)=>({growth_rate:0,payout_ratio:ratio,shares:payload.market.shares,...r,
          ...(mode==='D'?{profit:consensus?.rows.find(v=>v.year===payload.years[i])?.profit??null}:{}),
          ...(has(configured,'uniform_payout_ratio')?{payout_ratio:configured.uniform_payout_ratio}:{})}))};
    }
    function input(attrs,value,placeholder='',type='number') {
      return `<input ${attrs} type="${type}" ${type==='number'?'step="any"':''} value="${esc(value??'')}" placeholder="${esc(placeholder)}">${type==='number'?'<small class="df-input-source"></small>':''}`;
    }
    function showValue(node,value,manual,source,editing) {
      const scaled=finite(value)===null?null:value/Number(node.dataset.scale||1);
      if(node!==editing)node.value=scaled===null?'':manual?String(scaled):String(Number(scaled.toFixed(node.dataset.factor||node.dataset.field==='shares'?4:2)));
      node.placeholder=scaled===null?'缺少计算依据':'';
      node.classList.toggle('df-auto-input',!manual);
      node.classList.toggle('df-manual-input',manual);
      node.title=(manual?'手动调整；清空后恢复自动':'自动 · '+source+'；未舍入值 '+scaled);
      const label=node.nextElementSibling;
      if(label?.classList.contains('df-input-source'))label.textContent=manual?'手动调整 · 清空恢复自动':scaled===null?'暂无适用自动值':'自动 · '+source;
    }
    function scaffold(refreshed=false) {
      const old=state;
      if(!old)restore();
      const m=payload.market;
      body.innerHTML=`<p class="df-intro">按财年归属预测 · 以当前未复权股价为基准 · 参数仅在本浏览器保存</p>
        <p id="df-status" role="status" aria-live="polite"></p>
        <div class="df-actions"><button type="button" id="df-reload">重读本地依据</button><button type="button" id="df-reset">恢复默认</button></div>
        <p class="df-note">${esc(payload.name)} ${esc(code)} · 基准日 ${esc(payload.as_of_date)} · 归属财年税前口径<br>
        股价：${num(m.price)} 元（${esc(m.price_date||'未记录')}，${m.price_source?.includes('腾讯')?'腾讯未复权收盘价':m.price!==null?'本地交易日快照未复权收盘价':'缺失'}）；总股本：${amount(m.shares)} 亿股（${esc(m.shares_date||'未记录')}）。<br>A/B/C读取本地资料；选择机构预期时联网读取同花顺，预测仅用于当前页面，不保存。手动参数与原始财务事实分离。</p>
        <details><summary>查看事实来源与计算证据</summary><pre id="df-evidence"></pre></details>
        <h3>历史参考</h3><p class="df-note">金额单位亿元；DPS为各事件原始税前每股金额之和，股本变化前后未复权，不直接与当前股本DPS比较。现金分红按所属财年汇总；覆盖未核实时仅为已知部分参考。</p>
        <div id="df-history"></div>
        <details><summary>核对分红事件、特别分红及年度完整性</summary><p class="df-note">实际派息日未记录。已实施仍需除权日到达才计入。事件金额可人工覆盖，须填写依据；未知性质不等于普通分红。确认年度完整或零分红也须填写核对依据。</p><div id="df-events"></div><div id="df-confirmations"></div></details>
        <h3>预测参数</h3><div class="df-grid">
        <label>当前未复权价（元，可调整）${input('id="df-price"',state.price,num(m.price))}</label>
        <label>正常化窗口<select id="df-window"><option value="3">最近3完整年</option><option value="5">最近5完整年</option></select></label>
        <label>中性正常化方法<select id="df-method"><option value="median">中位数</option><option value="mean">平均值</option></select></label>
        <label>分红率依据<select id="df-payout"><option value="auto">自动：近3年，缺数据时用最近一年</option><option value="1">最近一年</option><option value="3">三年累计分红/累计利润</option><option value="5">五年累计分红/累计利润</option><option value="manual">逐年手填</option><option value="policy">公开政策（暂无结构化数据，请手填）</option></select></label>
        <label class="df-check"><input id="df-exclude" type="checkbox" ${state.exclude?'checked':''}>排除已标记特别分红</label>
        </div><p id="df-ratio-source" class="df-note"></p>
        <p class="df-note">只需调整中性参数，悲观与乐观按下方系数自动联动。绿色为自动值，金色为手动调整，清空并移开恢复自动。中性三个年度参数自动生成，后两年增长率默认0%持平，可逐年修改。编辑A/B当年增长率会切换中性模式到C。手动利润优先于增长率，手动现金金额优先于分红率。</p>
        ${['neutral'].map(s=>`<fieldset class="df-scenario" data-scenario="${s}"><legend>中性主参数 · 自动生成，可调整</legend><div class="df-grid">
          <label>利润模式<select data-s="${s}" data-config="mode"><option value="A">A 当年累计＋剩余预测</option><option value="B">B 历史正常化</option><option value="C">C 三年增长率</option><option value="D">D 机构预期</option></select></label>
          <label>C模式上年全年基准（亿元）${input(`data-s="${s}" data-config="base" data-scale="100000000"`,has(state.scenarios[s],'base')?state.scenarios[s].base/1e8:null,'留空用上年实际')}</label>
          <label>A/B全年利润调整（%）${input(`data-s="${s}" data-config="adjustment" data-scale="0.01"`,has(state.scenarios[s],'adjustment')?state.scenarios[s].adjustment*100:null,'默认0')}</label>
          <label>A/B正常化金额调整（亿元）${input(`data-s="${s}" data-config="remaining" data-scale="100000000"`,has(state.scenarios[s],'remaining')?state.scenarios[s].remaining/1e8:null,'默认0')}</label>
          <label>未来三年统一分红率（%）${input(`id="df-uniform-payout" data-s="${s}" data-config="uniform_payout_ratio" data-scale="0.01" aria-label="未来三年统一分红率"`,has(state.scenarios[s],'uniform_payout_ratio')?state.scenarios[s].uniform_payout_ratio*100:null,'留空恢复原逐年设置')}</label>
          </div><p id="df-consensus-status" class="df-note" role="status"></p><button type="button" id="df-consensus-reload" hidden>重新读取机构预期</button><p id="df-basis-${s}" class="df-note"></p>
          ${table(['预测财年','利润增长率 %','预测归母利润 亿元','分红率 %','预测现金分红 亿元','总股本 亿股'],payload.years.map((y,i)=>[
            `${y}E`,...['growth_rate','profit','payout_ratio','cash','shares'].map(field=>{
              const scale={growth_rate:.01,profit:1e8,payout_ratio:.01,cash:1e8,shares:1e8}[field],r=state.scenarios[s].rows[i];
              return input(`aria-label="${names[s]}${y}${{growth_rate:'增长率',profit:'利润覆盖',payout_ratio:'分红率',cash:'现金覆盖',shares:'股本'}[field]}" data-s="${s}" data-i="${i}" data-field="${field}" data-scale="${scale}"`,has(r,field)?r[field]/scale:null,'自动/待补');
            })]))}<label>该情景手动参数依据${input(`data-s="${s}" data-config="note"`,state.scenarios[s].note,'例如公告、利润正常化假设','text')}</label>
          </fieldset>`).join('')}
        <h3>悲观与乐观 · 自动情景系数</h3>
        <p class="df-note">三个年度分别在中性结果上应用一次系数，不逐年叠乘。正利润按系数缩放；中性为亏损时，低于1会扩大亏损，高于1会减小亏损。中性手动指定现金金额时，沿用该金额假设按利润系数和分红率系数各缩放一次。系数不是公司预测。</p>
        <div class="df-grid">${['pessimistic','optimistic'].map(s=>`<label>${names[s]}利润系数（倍）${input(`data-factor-s="${s}" data-factor="profit_factor" aria-label="${names[s]}利润系数"`,state.factors[s].profit_factor,'历史参考/模型假设')}</label>`).join('')}</div>
        <p id="df-factor-source" class="df-note"></p><button type="button" id="df-reset-factors">恢复默认情景系数</button>
        <details><summary>更多情景系数：分红率与股本（默认沿用中性）</summary><div class="df-grid">${['pessimistic','optimistic'].flatMap(s=>[['payout_factor','分红率'],['shares_factor','股本']].map(([f,label])=>`<label>${names[s]}${label}系数（倍）${input(`data-factor-s="${s}" data-factor="${f}" aria-label="${names[s]}${label}系数"`,state.factors[s][f],'默认1')}</label>`)).join('')}</div><p class="df-note">分红率系数不能为负，股本系数须大于0；股本不因悲观/乐观自动反向调整。自定义系数组合可能使股息率次序变化，以实际计算为准。</p></details>
        <h3>年度预测结果与情景对比</h3><div id="df-results" aria-live="polite"></div>
        <h3>辅助分析</h3><div id="df-sustainability"></div><div class="df-grid">
        <label>目标情景<select id="df-target-s">${scenarioKeys.map(s=>`<option value="${s}">${names[s]}</option>`).join('')}</select></label>
        <label>目标年度<select id="df-target-y">${payload.years.map((y,i)=>`<option value="${i}">${y}E</option>`).join('')}</select></label>
        <label>目标预期股息率（%）${input('id="df-target-rate"',state.targetRate)}</label></div><p id="df-target" aria-live="polite"></p>
        <p class="df-note">预测股息率＝预测DPS÷当前未复权价；不代表未来市场价格下的实际股息率。三年累计按财年归属，可能包含买入前已除权部分，不等于从今天持有三年能收到的现金；不是年化收益率。不含股息税、再投资及股价变动。目标买入价仅是股息率参考，不是DCF内在价值。</p>`;
      el('window').value=state.window;el('method').value=state.method;el('payout').value=state.payoutMode;
      el('target-s').value=state.targetScenario;el('target-y').value=state.targetYear;
      root.querySelector('[data-s="neutral"][data-config="mode"]').value=state.scenarios.neutral.mode;
      const recentEvents=payload.events.filter(e=>Number(e.report_period.slice(0,4))>=payload.years[0]-5);
      el('events').innerHTML=table(['归属期/期间','预案/公告/除权','状态','DPS 元','来源股数 亿股','估算金额 亿元','特别性质','金额覆盖 亿元','人工依据'],recentEvents.map(e=>{
        const edit=state.edits[e.id]||{};
        return [esc(e.report_period)+(e.report_period.endsWith('12-31')?' 年度所属期':' 中期所属期'),[e.proposal_date,e.notice_date,e.ex_date].map(d=>esc(d||'未记录')).join('<br>'),({paid:'已实施且已除权',pending:'待实施或尚未除权',cancelled:'已取消'}[e.state]||'待核实')+(e.conflict?' 身份冲突':''),num(e.dps,2),amount(e.shares),amount(e.cash),
          `<select data-event="${esc(e.id)}" data-edit="kind" aria-label="${esc(e.report_period)}特别性质"><option value="unknown">未知</option><option value="regular">普通</option><option value="special">特别</option></select>`,
          input(`data-event="${esc(e.id)}" data-edit="cash" data-scale="100000000" aria-label="${esc(e.report_period)}事件金额覆盖"`,has(edit,'cash')?edit.cash/1e8:null,'留空用估算'),
          input(`data-event="${esc(e.id)}" data-edit="note" aria-label="${esc(e.report_period)}事件依据"`,edit.note,'核对依据','text')];
      }));
      root.querySelectorAll('[data-edit="kind"]').forEach(node=>{node.value=state.edits[node.dataset.event]?.kind||'unknown';});
      el('confirmations').innerHTML=table(['年度','核实完整','确认不分红','核对依据（必填）'],payload.history.map(h=>{
        const c=state.confirmations[String(h.year)]||{};
        return [h.year,`<input aria-label="${h.year}完整" type="checkbox" data-year="${h.year}" data-confirm="complete" ${c.complete?'checked':''}>`,
          `<input aria-label="${h.year}不分红" type="checkbox" data-year="${h.year}" data-confirm="zero" ${c.zero?'checked':''}>`,
          input(`data-year="${h.year}" data-confirm="note" aria-label="${h.year}完整性依据"`,c.note,'公告/财报及核对说明','text')];
      }));
      el('reset').onclick=()=>{consensusController?.abort();consensusSerial++;consensus=null;consensusLoading=false;consensusError='';state=defaults();persist();scaffold();};
      el('reset-factors').onclick=()=>{state.factors={pessimistic:{},optimistic:{}};persist();render();};
      el('reload').onclick=()=>load(true);
      el('consensus-reload').onclick=()=>loadConsensus();
      render(refreshed?'本地依据已更新；保留手动参数，请核对适用性。':'');
    }
    function render(message='',editing=null) {
      const hist=history(),price=has(state,'price')?state.price:payload.market.price,ratio=ratioDefault(hist);
      const pendingEvidence=Object.values(state.edits).some(e=>(has(e,'cash')||e.kind&&e.kind!=='unknown')&&!String(e.note||'').trim());
      el('status').textContent=[message,...payload.warnings.map(w=>w.message),state.legacy_scenarios?'已改为中性联动情景；旧版悲观/乐观独立参数已停用，原值保留在计算证据中。':'',pendingEvidence?'有事件人工标记/金额尚未填写依据，暂不应用。':'',storageError?'浏览器存储不可用，当前仅保留内存参数。':'',`本地读取时间 ${loadedAt||''}`,savedAt?`手动参数保存时间 ${new Date(savedAt).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}`:''].filter(Boolean).join(' ');
      el('history').innerHTML=table(['财年','归母利润 亿元','现金分红 亿元','分红率','原始DPS 元','FCF 亿元','FCF覆盖 倍','资料状态'],hist.map(r=>[r.year,amount(r.profit),amount(r.cash),pct(r.payout_ratio),num(r.dps,2),payload.sustainability.financial_company?'不适用':amount(r.fcf),payload.sustainability.financial_company?'不适用':num(r.coverage),esc(r.state)+(r.unknown?'；特别性质未核实':'')+(r.excluded?`；排除${r.excluded}笔`:'')+(r.payout_ratio>1?'；分红超过利润':'')]));
      const ratioWindow=state.payoutMode==='auto'?(payout_basis(hist,payload.years[0],3)!==null?3:1):Number(state.payoutMode);
      el('ratio-source').textContent=`自动分红率参考：${pct(ratio)}。${ratio===null?'所选方式缺少适用数据，或已选择手填/公开政策。':`${payload.years[0]-ratioWindow}—${payload.years[0]-1}年累计已实施现金分红÷累计归母利润，已自动用于三年预测。`} 金额沿用本地估算；覆盖未核实不阻止生成参考值，非公司承诺。${state.exclude?'仅排除已标记特别分红，未知性质仍保留并提示。':'当前包含已知全部派息，特别性质未知。'} ${hist.some(r=>r.profit!==null&&r.profit<=0)?'历史存在非正利润年份，累计比例可能失真，请核对。':''} 缺年、缺金额或仍有待实施事件的窗口不自动使用。`;
      showValue(el('price'),price,has(state,'price'),'本地未复权收盘价 '+(payload.market.price_date||''),editing);
      const results={};
      const neutralParameters=scenarioParameters('neutral',hist);
      const factorDefaults=scenario_factors(payload.reports,payload.years[0],neutralParameters.mode,state.window,state.method);
      const effectiveFactors={};
      for(const s of ['neutral']) {
        const params=neutralParameters,config=state.scenarios[s];
        results[s]=calculate_scenario(payload.years,params,price);
        const basis=params.basis;
        el('basis-'+s).textContent=`${params.mode==='C'?'C模式基准为上年全年利润 '+amount(params.base_profit)+'亿元。':basis.reason+'；当年自动利润 '+amount(params.first_profit)+'亿元。'} ${basis.samples.length?'历史样本（亿元）：'+basis.samples.map(amount).join('、')+'；年份 '+basis.periods.join('、')+'。':''} ${basis.realized!==null?'当年已实现 '+amount(basis.realized)+'亿元。':''} 悲观/乐观仅为历史分位假设。${basis.samples.some(v=>v!==null&&v<0)?'样本存在负值，请核对周期性及正常化。':''}`;
        if(params.mode==='D')el('basis-'+s).textContent='逐年采用同花顺机构净利润预测均值；增长率由预测利润反推。缺失年份不递推，悲观/乐观继续按情景系数计算。';
        for(const [field,value,source] of [['base',params.base_profit,'上年实际归母利润'],['adjustment',has(config,'adjustment')?config.adjustment:0,'默认不调整'],['remaining',has(config,'remaining')?config.remaining:0,'默认不调整']]) {
          const node=root.querySelector(`[data-s="${s}"][data-config="${field}"]`);
          showValue(node,value,has(config,field),source,editing);node.disabled=params.mode==='D';
        }
        const uniform=has(config,'uniform_payout_ratio'),uniformNode=el('uniform-payout');
        if(uniformNode!==editing)uniformNode.value=uniform&&finite(config.uniform_payout_ratio)!==null?String(config.uniform_payout_ratio*100):'';
        uniformNode.classList.toggle('df-manual-input',uniform);
        uniformNode.nextElementSibling.textContent='统一设置三年，留空恢复默认';
        root.querySelectorAll(`[data-s="${s}"][data-field]`).forEach(node=>{
          const i=Number(node.dataset.i),f=node.dataset.field;
          const result=results[s].rows[i],institutional=params.mode==='D'&&['profit','growth_rate'].includes(f),manual=!institutional&&has(config.rows[i],f);
          const automatic={profit:result.profit,cash:result.cash,growth_rate:params.mode==='D'||i===0&&params.mode!=='C'||has(config.rows[i],'profit')?result.growth_rate:0,
            payout_ratio:has(config.rows[i],'cash')?result.payout_ratio:ratio,shares:payload.market.shares};
          const source={profit:i===0?'利润模型计算':'上年有效利润逐年递推',cash:'有效利润×分红率',growth_rate:i===0&&params.mode!=='C'?'由全年利润反推同比':'增长假设；默认0%',
            payout_ratio:has(config.rows[i],'cash')?'按手动现金金额反推':`${ratioWindow}年累计分红/累计利润`,shares:'沿用本地总股数，默认不变'};
          if(institutional)source[f]=f==='profit'?`同花顺净利润均值 · ${consensus?.rows[i]?.institutions??'—'}家机构`:'机构预测利润反推同比';
          const unified=uniform&&f==='payout_ratio';
          showValue(node,unified?params.rows[i].payout_ratio:manual?config.rows[i][f]:automatic[f],manual||unified,source[f],editing);
          node.disabled=unified||institutional;
          if(unified)node.nextElementSibling.textContent='三年统一设置 · 清空上方后可逐年调整';
          if((manual||unified)&&(f==='growth_rate'&&has(config.rows[i],'profit')||f==='payout_ratio'&&has(config.rows[i],'cash')))node.nextElementSibling.textContent+='；'+(f==='growth_rate'?'利润调整优先，有效同比 '+pct(result.growth_rate):'现金金额优先，有效分红率 '+pct(result.payout_ratio));
        });
      }
      for(const s of ['pessimistic','optimistic']) {
        effectiveFactors[s]={...factorDefaults[s],...state.factors[s]};
        results[s]=derive_scenario(payload.years,neutralParameters,price,effectiveFactors[s]);
        root.querySelectorAll(`[data-factor-s="${s}"]`).forEach(node=>{
          const field=node.dataset.factor,manual=has(state.factors[s],field);
          showValue(node,effectiveFactors[s][field],manual,field==='profit_factor'?factorDefaults[s].source:'默认1倍，沿用中性',editing);
        });
      }
      const inConsensus=state.scenarios.neutral.mode==='D';
      el('consensus-reload').hidden=!inConsensus;el('consensus-reload').disabled=consensusLoading;
      const obtained=new Date(consensus?.obtained_at),obtainedText=Number.isNaN(obtained.getTime())?'未记录':obtained.toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
      el('consensus-status').textContent=!inConsensus?'':consensusLoading?'正在读取同花顺机构预期…':consensusError?'机构预期读取失败：'+consensusError:consensus?`${consensus.source} · 取得时间 ${obtainedText}（北京时间） · 三年预测仅保留在当前页面内存，不写入数据库或浏览器存储。${(consensus.warnings||[]).join('；')}`:'请选择重新读取机构预期';
      if(inConsensus&&consensus&&!consensusLoading&&!consensusError) {
        const link=document.createElement('a');
        link.href=`https://stockpage.10jqka.com.cn/${encodeURIComponent(code)}/worth/#forecastdetail`;
        link.textContent='查看同花顺预测页面';link.target='_blank';link.rel='noopener noreferrer';
        el('consensus-status').append(' · ',link);
      }
      el('factor-source').textContent=['pessimistic','optimistic'].map(s=>`${names[s]}利润系数 ${num(effectiveFactors[s].profit_factor,4)} 倍；${has(state.factors[s],'profit_factor')?'手动调整':factorDefaults[s].source}`).join('。')+'。中性参数变化后，两个情景的全部年度结果自动重新计算。';
      root.querySelectorAll('[data-edit="cash"]').forEach(node=>{
        const id=node.dataset.event,edit=state.edits[id]||{},event=payload.events.find(e=>e.id===id);
        showValue(node,has(edit,'cash')?edit.cash:event.cash,has(edit,'cash'),'事件DPS×来源股数估算',editing);
      });
      el('results').innerHTML=scenarioKeys.map(s=>`<h4>${names[s]} · ${s==='neutral'?state.scenarios.neutral.mode+'模式':'中性参数×情景系数'}</h4>${table(['财年','归母利润 亿元','有效同比','分红率','现金分红 亿元','总股本 亿股','DPS 元','预期股息率','提示'],results[s].rows.map(r=>[`${r.year}E`,amount(r.profit),pct(r.growth_rate),pct(r.payout_ratio),amount(r.cash),amount(r.shares),num(r.dps,2),pct(r.yield_rate),esc(r.warnings.join('；'))]))}<p class="df-total">三财年累计DPS ${num(results[s].cumulative_dps,2)} 元/股 · 累计股息回报 ${pct(results[s].cumulative_yield)}${results[s].cumulative_dps===null?'（存在缺失，不累计已知部分冒充完整值）':''}</p>`).join('');
      const completed=hist.filter(r=>r.year<payload.years[0]),last=completed.at(-1),three=completed.slice(-3);
      const usable=three.every(r=>!r.pending&&r.fcf!==null&&r.cash!==null),totalCash=finite(three.reduce((a,r)=>a+(r.cash||0),0));
      const fcf3=usable?finite(three.reduce((a,r)=>a+r.fcf,0)):null;
      const cover3=fcf3!==null&&totalCash!==null&&totalCash>0?finite(fcf3/totalCash):null;
      const sus=payload.sustainability;
      const debtNames={SHORTTERMBORR:'短期借款',SHORTTERMBDSPAYA:'短期应付债券',DUENONCLIAB:'一年内到期非流动负债',LONGBORR:'长期借款',BDSPAYA:'应付债券',LEASELIAB:'租赁负债'};
      el('sustainability').innerHTML=sus.financial_company?'<p class="df-note">金融企业：标准FCF及简化净债务不适用；仍可使用利润和分红预测。</p>':
        `<p>最近一年FCF覆盖参考：${!last.pending?num(last.coverage):'—（仍待实施）'} 倍；最近三年累计FCF覆盖参考：${num(cover3)} 倍。已按本地已实施金额自动计算，年度完整性未核实则仅作估算参考。</p>
        <p>最新资产负债表期 ${esc(sus.period||'未记录')}：货币资金 ${amount(sus.cash)} 亿元；${sus.missing_debt.length?'已知债务部分合计':'简化有息债务'} ${amount(sus.debt)} 亿元；净现金 ${amount(sus.net_cash)} 亿元。${sus.missing_debt.length?'债务缺项：'+esc(sus.missing_debt.map(k=>debtNames[k]||k).join('、'))+'，不能判定净现金/净负债。':''}</p>
        <p class="df-note">FCF＝经营现金流−资本开支。覆盖率按分红归属财年匹配，不是实际支付年度口径；零分红覆盖倍数不适用。货币资金不等于可分配现金。覆盖倍数不是风险评级。</p>
        <p class="df-note">${fcf3===null?'历史连续三年FCF或已实施分红数据不足，未生成现金压力比较。':scenarioKeys.map(s=>`${names[s]}：${results[s].rows.some(r=>r.cash!==null&&r.cash>fcf3/3)?'有预测年度现金分红超过历史三年平均FCF，请核对现金流压力':'预测分红未超过历史三年平均FCF或存在缺值，不能据此确定安全'}`).join('；')+'。沿用历史FCF仅为压力假设，不是未来现金流预测。'}</p>`;
      const selected=results[state.targetScenario]?.rows[Number(state.targetYear)],target=target_price(selected?.dps,finite(state.targetRate)===null?null:state.targetRate/100);
      el('target').textContent=`目标买入参考价：${num(target)} 元。${target===null?'需有效正DPS和正目标股息率。':'基于所选年度/情景，不等于DCF内在价值。'}`;
      el('evidence').textContent=JSON.stringify({as_of_date:payload.as_of_date,calculated_at:payload.calculated_at,methodology_version:payload.methodology_version,market:payload.market,
        profit_sources:hist.map(r=>({year:r.year,sources:r.sources})),events:payload.events.map(e=>({id:e.id,source:e.source,content_hash:e.content_hash,obtained_at:e.obtained_at,run_id:e.run_id})),sustainability:payload.sustainability,
        automatic:{window:state.window,method:state.method,payoutMode:state.payoutMode,payout_ratio:ratio,scenario_factor_defaults:factorDefaults,effective_factors:effectiveFactors},manual_parameters:state},null,2);
    }
    function changed(event) {
      if(!payload||!state)return;
      const node=event.target,d=node.dataset;
      const value=node.type==='number'?node.value.trim()===''?undefined:finite(node.value):node.value;
      const set=(object,field,scale=1)=>{if(value===undefined)delete object[field];else object[field]=typeof value==='number'?finite(value*scale):value;};
      if(d.factorS&&d.factor)set(state.factors[d.factorS],d.factor);
      else if(d.s&&d.field) {
        set(state.scenarios[d.s].rows[Number(d.i)],d.field,Number(d.scale));
        if(d.field==='growth_rate'&&d.i==='0'&&value!==undefined) {
          state.scenarios[d.s].mode='C';root.querySelector(`[data-s="${d.s}"][data-config="mode"]`).value='C';
        }
      } else if(d.s&&d.config)set(state.scenarios[d.s],d.config,Number(d.scale||1));
      else if(d.event&&d.edit) {state.edits[d.event]||={};set(state.edits[d.event],d.edit,Number(d.scale||1));}
      else if(d.year&&d.confirm) {state.confirmations[d.year]||={};state.confirmations[d.year][d.confirm]=node.type==='checkbox'?node.checked:value;}
      else {
        const field={price:'price',window:'window',method:'method',payout:'payoutMode',exclude:'exclude','target-s':'targetScenario','target-y':'targetYear','target-rate':'targetRate'}[node.id.replace('df-','')];
        if(!field)return;
        if(field==='exclude')state.exclude=node.checked;
        else if(field==='window'||field==='targetYear')state[field]=Number(value);
        else set(state,field);
      }
      persist();render('',node);
      if(d.config==='mode') {
        if(value==='D') {if(!consensusLoading)loadConsensus();}
        else {consensusController?.abort();consensusSerial++;consensusLoading=false;consensus=null;consensusError='';render();}
      }
    }
    body.addEventListener('input',changed);
    body.addEventListener('focusout',event=>{if(event.target.type==='number'&&payload&&state)render();});
    // Select/checkbox change is needed for keyboard and browser automation as well.
    body.addEventListener('change',event=>{if(event.target.tagName==='SELECT'||event.target.type==='checkbox')changed(event);});
    async function loadConsensus() {
      consensusController?.abort();consensusController=new AbortController();
      const request=++consensusSerial,requestedCode=code,year=payload.years[0];
      consensus=null;consensusError='';consensusLoading=true;render();
      try {
        const response=await fetch('/api/institution-forecast?'+new URLSearchParams({code:requestedCode}),{signal:consensusController.signal,cache:'no-store'});
        const data=await response.json();
        if(request!==consensusSerial||requestedCode!==code||year!==payload.years[0]||state.scenarios.neutral.mode!=='D')return;
        if(!response.ok)throw new Error(data.error||'同花顺暂不可用');
        if(data.code!==code||JSON.stringify(data.years)!==JSON.stringify(payload.years)||!Array.isArray(data.rows)||data.rows.length!==3||data.rows.some((r,i)=>r.year!==payload.years[i]))throw new Error('机构预期股票或年度不匹配');
        consensus=data;
      } catch(error) {
        if(error.name!=='AbortError'&&request===consensusSerial)consensusError=error.message;
      } finally {
        if(request===consensusSerial){consensusLoading=false;render();}
      }
    }
    async function load(refresh=false) {
      if(root.hidden)return;
      if(payload&&!refresh&&!dirty)return;
      if(loadingCode===code&&!refresh)return;
      loadingCode=code;
      controller?.abort();controller=new AbortController();const request=++serial,requestedCode=code;
      try {
        const response=await fetch('/api/dividend-forecast?'+new URLSearchParams({code:requestedCode}),{signal:controller.signal,cache:'no-store'});
        const data=await response.json();
        if(request!==serial||requestedCode!==code)return;
        if(!response.ok)throw new Error(data.error||'本地预测资料读取失败');
        if(data.code!==code)throw new Error('返回股票不匹配');
        const previousYear=payload?.years[0];payload=data;dirty=false;loadedAt=new Date().toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
        if(previousYear!==undefined&&previousYear!==data.years[0]){state=null;consensusController?.abort();consensusSerial++;consensus=null;consensusLoading=false;consensusError='';}
        scaffold(refresh);
      } catch(error) {
        if(error.name==='AbortError'||request!==serial)return;
        if(payload&&el('status'))el('status').textContent='重读失败，保留此前依据：'+error.message;
        else {body.innerHTML=`<p role="alert">${esc(error.message)}</p><button type="button" id="df-retry">重试读取本地依据</button>`;el('retry').onclick=()=>load(true);}
      } finally {if(request===serial)loadingCode=null;}
    }
    function context(detail) {
      if(detail.code!==code) {consensusController?.abort();consensusSerial++;consensus=null;consensusLoading=false;consensusError='';controller?.abort();serial++;payload=null;state=null;savedAt=null;dirty=false;loadingCode=null;code=detail.code;body.innerHTML='<p class="df-note">正在读取本地预测依据…</p>';}
      root.hidden=detail.metric!=='dividend_yield';
      if(!root.hidden)load();
    }
    window.addEventListener('dividend-forecast-context',event=>context(event.detail));
    function facts(event) {if(event.detail?.code===code&&payload){dirty=true;clearTimeout(timer);timer=setTimeout(()=>load(true),250);}}
    window.addEventListener('dividend-forecast-facts',facts);
    window.addEventListener('financial-facts-updated',facts);
    window.addEventListener('dashboard-data-state',event=>{if(!event.detail?.updating)facts(event);});
    root.addEventListener('toggle',()=>{if(root.open)load();});
    const selected=document.querySelector('[data-valuation-metric].active')?.dataset.valuationMetric;
    context({code,metric:selected});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
})();
