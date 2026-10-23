/* One Markdown document per stock; shared editor, conditional autosave, local facts. */
(() => {
  'use strict';
  const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const el = id => document.getElementById(id);
  const localDate = () => new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const localTime = () => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date());
  const stripHeader = text => text.replace(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/,'');
  let token, sessionRequest;
  async function api(path, command, method='POST') {
    const options={cache:'no-store'};
    if(command!==undefined){
      if(!token){sessionRequest??=fetch('/api/notes/settings',{cache:'no-store'}).then(r=>r.json()).then(x=>{if(x.error)throw Error(x.error);token=x.session_token;}).finally(()=>sessionRequest=null);await sessionRequest;}
      options.method=method;options.headers={'Content-Type':'application/json','X-Local-Session':token};options.body=JSON.stringify(command);
    }
    const response=await fetch(path,options),data=await response.json();
    if(!response.ok)throw Object.assign(Error(data.error||'笔记操作失败'),{data,status:response.status});
    return data;
  }
  let draftDB;
  function database(){
    return draftDB??=new Promise((resolve,reject)=>{
      const request=indexedDB.open('investment-note-drafts-v1',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('drafts',{keyPath:'key'});
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
  }
  async function draft(action,key,value){
    const db=await database();return new Promise((resolve,reject)=>{
      const tx=db.transaction('drafts',action==='get'?'readonly':'readwrite'),store=tx.objectStore('drafts');
      const request=action==='get'?store.get(key):action==='delete'?store.delete(key):store.put({...value,key});
      tx.oncomplete=()=>resolve(request.result);tx.onerror=()=>reject(tx.error);
    });
  }
  window.initInvestmentNotes = state => {
    const main=el('notes-panel');
    main.innerHTML=`<div class="notes-head"><div><h2>投资笔记</h2><p>把当时的想法留给以后复盘</p></div><div class="notes-actions"><button type="button" id="notes-current">当前股票</button><button type="button" id="notes-all">全部笔记</button></div></div><div id="notes-main-host"></div>`;
    const quick=document.createElement('dialog');quick.className='notes-dialog';quick.id='notes-quick';
    quick.innerHTML='<div class="notes-head"><h2>记一笔</h2><div class="notes-actions"><button type="button" id="notes-full">打开完整笔记</button><button type="button" id="notes-close">关闭</button></div></div><div id="notes-quick-host"></div>';
    document.body.append(quick);
    const workspace=document.createElement('section');workspace.className='panel notes-workspace';
    workspace.innerHTML=`<div class="notes-head"><strong id="notes-company"></strong><span id="notes-filename" class="notes-location"></span></div><div class="notes-tools"><button type="button" id="notes-new">新增记录</button><button type="button" id="notes-insert">插入数据</button><button type="button" id="notes-edit" aria-pressed="true">编辑</button><button type="button" id="notes-preview-button" aria-pressed="false">预览</button><button type="button" id="notes-bold">加粗</button><button type="button" id="notes-list-format">列表</button><button type="button" id="notes-undo">撤销</button><button type="button" id="notes-redo">重做</button><button type="button" id="notes-export">导出</button><button type="button" id="notes-history">历史版本</button><button type="button" id="notes-retry" hidden>重试保存</button><span id="notes-status" class="notes-status" role="status" aria-live="polite">尚未读取</span></div><nav class="notes-toc" id="notes-toc" aria-label="笔记日期目录"></nav><div id="notes-conflict" class="notes-conflict" hidden><h3>文件有外部修改</h3><p id="notes-conflict-message"></p><div class="notes-conflict-grid"><label>本地草稿（可在正文编辑后再比较）<textarea id="notes-local-copy" readonly></textarea></label><label>磁盘版本<textarea id="notes-disk-copy" readonly></textarea></label></div><div class="notes-actions"><button type="button" id="notes-use-disk">采用磁盘版本</button><button type="button" id="notes-use-local">采用当前编辑内容</button><button type="button" id="notes-copy-draft">导出草稿</button></div></div><div id="notes-editor-host"><textarea id="notes-source" aria-label="投资笔记 Markdown 正文"></textarea></div><article class="notes-preview" id="notes-rendered" hidden></article>`;
    el('notes-main-host').append(workspace);
    const editor=CodeMirror.fromTextArea(el('notes-source'),{mode:'markdown',lineWrapping:true,indentUnit:2,tabSize:2,readOnly:true,extraKeys:{'Ctrl-S':()=>flush(active),'Cmd-S':()=>flush(active)}});
    editor.getWrapperElement().setAttribute('aria-label','投资笔记 Markdown 编辑器');
    const md=window.markdownit({html:false,linkify:false,breaks:true}).use(window.markdownitFootnote);
    const contexts=new Map();let active=null,loadSequence=0,muting=false,preview=false,saveTimer,loading=false,outlineOpen=false;
    const outlineButton=document.createElement('button');outlineButton.type='button';outlineButton.id='notes-outline';outlineButton.textContent='大纲';outlineButton.setAttribute('aria-expanded','false');outlineButton.setAttribute('aria-controls','notes-toc');el('notes-preview-button').after(outlineButton);
    el('notes-toc').hidden=true;
    outlineButton.onclick=()=>{outlineOpen=!outlineOpen;outlineButton.setAttribute('aria-expanded',String(outlineOpen));updateToc();};
    function status(message,error=false){el('notes-status').textContent=message;el('notes-status').dataset.error=String(error);}
    function keyOf(doc){return `${doc.directory_version}:${doc.code}`;}
    function changed(ctx){return ctx&&ctx.content!==ctx.saved;}
    function showStatus(ctx=active){if(ctx!==active)return;status(ctx?.conflict?'存在外部修改，自动保存已暂停':ctx?.saving?'保存中…':changed(ctx)?'未保存':ctx?.exists?'已保存 '+localTime():'写下第一条记录后自动保存',Boolean(ctx?.conflict));el('notes-retry').hidden=!changed(ctx)||Boolean(ctx?.conflict);}
    function download(text,name){const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    async function remember(ctx){
      ctx.draftPending=(ctx.draftPending||0)+1;
      try{await draft('put',ctx.key,{content:ctx.content,version:ctx.version,file:ctx.file,code:ctx.code,directory_version:ctx.directory_version});}
      catch{ctx.draftError=true;if(ctx===active)status('浏览器恢复草稿不可用，请保持页面打开；仍尝试保存到文件',true);}
      finally{ctx.draftPending--;}
    }
    function renderPreview(){
      const container=el('notes-rendered');container.innerHTML=DOMPurify.sanitize(md.render(stripHeader(active?.content||'')),{USE_PROFILES:{html:true}});
      container.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach((heading,index)=>{heading.id='notes-heading-'+index;});
      container.querySelectorAll('img').forEach(image=>{
        const source=image.getAttribute('src')||'';
        if(!/^(?:[a-z][a-z0-9+.-]*:|\/|\\)/i.test(source))image.src='/api/notes/resource?'+new URLSearchParams({path:source});else image.removeAttribute('src');
      });
      container.querySelectorAll('a').forEach(link=>{
        const source=link.getAttribute('href')||'';
        if(source.startsWith('#'))return;
        if(/^https?:\/\//i.test(source)){link.target='_blank';link.rel='noopener noreferrer';}
        else if(!/^(?:[a-z][a-z0-9+.-]*:|\/|\\)/i.test(source))link.href='/api/notes/resource?'+new URLSearchParams({path:source});
        else link.removeAttribute('href');
      });
    }
    function setPreview(value){preview=value;el('notes-editor-host').hidden=value;el('notes-rendered').hidden=!value;el('notes-edit').setAttribute('aria-pressed',String(!value));el('notes-preview-button').setAttribute('aria-pressed',String(value));if(value)renderPreview();else{editor.refresh();editor.focus();}}
    function updateToc(){
      const toc=el('notes-toc');toc.hidden=!outlineOpen;if(!outlineOpen)return;toc.replaceChildren();
      const full=editor.getValue(),body=stripHeader(full),offset=full.slice(0,full.length-body.length).split('\n').length-1;
      const tokens=md.parse(body,{});let index=0;
      tokens.forEach((token,i)=>{
        if(token.type!=='heading_open'||!token.map)return;
        const inline=tokens[i+1],label=inline?.children?.map(t=>['text','code_inline','image'].includes(t.type)?t.content:t.type==='softbreak'?' ':'').join('')||inline?.content||'无标题';
        const headingIndex=index++,line=token.map[0]+offset,button=document.createElement('button');button.type='button';button.textContent=label;button.style.paddingLeft=(12+(Number(token.tag.slice(1))-1)*16)+'px';
        button.onclick=()=>{
          if(preview){const heading=el('notes-heading-'+headingIndex);if(heading){heading.scrollIntoView({block:'start',behavior:'instant'});heading.tabIndex=-1;heading.focus({preventScroll:true});}}
          else{const pos={line,ch:0};editor.setCursor(pos);editor.scrollIntoView(pos,80);editor.focus();}
        };toc.append(button);
      });
      if(!index)toc.textContent='文档暂无标题，可用 #、## 等 Markdown 标题组织记录。';
    }
    function showDocument(ctx){
      active=ctx;muting=true;editor.setValue(ctx.content);editor.clearHistory();if(ctx.cursor)editor.setCursor(ctx.cursor);muting=false;editor.setOption('readOnly',false);
      el('notes-company').textContent=`${ctx.name||ctx.code} · ${ctx.code}`;el('notes-filename').textContent=ctx.file;
      conflictUI();showStatus();updateToc();if(preview)renderPreview();requestAnimationFrame(()=>editor.refresh());
    }
    function conflictUI(){
      const ctx=active;el('notes-conflict').hidden=!ctx?.conflict;
      if(!ctx?.conflict)return;
      el('notes-conflict-message').textContent=ctx.conflictMessage||'两边内容均保留。可编辑正文合并后，选择采用当前编辑内容。';
      el('notes-local-copy').value=ctx.content;el('notes-disk-copy').value=ctx.remote?.content??'目录变化或文件暂时不可读，请重试读取。';
      const canResolve=ctx.remote&&ctx.remote.directory_version===ctx.directory_version;
      el('notes-use-disk').disabled=!canResolve;el('notes-use-local').disabled=!canResolve;
    }
    async function flush(ctx){
      clearTimeout(saveTimer);
      if(!ctx||!changed(ctx)||ctx.conflict)return !ctx?.conflict;
      if(ctx.saving){await ctx.saving;if(ctx.failed)return false;return changed(ctx)&&!ctx.conflict?flush(ctx):!ctx.conflict;}
      const text=ctx.content;
      ctx.saving=(async()=>{
        try{
          const saved=await api('/api/notes/document',{code:ctx.code,name:ctx.name,content:text,version:ctx.version,directory_version:ctx.directory_version},'PUT');
          ctx.version=saved.version;ctx.file=saved.file;ctx.exists=true;ctx.saved=text;
          if(!changed(ctx)){await draft('delete',ctx.key).catch(()=>{});}else await remember(ctx);
          if(ctx===active)el('notes-filename').textContent=ctx.file;
          ctx.failed=false;
        }catch(error){
          ctx.failed=true;
          if(error.status===409){ctx.conflict=true;ctx.remote=error.data.current;ctx.conflictMessage=error.message;}
          await remember(ctx);
          if(ctx===active){conflictUI();status('保存失败：'+error.message,true);el('notes-retry').hidden=false;}
        }
      })();showStatus(ctx);await ctx.saving;ctx.saving=null;
      if(ctx.failed)return false;
      if(changed(ctx))return flush(ctx);
      showStatus(ctx);return true;
    }
    async function openDocument(code=state.code,name=state.name){
      const sequence=++loadSequence;loading=true;editor.setOption('readOnly',true);
      if(active)await flush(active);
      try{
        const doc=await api('/api/notes/document?'+new URLSearchParams({code,name:name||''}));
        if(sequence!==loadSequence)return false;
        const key=keyOf(doc);let ctx=contexts.get(key);
        if(!ctx){
          ctx={...doc,name,content:doc.content,saved:doc.content,key};
          const recovery=await draft('get',key).catch(()=>null);
          if(sequence!==loadSequence)return false;
          if(recovery&&recovery.content!==doc.content){ctx.content=recovery.content;if(recovery.version!==doc.version){ctx.conflict=true;ctx.remote=doc;ctx.conflictMessage='发现未保存草稿，磁盘版本也已变化，请比较后恢复。';}}
          contexts.set(key,ctx);
        }else if(doc.version!==ctx.version){
          if(changed(ctx)||ctx.conflict){ctx.remote=doc;ctx.conflict=true;}
          else if(!doc.exists&&ctx.exists){ctx.remote=doc;ctx.conflict=true;ctx.conflictMessage='笔记被外部删除或改名，请先核对存储目录；不会自动重建旧文件。';}
          else Object.assign(ctx,doc,{saved:doc.content});
        }
        showDocument(ctx);if(changed(ctx)&&!ctx.conflict)saveTimer=setTimeout(()=>flush(ctx),800);
        return true;
      }catch(error){status('读取失败：'+error.message,true);return false;}
      finally{if(sequence===loadSequence){loading=false;editor.setOption('readOnly',!active);}}
    }
    editor.on('change',()=>{
      if(muting||!active)return;
      const ctx=active;ctx.content=editor.getValue();ctx.failed=false;remember(ctx);showStatus();conflictUI();updateToc();clearTimeout(saveTimer);
      if(!ctx.conflict)saveTimer=setTimeout(()=>flush(ctx),800);
    });
    editor.on('cursorActivity',()=>{if(!muting&&active)active.cursor=editor.getCursor();});
    function newRecord(){
      if(!active||loading)return;setPreview(false);
      let text=editor.getValue();
      if(!text.trim()){
        const title=String(active.name||active.code).replace(/[\r\n]/g,' ');
        text=`# ${title} · 投资笔记\n\n`;
        muting=true;editor.setValue(text);muting=false;
      }
      const bodyStart=text.length-stripHeader(text).length;
      const titleMatch=/^# .*(?:\r?\n|$)/m.exec(text.slice(bodyStart));
      const index=titleMatch?bodyStart+titleMatch.index+titleMatch[0].length:bodyStart;
      const date=localDate(),sameDay=new RegExp(`^## ${date}(?:[ \\r\\n]|$)`,'m').test(text);
      const heading=`\n## ${date}${sameDay?' '+localTime():''}\n\n`;
      const pos=editor.posFromIndex(index);editor.replaceRange(heading+'\n',pos);editor.setCursor(editor.posFromIndex(index+heading.length));editor.scrollIntoView(editor.getCursor(),50);
    }
    async function checkExternal(){
      const ctx=active;if(!ctx||loading||ctx.saving||document.hidden||(main.hidden&&!quick.open))return;
      try{
        const disk=await api('/api/notes/document?'+new URLSearchParams({code:ctx.code,name:ctx.name||''}));
        if(ctx!==active||ctx.saving)return;
        if(disk.directory_version!==ctx.directory_version){ctx.conflict=true;ctx.remote=null;ctx.conflictMessage='存储目录已切换。请先导出当前草稿，再重新打开目标目录笔记。';}
        else if(disk.version!==ctx.version){
          if(changed(ctx)||ctx.conflict||(!disk.exists&&ctx.exists)){ctx.remote=disk;ctx.conflict=true;ctx.conflictMessage=!disk.exists?'文件被外部删除或改名；请核对目录，选择恢复后才会重建。':'检测到外部修改，请比较后选择。';}
          else{Object.assign(ctx,disk,{saved:disk.content});showDocument(ctx);status('已载入外部修改');}
        }
        conflictUI();if(ctx.conflict)showStatus();
      }catch(error){status('文件检查失败：'+error.message,true);}
    }
    setInterval(checkExternal,3000);window.addEventListener('focus',checkExternal);
    window.addEventListener('beforeunload',event=>{if([...contexts.values()].some(c=>changed(c)||c.saving||c.draftPending||c.conflict)){event.preventDefault();event.returnValue='';}});
    document.addEventListener('visibilitychange',()=>{if(document.hidden)flush(active);else checkExternal();});
    el('notes-use-disk').onclick=async()=>{
      const ctx=active;if(!ctx?.remote)return;
      try{
        await api('/api/notes/recovery',{code:ctx.code,content:ctx.content,directory_version:ctx.directory_version});
        Object.assign(ctx,ctx.remote,{saved:ctx.remote.content,conflict:false,remote:null});await draft('delete',ctx.key).catch(()=>{});showDocument(ctx);
      }catch(error){status('保留草稿副本失败：'+error.message,true);}
    };
    el('notes-use-local').onclick=()=>{
      const ctx=active;if(!ctx?.remote)return;ctx.version=ctx.remote.version;ctx.saved=ctx.remote.content;ctx.conflict=false;ctx.remote=null;ctx.failed=false;conflictUI();flush(ctx);
    };
    el('notes-copy-draft').onclick=()=>active&&download(active.content,active.file.replace(/\.md$/, '_草稿.md'));
    el('notes-edit').onclick=()=>setPreview(false);el('notes-preview-button').onclick=()=>setPreview(true);
    el('notes-new').onclick=newRecord;el('notes-retry').onclick=()=>flush(active);
    el('notes-bold').onclick=()=>{if(!active||loading)return;setPreview(false);editor.replaceSelection('**'+(editor.getSelection()||'文字')+'**');};
    el('notes-list-format').onclick=()=>{if(!active||loading)return;setPreview(false);editor.replaceSelection('- '+editor.getSelection());};
    el('notes-undo').onclick=()=>editor.undo();el('notes-redo').onclick=()=>editor.redo();
    el('notes-export').onclick=()=>active&&download(active.content,active.file);
    async function closeQuick(){await flush(active);quick.close();el('notes-main-host').append(workspace);}
    el('notes-close').onclick=closeQuick;quick.addEventListener('cancel',event=>{event.preventDefault();closeQuick();});
    el('notes-full').onclick=async()=>{await closeQuick();el('notes-tab').click();};
    el('notes-current').onclick=()=>openDocument();

    const browser=document.createElement('dialog');browser.className='notes-dialog';document.body.append(browser);
    el('notes-all').onclick=async()=>{
      browser.innerHTML='<div class="notes-head"><h3>全部笔记</h3><button type="button" data-close>关闭</button></div><input id="notes-search" aria-label="搜索笔记" placeholder="搜索股票、文件名或正文"><div class="notes-list" id="notes-browser-list"></div>';
      browser.querySelector('[data-close]').onclick=()=>browser.close();browser.showModal();
      try{
        const data=await api('/api/notes');
        const render=async()=>{
          const query=el('notes-search').value.trim().toLowerCase(),target=el('notes-browser-list');target.replaceChildren();
          const request=++browser.searchSequence;
          for(const row of data.items){
            if(!row.code)continue;
            let match=[row.code,row.title,row.file].join(' ').toLowerCase().includes(query);
            if(query&&!match){try{const doc=await api('/api/notes/document?'+new URLSearchParams({code:row.code}));match=doc.content.toLowerCase().includes(query);}catch{}}
            if(request!==browser.searchSequence)return;
            if(match){const button=document.createElement('button');button.innerHTML=`${esc(row.title||row.code)}<small>${esc(row.file)}</small>`;button.onclick=async()=>{if(await openDocument(row.code,row.title)){browser.close();}};target.append(button);}
          }
          if(!target.children.length)target.textContent='没有匹配的笔记。未关联文件可在后台维护中关联股票。';
        };browser.searchSequence=0;el('notes-search').oninput=render;render();
      }catch(error){el('notes-browser-list').textContent=error.message;}
    };
    el('notes-history').onclick=async()=>{
      if(!active)return;const ctx=active;
      browser.innerHTML='<div class="notes-head"><h3>历史版本</h3><button type="button" data-close>关闭</button></div><p>加载到编辑器后会自动保存；当前版本会先保留。冲突副本不自动清理。</p><div class="notes-list" id="notes-browser-list"></div>';
      browser.querySelector('[data-close]').onclick=()=>browser.close();browser.showModal();
      try{const data=await api('/api/notes/history?'+new URLSearchParams({code:ctx.code}));
        for(const row of data.items){const button=document.createElement('button');button.textContent=row.id+' · '+new Date(row.updated_at*1000).toLocaleString('zh-CN');button.onclick=async()=>{
          try{const old=await api('/api/notes/history?'+new URLSearchParams({code:ctx.code,id:row.id}));
            if(ctx!==active)return;
            const box=document.createElement('textarea');box.value=old.content;box.style.cssText='width:100%;height:35vh';
            const restore=document.createElement('button');restore.textContent='将此版本加载到编辑器';restore.onclick=()=>{editor.setValue(old.content);browser.close();setPreview(false);};
            el('notes-browser-list').replaceChildren(box,restore);
          }catch(error){status(error.message,true);}
        };el('notes-browser-list').append(button);}
        if(!data.items.length)el('notes-browser-list').textContent='暂无历史版本。';
      }catch(error){el('notes-browser-list').textContent=error.message;}
    };

    const picker=document.createElement('dialog');picker.className='notes-dialog';document.body.append(picker);
    let pickSequence=0;
    async function insertData(context={}){
      if(!active||loading)return;const ctx=active;
      const cursor=editor.getCursor();
      picker.innerHTML=`<div class="notes-head"><h3>插入已有本地数据</h3><button type="button" id="notes-pick-close">关闭</button></div><div class="notes-filter"><label>数据<select id="notes-pick-group"><option value="valuation">估值与分位</option><option value="financial">经营趋势</option><option value="chips">股东与融资</option><option value="business">主营构成</option><option value="statements">财务三表</option></select></label><label>周期<select id="notes-pick-mode"><option value="quarter">单季度</option><option value="ytd">当年累计</option><option value="year">年度</option><option value="ttm">TTM</option></select></label><label>日期／报告期<select id="notes-pick-period"><option value="">最新已有</option></select></label><label>历史区间<select id="notes-pick-years"><option value="10">近10年</option><option value="5">近5年</option><option value="3">近3年</option></select></label><label>维度<select id="notes-pick-dimension"><option value="2">产品</option><option value="1">行业</option><option value="3">地区</option></select></label><label>报表<select id="notes-pick-statement"><option value="lrb">利润表</option><option value="fzb">资产负债表</option><option value="llb">现金流量表</option></select></label><label>筹码<select id="notes-pick-kind"><option value="holders">股东户数</option><option value="financing">融资</option></select></label></div><p id="notes-pick-status" role="status"></p><input id="notes-pick-search" aria-label="筛选指标" placeholder="筛选指标或主营项目"><div id="notes-pick-list" class="notes-snapshot-list"></div><div class="notes-filter"><label>插入形式<select id="notes-pick-format"><option value="inline">单项文字</option><option value="table">数据表</option></select></label><label>插入位置<select id="notes-pick-position"><option value="cursor">当前光标</option><option value="new">新日期记录</option></select></label></div><pre id="notes-pick-preview" class="notes-snapshot-preview"></pre><div class="notes-actions"><button type="button" id="notes-pick-confirm" disabled>插入笔记</button></div>`;
      el('notes-pick-close').onclick=()=>{pickSequence++;picker.close();};picker.showModal();
      const assign=(id,value)=>{if(value&&[...el(id).options].some(o=>o.value===String(value)))el(id).value=value;};
      assign('notes-pick-group',context.group);assign('notes-pick-mode',context.mode);assign('notes-pick-years',context.years);assign('notes-pick-dimension',context.dimension);assign('notes-pick-statement',context.statement);assign('notes-pick-kind',context.kind);
      if(!ctx.content.trim()||!ctx.cursor)el('notes-pick-position').value='new';
      let snapshot=null,selected=new Set();
      const cell=x=>String(x).replace(/[|\r\n]/g,' ');
      function fragment(){
        const rows=(snapshot?.items||[]).filter(r=>selected.has(r.id));if(!rows.length)return '';
        const table=el('notes-pick-format').value==='table';
        const text=table?'\n\n| 指标 | 数值 | 日期／报告期 | 口径 |\n|---|---:|---|---|\n'+rows.map(r=>`| ${cell(r.label)} | ${cell(r.value)} | ${cell(r.date)} | ${cell(r.basis)} |`).join('\n')+'\n':rows.map(r=>`${r.label} ${r.value}${/Q\d/.test(r.label)?`（${r.basis}）`:''}`).join('；');
        return text;
      }
      function update(){const content=fragment();el('notes-pick-preview').textContent=content||'请选择需要插入的数据。';el('notes-pick-confirm').disabled=!content;}
      function renderRows(){
        const query=el('notes-pick-search').value.trim().toLowerCase();el('notes-pick-list').innerHTML=(snapshot?.items||[]).filter(r=>(r.label+' '+r.value).toLowerCase().includes(query)).map(r=>`<label class="notes-snapshot-row"><input type="checkbox" value="${esc(r.id)}" ${selected.has(r.id)?'checked':''}><span>${esc(r.label)}</span><strong>${esc(r.value)}</strong><small>${esc(r.date)} · ${esc(r.basis)}${r.available?'':' · 可显式插入缺失说明'}</small></label>`).join('');
        el('notes-pick-list').querySelectorAll('input').forEach(input=>input.onchange=()=>{if(input.checked)selected.add(input.value);else selected.delete(input.value);update();});
      }
      async function load(keepPeriod=false){
        const sequence=++pickSequence,group=el('notes-pick-group').value;
        selected.clear();snapshot=null;update();el('notes-pick-list').replaceChildren();el('notes-pick-status').textContent='正在读取本地数据…';
        for(const [id,visible] of Object.entries({'notes-pick-mode':['financial','statements'].includes(group),'notes-pick-period':group!=='valuation','notes-pick-years':group==='valuation','notes-pick-dimension':group==='business','notes-pick-statement':group==='statements','notes-pick-kind':group==='chips'}))el(id).closest('label').hidden=!visible;
        try{
          const request={code:ctx.code,group,mode:el('notes-pick-mode').value,years:el('notes-pick-years').value,dimension:el('notes-pick-dimension').value,statement:el('notes-pick-statement').value,kind:el('notes-pick-kind').value,comparison:context.comparison||'yoy'};
          if(group==='statements'&&!['quarter','ytd'].includes(request.mode)){request.mode='ytd';el('notes-pick-mode').value='ytd';}
          if(keepPeriod&&el('notes-pick-period').value)request.period=el('notes-pick-period').value;
          else if(context.period){request.period=context.period;context.period=null;}
          const fresh=await api('/api/notes/snapshot',request);
          if(sequence!==pickSequence||ctx!==active)return;
          snapshot=fresh;
          el('notes-pick-period').innerHTML='<option value="">最新已有</option>'+(snapshot.periods||[]).map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');assign('notes-pick-period',request.period);
          el('notes-pick-status').textContent=`${snapshot.name} · ${ctx.code} · 只读取已保存资料，插入后固定为当时快照。${group==='business'?'主营项目使用来源原始名称。':''}`;
          renderRows();update();
        }catch(error){if(sequence===pickSequence){el('notes-pick-status').textContent=error.message;}}
      }
      ['group','mode','years','dimension','statement','kind'].forEach(k=>el('notes-pick-'+k).onchange=()=>load());el('notes-pick-period').onchange=()=>load(true);
      el('notes-pick-search').oninput=renderRows;el('notes-pick-format').onchange=update;
      el('notes-pick-confirm').onclick=()=>{
        if(ctx!==active||ctx.code!==snapshot?.code){el('notes-pick-status').textContent='股票上下文已变化，请重新选择。';return;}
        const content=fragment();if(!content)return;setPreview(false);
        if(el('notes-pick-position').value==='new')newRecord();else editor.setCursor(cursor);
        editor.replaceSelection(content);picker.close();editor.focus();
      };
      load();
    }
    el('notes-insert').onclick=()=>insertData();
    async function openQuick(context,withData){
      if(!quick.open){el('notes-quick-host').append(workspace);quick.showModal();}
      updateFloating();
      if(await openDocument(state.code,state.name)){setPreview(false);if(withData)insertData(context);}
    }
    const value=id=>el(id)?.value;
    const statementContext=()=>({period:value('fs-period'),mode:value('fs-mode'),comparison:value('fs-comparison'),statement:document.querySelector('[data-statement][aria-selected=true]')?.dataset.statement});
    const floating=document.createElement('div');floating.id='notes-floating';floating.className='notes-floating-actions';floating.setAttribute('role','group');floating.setAttribute('aria-label','投资笔记快捷操作');
    floating.innerHTML='<button type="button" id="notes-floating-write">记一笔</button><button type="button" id="notes-floating-insert">插入笔记</button>';document.body.append(floating);
    const currentView=()=>document.querySelector('.research-tabs [aria-selected="true"]')?.id.replace(/-tab$/,'');
    const shell=el('trend-panel').closest('.shell');
    function currentModule(){
      const view=currentView();let candidates=[];
      if(view==='trend')candidates=[
        {target:document.querySelector('#trend-panel .comparison-panel'),key:'financial',label:'经营趋势',context:()=>({group:'financial',mode:document.querySelector('[data-period].active')?.dataset.period})},
        {target:document.querySelector('#trend-panel .valuation-panel'),key:'valuation',label:'估值与分位',context:()=>({group:'valuation',years:value('valuation-range')})},
        {target:el('shareholders-chart')?.closest('section.panel'),key:'shareholders',label:'股东户数',context:()=>({group:'chips',kind:'holders'})},
        {target:el('financing-chart')?.closest('section.panel'),key:'financing',label:'融资',context:()=>({group:'chips',kind:'financing'})},
      ];
      else if(view==='business'||view==='statements'){
        const context=view==='business'?()=>({group:'business',dimension:value('bs-dimension')}):()=>({group:'statements',...statementContext()});
        candidates=[...el(view+'-panel').querySelectorAll(view==='business'?'.bs-panel':'.fs-panel')].map(target=>({target,key:view,label:view==='business'?'主营构成':'财务三表',context}));
        candidates.push({target:el(view+'-panel'),key:view,label:view==='business'?'主营构成':'财务三表',context,fallback:true});
      }
      const visible=candidates.filter(item=>item.target?.getClientRects().length).map(item=>({...item,rect:item.target.getBoundingClientRect()})).filter(item=>item.rect.bottom>0&&item.rect.top<innerHeight);
      const modules=visible.some(item=>!item.fallback)?visible.filter(item=>!item.fallback):visible;
      const line=Math.min(240,innerHeight*.35);
      const distance=item=>item.rect.top<=line&&item.rect.bottom>=line?0:Math.min(Math.abs(item.rect.top-line),Math.abs(item.rect.bottom-line));
      const visibleHeight=item=>Math.min(item.rect.bottom,innerHeight)-Math.max(item.rect.top,0);
      return modules.sort((a,b)=>visibleHeight(b)-visibleHeight(a)||distance(a)-distance(b))[0];
    }
    function updateFloating(){
      const enabled=['trend','business','statements'].includes(currentView());
      const hadGutter=shell.classList.contains('notes-dock-active');shell.classList.toggle('notes-dock-active',enabled);
      if(hadGutter!==enabled)requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')));
      const module=enabled?currentModule():null;floating.hidden=!module||quick.open;
      if(!module)return;
      floating.dataset.module=module.key;floating.setAttribute('aria-label',module.label+' · 投资笔记快捷操作');
      el('notes-floating-insert').title='插入'+module.label+'数据到笔记';
      if(matchMedia('(max-width:800px)').matches){floating.style.left='';floating.style.top='';return;}
      floating.style.left=Math.round(module.rect.right)+'px';
      const height=floating.offsetHeight||100;
      floating.style.top=Math.round(Math.max(12,Math.min(Math.max(20,module.rect.top+20),module.rect.bottom-height-8,innerHeight-height-12)))+'px';
    }
    let dockFrame;
    function scheduleDock(){if(!dockFrame)dockFrame=requestAnimationFrame(()=>{dockFrame=null;updateFloating();});}
    window.addEventListener('scroll',scheduleDock,{passive:true});window.addEventListener('resize',scheduleDock);
    const dockObserver=new ResizeObserver(scheduleDock);['trend','business','statements'].forEach(key=>dockObserver.observe(el(key+'-panel')));
    el('notes-floating-write').onclick=()=>openQuick({},false);
    el('notes-floating-insert').onclick=()=>{
      const module=currentModule();if(module)openQuick(module.context(),true);
    };
    quick.addEventListener('close',updateFloating);updateFloating();
    window.addEventListener('research-tab-changed',event=>{
      if(event.detail.tab==='notes'){
        if(quick.open){quick.close();el('notes-main-host').append(workspace);}
        if(!active)openDocument();else{editor.refresh();checkExternal();}
      }else flush(active);
      updateFloating();
    });
    window.investmentNotes={flush:async()=>{for(const ctx of contexts.values())if(!await flush(ctx))return false;return true;},reload:()=>{contexts.clear();active=null;return main.hidden&&!quick.open?Promise.resolve():openDocument();}};
    setupSettings();if(!main.hidden)openDocument();
  };

  function setupSettings(){
    const tabs=document.querySelector('#maintenance-panel .maintenance-tabs');if(!tabs)return;
    const button=document.createElement('button');button.type='button';button.id='maintenance-notes-tab';button.role='tab';button.setAttribute('aria-selected','false');button.setAttribute('aria-controls','maintenance-notes-panel');button.tabIndex=-1;button.textContent='投资笔记设置';tabs.append(button);
    const section=document.createElement('section');section.id='maintenance-notes-panel';section.role='tabpanel';section.setAttribute('aria-labelledby',button.id);section.hidden=true;section.className='panel notes-settings';
    section.innerHTML=`<h3>投资笔记存储位置</h3><p>一只股票一个 Markdown 文件。目录位于后台所在电脑，可选择其他笔记软件的专用子目录。</p><p id="notes-setting-current"></p><label>存储目录<input class="notes-settings-path" id="notes-setting-path" placeholder="后台电脑上的绝对目录路径"></label><div class="notes-settings-actions"><button type="button" id="notes-setting-choose">选择文件夹</button><button type="button" id="notes-setting-check">检查目录</button><button type="button" id="notes-setting-scan">重新扫描</button></div><p><label>更换目录时<select id="notes-setting-mode"><option value="copy">复制现有笔记及附件后切换（保留原文件）</option><option value="use">使用目标目录已有笔记</option></select></label></p><div class="notes-settings-actions"><button type="button" id="notes-setting-preview">预览目录切换</button><button type="button" id="notes-setting-apply" hidden>确认应用此目录</button></div><p id="notes-setting-status" role="status"></p><pre id="notes-setting-preview-result" class="notes-settings-preview"></pre><details><summary>目录文件与股票关联</summary><div class="notes-list" id="notes-setting-files"></div></details><p>SQLite 备份不包含这些文件；完整备份请同时复制笔记目录（含附件和 .history）。</p>`;
    el('maintenance-panel').append(section);
    let settings,previewToken=null,busy=false;
    const message=(text,error=false)=>{el('notes-setting-status').textContent=text;el('notes-setting-status').className=error?'notes-error':'';};
    function invalidate(){previewToken=null;el('notes-setting-apply').hidden=true;el('notes-setting-preview-result').textContent='';}
    async function load(){
      try{settings=await api('/api/notes/settings');token=settings.session_token;el('notes-setting-current').textContent='当前目录：'+settings.directory+(settings.exists?'':'（首次保存时创建）');el('notes-setting-path').value=settings.directory;invalidate();
        const list=el('notes-setting-files');list.replaceChildren();for(const row of settings.items){const item=document.createElement('p');item.textContent=`${row.file} · ${row.code||row.error||'未关联股票'}`;
          if(!row.code&&!row.error){const associate=document.createElement('button');associate.type='button';associate.textContent='关联股票';associate.onclick=async()=>{
            const code=prompt('输入此文件对应的六位股票代码');if(!code)return;
            try{if(!await window.investmentNotes.flush())throw Error('请先处理未保存笔记');const response=await fetch('/api/notes/document?'+new URLSearchParams({code}));const existing=await response.json();if(existing.exists)throw Error('该股票已经有笔记，请手工合并');
              // Association uses a hash returned by the listing, not a blind overwrite.
              await api('/api/notes/associate',{code,file:row.file,version:row.version});await load();message('已关联股票');
            }catch(error){message(error.message,true);}
          };item.append(' ',associate);}list.append(item);}
        message(settings.error||`已识别 ${settings.items.filter(r=>r.code).length} 份股票笔记`,Boolean(settings.error));
      }catch(error){message(error.message,true);}
    }
    const allTabs=()=>[...tabs.querySelectorAll('[role=tab]')];
    allTabs().forEach(tab=>tab.addEventListener('click',()=>{
      const on=tab===button;section.hidden=!on;
      if(on){for(const other of allTabs()){const selected=other===button;other.setAttribute('aria-selected',String(selected));other.tabIndex=selected?0:-1;el(other.getAttribute('aria-controls')).hidden=!selected;}el('maintenance-refresh').hidden=true;load();}
      else{button.setAttribute('aria-selected','false');button.tabIndex=-1;}
    }));
    tabs.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();event.stopPropagation();const items=allTabs(),index=items.indexOf(document.activeElement),next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowRight'?1:items.length-1))%items.length;items[next].focus();items[next].click();
    },true);
    el('notes-setting-path').oninput=invalidate;el('notes-setting-mode').onchange=invalidate;
    el('notes-setting-scan').onclick=load;
    el('notes-setting-choose').onclick=async()=>{if(busy)return;busy=true;message('请在后台电脑的窗口中选择目录；也可取消后直接输入路径。');try{const data=await api('/api/notes/settings/choose',{});if(data.directory){el('notes-setting-path').value=data.directory;invalidate();}message(data.directory?'已选择目录，请检查或预览切换。':'已取消选择');}catch(error){message(error.message,true);}finally{busy=false;}};
    el('notes-setting-check').onclick=async()=>{try{const data=await api('/api/notes/settings/check',{directory:el('notes-setting-path').value});message('目录可读写：'+data.directory);}catch(error){message(error.message,true);}};
    el('notes-setting-preview').onclick=async()=>{invalidate();if(busy)return;busy=true;
      try{if(!await window.investmentNotes.flush())throw Error('请先处理未保存或冲突的笔记');const data=await api('/api/notes/settings/preview',{directory:el('notes-setting-path').value,mode:el('notes-setting-mode').value});previewToken=data.token;el('notes-setting-preview-result').textContent=`目标：${data.directory}\n涉及文件：${data.files}，待复制字节：${data.bytes_to_copy}\n目标已有笔记：${data.target_notes.length}\n${data.conflicts.length?'冲突：'+data.conflicts.join('；'):'校验通过，原目录将保留。'}`;el('notes-setting-apply').hidden=Boolean(data.conflicts.length);message('请核对预览，再应用目录。');}catch(error){message(error.message,true);}finally{busy=false;}
    };
    el('notes-setting-apply').onclick=async()=>{if(!previewToken||busy)return;busy=true;
      try{if(!await window.investmentNotes.flush())throw Error('请先处理未保存笔记');await api('/api/notes/settings/apply',{token:previewToken});await window.investmentNotes.reload();await load();message('存储目录已切换，原目录保留。');}catch(error){message(error.message,true);invalidate();}finally{busy=false;}
    };
  }
})();
