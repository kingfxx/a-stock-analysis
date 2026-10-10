/* Explicit generation only. All displayed research is text, never HTML. */
window.initBusinessJudgment = function(state, actions, openSettings) {
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  let token, timer, elapsedTimer, epoch=0, view='report', current=null, busy=false, openedCode=null;
  const selectedHistory=new Set();let historyDeleteButton,historyCount,deletingHistory=false;
  const dialog=el('dialog',undefined,'ai-dialog business-dialog');dialog.setAttribute('aria-label','经营判断');document.body.append(dialog);
  dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left || event.clientX>rect.right || event.clientY<rect.top || event.clientY>rect.bottom)dialog.close();});
  const error=e=>{dialog.querySelector('.business-error')?.remove();dialog.append(el('p',e?.message || '操作失败，请重试','ai-message business-error'));};
  const button=(text,fn)=>{const n=el('button',text,'ai-button');n.type='button';n.addEventListener('click',()=>Promise.resolve().then(fn).catch(error));return n;};
  const entry=button('经营判断',async()=>{openedCode=state.code;dialog.showModal();await refresh();});actions.insertBefore(entry,actions.children[1] || null);
  async function api(path,body) {
    if(body && !token)token=(await api('/api/ai/status')).session_token;
    const response=await fetch(path,body ? {method:'POST',headers:{'Content-Type':'application/json','X-Local-Session':token},body:JSON.stringify(body)} : undefined);
    const data=await response.json();if(!response.ok)throw new Error(data.error || '请求失败');return data;
  }
  const base='/api/business-judgment';
  const date=value=>value ? new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) : '—';
  function header() {
    clearInterval(elapsedTimer);
    dialog.replaceChildren();const h=el('header');h.append(el('h2',`${state.code} 经营判断`),button('关闭',()=>dialog.close()));dialog.append(h);
    const toolbar=el('div',undefined,'ai-toolbar');toolbar.append(button('当前结果',()=>refresh()),button('历史记录',()=>history()),button('AI 设置',()=>{dialog.close();return openSettings();}));dialog.append(toolbar);
  }
  function evidence(parent,ids,run) {
    const detail=el('details');detail.append(el('summary','查看依据 · '+ids.length+' 项资料'));
    for(const id of ids){const source=run.result.sources.find(s=>s.id===id);if(source){const link=el('a',source.title);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';detail.append(link,el('p','发布日期：'+(source.published_on || '未提供'),'ai-meta'),el('p',source.excerpt));if(source.origin==='local_report'){detail.append(el('p','来源：本地已保存财报；本次未联网重新读取该文件。','ai-meta'));for(const localId of source.local_evidence_ids || [])localEvidence(detail,run.input.evidence.find(e=>e.id===localId));}}else{const local=run.input.evidence.find(e=>e.id===id);localEvidence(detail,local);}}
    parent.append(detail);
  }
  function failure(parent,run) {
    if(!run?.error)return;
    parent.append(el('p',run.error,'ai-message'));
    if(!run.source_error)return;
    const detail=el('details');detail.append(el('summary','来源校验详情'),el('p','模型填写的链接未匹配实际检索来源，本次未保存成功报告。'),el('p','模型填写：'+run.source_error.model_url));
    for(const url of run.source_error.retrieved_candidates || [])detail.append(el('p','本次检索中的相近链接（仅供排查，未自动替换）：'+url));
    detail.append(el('p','可重新生成；仅主动生成会使用额度。'));parent.append(detail);
  }
  function industryMarkets(parent,value,run) {
    parent.append(el('h4','附表 · 行业地位（按核心业务市场比较）'));
    const wrap=el('div',undefined,'business-table-wrap'),table=el('table',undefined,'business-table business-position-table');table.dataset.framework='industry_position';
    const head=el('thead'),hr=el('tr');for(const label of ['市场与选择理由','时期、边界与口径','公司地位、同业与集中度','可核查依据与缺口'])hr.append(el('th',label));head.append(hr);table.append(head);
    const rank=v=>v===null || v===undefined ? '排名未取得':'第 '+v+' 名';
    const share=v=>v===null || v===undefined ? '份额未取得':v+'%';
    const body=el('tbody');for(const row of value.markets){
      const tr=el('tr');tr.id='business-industry_position-'+row.market;
      const market=el('td');market.append(el('strong',row.market),el('p',row.selection_reason));if(row.status!=='ready')market.append(el('p',row.status==='missing' ? '待核实':'证据有限','ai-message'));
      const scope=el('td');scope.append(el('p',row.period),el('p',row.scope),el('p',row.measure));
      const position=el('td');position.append(el('strong',row.company_entity+'：'+rank(row.company_rank)+' / '+share(row.company_share_pct)));
      for(const peer of row.competitors)position.append(el('p',peer.name+'：'+rank(peer.rank)+' / '+share(peer.share_pct)));
      if(!row.competitors.length)position.append(el('p','未取得同口径同行份额','ai-meta'));
      for(const [key,label] of [['cr3_pct','CR3'],['cr5_pct','CR5']])position.append(el('p',label+'：'+(row[key]===null || row[key]===undefined ? '不可计算':row[key]+'%')));
      const facts=el('td');facts.append(el('p',row.basis),el('p',row.limitations,'ai-meta'));evidence(facts,row.evidence_ids,run);
      tr.append(market,scope,position,facts);body.append(tr);
    }table.append(body);wrap.append(table);parent.append(wrap);
  }
  function industryPosition(parent,value) {
    parent.append(el('h4','附表 · 行业地位：全球班轮运力排行'));
    parent.append(el('p','查询时间：'+date(value.captured_at)+' · '+value.market,'ai-meta'));
    const table=el('table',undefined,'business-table business-position-table'),head=el('thead'),hr=el('tr');
    for(const label of ['排名','企业 / 集团','运营运力（TEU）','份额'])hr.append(el('th',label));head.append(hr);table.append(head);
    const body=el('tbody');for(const row of value.top){const tr=el('tr');for(const text of [String(row.rank),row.name,new Intl.NumberFormat('zh-CN').format(row.teu),row.share_pct+'%'])tr.append(el('td',text));if(row.name===value.company.name)tr.className='business-position-company';body.append(tr);}table.append(body);
    const wrap=el('div',undefined,'business-table-wrap');wrap.append(table);parent.append(wrap);
    parent.append(el('p',`${value.company.name} 排第 ${value.company.rank}，运力份额 ${value.company.share_pct}%；CR3约 ${value.cr3_pct}%，CR5约 ${value.cr5_pct}%（加总已四舍五入份额）。`));
    parent.append(el('p',value.basis+'。'+value.scope_note,'ai-meta'),el('p',value.date_note,'ai-meta'));
    const link=el('a','Alphaliner TOP 100 · 查看来源');link.href=value.source;link.target='_blank';link.rel='noopener noreferrer';parent.append(link);
  }
  function localEvidence(parent,item) {
    if(item?.metric==='industry_position'){industryPosition(parent,item.value);return;}
    if(!item){parent.append(el('p','该项本地依据未保存。','ai-message'));return;}
    const names={revenue:'营业收入',profit:'归母净利润',profit_cut:'扣非归母净利润',cash_flow:'经营活动现金流量净额',cash:'货币资金',debt_ratio:'资产负债率',roe:'净资产收益率（ROE）',roic:'投入资本回报率（ROIC）',gross_margin:'毛利率',net_margin:'净利率',capex:'资本开支',free_cash_flow:'简化自由现金流',interest_bearing_debt:'简化有息负债',net_cash:'净现金',code:'股票代码',name:'公司名称'};
    const labels={financial_period:'财务指标',yoy:'财务同比变化',company:'公司资料',report_excerpt:'财报原文'};
    const sources={'sina/normalized/manual':'新浪财务 / 本地标准化资料 / 手工覆盖（逐项口径见下）',existing_local_calculation:'本地财务计算',local_calculation:'本地计算',local_instrument:'本地股票资料'};
    parent.append(el('strong',item.label || labels[item.metric] || '本地资料'));
    parent.append(el('p',`报告期：${item.observed_on || '未提供'}${item.period_type==='ttm' ? ' · TTM（最近十二个月）及期末值，详见口径' : item.metric==='financial_period' ? ' · 累计金额 / 期末余额，详见口径':''}`,'ai-meta'));
    if(item.baseline_on)parent.append(el('p','同比基期：'+item.baseline_on,'ai-meta'));
    if(item.metric==='report_excerpt') {
      for(const page of Array.isArray(item.value) ? item.value : [])parent.append(el('p',`PDF 第 ${page.pdf_page ?? '未知'} 页：${page.text || '未保存文本'}`));
    } else {
      const table=el('table',undefined,'ai-fact-table');
      const entries=item.value && typeof item.value==='object' && !Array.isArray(item.value) ? Object.entries(item.value) : [['数值',item.value]];
      for(const [key,value] of entries) {
        const basis=item.basis?.[key] || {},ratio=['roe','roic','debt_ratio','gross_margin','net_margin'].includes(key);
        const unit=item.metric==='yoy' || ratio ? '%' : basis.unit || (['revenue','profit','profit_cut','cash_flow','cash','capex','free_cash_flow','interest_bearing_debt','net_cash'].includes(key) ? '元':item.unit || '');
        let text=value===null || value===undefined ? '资料缺失' : String(value);
        if(typeof value==='number' && Number.isFinite(value)) {
          const scale=unit==='元' && Math.abs(value)>=1e8 ? 1e8 : unit==='元' && Math.abs(value)>=1e4 ? 1e4 : 1;
          text=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(value/scale)+(scale===1e8 ? ' 亿元':scale===1e4 ? ' 万元':unit ? ' '+unit:'');
        }
        if(value && typeof value==='object')text='该项包含明细，需核对来源资料';
        const row=el('tr'),cell=el('td',text);row.append(el('th',(names[key] || key)+(item.metric==='yoy' ? '同比':'')),cell);
        const reportTypes={gjzb:'新浪关键指标',lrb:'利润表',fzb:'资产负债表',llb:'现金流量表',normalized_local:'本地标准化财务'};
        if(basis.report_type || basis.source)cell.append(el('p',basis.source==='manual' ? '手工覆盖':reportTypes[basis.report_type] || basis.source || basis.report_type,'ai-meta'));
        table.append(row);
      }
      parent.append(table);
    }
    if(/^https?:\/\//.test(item.source || '')){const link=el('a','查看来源公告');link.href=item.source;link.target='_blank';link.rel='noopener noreferrer';parent.append(link);}else parent.append(el('p','来源：'+(sources[item.source] || item.source || '未记录'),'ai-meta'));
    if(item.published_on)parent.append(el('p','披露日期：'+item.published_on,'ai-meta'));
    if(item.methodology)parent.append(el('p','口径说明：'+item.methodology,'ai-meta'));
  }
  function report(run) {
    for(const conflict of run.result?.retrieval?.market_conflicts || []) {
      dialog.append(el('p','行业地位待核实：'+conflict.market+'。'+conflict.reason,'ai-message'));
      const detail=el('details');detail.append(el('summary','查看原输出排行（未采用）'));
      detail.append(el('p',conflict.period+' · '+conflict.measure));
      for(const item of [{name:conflict.company_entity,rank:conflict.company_rank,share_pct:conflict.company_share_pct},...conflict.competitors])
        detail.append(el('p',item.name+'：原排名 '+(item.rank ?? '未取得')+' / 原份额 '+(item.share_pct===null ? '未取得':item.share_pct+'%')));
      dialog.append(detail);
    }
    const r=run.result;if(!r)return;
    dialog.append(el('p',`资料截止 ${r.as_of} · 生成 ${date(run.completed_at)} · 模型 ${run.resolved_model || run.model} · ${run.prompt_version}`,'ai-meta'));
    const started=r.retrieval?.replay?.started_at || run.started_at,completed=r.retrieval?.replay?.completed_at || run.completed_at;
    const seconds=started && completed ? Math.max(0,Math.round((new Date(completed)-new Date(started))/1000)) : null;
    if(seconds!==null)dialog.append(el('p',`生成耗时 ${seconds} 秒 · ${run.output_schema_version}`,'ai-meta'));
    if(r.retrieval?.replay)dialog.append(el('p',r.retrieval.replay.note || '此结果复用原模型响应重新校验，未再次调用模型。','ai-meta'));
    for(const check of r.retrieval?.concentration_checks || [])dialog.append(el('p',check.market+' · '+check.metric+' 未采用：'+check.reason+'。依赖该数字的文字判断需核实。','ai-message'));
    if(r.frameworks) {
      for(const [key,title] of [['pestel','① 宏观环境：PESTEL'],['five_forces','② 行业结构：波特五力'],['capabilities','③ 内部资源与公司能力']]) {
        dialog.append(el('h3',title));
        const wrap=el('div',undefined,'business-table-wrap'),table=el('table',undefined,'business-table business-framework-table');table.dataset.framework=key;
        const headers=['维度','可核查的依据',key==='capabilities' ? '优势如何转化为经营结果':key==='pestel' ? `对${run.name || run.code}的分析判断`:'分析判断',key==='pestel' ? '风险与后续验证点':key==='five_forces' ? '反证与验证点':'限制与反证'];
        const head=el('thead'),hr=el('tr');for(const label of headers)hr.append(el('th',label));head.append(hr);table.append(head);
        const body=el('tbody');for(const row of r.frameworks[key]){
          const tr=el('tr');tr.id='business-'+key+'-'+row.category;
          const pestelLabels={'政策':'政策 P','经济':'经济 E','社会需求':'社会 S','技术':'技术 T','环境':'环境 E','法律':'法律 L'};
          const dimension=el('td');dimension.append(el('strong',key==='pestel' ? pestelLabels[row.category] || row.category:row.category));if(row.status!=='ready')dimension.append(el('p',row.status==='limited' ? '证据有限':'待核实','ai-message'));
          const facts=el('td');facts.append(el('p',row.analysis));
          const sources=el('p',undefined,'business-inline-sources');for(const id of row.evidence_ids){const source=r.sources.find(s=>s.id===id);if(source){const a=el('a',source.title);a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';sources.append(a,document.createTextNode('　'));}}facts.append(sources);evidence(facts,row.evidence_ids,run);
          const judgment=el('td');judgment.append(el('strong',row.judgment),el('p',row.impact));
          const risks=el('td');risks.append(el('p',row.risk_verification || '旧版未单独保存限制、反证与验证点；重新生成可补齐。',row.risk_verification ? undefined:'ai-meta'));
          tr.append(dimension,facts,judgment,risks);body.append(tr);
        }table.append(body);wrap.append(table);dialog.append(wrap);
        if(key==='five_forces' && r.industry_position){if(r.industry_position.markets)industryMarkets(dialog,r.industry_position,run);else industryPosition(dialog,r.industry_position);}
        if(key==='five_forces' && r.industry_position_direct){const detail=el('details');detail.append(el('summary','直接采集的行业排行与口径'));industryPosition(detail,r.industry_position_direct);dialog.append(detail);}
      }
    } else {
      dialog.append(el('p','此历史版本仅保存了结论表，没有三张前置分析表。点击重新生成可保存完整分析。','ai-message'));
    }
    dialog.append(el('h3','最终结论'),el('p',r.summary,'business-thesis'));
    const wrap=el('div',undefined,'business-table-wrap'),table=el('table',undefined,'business-table business-conclusion-table'),head=el('thead'),hr=el('tr');
    for(const label of ['核心问题','明确判断','权衡理由','对整体判断的影响'])hr.append(el('th',label));head.append(hr);table.append(head);
    const body=el('tbody');for(const row of r.rows){const tr=el('tr');for(const key of ['question','judgment','reasoning','impact']){const td=el('td');td.append(el(key==='judgment' ? 'strong':'p',row[key]));if(key==='question' && row.status!=='ready')td.append(el('p',row.status==='limited' ? '证据有限':'待核实','ai-message'));if(key==='reasoning'){evidence(td,row.evidence_ids,run);for(const ref of row.framework_refs || []){const [group,...parts]=ref.split('.'),category=parts.join('.');const labels={pestel:'PESTEL',five_forces:'五力',capabilities:'公司能力',industry_position:'行业地位'};const link=el('a',labels[group]+' · '+category);link.href='#business-'+group+'-'+category;link.addEventListener('click',event=>{event.preventDefault();document.getElementById('business-'+group+'-'+category)?.scrollIntoView({block:'center',behavior:'smooth'});});const p=el('p');p.append(link);td.append(p);}}tr.append(td);}body.append(tr);}table.append(body);wrap.append(table);dialog.append(wrap);
    dialog.append(el('h3','什么会改变判断'));for(const item of r.change_conditions){const block=el('div');block.append(el('strong',item.condition),el('p',item.impact));evidence(block,item.evidence_ids,run);dialog.append(block);}
    if(r.unknowns.length){dialog.append(el('h3','关键缺口'));const ul=el('ul');for(const item of r.unknowns)ul.append(el('li',item));dialog.append(ul);}
    dialog.append(el('h3','来源与口径'));for(const source of r.sources){const p=el('p'),a=el('a',`${source.id} · ${source.title}`);a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';p.append(a,el('span',' · '+(source.published_on || '日期未提供')));if(source.origin==='local_report')p.append(el('span',' · 本地已保存财报（本次未重新联网读取）'));dialog.append(p);}
    for(const note of run.input.limitations)dialog.append(el('p',note,'ai-meta'));
  }
  async function refresh() {
    clearTimeout(timer);view='report';const seq=++epoch,code=state.code;
    const data=await api(base+'?code='+encodeURIComponent(code));if(seq!==epoch || code!==state.code || !dialog.open)return;
    current=data;header();dialog.append(el('p','依据宏观环境、行业五力和公司能力形成明确经营判断。生成分为联网检索与分析两阶段，共两次模型请求；查看与历史不会调用模型。','ai-meta'));
    const generate=button(data.report ? '重新生成':'生成经营判断',()=>generateReport());generate.classList.add('business-generate');generate.disabled=!!data.active || busy || !!data.quality?.updating;dialog.append(generate);
    if(data.quality?.updating)dialog.append(el('p','本地数据更新中，请稍后生成。','ai-message'));
    if(data.active){
      const active=data.active,clock=el('p',undefined,'ai-message business-progress');clock.setAttribute('aria-live','off');
      const updateClock=()=>{const queued=active.status==='queued',start=Date.parse(queued ? active.created_at : active.started_at || active.created_at),seconds=Number.isFinite(start) ? Math.max(0,Math.floor((Date.now()-start)/1000)):null;clock.textContent=(queued ? '任务等待中':'任务'+(active.status==='validating' ? '校验中':'生成中'))+(seconds===null ? '':queued ? ` · 已等待 ${seconds} 秒`:` · 已用 ${seconds} 秒`)+(queued ? '。':'，正在检索资料并分析。');};
      updateClock();elapsedTimer=setInterval(updateClock,1000);
      dialog.append(clock,button('取消任务',async()=>{await api(base+'/runs/'+active.id+'/cancel',{});await refresh();}));timer=setTimeout(()=>refresh().catch(error),2000);
    }
    const attempt=data.latest_attempt;failure(dialog,attempt);
    if(data.changed && data.report)dialog.append(el('p','资料日期或本地证据已变化；下面保留上次结果，点击重新生成更新。','ai-message'));
    if(data.report)report(data.report);else dialog.append(el('p',data.data_error || '尚无保存的经营判断。生成时将联网检索当前公司资料。'));
  }
  async function generateReport() {
    if(busy)return;busy=true;const code=state.code,clickedAt=Date.now();
    const generate=dialog.querySelector('.business-generate');if(generate)generate.disabled=true;
    const pending=el('p','正在提交 · 已用 0 秒','ai-message business-progress');pending.setAttribute('aria-live','polite');
    if(generate)generate.after(pending);else dialog.append(pending);
    const submissionTimer=setInterval(()=>{if(!pending.isConnected || !dialog.open){clearInterval(submissionTimer);return;}pending.textContent=`正在提交 · 已用 ${Math.floor((Date.now()-clickedAt)/1000)} 秒`;},1000);
    try{const status=await api('/api/ai/status');if(!status.connected || !status.plan_authorized || !status.model){dialog.close();await openSettings();return;}
      if(code!==state.code)return;
      await api(base,{code,model:status.model,request_key:crypto.randomUUID(),force:!!current?.report});
      if(code===state.code && dialog.open)await refresh();
    }finally{clearInterval(submissionTimer);pending.remove();busy=false;if(dialog.open && view==='report')dialog.querySelectorAll('button').forEach(b=>{if(b.textContent==='生成经营判断' || b.textContent==='重新生成')b.disabled=!!current?.active || !!current?.quality?.updating;});}
  }
  function updateHistorySelection() {
    if(!historyCount || !historyDeleteButton)return;
    historyCount.textContent='已选 '+selectedHistory.size+' 条';
    historyDeleteButton.disabled=deletingHistory || !selectedHistory.size;
  }
  async function deleteHistory() {
    if(deletingHistory || !selectedHistory.size)return;
    const code=state.code,ids=[...selectedHistory];
    if(!window.confirm('删除 '+code+' 的 '+ids.length+' 条经营判断记录？\n将从本地数据库中删除，无法在页面撤销。'))return;
    deletingHistory=true;updateHistorySelection();
    try {
      await api(base+'/history/delete',{code,run_ids:ids});
      if(code===state.code && dialog.open && view==='history')await history();
    } finally {deletingHistory=false;if(view==='history')updateHistorySelection();}
  }
  async function history(cursor,append=false) {
    clearTimeout(timer);clearInterval(elapsedTimer);view='history';const seq=++epoch,code=state.code;
    const data=await api(base+'/history?code='+encodeURIComponent(code)+(cursor ? '&cursor='+encodeURIComponent(cursor):''));if(seq!==epoch || code!==state.code || !dialog.open)return;
    if(!append){
      selectedHistory.clear();header();dialog.append(el('h3','已保存的经营判断'));
      const toolbar=el('div',undefined,'ai-toolbar');
      historyCount=el('span','已选 0 条','ai-meta');historyDeleteButton=button('删除所选',deleteHistory);
      toolbar.append(button('全选已加载',()=>{
        for(const checkbox of dialog.querySelectorAll('.ai-history-select:not(:disabled)')){checkbox.checked=true;selectedHistory.add(checkbox.value);}updateHistorySelection();
      }),button('清空选择',()=>{
        selectedHistory.clear();for(const checkbox of dialog.querySelectorAll('.ai-history-select'))checkbox.checked=false;updateHistorySelection();
      }),historyDeleteButton,historyCount);
      dialog.append(toolbar,el('p','删除所选经营判断、任务及无其他记录引用的快照；不删除 Checklist 或正式财报。正在分析的任务须先取消。','ai-meta'));updateHistorySelection();
    }
    dialog.querySelector('.business-more')?.remove();
    if(!data.items.length && !append)dialog.append(el('p','暂无历史记录。'));
    for(const item of data.items){
      const row=el('div',undefined,'ai-history-row'),checkbox=el('input',undefined,'ai-history-select');checkbox.type='checkbox';checkbox.value=item.id;
      checkbox.setAttribute('aria-label','选择记录 '+date(item.created_at)+' '+item.id);checkbox.disabled=['queued','running','validating'].includes(item.status);
      checkbox.addEventListener('change',()=>{if(checkbox.checked)selectedHistory.add(item.id);else selectedHistory.delete(item.id);updateHistorySelection();});
      const node=button(`${date(item.created_at)} · ${item.status} · ${item.summary || '无结果'}`,async()=>{const seq=++epoch,code=state.code;const run=await api(base+'/runs/'+item.id);if(seq!==epoch || code!==state.code || !dialog.open)return;view='saved';header();if(run.result)report(run);else if(run.error)failure(dialog,run);else dialog.append(el('p','该任务尚无成功结果。'));});
      node.classList.add('ai-history-item');row.append(checkbox,node);dialog.append(row);
    }
    if(data.next_cursor){const more=button('更多记录',()=>history(data.next_cursor,true));more.classList.add('business-more');dialog.append(more);}
  }
  dialog.addEventListener('close',()=>{clearTimeout(timer);clearInterval(elapsedTimer);epoch++;entry.focus();});
  window.addEventListener('dashboard-data-state',()=>{if(dialog.open && openedCode!==state.code){epoch++;dialog.close();}});
  window.addEventListener('pagehide',()=>{clearTimeout(timer);clearInterval(elapsedTimer);epoch++;});
};
