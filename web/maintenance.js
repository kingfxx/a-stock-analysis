/* Inventory is global to the daily database and refreshes only on this tab. */
(() => {
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const size=value=>value===null||value===undefined?'—':value<1024?`${value} B`:value<1048576?`${(value/1024).toFixed(1)} KiB`:`${(value/1048576).toFixed(2)} MiB`;
  const number=value=>Number(value).toLocaleString('zh-CN');
  const date=value=>!value?'未记录':new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  window.initMaintenance=()=>{
    const panel=document.getElementById('maintenance-panel');
    panel.innerHTML=`<div class="maintenance-head"><div><h2>后台维护</h2><p>本地数据与股票维护</p></div><button id="maintenance-refresh" type="button">刷新本地统计</button></div>
      <div class="maintenance-tabs" role="tablist" aria-label="后台维护分类"><button id="maintenance-database-tab" type="button" role="tab" aria-selected="true" aria-controls="maintenance-database-panel">数据库预览</button><button id="maintenance-stocks-tab" type="button" role="tab" aria-selected="false" aria-controls="maintenance-stocks-panel" tabindex="-1">未关注股票管理</button></div>
      <section id="maintenance-stocks-panel" role="tabpanel" aria-labelledby="maintenance-stocks-tab" hidden>
      <section class="panel maintenance-unfollowed"><div class="maintenance-controls"><h3>已取消关注 <span id="maintenance-unfollowed-count"></span></h3><button id="maintenance-unfollowed-refresh" type="button">刷新列表</button></div>
        <p>取消关注后保留财务数据、财报原件与研究历史，停止个股自动更新。恢复关注会重新加入“全部股票”和仍然存在的原分组；已删除的分组不会重建。</p>
        <div class="maintenance-unfollowed-actions"><input id="maintenance-unfollowed-search" type="search" placeholder="搜索代码或名称" aria-label="搜索已取消关注的股票"><button id="maintenance-unfollowed-select" type="button">选择结果</button><button id="maintenance-unfollowed-restore" type="button" disabled>恢复关注</button><span id="maintenance-unfollowed-selected">0 只已选</span></div>
        <p id="maintenance-unfollowed-status" role="status" aria-live="polite">切换到此页后读取列表。</p><div id="maintenance-unfollowed-list"></div>
      </section>
      <div id="maintenance-stock-cleanup"></div>
      <section class="panel maintenance-restore"><h3>整理数据库空间</h3><p>清理记录后执行空间整理，将库内空闲空间归还磁盘。整理期间暂停其他请求，有运行任务时不能执行；数据库目录及临时目录需预留当前数据库大小两倍的空闲空间。整理不创建备份。</p><button id="maintenance-compact-open" type="button" disabled>整理数据库空间…</button><p id="maintenance-compact-status" role="status" aria-live="polite">整理完成后显示实际文件大小变化。</p></section>
      <dialog id="maintenance-compact-dialog"><form method="dialog"><h3>确认整理数据库空间</h3><p>将整理当前日常数据库，不删除业务记录。期间暂停访问，可能需要数分钟，请等待完成。</p><div class="maintenance-dialog-actions"><button value="cancel">取消</button><button id="maintenance-compact-confirm" type="button">执行整理</button></div></form></dialog>
      </section>
      <section id="maintenance-database-panel" role="tabpanel" aria-labelledby="maintenance-database-tab">
      <p id="maintenance-status" role="status" aria-live="polite">切换到此页后读取统计。</p>
      <div id="maintenance-overview"></div>

      <section class="panel maintenance-restore"><h3>数据库恢复</h3><p>选择备份并校验，确认后恢复。当前数据库会先自动备份；PDF 和原始响应文件需单独保留。</p>
        <div class="maintenance-restore-source"><label>已有备份<select id="maintenance-backup"><option value="">请选择备份文件</option></select></label><span>或</span><label class="maintenance-file-label">选择本地文件<input id="maintenance-backup-file" type="file" accept=".sqlite3,.sqlite,.db"></label><button id="maintenance-restore-preview" type="button" disabled>校验备份</button></div>
        <p id="maintenance-restore-status" role="status" aria-live="polite">只校验不会替换当前数据。</p><div id="maintenance-restore-detail"></div><button id="maintenance-restore-open" type="button" hidden>恢复此备份…</button>
      </section>
      <dialog id="maintenance-restore-dialog"><form method="dialog"><h3>确认恢复数据库</h3><p id="maintenance-restore-confirm-summary"></p><p>备份之后新增或修改的数据将被替换。系统会先保存当前数据库，恢复期间暂时停止访问，随后重新加载后台。</p><label>输入“恢复”以确认<input id="maintenance-restore-word" autocomplete="off"></label><div class="maintenance-dialog-actions"><button value="cancel">取消</button><button id="maintenance-restore-confirm" type="button" disabled>确认恢复</button></div></form></dialog>
      <section class="panel maintenance-inventory"><div class="maintenance-controls"><h3>数据表清单 <span id="maintenance-count"></span></h3><div><label>筛选<input id="maintenance-search" type="search" placeholder="表名或用途" aria-label="搜索表名或用途"></label><label>分类<select id="maintenance-category"><option value="">全部分类</option></select></label></div></div>
      <div class="maintenance-table-scroll"><table><thead><tr>${[['name','表名 / 分类'],['description','存储内容'],['rows','数据条数'],['table_bytes','表大小'],['index_bytes','索引大小'],['total_bytes','合计占用'],['updated_at','数据更新时间']].map(([key,label])=>`<th scope="col" data-column="${key}"><button type="button" data-maintenance-sort="${key}">${label}<span></span></button></th>`).join('')}</tr></thead><tbody id="maintenance-rows"><tr><td colspan="7">尚未读取统计</td></tr></tbody></table></div>
      <div id="maintenance-notes" class="maintenance-notes"></div></section></section>`;
    const el=id=>document.getElementById(id);
    window.initStockCleanup(panel);
    let snapshot=null,loading=false,compactBusy=false,sort='total_bytes',direction=-1;
    let session='',preview=null,restoreBusy=false,backupLoading=null;
    let unfollowed=[],unfollowedLoaded=false,unfollowedBusy=false,unfollowedSelected=new Set();
    const unfollowedResults=()=>{
      const query=el('maintenance-unfollowed-search').value.trim().toLowerCase();
      return unfollowed.filter(stock=>`${stock.code} ${stock.name||''}`.toLowerCase().includes(query));
    };
    function unfollowedControls(){
      el('maintenance-unfollowed-selected').textContent=`${unfollowedSelected.size} 只已选`;
      el('maintenance-unfollowed-restore').disabled=unfollowedBusy||!unfollowedSelected.size;
      el('maintenance-unfollowed-refresh').disabled=unfollowedBusy;
      el('maintenance-unfollowed-select').disabled=unfollowedBusy||!unfollowedResults().length;
    }
    function renderUnfollowed(){
      const rows=unfollowedResults();
      el('maintenance-unfollowed-count').textContent=`${rows.length} / ${unfollowed.length} 只`;
      el('maintenance-unfollowed-list').innerHTML=rows.length?rows.map(stock=>`<label class="maintenance-unfollowed-row"><input type="checkbox" data-unfollowed-code="${esc(stock.code)}" ${unfollowedSelected.has(stock.code)?'checked':''} ${unfollowedBusy?'disabled':''}><span><strong>${esc(stock.name||'名称暂缺')} · ${esc(stock.code)}</strong><small>原分组：${esc(stock.groups.map(group=>group.name).join(' / ')||'无可恢复分组')}</small></span><time datetime="${esc(stock.unfollowed_at)}">${esc(date(stock.unfollowed_at))}</time></label>`).join(''):`<p class="maintenance-unfollowed-empty">${unfollowed.length?'没有匹配的股票':'暂无已取消关注的股票'}</p>`;
      unfollowedControls();
    }
    async function loadUnfollowed(){
      if(unfollowedBusy)return;
      unfollowedBusy=true;unfollowedControls();el('maintenance-unfollowed-status').textContent='正在读取本地列表…';
      try{
        const response=await fetch('/api/maintenance/unfollowed',{cache:'no-store'}),data=await response.json();
        if(!response.ok)throw new Error(data.error||'读取失败');
        unfollowed=data.stocks;unfollowedLoaded=true;
        unfollowedSelected=new Set([...unfollowedSelected].filter(code=>unfollowed.some(stock=>stock.code===code)));
        el('maintenance-unfollowed-status').textContent='仅查看本地记录；恢复关注不会立即采集数据或调用模型。';
      }catch(error){el('maintenance-unfollowed-status').textContent='列表未更新：'+error.message;}
      finally{unfollowedBusy=false;renderUnfollowed();}
    }
    el('maintenance-unfollowed-refresh').addEventListener('click',loadUnfollowed);
    el('maintenance-unfollowed-search').addEventListener('input',renderUnfollowed);
    el('maintenance-unfollowed-select').addEventListener('click',()=>{unfollowedResults().forEach(stock=>unfollowedSelected.add(stock.code));renderUnfollowed();});
    el('maintenance-unfollowed-list').addEventListener('change',event=>{
      const input=event.target.closest('[data-unfollowed-code]');if(!input)return;
      if(input.checked)unfollowedSelected.add(input.dataset.unfollowedCode);else unfollowedSelected.delete(input.dataset.unfollowedCode);
      unfollowedControls();
    });
    el('maintenance-unfollowed-restore').addEventListener('click',async()=>{
      if(unfollowedBusy||!unfollowedSelected.size)return;
      const codes=[...unfollowedSelected];unfollowedBusy=true;renderUnfollowed();
      try{
        const response=await fetch('/api/maintenance/unfollowed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'restore',codes})}),data=await response.json();
        if(!response.ok)throw new Error(data.error||'恢复失败');
        unfollowed=data.stocks;unfollowedSelected.clear();
        window.dispatchEvent(new CustomEvent('stock-library-updated',{detail:{library:data.library,restoredCodes:codes}}));
        el('maintenance-unfollowed-status').textContent=`已恢复关注 ${codes.length} 只股票。可从“我的股票”打开，已有资料可继续使用。`;
      }catch(error){el('maintenance-unfollowed-status').textContent='恢复未完成：'+error.message;}
      finally{unfollowedBusy=false;renderUnfollowed();}
    });
    window.addEventListener('stock-library-updated',()=>{unfollowedLoaded=false;if(!panel.hidden&&!el('maintenance-stocks-panel').hidden&&!unfollowedBusy)loadUnfollowed();});
    const restoreStatus=message=>{el('maintenance-restore-status').textContent=message;};
    function invalidatePreview(){preview=null;el('maintenance-restore-open').hidden=true;el('maintenance-restore-detail').innerHTML='';}
    function sourceSelected(){return Boolean(el('maintenance-backup-file').files.length||el('maintenance-backup').value);}
    async function backups(){
      if(backupLoading)return backupLoading;
      backupLoading=(async()=>{
        const response=await fetch('/api/maintenance/backups',{cache:'no-store'}),data=await response.json();
        if(!response.ok)throw new Error(data.error||'无法读取备份列表');session=data.session_token;
        const selected=el('maintenance-backup').value;
        el('maintenance-backup').innerHTML='<option value="">请选择备份文件</option>'+data.backups.map(r=>`<option value="${esc(r.name)}">${esc(r.name)} · ${size(r.bytes)} · ${esc(date(r.file_time))}</option>`).join('');
        el('maintenance-backup').value=selected;el('maintenance-restore-preview').disabled=restoreBusy||!sourceSelected();
        if(data.restore_status.phase==='uncertain'&&!restoreBusy)restoreStatus(data.restore_status.message);
        if(data.restore_status.phase==='complete'&&!restoreBusy&&!preview)restoreStatus(`上次恢复已完成：${date(data.restore_status.finished_at)}。恢复前备份：${data.restore_status.rollback_backup}。`);
      })();
      try{return await backupLoading;}finally{backupLoading=null;}
    }
    async function restoreApi(path,command){
      if(!session)await backups();
      const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Session':session},body:JSON.stringify(command)}),data=await response.json();
      if(!response.ok)throw new Error(data.error||'恢复操作失败');return data;
    }
    el('maintenance-compact-open').addEventListener('click',()=>{
      if(loading||compactBusy||restoreBusy)return;
      el('maintenance-compact-dialog').showModal();
    });
    el('maintenance-compact-confirm').addEventListener('click',async()=>{
      if(compactBusy||restoreBusy)return;
      el('maintenance-compact-dialog').close();compactBusy=true;el('maintenance-compact-open').disabled=true;el('maintenance-refresh').disabled=true;restoreControls(true);
      el('maintenance-compact-status').textContent='正在检查任务、磁盘空间并整理数据库，请等待完成，勿重复提交…';
      try{
        const result=await restoreApi('/api/maintenance/compact',{confirm:'整理'});
        el('maintenance-compact-status').textContent=`整理完成：${size(result.before_bytes)} → ${size(result.after_bytes)}，释放 ${size(result.released_bytes)}。`;
        await load(true);
      }catch(error){el('maintenance-compact-status').textContent='整理未完成或结果待核对：'+error.message;}
      finally{compactBusy=false;restoreControls(false);el('maintenance-compact-open').disabled=loading||!snapshot;el('maintenance-refresh').disabled=loading;}
    });
    function restoreControls(busy){
      restoreBusy=busy;el('maintenance-backup').disabled=busy;el('maintenance-backup-file').disabled=busy;
      el('maintenance-restore-preview').disabled=busy||!sourceSelected();el('maintenance-restore-open').disabled=busy;
    }
    async function prepareRestore(){
      invalidatePreview();restoreControls(true);restoreStatus('正在检查备份完整性、结构及兼容性；大文件可能需要稍候…');
      try{
        let command={backup:el('maintenance-backup').value};
        const file=el('maintenance-backup-file').files[0];
        if(file){
          if(file.size>4*1024**3)throw new Error('备份文件最大 4 GiB');
          if(!session)await backups();
          const response=await fetch('/api/maintenance/restore/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Local-Session':session,'X-Backup-Name':encodeURIComponent(file.name)},body:file});
          const data=await response.json();if(!response.ok)throw new Error(data.error||'上传失败');command={upload:data.upload};
        }
        preview=await restoreApi('/api/maintenance/restore/preview',command);
        const added=preview.added_tables||[];
        el('maintenance-restore-detail').innerHTML=`<dl><dt>恢复文件</dt><dd>${esc(preview.name)}</dd><dt>文件时间 / 大小</dt><dd>${esc(date(preview.file_time))} · ${size(preview.bytes)}</dd><dt>结构版本</dt><dd>v${preview.source_version}${preview.upgraded?` → v${preview.target_version}（临时副本已升级）`:' · 与当前程序兼容'}</dd><dt>原备份数据范围</dt><dd>${preview.source_table_count} 张表 · ${number(preview.instruments)} 个已记录标的</dd><dt>升级副本结构</dt><dd>${preview.table_count} 张表${added.length?` · 补建 ${added.length} 张表`:''}</dd>${added.length?`<dt>补建表及记录数</dt><dd>${added.map(r=>`${esc(r.name)}：${number(r.rows)} 条`).join('<br>')}</dd>`:''}</dl>${preview.upgraded?'<p>旧备份经迁移后可兼容当前程序；补建结构不代表补齐历史数据，也不会合并当前数据库的数据。恢复后保留的是备份数据及迁移结果。</p>':''}`;
        el('maintenance-restore-open').hidden=false;restoreStatus('完整性与结构兼容性校验通过，不代表数据与当前库一样齐全。当前数据库尚未改变，校验结果 15 分钟内有效。');
      }catch(error){restoreStatus('校验未通过：'+error.message);}finally{restoreControls(false);}
    }
    async function waitForRestore(id){
      for(let attempt=0;attempt<300;attempt++){
        await new Promise(resolve=>setTimeout(resolve,2000));
        try{
          const response=await fetch('/api/maintenance/restore/status',{cache:'no-store',signal:AbortSignal.timeout(5000)});if(!response.ok)continue;
          const data=await response.json();if(id&&data.id!==id)continue;
          if(!id&&data.phase==='idle'){restoreStatus('恢复请求未提交，当前数据未替换；可重新校验备份。');restoreControls(false);return;}
          if(data.phase==='complete'){restoreStatus(`恢复完成，后台已重新加载。恢复前备份：${data.rollback_backup}。即将刷新页面…`);invalidatePreview();await new Promise(resolve=>setTimeout(resolve,1500));location.reload();return;}
          if(data.phase==='failed'){restoreStatus('恢复未完成：'+data.message+(data.rolled_back?'；已回退至恢复前数据库。':'；当前数据未替换。'));restoreControls(false);return;}
          if(data.phase==='uncertain'){restoreStatus(data.message);return;}
        }catch{} // The listener deliberately pauses while replacing the database.
      }
      restoreStatus('后台尚未返回结果，请检查后台日志或稍后刷新；不要重复提交恢复。');
    }
    el('maintenance-backup').addEventListener('change',()=>{el('maintenance-backup-file').value='';invalidatePreview();el('maintenance-restore-preview').disabled=!sourceSelected();restoreStatus('只校验不会替换当前数据。');});
    el('maintenance-backup-file').addEventListener('change',()=>{el('maintenance-backup').value='';invalidatePreview();el('maintenance-restore-preview').disabled=!sourceSelected();restoreStatus('只校验不会替换当前数据。');});
    el('maintenance-restore-preview').addEventListener('click',prepareRestore);
    el('maintenance-restore-open').addEventListener('click',()=>{if(!preview)return;el('maintenance-restore-confirm-summary').textContent=`将使用 ${preview.name}（v${preview.source_version}${preview.upgraded?` 升级至 v${preview.target_version}`:''}）替换当前数据库。${preview.added_tables?.length?'旧备份缺少的表已在副本中补建，当前库中的对应数据不会合并保留。':''}`;el('maintenance-restore-word').value='';el('maintenance-restore-confirm').disabled=true;el('maintenance-restore-dialog').returnValue='';el('maintenance-restore-dialog').showModal();});
    el('maintenance-restore-dialog').addEventListener('close',async()=>{if(el('maintenance-restore-dialog').returnValue==='confirm')return;try{await restoreApi('/api/maintenance/restore/cancel',{});invalidatePreview();restoreStatus('已取消，当前数据库未改变。');}catch(error){restoreStatus('取消未完成：'+error.message);}});
    el('maintenance-restore-word').addEventListener('input',()=>{el('maintenance-restore-confirm').disabled=el('maintenance-restore-word').value!=='恢复';});
    el('maintenance-restore-confirm').addEventListener('click',async()=>{
      if(!preview||el('maintenance-restore-word').value!=='恢复'||restoreBusy)return;
      el('maintenance-restore-dialog').close('confirm');restoreControls(true);restoreStatus('已确认，正在备份当前数据库并恢复。后台会暂时不可访问，请勿重复操作…');
      try{const data=await restoreApi('/api/maintenance/restore/confirm',{token:preview.token,confirm:'恢复'});await waitForRestore(data.id);}
      catch(error){restoreStatus('请求结果待确认：'+error.message+'；正在检查后台状态，请勿重复提交。');await waitForRestore(null);}
    });
    function renderRows(){
      if(!snapshot)return;
      const search=el('maintenance-search').value.trim().toLowerCase(),category=el('maintenance-category').value;
      const rows=snapshot.tables.filter(r=>(!category||r.category===category)&&`${r.name} ${r.description}`.toLowerCase().includes(search));
      rows.sort((a,b)=>{
        const x=a[sort],y=b[sort];if(x===null||x===undefined)return y===null||y===undefined?0:1;if(y===null||y===undefined)return -1;
        return direction*(typeof x==='number'?x-y:String(x).localeCompare(String(y),'zh-CN'))||a.name.localeCompare(b.name);
      });
      el('maintenance-count').textContent=`${rows.length} / ${snapshot.tables.length} 张`;
      panel.querySelectorAll('[data-column]').forEach(th=>{const active=th.dataset.column===sort;th.setAttribute('aria-sort',active?(direction===1?'ascending':'descending'):'none');th.querySelector('span').textContent=active?(direction===1?' ↑':' ↓'):'';});
      el('maintenance-rows').innerHTML=rows.length?rows.map(r=>`<tr><td><code>${esc(r.name)}</code><small>${esc(r.category)} · ${r.column_count} 列</small></td><td class="maintenance-description">${esc(r.description)}</td><td class="maintenance-numeric">${number(r.rows)}</td><td class="maintenance-numeric">${size(r.table_bytes)}</td><td class="maintenance-numeric">${size(r.index_bytes)}<small>${r.index_count} 个索引</small></td><td class="maintenance-numeric maintenance-total">${size(r.total_bytes)}</td><td class="maintenance-time">${r.rows===0?'暂无数据':esc(date(r.updated_at))}<small>${esc(r.time_basis)}</small></td></tr>`).join(''):'<tr><td colspan="7">没有符合条件的数据表</td></tr>';
    }
    function render(){
      const s=snapshot.summary;
      const parts=[['表数据',s.table_bytes,'tables'],['索引',s.index_bytes,'indexes'],['空闲页',s.free_bytes,'free'],['系统页',s.system_bytes,'system']];
      const latest=s.backups.latest;
      el('maintenance-overview').innerHTML=`<section class="panel maintenance-database"><div class="maintenance-summary"><div><span>数据库文件</span><strong>${size(s.database_bytes)}</strong></div><div><span>空闲页空间</span><strong>${size(s.free_bytes)}</strong></div><div><span>数据表</span><strong>${s.table_count}<small> 张</small></strong></div><div><span>结构版本</span><strong>v${s.schema_version}</strong></div></div>
        <div class="maintenance-space" aria-label="数据库空间分布">${parts.filter(p=>p[1]>0).map(([label,bytes,key])=>`<span class="maintenance-space-${key}" style="width:${bytes/s.database_bytes*100}%" title="${label} ${size(bytes)}"></span>`).join('')}</div>
        <div class="maintenance-space-legend">${parts.map(([label,bytes,key])=>`<span><i class="maintenance-space-${key}"></i>${label} ${size(bytes)}</span>`).join('')}</div>
        <dl class="maintenance-details"><dt>当前数据库</dt><dd><code>${esc(s.database_path)}</code></dd><dt>运行信息</dt><dd>SQLite ${esc(s.sqlite_version)} · ${esc(s.journal_mode)} · 页大小 ${size(s.page_size)}</dd><dt>最近备份</dt><dd>${latest?`${esc(latest.name)} · ${size(latest.bytes)}<small>${esc(date(latest.created_at))} · 备份目录共 ${s.backups.count} 份 / ${size(s.backups.total_bytes)}</small>`:'暂无数据库备份'}</dd></dl></section>`;
      const categories=[...new Set(snapshot.tables.map(r=>r.category))];
      const previous=el('maintenance-category').value;
      el('maintenance-category').innerHTML='<option value="">全部分类</option>'+categories.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');el('maintenance-category').value=previous;
      el('maintenance-notes').innerHTML=snapshot.notes.map(n=>`<p>${esc(n)}</p>`).join('')+(s.size_method==='unavailable'?'<p>当前 WAL 模式缺少 dbstat 扩展，逐表物理大小暂不可用。</p>':'');
      renderRows();
    }
    async function load(refresh=false){
      if(loading)return;loading=true;el('maintenance-refresh').disabled=true;el('maintenance-compact-open').disabled=true;
      el('maintenance-status').textContent='正在统计本地记录和 SQLite 占用页…';
      try{
        const response=await fetch('/api/maintenance/storage'+(refresh?'?refresh=1':''),{cache:'no-store'}),data=await response.json();
        if(!response.ok)throw new Error(data.error||'读取统计失败');
        snapshot=data;render();el('maintenance-status').textContent=`统计时间：${date(data.generated_at)} · 使用“刷新本地统计”更新`;await backups();
      }catch(error){el('maintenance-status').textContent='统计未更新：'+error.message;}
      finally{loading=false;el('maintenance-refresh').disabled=compactBusy;el('maintenance-compact-open').disabled=compactBusy||restoreBusy||!snapshot;}
    }
    el('maintenance-refresh').addEventListener('click',()=>load(true));
    window.addEventListener('stock-cleanup-complete',()=>{load(true);loadUnfollowed();});
    el('maintenance-search').addEventListener('input',renderRows);el('maintenance-category').addEventListener('change',renderRows);
    panel.querySelector('thead').addEventListener('click',event=>{const button=event.target.closest('[data-maintenance-sort]');if(!button)return;const key=button.dataset.maintenanceSort;direction=key===sort?-direction:(['rows','table_bytes','index_bytes','total_bytes','updated_at'].includes(key)?-1:1);sort=key;renderRows();});
    const tabs=[el('maintenance-database-tab'),el('maintenance-stocks-tab')];
    function loadActiveTab(){
      if(el('maintenance-stocks-panel').hidden){if(!snapshot)load();}
      else if(!unfollowedLoaded)loadUnfollowed();
    }
    tabs.forEach((tab,index)=>{
      tab.addEventListener('click',()=>{
        tabs.forEach(item=>{const active=item===tab;item.setAttribute('aria-selected',String(active));item.tabIndex=active?0:-1;el(item.getAttribute('aria-controls')).hidden=!active;});
        el('maintenance-refresh').hidden=index===1;loadActiveTab();
      });
      tab.addEventListener('keydown',event=>{
        if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
        event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?1:1-index;tabs[next].focus();tabs[next].click();
      });
    });
    document.getElementById('maintenance-tab').addEventListener('click',loadActiveTab);
    if(!panel.hidden)loadActiveTab();
  };
})();
