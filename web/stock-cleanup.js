/* Cleanup is explicit: choose scope, inspect a server preview, then confirm. */
window.initStockCleanup = function (panel) {
  const host = document.getElementById('maintenance-stock-cleanup');
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const size = value => value < 1024 ? `${value} B` : value < 1048576 ? `${(value/1024).toFixed(1)} KiB` : `${(value/1048576).toFixed(2)} MiB`;
  const labels = {cache:'可重建缓存',financial:'个股财务与行情数据',research:'研究历史与导出研报',reports:'正式财报原件'};
  host.innerHTML = `<section class="panel maintenance-cleanup"><div class="maintenance-controls"><h3>未关注股票数据清理</h3><button id="stock-cleanup-refresh" type="button">刷新候选</button></div>
    <p>仅处理已取消关注的个股资料。行业表及行业来源文件保留；执行前再次检查引用和运行任务。</p>
    <div class="maintenance-cleanup-filters"><label>搜索<input id="stock-cleanup-search" type="search" placeholder="代码或名称"></label><label>取消关注至少<input id="stock-cleanup-days" type="number" min="0" max="36500" value="0">天</label><button id="stock-cleanup-select" type="button">选择结果</button><span id="stock-cleanup-count">尚未读取</span></div>
    <div id="stock-cleanup-candidates" class="maintenance-cleanup-candidates"></div>
    <fieldset id="stock-cleanup-categories"><legend>清理类别（默认只清理缓存）</legend>${Object.entries(labels).map(([key,label])=>`<label><input type="checkbox" value="${key}" ${key==='cache'?'checked':''}>${label}</label>`).join('')}</fieldset>
    <p class="maintenance-cleanup-note">勾选“个股财务与行情数据”会删除所选股票全部 financial_reports 记录及对应同步标记；行业指标与历史研究快照保留。缓存可重新生成。清理前不创建备份，成功清理后无法通过本次操作恢复。SQLite 删除记录先形成库内可复用空间，本操作不收缩数据库文件。</p>
    <button id="stock-cleanup-preview" type="button" disabled>预览清理范围</button><p id="stock-cleanup-status" role="status" aria-live="polite">切换到后台维护后读取候选；不会自动清理。</p>
    <div id="stock-cleanup-detail"></div><button id="stock-cleanup-open" type="button" hidden>确认清理此范围…</button>
    <dialog id="stock-cleanup-dialog"><form method="dialog"><h3>确认清理未关注股票数据</h3><p id="stock-cleanup-confirm-summary"></p><p>仅执行已预览的类别。financial_reports 原始记录按所选财务范围全部清理；研究快照保留。其他资料仍检查共享引用与页面租约。资料变化时会拒绝执行，要求重新预览。</p><label>输入“清理”以确认<input id="stock-cleanup-word" autocomplete="off"></label><div class="maintenance-dialog-actions"><button value="cancel">取消</button><button id="stock-cleanup-confirm" type="button" disabled>执行清理</button></div></form></dialog>
    </section>`;
  const el = id => document.getElementById(id);
  let stocks = [], selected = new Set(), loaded = false, busy = false, preview = null, session = '';
  function results() {
    const query = el('stock-cleanup-search').value.trim().toLowerCase();
    const days = Math.max(0,Number(el('stock-cleanup-days').value)||0);
    return stocks.filter(stock => stock.unfollowed_days >= days && `${stock.code} ${stock.name||''}`.toLowerCase().includes(query));
  }
  const categories = () => [...el('stock-cleanup-categories').querySelectorAll('input:checked')].map(input=>input.value);
  const status = text => { el('stock-cleanup-status').textContent = text; };
  function invalidate() { preview=null;el('stock-cleanup-detail').replaceChildren();el('stock-cleanup-open').hidden=true; }
  function controls() {
    host.querySelectorAll('input,button').forEach(node=>{node.disabled=busy;});
    el('stock-cleanup-preview').disabled=busy||!selected.size||!categories().length;
    el('stock-cleanup-open').disabled=busy||!preview||preview.blocked.length>0||!(preview.delete_rows||preview.delete_files);
    el('stock-cleanup-confirm').disabled=busy||el('stock-cleanup-word').value!=='清理';
    el('stock-cleanup-select').disabled=busy||!results().length;
  }
  function renderCandidates() {
    const rows=results();
    el('stock-cleanup-count').textContent=`${rows.length} / ${stocks.length} 只 · 已选 ${selected.size} 只`;
    el('stock-cleanup-candidates').innerHTML=rows.length?rows.map(stock=>{
      const values=Object.values(stock.summary),records=values.reduce((sum,c)=>sum+c.rows,0),bytes=values.reduce((sum,c)=>sum+c.file_bytes,0);
      return `<div class="maintenance-cleanup-stock"><label><input type="checkbox" data-cleanup-code="${esc(stock.code)}" ${selected.has(stock.code)?'checked':''}><strong>${esc(stock.name||'名称暂缺')} · ${esc(stock.code)}</strong></label><span>取消关注 ${stock.unfollowed_days} 天</span><span>${records.toLocaleString('zh-CN')} 条相关记录 · 文件 ${size(bytes)}</span><details><summary>按类别查看占用</summary>${Object.entries(stock.summary).map(([key,c])=>`<p>${esc(labels[key])}：${c.rows.toLocaleString('zh-CN')} 条 · ${c.files} 个文件 / ${size(c.file_bytes)}</p>`).join('')}</details></div>`;
    }).join(''):`<p>${stocks.length?'没有符合筛选条件的股票':'暂无已取消关注的股票'}</p>`;
    controls();
  }
  async function api(path, command) {
    const response=await fetch(path,command===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-Local-Session':session},body:JSON.stringify(command)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'清理操作失败');return data;
  }
  async function load() {
    if(busy)return;busy=true;controls();status('正在统计未关注股票的本地记录和文件…');
    try {
      const data=await api('/api/maintenance/stock-cleanup');stocks=data.stocks;session=data.session_token;loaded=true;
      selected=new Set([...selected].filter(code=>stocks.some(stock=>stock.code===code)));invalidate();
      status(data.blocked.length?`当前暂不能执行清理：${data.blocked.join('；')}`:'请选择股票和类别，再预览清理范围。行业资料和共享引用会保留。');
    }catch(error){status('候选未更新：'+error.message);}
    finally{busy=false;renderCandidates();}
  }
  el('stock-cleanup-refresh').addEventListener('click',load);
  for(const id of ['stock-cleanup-search','stock-cleanup-days'])el(id).addEventListener('input',renderCandidates);
  el('stock-cleanup-select').addEventListener('click',()=>{results().forEach(stock=>selected.add(stock.code));invalidate();renderCandidates();});
  el('stock-cleanup-candidates').addEventListener('change',event=>{
    const input=event.target.closest('[data-cleanup-code]');if(!input)return;
    if(input.checked)selected.add(input.dataset.cleanupCode);else selected.delete(input.dataset.cleanupCode);
    invalidate();renderCandidates();
  });
  el('stock-cleanup-categories').addEventListener('change',()=>{
    if(categories().includes('financial'))el('stock-cleanup-categories').querySelector('[value="cache"]').checked=true;
    invalidate();controls();
  });
  el('stock-cleanup-preview').addEventListener('click',async()=>{
    if(busy||!selected.size)return;invalidate();busy=true;controls();status('正在核对清理范围、共享引用及运行任务…');
    try{
      preview=await api('/api/maintenance/stock-cleanup/preview',{codes:[...selected],categories:categories()});
      const reasons=[...new Set(Object.values(preview.summary).flatMap(summary=>Object.values(summary).flatMap(c=>c.reasons)))];
      el('stock-cleanup-detail').innerHTML=`<div class="maintenance-cleanup-preview"><h4>本次清理预览</h4><p>${preview.codes.length} 只股票（${preview.codes.map(esc).join('、')}） · ${preview.categories.map(c=>esc(labels[c])).join('、')}</p><p><strong>拟删除 ${preview.delete_rows.toLocaleString('zh-CN')} 条记录、${preview.delete_files} 个文件，文件合计 ${size(preview.file_bytes)}</strong></p>
        ${Object.entries(preview.summary).map(([code,summary])=>`<div class="maintenance-cleanup-breakdown"><strong>${esc(code)}</strong>${preview.categories.map(key=>{const c=summary[key];return `<p>${esc(labels[key])}：清理 ${c.delete_rows} 条 / ${c.delete_files} 个文件；保留 ${c.rows-c.delete_rows} 条 / ${c.files-c.delete_files} 个文件</p>`;}).join('')}</div>`).join('')}
        ${reasons.length?`<h4>保留原因</h4><ul>${reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul>`:''}
        ${preview.files.length?`<details><summary>查看待清理文件清单（${preview.files.length} 个）</summary><ul>${preview.files.map(file=>`<li><code>${esc(file.path)}</code> · ${size(file.bytes)}</li>`).join('')}</ul></details>`:''}
        ${preview.notes.map(note=>`<p>${esc(note)}</p>`).join('')}</div>`;
      el('stock-cleanup-open').hidden=false;
      status(preview.blocked.length?`暂不能清理：${preview.blocked.join('；')}。任务结束后重新预览。`:preview.delete_rows||preview.delete_files?'预览 15 分钟内有效，确认后才会执行。':'没有可清理资料；请查看保留原因。');
    }catch(error){status('预览失败：'+error.message);}
    finally{busy=false;controls();}
  });
  el('stock-cleanup-open').addEventListener('click',()=>{
    if(!preview||busy)return;
    el('stock-cleanup-confirm-summary').textContent=`${preview.codes.join('、')}：${preview.categories.map(c=>labels[c]).join('、')}。拟删除 ${preview.delete_rows} 条记录和 ${preview.delete_files} 个文件（${size(preview.file_bytes)}）。执行前不创建备份，成功清理后无法通过本次操作恢复。`;
    el('stock-cleanup-word').value='';controls();el('stock-cleanup-dialog').showModal();
  });
  el('stock-cleanup-word').addEventListener('input',controls);
  el('stock-cleanup-confirm').addEventListener('click',async()=>{
    if(!preview||busy||el('stock-cleanup-word').value!=='清理')return;
    const token=preview.token;el('stock-cleanup-dialog').close();busy=true;controls();status('正在检查引用并清理，请勿重复提交…');
    try{
      const result=await api('/api/maintenance/stock-cleanup/execute',{token,confirm:'清理'});
      invalidate();selected.clear();loaded=false;
      status(`已清理 ${result.delete_rows} 条记录、${result.delete_files} 个文件（${size(result.file_bytes)}）。行业数据与共享引用保留。 SQLite 文件未执行收缩。`);
      window.dispatchEvent(new CustomEvent('stock-cleanup-complete'));
      // Refresh candidates without replacing the completion message.
      const data=await api('/api/maintenance/stock-cleanup');stocks=data.stocks;session=data.session_token;loaded=true;
    }catch(error){invalidate();status('清理未完成或结果待核对：'+error.message+'。请刷新候选与本地统计，勿重复提交旧预览。');}
    finally{busy=false;renderCandidates();}
  });
  const stocksVisible=()=>!panel.hidden&&!document.getElementById('maintenance-stocks-panel').hidden;
  document.getElementById('maintenance-stocks-tab').addEventListener('click',()=>{if(!loaded)load();});
  document.getElementById('maintenance-tab').addEventListener('click',()=>{if(stocksVisible()&&!loaded)load();});
  window.addEventListener('stock-library-updated',()=>{loaded=false;invalidate();if(stocksVisible()&&!busy)load();});
  if(stocksVisible())load();
};
