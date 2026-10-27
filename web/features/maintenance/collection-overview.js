/* Schedule view over the existing forms, commands and shared progress renderer. */
(()=>{'use strict';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusNames={running:'运行中',complete:'执行完成',failed:'失败',cancelled:'已停止',pending:'待恢复'};
window.initCollectionOverview=({root,post,reload,makeEditor})=>{
  let tasks=[],filter='all',selected=null;
  const expanded=new Set();
  const date=value=>window.DashboardTime.format(value,'—');
  root.innerHTML=`<div class="schedule-toolbar"><span data-schedule-totals></span><div class="schedule-header-actions"><button type="button" id="collection-overview-refresh">刷新</button><div class="schedule-filters" aria-label="筛选计划">${[['all','全部'],['enabled','已激活'],['paused','已暂停']].map(([key,label])=>`<button type="button" data-schedule-filter="${key}" aria-pressed="${key==='all'}">${label}</button>`).join('')}</div></div></div><section class="schedule-editor" data-schedule-editor hidden></section><div class="schedule-columns schedule-grid" aria-hidden="true"><span>计划 / 内容</span><span>频率 · 北京时间</span><span>下次运行</span><span>状态</span><span>操作</span></div><div data-schedule-rows></div><p class="schedule-footnote" data-schedule-message role="status" aria-live="polite">仅计划触发 · 每条最近 5 次 · 含运行中</p>`;
  const el=selector=>root.querySelector(selector);
  function content(task,rule){
    const params=rule.params;
    if(task.task_key==='stocks')return `${rule.group_name} · ${(params.datasets||[]).map(key=>task.datasets[key]||key).join('、')}`;
    if(params.target)return params.target+' · '+task.name;
    if(params.providers)return params.providers.map(key=>({csindex:'中证',sws:'申万',legu:'乐咕'})[key]||key).join('、');
    return ({shipping_weekly:'CCFI 全部航线、SCFI 综合',shipping_monthly:'月度指数与吞吐量',market_congestion:'大盘拥挤度',sovereign_yields:'中美国债收益率'})[task.task_key]||task.name;
  }
  function frequency(rule){
    const r=rule.rule;
    if(r.frequency==='financial_season')return `财报季<span class="schedule-sub">前 ${r.days_before} / 后 ${r.days_after} 天</span>`;
    if(r.frequency==='weekly')return '每周'+['一','二','三','四','五','六','日'][r.weekday];
    if(r.frequency==='monthly')return r.window?`每月 ${r.window[0]}—${r.window[1]} 日`:`每月 ${r.day} 日`;
    return '每天';
  }
  function entries(){return tasks.flatMap(task=>(task.rules||[]).map(rule=>({task,rule})));}
  function find(identity){return entries().find(({rule})=>rule.schedule_id===identity);}
  function render(){
    const all=entries(),enabled=all.filter(({rule})=>rule.enabled).length;
    const running=all.filter(({rule})=>rule.history?.some(run=>run.status==='running')).length;
    el('[data-schedule-totals]').textContent=`${all.length} 个计划 · 已激活 ${enabled} · 运行中 ${running}`;
    el('[data-schedule-rows]').innerHTML=all.filter(({rule})=>filter==='all'||Boolean(rule.enabled)===(filter==='enabled')).map(({task,rule})=>{
      const history=rule.history||[],latest=history[0],live=history.some(run=>run.status==='running');
      return `<article class="schedule-row" data-schedule-id="${esc(rule.schedule_id)}"><div class="schedule-grid schedule-row-head"><div><strong>${esc(task.name)}</strong><span class="schedule-sub">${esc(content(task,rule))}</span></div><div>${frequency(rule)}<span class="schedule-sub">${esc(rule.local_time)}</span></div><div class="schedule-time">${esc(date(rule.next_check))}${live?'<span class="schedule-live schedule-sub">当前运行中</span>':''}</div><span class="schedule-state ${rule.enabled?'enabled':''}">${rule.enabled?'● 已激活':'○ 已暂停'}</span><div class="schedule-actions"><button type="button" data-schedule-edit>编辑</button><button type="button" data-schedule-toggle>${rule.enabled?'暂停':'激活'}</button></div></div><details data-schedule-history ${expanded.has(rule.schedule_id)?'open':''}><summary>最近 ${history.length} 次 <span class="schedule-${esc(latest?.status||'empty')}">${esc(statusNames[latest?.status]||'尚无运行')}</span></summary><div class="schedule-runs">${history.map((run,index)=>`<button type="button" class="schedule-run" data-schedule-run="${index}" aria-expanded="false"><span class="schedule-time">${esc(date(run.started_at))}</span><span class="schedule-${esc(run.status)}">${esc(statusNames[run.status]||run.status)}</span><span>${esc(run.error||run.slot_error||(run.slot_status==='pending'?'待恢复':run.result?.message||run.slot_key))}</span><span class="schedule-open">查看进度 ›</span></button>${selected?.identity===rule.schedule_id&&selected.ref===run.operation_ref?'<div class="index-progress-details" data-schedule-progress role="status" aria-live="polite"><p data-progress-summary></p><ul data-progress-sources></ul></div>':''}`).join('')||'<p class="schedule-empty">尚无计划运行</p>'}</div></details></article>`;
    }).join('')||'<p class="schedule-empty">暂无符合条件的计划</p>';
    if(selected){
      const entry=find(selected.identity),run=entry?.rule.history?.find(run=>run.operation_ref===selected.ref);
      const box=root.querySelector('[data-schedule-progress]');
      if(run&&box){
        box.previousElementSibling.setAttribute('aria-expanded','true');
        window.CollectionProgress.render({summary:box.querySelector('[data-progress-summary]'),list:box.querySelector('[data-progress-sources]')},run,run.result);
      }
    }
  }
  root.addEventListener('toggle',event=>{
    if(!event.target.matches('[data-schedule-history]')||!event.target.isConnected)return;
    const identity=event.target.closest('[data-schedule-id]').dataset.scheduleId;
    if(event.target.open)expanded.add(identity);else expanded.delete(identity);
  },true);
  root.addEventListener('click',async event=>{
    const refresh=event.target.closest('#collection-overview-refresh');
    if(refresh){refresh.disabled=true;try{await reload();}finally{refresh.disabled=false;}return;}
    const filterButton=event.target.closest('[data-schedule-filter]');
    if(filterButton){filter=filterButton.dataset.scheduleFilter;root.querySelectorAll('[data-schedule-filter]').forEach(button=>button.setAttribute('aria-pressed',String(button===filterButton)));render();return;}
    if(event.target.closest('[data-schedule-close-editor]')){el('[data-schedule-editor]').hidden=true;el('[data-schedule-editor]').replaceChildren();return;}
    const row=event.target.closest('[data-schedule-id]');if(!row)return;
    const identity=row.dataset.scheduleId,entry=find(identity);if(!entry)return;
    const {task,rule}=entry;
    if(event.target.closest('[data-schedule-edit]')){
      const editor=el('[data-schedule-editor]');
      editor.innerHTML=`<div class="schedule-editor-head"><strong>编辑计划 · ${esc(task.name)}</strong><button type="button" data-schedule-close-editor>收起</button></div>`;
      editor.append(makeEditor(task,rule));editor.hidden=false;
      editor.querySelector('[name=local_time]').focus();return;
    }
    const toggle=event.target.closest('[data-schedule-toggle]');
    if(toggle){
      toggle.disabled=true;
      try{await post('schedule',{task_key:task.task_key,schedule:{action:'toggle',schedule_key:identity,enabled:!rule.enabled}});await reload();el('[data-schedule-message]').textContent=rule.enabled?'已暂停；当前运行继续。':'已激活；从下一个未来时点运行。';}
      catch(error){el('[data-schedule-message]').textContent=error.message;toggle.disabled=false;}return;
    }
    const button=event.target.closest('[data-schedule-run]');if(!button)return;
    const run=rule.history[Number(button.dataset.scheduleRun)];
    selected=selected?.identity===identity&&selected.ref===run.operation_ref?null:{identity,ref:run.operation_ref};
    expanded.add(identity);render();
  });
  return {update(value){tasks=value;render();}};
};
})();
