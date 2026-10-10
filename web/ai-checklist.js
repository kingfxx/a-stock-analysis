/* Convert the existing product-summary format without changing saved reports. */
window.checklistProductBlocks = function(text, evidence=[], corrections=[]) {
  const markers=[...text.matchAll(/(?:[\[【［]|^[ \t]*|\n[ \t]*)(20\d{2}年\s*(?:上半年|下半年|半年度|度|\d{1,2}\s*[-–—]\s*\d{1,2}月)?)(?:[\]】］]|\s*[｜|：:]\s*)/g)];
  if (!markers.length) return [{type:'text',text}];
  const blocks=[];
  const correctedPeriods=new Set();
  const paragraph=value=>{const cleaned=value.replace(/^[；;\s]+|[；;\s]+$/g,'');if(cleaned && !/^[。.]$/.test(cleaned))blocks.push({type:'text',text:cleaned});};
  paragraph(text.slice(0,markers[0].index));
  for(let i=0;i<markers.length;i++) {
    const marker=markers[i], section=text.slice(marker.index+marker[0].length,markers[i+1]?.index ?? text.length);
    const period=marker[1].slice(0,4)+(/上半年|半年度|1\s*[-–—]\s*6月/.test(marker[1]) ? '-06-30' : '-12-31');
    const correction=corrections.find(c=>c.period===period);
    if(correction) {
      if(!correctedPeriods.has(period)) {
        correctedPeriods.add(period);
        paragraph('该期原数值解析有误，以下按保存的财报原文重新核算；原生成描述及证据保留供核对。');
        for(const key of ['revenue_mix','profit_mix']) {
          if(!correction.metrics[key])continue;
          blocks.push({type:'table',caption:marker[1],headers:['产品 / 业务',key==='revenue_mix' ? '收入' : '毛利','占比'],rows:[],correction:correction.metrics[key],evidenceId:correction.evidence_id});
        }
        if(!Object.keys(correction.metrics).length)paragraph('该期表格无法可靠核算，金额及占比待核对。');
      }
      continue;
    }
    const rows=[...section.matchAll(/(?:^|[；;\n])\s*([^；;\n—]+?)\s*—\s*(?:(?:营业收入|收入|毛利|分部净利润|税前利润|利润)\s*)?(-?[0-9][0-9,.]*)\s*(亿元|万元|千元|元)\s*(?:[—，,]\s*([^；;\n]+)|[（(]\s*([-+]?\d+(?:\.\d+)?%)\s*[）)])/g)];
    if(!rows.length){paragraph(marker[0]+section);continue;}
    const preamble=section.slice(0,rows[0].index).trim().replace(/[；;]$/,'');
    const rowIncome=rows.some(row=>/收入占|营收占|营业收入/.test(row[4] || ''));
    const basis=/毛利/.test(preamble) ? '毛利' : /(?:收入|营收)/.test(preamble) ? '收入' : /税前/.test(preamble) ? '税前利润' : /净利润/.test(preamble) ? '分部净利润' : rowIncome ? '收入' : '金额';
    // Keep different units explicit rather than silently converting amounts.
    const units=new Set(rows.map(row=>row[3]));
    const unit=units.size===1 ? rows[0][3] : null;
    blocks.push({type:'table',caption:marker[1]+(preamble ? ' · '+preamble : ''),
      headers:['产品 / 业务',(basis==='金额' ? '金额' : basis)+(unit ? '（'+unit+'）' : ''),'占比 / 说明'],
      rows:rows.map(row=>[row[1].trim(),row[2]+(unit ? '' : row[3]),(row[4] || row[5]).trim().replace(/[。.]$/,'')])});
    let cursor=0;
    for(const row of rows){if(row.index>cursor && cursor>0)paragraph(section.slice(cursor,row.index));cursor=row.index+row[0].length;}
    paragraph(section.slice(cursor));
  }
  // For new snapshots, display verified numbers independently of model wording.
  const amount=(value,unit)=>{
    const yuan=value*({'元':1,'千元':1000,'万元':10000,'亿元':100000000}[unit]);
    const scale=Math.abs(yuan)>=100000000 ? 100000000 : Math.abs(yuan)>=10000 ? 10000 : 1;
    return new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(yuan/scale)+({1:'元',10000:'万元',100000000:'亿元'}[scale]);
  };
  for(const block of blocks) {
    if(block.type!=='table')continue;
    if(block.headers[1].startsWith('金额'))continue;
    const year=block.caption.match(/^20\d{2}/)?.[0];
    const period=year+(/上半年|1\s*[-–—]\s*6月/.test(block.caption.split(' · ')[0]) ? '-06-30' : '-12-31');
    const key=block.headers[1].includes('收入') ? 'revenue_mix' : 'profit_mix';
    let selected;
    for(const entry of evidence) {
      if(entry.metric!=='report_excerpt' || !Array.isArray(entry.value))continue;
      for(const page of entry.value) {
        const metric=page.business_metrics?.[key];
        if(metric?.period!==period || !Array.isArray(metric.rows) || !['元','千元','万元','亿元'].includes(metric.unit))continue;
        if(!selected || metric.classification==='产品')selected={metric,id:entry.id};
      }
    }
    if(block.correction)selected={metric:block.correction,id:block.evidenceId};
    if(!selected)continue;
    const m=selected.metric, measure=key==='revenue_mix' ? 'revenue' : 'profit';
    const label=key==='revenue_mix' ? '收入' : m.basis.includes('毛利') ? '毛利' : m.basis.includes('净利润') ? '分部净利润' : m.basis.includes('税前') ? '税前利润' : '利润';
    block.caption=block.caption.split(' · ')[0]+' · '+m.basis+' · 合计'+amount(m.denominator,m.unit);
    block.headers=['产品 / 业务',label+'金额',label==='收入' ? '收入占比' : '贡献占比'];
    block.rows=m.rows.map(row=>[row.name,row[measure]===null ? '资料缺失' : amount(row[measure],m.unit),row.share_pct===null ? '资料缺失' : row.share_pct.toFixed(2)+'%']);
    block.evidenceId=selected.id;
    block.note=m.note;
  }
  return blocks;
};

