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
    item.formula, item.reason, item.note].filter(Boolean).join(' · ');
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
  const meanings = {
    assets:'公司账面上拥有的全部资产，包括现金、存货、厂房设备和对外投资等，帮助了解资产规模。',
    equity:'全部资产扣除全部负债后，账面上属于股东的部分。',
    liabilities:'公司尚需偿还或履行的债务和付款义务，包括借款、应付货款等。',
    leverage:'每100元资产对应多少元负债，帮助了解公司总体负债程度。',
    debt:'借款、债券和租赁等融资债务的账面金额，帮助了解融资负担。',
    debt_ratio:'每100元资产对应多少元融资债务，帮助了解资产对融资债务的覆盖情况。',
    gross_margin:'每100元营业收入扣除营业成本后，还剩多少元用于支付期间费用、税费并形成利润。',
    parent_net_margin:'每100元营业收入，最终形成多少元归属于母公司股东的净利润。',
    net_margin:'每100元营业收入，最终形成多少元包含少数股东损益的合并净利润。',
    cash_profit:'经营活动产生的净现金相当于合并净利润的多少，帮助观察利润的现金支持情况。',
    cash_after_capex:'经营活动产生的净现金，扣除购建厂房、设备等长期资产的支出后，还剩多少现金。',
    gross_profit:'营业收入扣除营业成本后的金额，还需要承担期间费用和税费等。',
    period_expenses:'销售、管理、研发和财务费用的合计，反映这些费用对利润的影响。',
    TOTASSET:'公司账面上拥有的全部资产，反映资产规模。',
    TOTLIAB:'公司尚需偿还或履行的全部债务和付款义务。',
    RIGHAGGR:'资产扣除负债后属于全体股东的账面权益。',
    TOTSHAREQUI:'资产扣除负债后属于全体股东的账面权益。',
    PARESHARRIGH:'合并股东权益中归属于母公司股东的部分，不包含少数股东权益。',
    MINYSHARRIGH:'在利润表中表示属于子公司其他股东的损益；在资产负债表中表示他们拥有的权益。',
    CURFDS:'账面现金、银行存款等资金；部分可能受限，并非全部可自由使用。',
    ACCORECE:'已确认销售但尚未收到的客户款项，需要结合回款和坏账风险观察。',
    NOTESRECE:'公司持有、尚未到期收款的商业票据。',
    NOTESACCORECE:'尚未收回的客户款项及相关票据合计。',
    RECFINANC:'按相应金融资产分类列示的应收票据等，可用于持有收款或转让融资。',
    PREP:'已提前支付给供应商、但尚未完成结算的采购款。',
    INVE:'尚未销售的商品、原材料、在产品等，需结合周转和跌价风险观察。',
    GOODWILL:'并购中购买价超过所取得可辨认净资产公允价值份额的部分，未来可能发生减值。',
    INTAASSET:'专利、土地使用权等没有实物形态的长期资产的账面金额。',
    EQUIINVE:'对联营、合营或子公司等长期股权投资的账面金额。',
    FIXEDASSENET:'厂房、机器设备等固定资产扣除折旧和减值后的账面金额。',
    FIXEDASSECLEATOT:'固定资产及相关清理项目的账面合计金额。',
    CONSPROG:'正在建设、尚未达到预定可使用状态的工程投入。',
    CONSPROGTOT:'在建工程等相关项目的账面合计金额。',
    SHORTTERMBORR:'通常需要在一年内偿还的借款。',
    SHORTTERMBDSPAYA:'公司发行、需要在短期内偿还的债券。',
    LONGBORR:'期限较长的借款；已重分类的一年内到期部分另行列示。',
    BDSPAYA:'公司发行、尚未偿还的债券账面金额。',
    DUENONCLIAB:'原属长期负债、将在一年内到期的部分，可能包含借款、债券和租赁等。',
    LEASELIAB:'租赁合同形成、尚未支付的租赁付款义务的账面金额；一年内到期部分通常另行列示。',
    NOTESPAYA:'公司开出票据、尚未向供应商等支付的款项。',
    ACCOPAYA:'已采购商品或服务但尚未向供应商支付的款项。',
    NOTESACCOPAYA:'应付票据和应付账款合计，通常与采购结算有关。',
    CONTRACTLIAB:'已经收取或应收客户对价，但还需要向客户交付商品或服务的义务。',
    ADVAPAYM:'提前收到客户的款项，后续通常需要交付商品或服务。',
    COPEWORKERSAL:'应向员工支付、尚未结清的薪酬等。',
    TAXESPAYA:'按税法等规定应缴、尚未结清的税费。',
    LONGPAYA:'除长期借款和应付债券等以外的长期应付款，不能全部视为有息债务。',
    LONGPAYATOT:'长期应付款相关项目的合计，是否有息需要查看明细。',
    BIZINCO:'销售商品、提供服务等业务确认的收入，不等于已经收到的现金。',
    BIZTOTINCO:'营业收入及适用行业利息、保费等收入项目的合计，具体组成随行业而异。',
    BIZCOST:'与营业收入对应的商品或服务成本，不包含全部期间费用。',
    BIZTOTCOST:'营业成本、期间费用等项目的合计，不能直接代替营业成本计算毛利率。',
    BIZTAX:'经营业务相关的税金及附加，不等于所得税费用。',
    SALESEXPE:'销售和推广商品、服务发生的费用。',
    MANAEXPE:'组织和管理公司经营发生的费用。',
    DEVEEXPE:'当期计入损益的研发费用，不等于全部研发现金投入。',
    FINEXPE:'筹集资金等形成的净费用；负数可能表示利息收入等超过相关支出。',
    PERPROFIT:'营业收入扣除相关成本费用，并计入营业相关收益和损失后的利润。',
    OPERPROFIT:'营业相关收入、成本费用及收益损失形成的利润。',
    TOTPROFIT:'扣除所得税前的利润，包括营业利润和营业外收支。',
    INCOTAXEXPE:'当期确认的所得税费用，不等于当期实际缴纳的所得税现金。',
    INCOTAX:'当期确认的所得税费用。',
    NETPROFIT:'扣除所得税后的合并利润，包含属于少数股东的部分。',
    PARENETP:'合并净利润中属于母公司股东的部分，包含非经常性损益。',
    NETPARECOMPPROF:'合并净利润中属于母公司股东的部分，包含非经常性损益。',
    NPCUT:'从归母净利润中扣除非经常性损益后的利润，帮助观察较常规的盈利表现。',
    OTHERINCO:'按会计规定计入其他收益的金额，可能包括政府补助等。',
    INVEINCO:'投资产生的损益，不等于当期实际收到的投资现金。',
    VALUECHGLOSS:'相关资产或负债公允价值变动形成的损益，可能尚未实现现金收支。',
    CREDITIMPLOSSEPROFIT:'对信用损失风险确认的损益，需按报表正负号判断对利润的影响。',
    ASSEIMPALOSSPROFIT:'资产发生减值等形成的损益，需按报表正负号判断对利润的影响。',
    ASSETSDISLINCO:'出售或处置相关资产形成的损益，不等于收到的全部处置现金。',
    NONOREVE:'与日常经营活动没有直接关系的收入。',
    NONOEXPE:'与日常经营活动没有直接关系的支出。',
    MANANETR:'销售、采购、员工薪酬和税费等经营收付款相抵后的净现金。',
    INVNETCASHFLOW:'投资回收和资产处置等收款，减去购建资产和对外投资等付款后的净现金。',
    FINNETCFLOW:'借款、发行权益等收到的现金，减去还款、分红等支付的现金后的净额。',
    ACQUASSETCASH:'购建固定资产、无形资产和其他长期资产实际支付的现金，作为本页资本支出。',
    LABORGETCASH:'销售商品和提供服务实际收到的现金，可能包含以前期间收入的回款。',
    LABOPAYC:'购买商品和接受服务实际支付的现金。',
    PAYWORKCASH:'支付给员工及为员工支付的现金。',
    PAYTAX:'实际支付的各项税费现金。',
    BIZCASHINFL:'经营活动收到的现金合计，尚未扣除经营付款。',
    BIZCASHOUTF:'经营活动支付的现金合计。',
    INVCASHINFL:'收回投资、投资收益和资产处置等收到的现金合计，可能含本金回收。',
    INVCASHOUTF:'购建长期资产和对外投资等支付的现金合计，不全部属于本页Capex。',
    INVPAYC:'对外投资实际支付的现金。',
    RECEFROMLOAN:'取得借款实际收到的现金，不属于经营收入。',
    DEBTPAYCASH:'偿还债务本金实际支付的现金。',
    DIVIPROFPAYCASH:'分配股利、利润或偿付利息支付的现金。',
    FINCASHINFL:'筹资活动收到的现金合计。',
    FINCASHOUTF:'筹资活动支付的现金合计。',
    CHGEXCHGCHGS:'汇率变动对现金及现金等价物折算金额的影响。',
    EXCHCHGCASHEFFE:'汇率变动对现金及现金等价物折算金额的影响。',
    CASHNETR:'期末现金及现金等价物相较期初增加或减少的金额。',
    CASHEQUINETINCR:'期末现金及现金等价物相较期初增加或减少的金额。',
    INICASHBALA:'所选期间开始时的现金及现金等价物余额。',
    CASHEQUIOPENBALA:'所选期间开始时的现金及现金等价物余额。',
    FINALCASHBALA:'所选期间结束时的现金及现金等价物余额，与货币资金口径不同。',
    CASHEQUFINBALA:'所选期间结束时的现金及现金等价物余额，与货币资金口径不同。',
  };
  const meaningText = (item,key) => meanings[key] || (item?.field==='MINYSHARRIGH'?(item.item_source==='fzb'?'合并子公司中属于其他股东的权益，不属于母公司股东。':'合并净利润中属于子公司其他股东的损益。'):meanings[item?.field]) || (
    item?.balance_side==='liabilities'?`“${item.label}”反映公司这类债务或付款义务的期末账面金额。`:
    item?.balance_side==='equity'?`“${item.label}”反映股东权益中这一部分的期末账面金额。`:
    item?.balance_side==='assets'?`“${item.label}”反映公司这类资产的期末账面金额。`:
    `“${item?.label || '该指标'}”是原报表列示的财务项目，帮助了解该项经营、资产或现金情况；具体组成需结合财报附注。`);
  Object.assign(meanings,{
    OTHERCURRASSE:'除单列项目以外的其他流动资产，具体组成需查财报附注。',
    OTHERNONCASSE:'除单列项目以外的其他非流动资产，具体组成需查财报附注。',
    OTHERCURRELIABI:'除单列项目以外的其他流动负债，不能全部视为有息债务。',
    OTHERNONCLIABI:'除单列项目以外的其他非流动负债，具体义务需查财报附注。',
    TOTCURRASSET:'通常在正常营业周期内变现、出售或耗用的资产等流动资产的合计。',
    TOTALNONCASSETS:'除流动资产以外的长期资产合计。',
    TOTALCURRLIAB:'通常在正常营业周期内或短期内需要结清的负债合计。',
    TOTALNONCLIAB:'除流动负债以外的长期负债合计。',
    CASHCENBANK:'银行等存放在中央银行的款项，可能包括不能自由动用的准备金。',
    PLAC:'拆出给其他金融机构的资金，主要用于观察金融企业资金运用。',
    FINCOSTSPAIDCASH:'按来源科目列示的资金运用金额，具体交易性质需结合财报附注。',
    LENDANDLOANCUST:'向客户发放贷款和垫款形成的资产，需结合信用风险观察。',
    LENDANDLOAN:'向客户发放贷款和垫款形成的资产，需结合信用风险观察。',
    LOANADVANCES:'向客户发放贷款和垫款形成的资产，需结合信用风险观察。',
    TRADFINASSET:'按公允价值计量、变动计入损益的金融资产，金额可能随市场波动。',
    HOLDINVEDUE:'按来源会计分类列示的持有至到期投资账面金额。',
    CLIEDEPO:'客户等存入的款项，是金融企业需要偿还的负债。',
    DEPOSIT:'客户等存入的款项，是金融企业需要偿还的负债。',
    FDSBORR:'从其他金融机构等拆入的资金，属于融资负债。',
    NETINTEINCO:'利息收入扣除利息支出后的净额，反映金融企业利息业务收入。',
    NETPOUNINCO:'手续费及佣金收入扣除相关支出后的净额。',
    NETINVINCO:'投资业务形成的净收益，具体组成以报表附注为准。',
    EARNPREM:'按保险业务会计口径确认的已赚保费，不等于当期全部保费收款。',
    ASSEIMPALOSS:'资产减值对当期损益的影响，需结合来源正负号判断。',
  });
  const calculationText = (item,unit,key) => {
    const inputs=item?.inputs || [];
    if(!inputs.length)return '';
    const v=inputs.map(x=>x.value),n=inputs.map(x=>amount(x,unit));
    if(key==='debt')return '有效分项合计：'+inputs.filter(x=>x.value!==null).map(x=>amount(x,unit)).join(' ＋ ')+(v.some(x=>x!==null)?' ＝ '+amount(item,unit):'全部缺失，无法计算');
    if(v.some(x=>x===null||x===undefined))return '所需计算／核对值缺失，无法完整代入公式。';
    if(['leverage','debt_ratio','parent_net_margin','net_margin','cash_profit'].includes(key))return v[1]>0?`代入公式：${n[0]} ÷ ${n[1]} × 100% ＝ ${(v[0]/v[1]*100).toFixed(2)}%`:'分母非正，不计算比率。';
    if(key==='gross_margin')return v[0]>0?`代入公式：（${n[0]} − ${n[1]}） ÷ ${n[0]} × 100% ＝ ${((v[0]-v[1])/v[0]*100).toFixed(2)}%`:'营业收入非正，不计算毛利率。';
    if(['cash_after_capex','gross_profit','equity'].includes(key))return `${key==='equity'?'核对公式':'代入公式'}：${n[0]} − ${n[1]} ＝ ${amount({value:v[0]-v[1],unit:inputs[0].unit},unit)}`;
    return '';
  };
  const originText = item => !item ? '该期间无适用资料' : [
    item.source ? '新浪'+({gjzb:'关键指标',fzb:'资产负债表',lrb:'利润表',llb:'现金流量表'}[item.source] || item.source) : '',
    item.field ? '字段 '+item.field : '',item.item_source?'底层表 '+item.item_source:'',
    item.method,item.baseline_source?'差分基期资料来源 '+item.baseline_source:'',
  ].filter(Boolean).join(' · ') || '该期间无适用资料';
  const inputHTML = (item,unit) => (item?.inputs || []).map(x=>`<li>${esc(x.label)}：${esc(amount(x,unit))}（${esc(x.period || '报告期待补')}）${x.reason?' · '+esc(x.reason):''}${x.inputs?`<ul>${inputHTML(x,unit)}</ul>`:''}</li>`).join('');
  const actualSources = item => {
    const origins=new Map();
    const visit=x=>{
      if(!x)return;
      if(x.source || x.field){
        const key=[x.source,x.item_source,x.method,x.baseline_source].join(':');
        if(!origins.has(key))origins.set(key,{...x,fields:new Set()});
        if(x.field)origins.get(key).fields.add(x.field);
      }
      (x.inputs || []).forEach(visit);
    };
    visit(item);
    return [...origins.values()].map(x=>originText({...x,field:[...x.fields].join('、')})).join('；') || originText(item);
  };
  const valueHTML = (item,unit,title,context) => {
    if(!item)return `<div class="fs-source-values"><p>${esc(title)}：待补。</p></div>`;
    const balance=context.kind==='fzb'||item.source==='fzb'||/^(期初|期末)/.test(item.label || '');
    const windowText=/^期初/.test(item.label || '')?'所选期间期初余额':balance?'期末余额':context.mode==='quarter'?'单季度':'本年累计';
    const scope=[item.currency,item.scope].filter(Boolean).join(' · ');
    return `<div class="fs-source-values"><p>${esc(title)} ${esc(item.period || '报告期待补')}：${esc(amount(item,unit))}${title==='本期'?' · '+esc(windowText)+(scope?' · '+esc(scope):''):''}</p></div>`;
  };
  const sourceBodyHTML = (item,old,unit,key,context={}) => {
    const formula=item?.formula || (item?.method==='直接取数'?'直接读取报表项目，不另行加总。':item?.method || '该报告期缺少适用资料');
    const calculation=calculationText(item,unit,key);
    return `<section class="fs-source-item"><h4>${esc(item?.label || '待补指标')}</h4>`+
      `<p><b>指标意义：</b>${esc(meaningText(item,key))}</p>`+
      `<p><b>计算方法／来源：</b>${esc(formula)}</p>`+
      `<div><b>口径说明：</b>${valueHTML(item,unit,'本期',context)}${valueHTML(old,unit,'比较期',context)}`+
      `<p>实际来源：${esc(actualSources(item))}</p>`+
      (item?.inputs?.length?`<p>本期参与计算／核对的具体值：</p><ul>${inputHTML(item,unit)}</ul>`:'')+
      (calculation?`<p>${esc(calculation)}</p>`:'')+
      (item?.baseline_period?`<p>差分基期：${esc(item.baseline_period)} · 字段 ${esc(item.baseline_field || '待补')}</p>`:'')+
      (item?.reason?`<p>缺值／不适用原因：${esc(item.reason)}</p>`:'')+
      (item?.note?`<p>${esc(item.note)}</p>`:'')+
      '<p>金额显示四舍五入，计算使用原始精度；缺失值不代表零。</p></div></section>';
  };
  const sourceHTML = (item,unit='亿元',old,key,context={}) => `<details class="fs-source"><summary>说明</summary>${sourceBodyHTML(item,old,unit,key,context)}</details>`;
  const shareText = (value,total,label) => `占${label}：`+(value!==null && value!==undefined && total>0 ? (value/total*100).toFixed(2)+'%' : '待补');
  const balanceShare = (item,values) => {
    const labels={assets:'总资产',liabilities:'总负债',equity:'股东权益'};
    const label=labels[item.balance_side];
    const total=label?values[item.balance_side]?.value:null;
    return {label:label || '适用合计',pct:item.value!==null && total>0 ? item.value/total*100:null,total};
  };
  // Export only the pure presentation helpers for the local regression tests.
  window.financialStatementFormat = {amount,sourceText,esc,waterfallLabel,waterfallTickFormat,cashChangeText,shareText,balanceShare,sourceHTML,sourceBodyHTML};
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
      return `<table class="fs-table"><thead><tr><th>科目</th><th>本期</th><th>占比</th><th>比较期</th><th>变动额</th><th>说明</th></tr></thead><tbody>`+
        rows.map(x=>{const old=prior.get(x.item_source+':'+x.field),ratio=kind==='fzb'?balanceShare(x,data.values):null;
          const share=x.unit==='元'&&x.value!==null?(kind==='fzb'?(ratio.pct===null?'—':ratio.pct.toFixed(2)+'%'):
            denominator>0?(x.value/denominator*100).toFixed(2)+'%':'—'):'—';
          const diff=x.value!==null&&old?.value!==null&&old?.value!==undefined&&old.unit===x.unit?amount({...x,value:x.value-old.value},currentUnit):'—';
          return `<tr><td>${esc(x.label)}</td><td class="${x.value===null?'fs-missing':''}" title="${esc(x.reason || '')}">${esc(amount(x,currentUnit))}</td><td>${share}</td><td>${esc(amount(old,currentUnit))}</td><td>${esc(diff)}</td><td>${sourceHTML(x,currentUnit,old,null,{kind,mode:data.mode})}</td></tr>`;}).join('')+'</tbody></table>';
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
      const metricItem = (key,group) => {
        const x=group[key];
        if(key==='equity')return {...x,label:'净资产',note:'合并股东权益合计，包含少数股东权益。',
          inputs:[group.assets,group.liabilities],formula:'直接读取所有者权益合计；用资产总额 − 负债总额核对。'};
        const checks={leverage:['liabilities','assets'],gross_margin:['revenue','cost'],net_margin:['net_profit','revenue']};
        const formulas={leverage:'负债总额 ÷ 资产总额 × 100%',gross_margin:'（营业收入 − 营业成本） ÷ 营业收入 × 100%',net_margin:'合并净利润 ÷ 营业收入 × 100%'};
        return checks[key]&&!x.inputs?{...x,inputs:checks[key].map(k=>group[k]),formula:'读取同口径来源比率，并用以下公式核对：'+formulas[key]}:x;
      };
      const metricHTML = key => {
        const x=metricItem(key,data.values),old=metricItem(key,data.baseline_values);
        return `<span class="fs-label">${esc(x.label)}</span><strong>${esc(amount(x,unit).replace(/元$/,''))}</strong><small>比较期：${esc(amount(old,unit).replace(/元$/,''))}${x.reason?'<br>'+esc(x.reason):''}</small>`;
      };
      el('fs-metrics').innerHTML=metricKeys.map(key=>{
        const extra=kind==='fzb'?{assets:'equity',leverage:'debt_ratio',liabilities:'debt'}[key]:kind==='lrb'&&key==='gross_margin'?'parent_net_margin':null;
        const keys=extra?[key,extra]:[key];
        const details=`<details class="fs-source fs-card-source"><summary>说明</summary>${keys.map(k=>sourceBodyHTML(metricItem(k,data.values),metricItem(k,data.baseline_values),unit,k,{kind,mode:data.mode})).join('')}</details>`;
        return `<div class="fs-metric${extra?' fs-metric-pair':''}"><div class="fs-metric-main">${metricHTML(key)}</div>${extra?`<div class="fs-metric-extra">${metricHTML(extra)}</div>`:''}${details}</div>`;
      }).join('');
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
      el('fs-notes').textContent='金额优先核对同口径关键指标，缺少适用值时回退三表。累计与单季度分别计算；余额科目不做季度差分。缺失字段、空值均不当作零，有息债务优先资产负债表，缺值回退关键指标，仅汇总有效分项，缺项列于来源且不代表零；一年内到期非流动负债可能包含非有息项目。货币资金可能含受限资金，不等于可用现金。自由现金流采用经营净现金流减 Capex，不等于严格 FCFF／FCFE；投资收益等不自动认定为非经常性损益。'+(data.financial_company?'金融企业不套用工业企业毛利率及自由现金流指标，完整科目以原报表为准。':'');
    }
    try{const tabs=['trend','statements','industry','maintenance'],requested=new URLSearchParams(window.location?.search||'').get('tab');const selected=tabs.includes(requested)?requested:sessionStorage.getItem('investment-research-tab-v1');if(tabs.includes(selected))selectTab(selected);}catch{}
  };
})();