/* Local reads never trigger inference. Model operations have explicit buttons. */
window.initAIChecklist = function(state) {
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, action) => {
    const node = el('button', text, 'ai-button'); node.type = 'button';
    node.addEventListener('click', () => Promise.resolve().then(action).catch(showError)); return node;
  };
  const date = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) : '—';
  let overview = {}, status = {}, currentReport = null, timer = null, loginTimer = null, epoch = 0;
  let updating = false, openedBy = null, historyCursor = null, pollingDelay = 800, view = 'report';
  const selectedHistory = new Set();
  let historyDeleteButton = null, historyCount = null, deletingHistory = false;
  const actions = el('span', undefined, 'ai-context-actions');
  const entry = button('Checklist', openReport);
  const researchEntry = button('AI 研报', openResearch);
  const settingsEntry = button('AI 设置', openSettings);
  actions.append(entry, researchEntry, settingsEntry); document.querySelector('.context').append(actions);
  window.initBusinessJudgment?.(state, actions, openSettings);
  const dialog = el('dialog', undefined, 'ai-dialog'); dialog.setAttribute('aria-label','投资 checklist');
  const settings = el('dialog', undefined, 'ai-dialog'); settings.setAttribute('aria-label','AI 设置');
  const research = el('dialog', undefined, 'ai-dialog'); research.setAttribute('aria-label','AI 研报');
  document.body.append(dialog, settings, research);
  for (const node of [dialog, settings, research]) {
    node.addEventListener('click', event => { if (event.target === node && event.clientX < node.getBoundingClientRect().left) node.close(); });
    node.addEventListener('close', () => { if (node === settings) clearTimeout(loginTimer); openedBy?.focus(); });
  }
  function showError(error) {
    const message = error?.message || '操作未完成，请重试';
    const target = settings.open ? settings : research.open ? research : dialog.open ? dialog : document.querySelector('.context');
    target.querySelector('.ai-operation-error')?.remove();
    const node = el('p', message, 'ai-message ai-operation-error'); node.setAttribute('role','alert'); target.append(node);
  }
  async function api(path, command) {
    if (command !== undefined && !status.session_token) status = await api('/api/ai/status');
    const response = await fetch(path, command === undefined ? {cache:'no-store'} : {
      method:'POST', headers:{'Content-Type':'application/json', 'X-Local-Session':status.session_token}, body:JSON.stringify(command)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求未完成');
    return data;
  }
  function badge(verdict) { const node = el('span', verdict, 'ai-badge'); node.dataset.verdict = verdict; return node; }
  function dataDates(report) {
    const dates=report.quality?.dates || {};
    const chips=[dates.shareholders,dates.financing].filter(Boolean).sort().at(-1);
    return '财报截至 ' + (dates.financial || '缺失') + ' / 估值截至 ' + (dates.valuation || '缺失') + ' / 筹码截至 ' + (chips || '缺失');
  }
  function renderSummary() {
    entry.textContent = overview.active ? '分析中…' : 'Checklist';
  }
  async function openResearch() {
    open(research); research.replaceChildren();
    const code = state.code;
    header(research, 'AI 研报 · '+(state.name || '')+' '+code);
    const loading = el('p', '正在读取历史研报…', 'ai-meta'); research.append(loading);
    const data = await api('/api/research-reports?code='+encodeURIComponent(code));
    if (!research.open || code !== state.code || !loading.isConnected) return;
    loading.remove();
    if (!data.reports.length) {
      research.append(el('p', '当前股票暂无 AI 研报。', 'ai-message'));
      return;
    }
    research.append(el('p', '共 '+data.reports.length+' 份研报，点击在新窗口打开 HTML 版本。', 'ai-meta'));
    for (const report of data.reports) {
      const row = el('section', undefined, 'ai-research-item');
      const link = el('a', report.title, 'ai-button ai-history-item');
      link.href = report.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      row.append(link, el('p', report.date+' · '+report.version, 'ai-meta'));
      research.append(row);
    }
  }
  async function refresh() {
    const sequence = ++epoch, code = state.code;
    const data = await api('/api/analysis?code=' + encodeURIComponent(code));
    if (sequence !== epoch || code !== state.code) return;
    overview = data; renderSummary();
    if (dialog.open && view === 'report') {
      if (!currentReport || currentReport.id === overview.report?.id || currentReport.status !== 'succeeded') currentReport = overview.report;
      renderReport();
    }
    if (overview.active) poll(overview.active.id, code); else clearTimeout(timer);
  }
  function header(node, title) {
    const head = el('header'); head.append(el('h2', title), button('×', () => node.close()));
    head.lastChild.setAttribute('aria-label','关闭'); node.append(head);
  }
  function open(node, trigger) {
    openedBy = trigger || document.activeElement;
    if (!node.open) node.showModal();
  }
  async function openReport() {
    view='report'; open(dialog); currentReport = overview.report; renderReport();
    await refresh();

  }
  function generationTime(run) {
    if (run.status!=='succeeded' || !run.started_at || !run.completed_at) return '';
    const seconds=(Date.parse(run.completed_at)-Date.parse(run.started_at))/1000;
    if (!Number.isFinite(seconds) || seconds<0) return '';
    const rounded=Math.round(seconds*10)/10;
    const text=rounded<60 ? rounded.toFixed(1)+' 秒' : Math.floor(rounded/60)+' 分 '+(rounded%60).toFixed(1)+' 秒';
    return ' · 生成耗时 '+text;
  }
  function taskText(run) {
    const labels = {queued:'正在整理数据 / 等待分析', running:'正在分析', validating:'正在核对结论与数据依据',
      succeeded:'分析完成', failed:'分析失败', cancelled:'分析已取消', interrupted:'分析已中断'};
    const elapsed = run.started_at && ['running','validating'].includes(run.status) ? ' · 已用 ' + Math.max(0,Math.floor((Date.now()-Date.parse(run.started_at))/1000)) + ' 秒' : '';
    return (labels[run.status] || run.status) + elapsed + generationTime(run);
  }
  function dataRange(node) {
    const details = el('details'); details.append(el('summary','本次分析的数据范围'));
    details.append(el('p','将当前股票的经营、估值、筹码摘要发送给 OpenAI，不包含分组、最近查看、个人笔记或整个数据库。'));
    if (overview.data_range) {
      details.append(el('p', '证据 ' + overview.data_range.evidence_count + ' 条 · 18 项定性检查'));
      details.append(el('pre', JSON.stringify(overview.data_range.input, null, 2)));
    }
    node.append(details);
  }
  function evidenceLinks(node, ids, report) {
    for (const id of new Set(ids || [])) {
      const evidence = report.input.evidence.find(item => item.id === id);
      if (!evidence) continue;
      const details = el('details', undefined, 'ai-evidence');
      details.dataset.evidenceId = id;
      const labels={holders_price:'股东人数与股价',financing_price:'融资余额与股价',financial_period:'财务指标',revenue:'营业收入',profit:'归母净利润',revenue_growth:'营收同比',profit_growth:'利润同比',gross_margin:'毛利率',net_margin:'净利率',roe:'ROE',roic:'ROIC',operating_cash_flow:'经营现金流',capex:'资本开支',free_cash_flow:'简化自由现金流',cash_dividend:'已实施分红',dividend_payout_ratio:'已实施分红率',monetary_funds:'货币资金',interest_bearing_debt:'简化有息负债',net_cash:'净现金',pe:'PE',pb:'PB',ps:'PS',dividend_yield:'股息率',pe_raw:'原始PE',holders:'股东人数',close:'收盘价',report_excerpt:'财报披露',company:'公司资料',data_quality:'资料完整性',market_cap:'市值',yoy:'同比变化',financing:'融资摘要',price_trend:'前复权价格趋势'};
      const period={quarter:'单季',ttm:'TTM',year:'年度'};
      details.append(el('summary',(evidence.label || labels[evidence.metric] || '财务指标') + (evidence.period_type ? ' · ' + (period[evidence.period_type] || evidence.period_type) : '') + (evidence.years ? ' · '+evidence.years+'年历史' : '') + (evidence.comparison ? ' · '+({level:'时点值',qoq:'环比',yoy:'同比',coverage:'覆盖倍数'}[evidence.comparison] || evidence.comparison) : '') + (evidence.window_label ? ' · '+evidence.window_label : '') + (evidence.observed_on ? ' · '+evidence.observed_on : '')));
      details.append(el('p','实际观察日期：'+(evidence.observed_on || '缺失')+' · 来源：'+(evidence.source || '未知'),'ai-meta'));
      const number=value => value===null ? '资料缺失' : new Intl.NumberFormat('zh-CN',{maximumFractionDigits:4}).format(value);
      if (evidence.metric==='report_excerpt') {
        for (const excerpt of evidence.value) details.append(el('p','PDF 第 '+excerpt.pdf_page+' 页：'+excerpt.text));
        const link=el('a','查看来源公告');link.href=evidence.source;link.target='_blank';link.rel='noopener noreferrer';if (/^https:\/\//.test(evidence.source)) details.append(link);
      } else if (evidence.metric==='calculated_fact') {
        details.append(el('p','程序计算：'+number(evidence.value)+' '+evidence.unit+' · '+({positive:'正值',negative:'负值',zero:'零',up:'上升',down:'下降',flat:'不变'}[evidence.direction] || evidence.direction)));
        if (evidence.baseline_on) details.append(el('p','比较基期：'+evidence.baseline_on+' · 基期值：'+number(evidence.baseline_value)+' · 当期值：'+number(evidence.current_value),'ai-meta'));
      } else if (['holders_price','financing_price'].includes(evidence.metric)) {
        const facts=evidence.value;
        if (!facts.usable) details.append(el('p','无法检验：'+facts.unavailable_reason,'ai-message'));
        details.append(el('p','实际区间：'+(facts.start_on || '缺失')+' 至 '+(facts.end_on || '缺失'),'ai-meta'));
        details.append(el('p','起点 '+number(facts.start_value)+' → 终点 '+number(facts.end_value)+' '+evidence.unit));
        details.append(el('p','区间变化：'+number(facts.change_pct)+(facts.change_pct===null ? '' : '%')));
        details.append(el('p','同期前复权股价变化：'+number(facts.price_change_pct)+(facts.price_change_pct===null ? '' : '%')));
        details.append(el('p','股价日期：'+(facts.price_start_on || '缺失')+' 至 '+(facts.price_end_on || '缺失'),'ai-meta'));
        if (facts.scope) details.append(el('p','股东口径：'+({total:'总股东人数',a_share:'A股股东人数',unknown:'未知'}[facts.scope] || facts.scope)+' · 最新公告日期：'+(facts.end_announced_on || '缺失'),'ai-meta'));
        details.append(el('p','个人分析框架：主力出货与踩踏属于待验证假设，以上为区间事实。','ai-meta'));
      } else if (evidence.metric==='financial_period') {
        const table=el('table',undefined,'ai-fact-table');
        for (const [key,value] of Object.entries(evidence.value)) {
          const row=el('tr'); const ratio=['debt_ratio','revenue_growth','profit_growth','gross_margin','net_margin','roe','roic','dividend_payout_ratio'].includes(key);
          const text=key==='dividend_payout_ratio' && evidence.period_type!=='year' ? '仅年度提供' : number(value)+(value===null ? '' : ratio ? '%' : ' 元');
          row.append(el('th',labels[key] || ({profit_cut:'扣非归母净利润',cash_flow:'经营现金流',cash:'货币资金',debt_ratio:'资产负债率'}[key] || key)),el('td',text)); table.append(row);
        }
        details.append(table);
      } else if (typeof evidence.value==='number' || evidence.value===null) {
        details.append(el('p','当时数值：'+number(evidence.value)+(evidence.value===null ? '' : ' '+evidence.unit)));
        if (evidence.percentile!==undefined) details.append(el('p','历史分位：'+number(evidence.percentile)+'% · 样本数：'+evidence.sample_count+' · 取样频率：'+({trading_day:'交易日',week:'周',month:'月'}[evidence.sample_frequency] || evidence.sample_frequency)));
        if (evidence.sparse_hint) details.append(el('p',evidence.sparse_hint,'ai-meta'));
      }
      if (evidence.methodology) details.append(el('p',evidence.methodology,'ai-meta'));
      const raw=el('details'); raw.append(el('summary','查看完整证据记录'),el('pre',JSON.stringify(evidence,null,2))); details.append(raw);
      node.append(details);
    }
  }
  let preparing = false;
  async function prepareReports(force) {
    if (preparing) return;
    const code=state.code; preparing=true; renderReport();
    try {
      await api('/api/analysis/prepare',{code,refresh:force});
      if (state.code===code) await refresh();
    } finally {preparing=false; if (state.code===code && view==='report') renderReport();}
  }
  function renderReport() {
    dialog.replaceChildren(); header(dialog,'投资 checklist · '+(state.name || '')+' '+state.code);
    const toolbar=el('div',undefined,'ai-toolbar');
    toolbar.append(button('历史记录',openHistory));
    const prepare=button(preparing ? '财报准备中…' : '准备财报资料',()=>prepareReports(false));
    const update=button('刷新财报版本',()=>prepareReports(true));
    prepare.disabled=update.disabled=preparing || Boolean(overview.active);
    const generateButton=button(overview.report ? '重新生成' : '生成 checklist',()=>generate(Boolean(overview.report)));
    generateButton.disabled=preparing || Boolean(overview.active || updating || overview.quality?.updating || overview.data_error);
    toolbar.append(prepare,update,generateButton,el('span','生成时使用 ChatGPT 额度','ai-meta'));dialog.append(toolbar);
    const cached=el('div',undefined,'checklist-report-cache');
    cached.append(el('strong','本地财报资料'));
    for (const doc of overview.company_reports || []) {
      const type=doc.report_type==='annual' ? '年报' : '中报';
      const states={ready:'已解析，可复用',unparsed:'已下载，待解析',missing_file:'原件缺失',missing:'尚未下载'};
      const row=el('p',type+'：'+(doc.report_period || '尚未下载')+(doc.version ? ' · '+doc.version : '')+' · '+states[doc.state],'ai-meta');
      if (doc.outdated) row.append(el('span','；早于本地财务期 '+doc.expected_period+'，请准备或刷新财报','ai-message'));
      if (doc.report_period && /^https:\/\//.test(doc.source_url || '')) {const link=el('a',' 来源公告');link.href=doc.source_url;link.target='_blank';link.rel='noopener noreferrer';row.append(link);}
      cached.append(row);
    }
    dialog.append(cached);
    if (overview.active) dialog.append(el('p',taskText(overview.active),'ai-meta'),button('取消生成',async()=>{await api('/api/analysis/runs/'+overview.active.id+'/cancel',{});await refresh();}));
    if (overview.data_error) dialog.append(el('p',overview.data_error,'ai-message'));
    if (overview.latest_attempt?.error) dialog.append(el('p',overview.latest_attempt.error,'ai-message'));
    const report=currentReport;
    if (!report?.result?.items) {const ready=overview.company_reports?.every(doc=>doc.state==='ready' && !doc.outdated);dialog.append(el('p',ready ? '财报资料已准备好，可点击生成 checklist。' : '先准备年报和中报资料，再点击生成。已有解析结果会直接复用。'));dataRange(dialog);return;}
    if (report.snapshot_hash!==overview.current_snapshot_hash) dialog.append(el('p','资料已变化，本版本保留生成时的依据，可重新生成。','ai-message'));
    dialog.append(el('p','生成于 '+date(report.completed_at)+generationTime(report)+' · '+dataDates(report),'ai-meta'));
    const table=el('table',undefined,'checklist-table');
    const titles=Object.fromEntries(report.input.checklist_items.map(i=>[i.id,i.title]));
    const labels={ready:'资料完整',limited:'资料有限',missing:'待补资料',not_applicable:'不适用'};
    for (const item of report.result.items) {
      const row=el('tr'),title=el('th',titles[item.id]),cell=el('td');
      const meta=el('div',undefined,'checklist-item-meta');
      const evidence=el('div',undefined,'checklist-item-evidence');
      evidence.id='checklist-evidence-'+report.id+'-'+item.id; evidence.hidden=true;
      const toggle=button('展开核实资料',()=>{
        evidence.hidden=!evidence.hidden;
        toggle.textContent=evidence.hidden ? '展开核实资料' : '收起核实资料';
        toggle.setAttribute('aria-expanded',String(!evidence.hidden));
      });
      toggle.classList.add('checklist-evidence-toggle');
      toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls',evidence.id);
      meta.append(el('span',labels[item.status],'ai-meta'),toggle);
      const conclusion=item.id==='cycle' ? item.conclusion.replace(/判断\s*[\/／]\s*推断/g,'判断') : item.conclusion;
      const blocks=item.id==='products' ? window.checklistProductBlocks(conclusion,report.input.evidence,report.product_display_corrections) : [{type:'text',text:conclusion}];
      for (const block of blocks) {
        if (block.type==='text') {cell.append(el('p',block.text));continue;}
        const wrap=el('div',undefined,'checklist-product-wrap');
        const productTable=el('table',undefined,'checklist-product-table');
        productTable.append(el('caption',block.caption));
        const head=el('thead'),headRow=el('tr');
        for(const label of block.headers){const th=el('th',label);th.scope='col';headRow.append(th);}
        head.append(headRow);productTable.append(head);
        const body=el('tbody');
        for(const values of block.rows){const tr=el('tr');values.forEach((value,index)=>{const entry=el(index===0 ? 'th' : 'td',value);if(index===0)entry.scope='row';tr.append(entry);});body.append(tr);}
        productTable.append(body);wrap.append(productTable);cell.append(wrap);
        if(block.note)cell.append(el('p',block.note,'ai-meta'));
      }
      cell.append(meta,evidence);
      if(item.id==='products' && report.product_display_corrections?.length) {
        const original=el('details');
        original.append(el('summary','查看原生成描述（含错误数值）'),el('p',conclusion));
        evidence.append(original);
      }
      evidenceLinks(evidence,[...new Set([...item.evidence_ids,...blocks.map(b=>b.evidenceId).filter(Boolean)])],report);row.append(title,cell);table.append(row);
    }
    dialog.append(table);
    const facts=el('details');facts.append(el('summary','生成时的数据与来源'),el('pre',JSON.stringify(report.input,null,2)));dialog.append(facts);
    dialog.append(el('p','模型 '+(report.resolved_model || report.model)+' · '+report.prompt_version,'ai-meta'));
  }
  async function generate(force) {
    if (overview.active) {poll(overview.active.id,state.code); return;}
    if (updating || overview.quality?.updating) throw new Error('数据更新中，请稍候');
    if (overview.data_error) throw new Error(overview.data_error);
    status = await api('/api/ai/status');
    if (!status.connected || !status.plan_authorized || !status.model) { await openSettings(); return; }
    const code = state.code;
    const run = await api('/api/analysis',{code,model:status.model,request_key:crypto.randomUUID(),force});
    if (state.code !== code) return;
    if (run.status === 'succeeded') currentReport = run;
    await refresh();
  }
  function poll(id, code) {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const run = await api('/api/analysis/runs/' + id);
        if (code !== state.code || run.code !== code || overview.active?.id !== id) return;
        if (['queued','running','validating'].includes(run.status)) {
          overview.active = run; renderSummary();
          if (dialog.open && view === 'report') {const scroll = dialog.scrollTop; renderReport(); dialog.scrollTop = scroll;}
          pollingDelay = Math.min(3000,pollingDelay + 400); poll(id,code);
        } else { pollingDelay=800; currentReport=run.status==='succeeded' ? run : overview.report; await refresh(); }
      } catch (error) {showError(error);}
    }, pollingDelay);
  }
  async function openHistory() {
    view='history'; open(dialog); historyCursor=null; selectedHistory.clear(); dialog.replaceChildren(); header(dialog,'checklist历史 · ' + state.code);
    const toolbar=el('div',undefined,'ai-toolbar');
    historyCount=el('span','已选 0 条','ai-meta');
    historyDeleteButton=button('删除所选',deleteHistory);
    toolbar.append(button('本次报告',() => {view='report'; currentReport=overview.report; renderReport();}),
      button('全选已加载',() => {
        for (const checkbox of dialog.querySelectorAll('.ai-history-select:not(:disabled)')) {
          checkbox.checked=true; selectedHistory.add(checkbox.value);
        }
        updateHistorySelection();
      }),button('清空选择',() => {
        selectedHistory.clear();
        for (const checkbox of dialog.querySelectorAll('.ai-history-select')) checkbox.checked=false;
        updateHistorySelection();
      }),historyDeleteButton,historyCount);
    dialog.append(toolbar,el('p','删除会移除本地数据库中的报告、任务记录及不再被其他报告引用的数据快照。正在分析的任务须先取消。','ai-meta'));
    updateHistorySelection();
    await moreHistory();
  }
  function updateHistorySelection() {
    historyCount.textContent='已选 '+selectedHistory.size+' 条';
    historyDeleteButton.disabled=deletingHistory || !selectedHistory.size;
  }
  async function deleteHistory() {
    if (deletingHistory || !selectedHistory.size) return;
    const code=state.code, ids=[...selectedHistory];
    if (!window.confirm('删除 '+code+' 的 '+ids.length+' 条checklist记录？\n将从本地数据库中删除，无法在页面撤销。')) return;
    deletingHistory=true; updateHistorySelection();
    try {
      await api('/api/analysis/history/delete',{code,run_ids:ids});
      if (state.code!==code) return;
      await refresh(); currentReport=overview.report;
      if (dialog.open && view==='history') await openHistory();
      else if (dialog.open && view==='report') renderReport();
    } finally { deletingHistory=false; if (view==='history') updateHistorySelection(); }
  }
  async function moreHistory() {
    const code = state.code;
    const data = await api('/api/analysis/history?code=' + encodeURIComponent(code) + (historyCursor ? '&cursor=' + historyCursor : ''));
    if (code !== state.code || !dialog.open || view!=='history') return;
    dialog.querySelector('[data-more-history]')?.remove();
    if (!data.items.length && !historyCursor) dialog.append(el('p','暂无checklist记录'));
    for (const item of data.items) {
      const row=el('div',undefined,'ai-history-row');
      const checkbox=el('input',undefined,'ai-history-select'); checkbox.type='checkbox'; checkbox.value=item.id;
      checkbox.setAttribute('aria-label','选择记录 '+date(item.created_at)+' '+item.id);
      checkbox.disabled=['queued','running','validating'].includes(item.status);
      checkbox.addEventListener('change',() => {
        if (checkbox.checked) selectedHistory.add(item.id); else selectedHistory.delete(item.id);
        updateHistorySelection();
      });
      const node = button(date(item.created_at) + ' · ' + taskText(item) + ' · ' + item.model + '\n' + (item.summary || ''), async () => {
        const report = await api('/api/analysis/runs/' + item.id);
        if (report.code !== state.code) return;
        view='report'; currentReport=report; renderReport();
        if (report.error) dialog.append(el('p',report.error,'ai-message'));
      }); node.classList.add('ai-history-item'); row.append(checkbox,node); dialog.append(row);
    }
    historyCursor=data.next_cursor;
    if (historyCursor) {const more=button('更多历史',moreHistory); more.dataset.moreHistory='true'; dialog.append(more);}
  }
  async function openSettings() {
    open(settings); settings.replaceChildren(); header(settings,'AI 设置');
    status = await api('/api/ai/status');
    settings.append(el('p',status.connected ? '账号：' + status.account : '尚未连接 ChatGPT'));
    settings.append(el('p',status.plan_authorized ? '已授权使用 ChatGPT 额度' : '尚未授权使用 ChatGPT 额度','ai-meta'));
    if (status.error) settings.append(el('p',status.error,'ai-message'));
    settings.append(el('p','连接会请求使用你的 ChatGPT 计划额度。分析时仅发送当前股票摘要。登录和选择模型不会自动开始分析。'));
    const connect = button('Continue with ChatGPT',async () => {
      const popup=window.open('about:blank','stock-chatgpt-auth');
      try {
        const result=await api('/api/ai/connect',{});
        if (popup) popup.location.href=result.authorization_url;
        else window.location.href=result.authorization_url;
        waitLogin();
      } catch(error) {popup?.close(); throw error;}
    }); settings.append(connect);
    if (status.connected) {
      settings.append(button('断开连接',async () => {await api('/api/ai/disconnect',{}); await openSettings(); await refresh();}));
      if (status.plan_authorized) {
        settings.append(el('h3','默认模型'));
        const models=await api('/api/ai/models'); const select=el('select'); select.setAttribute('aria-label','选择默认模型');
        select.append(el('option','请选择模型')); select.firstChild.value='';
        for (const model of models.models) {const option=el('option',model.display_name); option.value=model.slug; select.append(option);}
        select.value=status.model || ''; settings.append(select,button('保存模型',async () => {
          await api('/api/ai/preferences',{model:select.value}); status=await api('/api/ai/status'); settings.append(el('p','模型已保存；点击生成或重新分析才使用额度。','ai-meta'));
        }));
      }
    }
    const link=el('a','ChatGPT 额度与授权管理'); link.href='https://chatgpt.com/#settings/Usage'; link.target='_blank'; link.rel='noopener noreferrer';
    settings.append(el('p','Checklist 与经营判断共用当前账号、额度授权及模型。经营判断生成需要联网搜索权限。'),link); dataRange(settings);
  }
  function waitLogin() {
    clearTimeout(loginTimer); const deadline=Date.now()+600000;
    async function check() {
      if (!settings.open || Date.now()>deadline) return;
      try {const next=await api('/api/ai/status'); if (!next.connecting) {await openSettings(); return;}}
      catch(error) {showError(error); return;}
      loginTimer=setTimeout(check,2000);
    }
    loginTimer=setTimeout(check,2000);
  }
  window.addEventListener('dashboard-data-state',event => {
    if (event.detail.code !== state.code) return;
    updating=event.detail.updating; renderSummary();
    if (dialog.open && view === 'report') renderReport();
    if (!updating) refresh().catch(showError);
  });
  window.addEventListener('pagehide',() => {clearTimeout(timer); clearTimeout(loginTimer); ++epoch;});
  renderSummary(); refresh().catch(showError);
  return {refresh};
};
