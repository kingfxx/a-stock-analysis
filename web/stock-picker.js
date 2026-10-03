// Group selection and management use the local server; searches never request sources.
window.initStockPicker = function (state) {
  const get = id => document.getElementById(id);
  const picker = get('cached-picker'), search = get('cached-search'), list = get('cached-list');
  let library = state.stock_library || {}, managing = false, busy = false, visited = false, revision = 0;
  let selected = library.selected_group || 'recent', target = '', checked = new Set(), drag = null, dragClickUntil = 0;
  const fixed = [{key:'recent',name:'最近查看'}, {key:'all',name:'全部股票'}, {key:'ungrouped',name:'未分组'}];
  const groups = () => library.groups || [];
  const entries = () => [...fixed, ...groups().map(group => ({key:'group:' + group.id,name:group.name}))];
  const stocks = () => library.stocks || state.cached_stocks || [];
  const memberGroups = code => groups().filter(group => group.codes.includes(code));
  function groupStocks(key) {
    if (key === 'all') return stocks();
    if (key === 'ungrouped') return stocks().filter(stock => !memberGroups(stock.code).length);
    if (key === 'recent') return (library.recent || []).map(code => stocks().find(stock => stock.code === code)).filter(Boolean);
    const group = groups().find(group => key === 'group:' + group.id);
    return (group?.codes || []).map(code => stocks().find(stock => stock.code === code)).filter(Boolean);
  }
  function results() {
    const query = search.value.trim().toLowerCase();
    return query ? stocks().filter(stock => (stock.search || stock.code + ' ' + (stock.name || '')).toLowerCase().includes(query)) : groupStocks(selected);
  }
  function message(text) { get('stock-picker-message').textContent = text; }
  function positionMenu() {
    if (!picker.open) return;
    const menu = picker.querySelector('.cached-menu');
    menu.style.transform = '';
    const bounds = menu.getBoundingClientRect();
    const offset = bounds.left < 12 ? 12 - bounds.left : bounds.right > innerWidth - 12 ? innerWidth - 12 - bounds.right : 0;
    menu.style.transform = `translateX(${offset}px)`;
    menu.style.maxHeight = Math.max(180, innerHeight - bounds.top - 12) + 'px';
  }
  async function change(command) {
    if (busy) return false;
    busy = true; revision++; message(''); render();
    try {
      const response = await fetch('/api/stock-groups', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(command)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '保存分组失败');
      library = data; selected = data.selected_group;
      return true;
    } catch (error) { message(error.message); return false; }
    finally { busy = false; render(); }
  }
  async function choose(code) {
    if (busy) return;
    await change({action:'visit',code});
    window.location.href = '/?code=' + encodeURIComponent(code);
  }
  function button(text, action, disabled = false) {
    const node = document.createElement('button'); node.type = 'button'; node.textContent = text;
    node.disabled = busy || disabled; node.addEventListener('click', action); return node;
  }
  function stopDrag() {
    const current = drag;
    if (!current) return null;
    drag = null; cancelAnimationFrame(current.frame);
    current.preview?.remove(); current.row.classList.remove('stock-dragging');
    list.querySelectorAll('.stock-drop-before,.stock-drop-after').forEach(row => row.classList.remove('stock-drop-before','stock-drop-after'));
    if (current.handle.hasPointerCapture(current.pointerId)) current.handle.releasePointerCapture(current.pointerId);
    return current;
  }
  function updateDrag() {
    if (!drag?.active) return;
    const bounds = list.getBoundingClientRect(), menu = picker.querySelector('.cached-menu').getBoundingClientRect();
    const top = Math.max(bounds.top,menu.top), bottom = Math.min(bounds.bottom,menu.bottom,innerHeight);
    const insideX = drag.x >= bounds.left && drag.x <= bounds.right;
    if (insideX && drag.y >= top-24 && drag.y <= bottom+24) {
      const step = drag.y < top+32 ? -12 : drag.y > bottom-32 ? 12 : 0;
      list.scrollTop += step;
    }
    drag.valid = insideX && drag.y >= top && drag.y <= bottom;
    const rows = [...list.querySelectorAll('.stock-member-row')].filter(row => row !== drag.row);
    const before = rows.find(row => {const rect=row.getBoundingClientRect();return drag.y < rect.top+rect.height/2;});
    const codes = drag.original.filter(code => code !== drag.code);
    codes.splice(before ? codes.indexOf(before.dataset.stockCode) : codes.length,0,drag.code); drag.codes = codes;
    list.querySelectorAll('.stock-drop-before,.stock-drop-after').forEach(row => row.classList.remove('stock-drop-before','stock-drop-after'));
    if (drag.valid) (before || rows.at(-1))?.classList.add(before ? 'stock-drop-before' : 'stock-drop-after');
    drag.preview.style.left = Math.max(12,Math.min(drag.x+12,innerWidth-drag.preview.offsetWidth-12)) + 'px';
    drag.preview.style.top = Math.max(12,Math.min(drag.y+12,innerHeight-drag.preview.offsetHeight-12)) + 'px';
    drag.frame = requestAnimationFrame(updateDrag);
  }
  list.addEventListener('pointerdown', event => {
    const handle = event.target.closest('.stock-drag-handle');
    if (!handle || handle.disabled || busy || drag || event.button !== 0 || !event.isPrimary) return;
    const group = groups().find(group => selected === 'group:' + group.id);
    if (!group || search.value.trim()) return;
    event.preventDefault(); handle.focus({preventScroll:true}); handle.setPointerCapture(event.pointerId);
    drag = {handle,row:handle.closest('.stock-member-row'),code:handle.dataset.stockCode,groupId:group.id,
      original:[...group.codes],codes:[...group.codes],pointerId:event.pointerId,startY:event.clientY,
      x:event.clientX,y:event.clientY,active:false,valid:false};
  });
  document.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag.x=event.clientX;drag.y=event.clientY;
    if (!drag.active && Math.abs(drag.y-drag.startY) >= 5) {
      drag.active=true;drag.row.classList.add('stock-dragging');
      drag.preview=document.createElement('div');drag.preview.className='stock-drag-preview';
      drag.preview.textContent=drag.row.querySelector('.stock-name').firstChild.textContent;
      document.body.appendChild(drag.preview);updateDrag();
    }
    if (drag.active) event.preventDefault();
  }, {passive:false});
  document.addEventListener('pointerup', async event => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag.x=event.clientX;drag.y=event.clientY;
    if (drag.active) {cancelAnimationFrame(drag.frame);updateDrag();}
    const current = stopDrag();
    if (current.active) dragClickUntil = performance.now()+100;
    if (!current.active || !current.valid || current.codes.join(',') === current.original.join(',')) return;
    if (await change({action:'reorder_members',id:current.groupId,codes:current.codes})) {
      list.querySelector(`.stock-drag-handle[data-stock-code="${current.code}"]`)?.focus({preventScroll:true});
    }
  });
  document.addEventListener('pointercancel', event => {if(drag?.pointerId===event.pointerId)stopDrag();});
  document.addEventListener('click', event => {
    if (event.detail && performance.now()<dragClickUntil) {event.preventDefault();event.stopPropagation();dragClickUntil=0;}
  }, true);
  window.addEventListener('blur', stopDrag);
  function renderList() {
    stopDrag();
    const scrollTop = list.scrollTop;
    list.replaceChildren();
    const matched = results();
    const orderedGroup = managing && !search.value.trim() && groups().find(group => selected === 'group:' + group.id);
    get('stock-results-label').textContent = (search.value.trim() ? '搜索全部股票' : entries().find(entry => entry.key === selected)?.name || '全部股票') + ' · ' + matched.length + ' 只' + (orderedGroup ? ' · 拖动 ⠿ 调整顺序' : '');
    for (const stock of matched) {
      const node = managing ? document.createElement('label') : button('', () => choose(stock.code));
      if (managing) {
        node.className = 'stock-check-row';
        const input = document.createElement('input'); input.type = 'checkbox'; input.checked = checked.has(stock.code); input.disabled = busy;
        input.addEventListener('change', () => { if(input.checked)checked.add(stock.code);else checked.delete(stock.code);updateCount(); });
        node.appendChild(input);
      }
      const text = document.createElement('span'); text.className = 'stock-name'; text.textContent = (stock.name || '名称暂缺') + ' · ' + stock.code;
      const tags = document.createElement('span'); tags.className = 'stock-tags';
      tags.textContent = (stock.code === state.code ? '当前查看 · ' : '') + (memberGroups(stock.code).map(group => group.name).join(' / ') || '未分组');
      text.appendChild(tags); node.appendChild(text);
      if (orderedGroup) {
        const row = document.createElement('div'); row.className = 'stock-member-row'; row.dataset.stockCode=stock.code;
        const handle = button('⠿', () => {}, orderedGroup.codes.length<2);
        handle.className='stock-drag-handle';handle.dataset.stockCode=stock.code;
        handle.title='拖动调整顺序';handle.setAttribute('aria-label','拖动'+(stock.name || stock.code)+'调整顺序');
        row.appendChild(handle); row.appendChild(node);
        const index = orderedGroup.codes.indexOf(stock.code);
        for (const [label,offset] of [['↑',-1],['↓',1]]) {
          const move = button(label, async () => {
            const codes = [...orderedGroup.codes];
            [codes[index],codes[index+offset]] = [codes[index+offset],codes[index]];
            if (await change({action:'reorder_members',id:orderedGroup.id,codes})) {
              const moved = list.querySelector(`[data-stock-code="${stock.code}"][data-offset="${offset}"]`);
              (moved?.disabled ? moved.closest('.stock-member-row').querySelector('input') : moved)?.focus();
            }
          }, index+offset<0 || index+offset>=orderedGroup.codes.length);
          move.className = 'stock-member-move'; move.dataset.stockCode = stock.code; move.dataset.offset = String(offset);
          move.setAttribute('aria-label', (stock.name || stock.code) + (offset<0?'上移':'下移'));
          row.appendChild(move);
        }
        list.appendChild(row);
      } else list.appendChild(node);
    }
    if (!matched.length) { const text = document.createElement('p'); text.textContent = search.value.trim() ? '没有匹配的股票，可在股票代码栏查询新股票。' : selected === 'recent' ? '尚无最近查看记录，可切换“全部股票”。' : '本组暂无股票，可通过“管理分组”添加。';list.appendChild(text); }
    list.scrollTop = scrollTop; updateCount();
  }
  function updateCount() { get('stock-selected-count').textContent = checked.size + ' 只已选'; }
  function render() {
    if (!entries().some(entry => entry.key === selected)) selected = 'all';
    get('stock-picker-summary').textContent = '我的股票 · ' + entries().find(entry => entry.key === selected).name;
    get('stock-group-edit').hidden = !managing; get('stock-group-target').hidden = !managing; get('stock-batch').hidden = !managing;
    get('stock-manage').textContent = managing ? '完成' : '管理分组';
    get('stock-manage').disabled = busy;
    const nav = get('stock-groups'); nav.replaceChildren();
    for (const entry of entries()) {
      const row = document.createElement('div'); row.className = 'stock-group-row';
      const selectButton = button(entry.name + ' ' + groupStocks(entry.key).length, async () => {
        search.value = ''; const old = selected; selected = entry.key;
        if(entry.key.startsWith('group:'))target=entry.key.slice(6);
        if(!await change({action:'select',group:entry.key})){selected=old;render();}
      });
      selectButton.setAttribute('aria-pressed', String(entry.key === selected)); row.appendChild(selectButton);
      if (managing && entry.key.startsWith('group:')) {
        const index = groups().findIndex(group => entry.key === 'group:' + group.id);
        for (const [label,offset] of [['↑',-1],['↓',1]]) {
          const move = button(label, () => {const ids=groups().map(group=>group.id);[ids[index],ids[index+offset]]=[ids[index+offset],ids[index]];change({action:'reorder',ids});}, index+offset<0 || index+offset>=groups().length);
          move.className='group-move';move.setAttribute('aria-label',entry.name+(offset<0?'上移':'下移'));row.appendChild(move);
        }
      }
      nav.appendChild(row);
    }
    if (!groups().some(group => String(group.id) === target)) target = String(groups()[0]?.id || '');
    const select = get('stock-target-group'); select.replaceChildren();
    for (const group of groups()) {const option=document.createElement('option');option.value=String(group.id);option.textContent=group.name;select.appendChild(option);}
    select.value=target;select.disabled=busy || !groups().length;
    for (const id of ['stock-group-create','stock-select-results']) get(id).disabled=busy;
    for (const id of ['stock-group-rename','stock-group-delete','stock-group-add','stock-group-remove']) get(id).disabled=busy || !target;
    renderList(); positionMenu();
  }
  get('stock-manage').addEventListener('click', () => {managing=!managing;checked.clear();message('');render();});
  get('stock-target-group').addEventListener('change', event => {target=event.target.value;});
  get('stock-group-create').addEventListener('click', async () => {
    if(await change({action:'create',name:get('stock-group-name').value})){target=String(groups().at(-1).id);get('stock-group-name').value='';render();}
  });
  get('stock-group-rename').addEventListener('click', async () => {if(await change({action:'rename',id:Number(target),name:get('stock-group-name').value}))get('stock-group-name').value='';});
  get('stock-group-delete').addEventListener('click', () => {
    const name=groups().find(group=>String(group.id)===target)?.name;
    if(confirm(`删除“${name}”？\n\n仅解除该组的归属，股票与已有数据仍保留。`))change({action:'delete',id:Number(target)});
  });
  for (const [id,add] of [['stock-group-add',true],['stock-group-remove',false]]) get(id).addEventListener('click', async () => {
    if(!checked.size){message('请先选择股票');return;}
    if(await change({action:'membership',id:Number(target),codes:[...checked],add})){checked.clear();render();}
  });
  get('stock-select-results').addEventListener('click', () => {results().forEach(stock=>checked.add(stock.code));renderList();});
  search.addEventListener('input', renderList);
  search.addEventListener('keydown', event => {
    if(event.key==='Enter' && !managing && results()[0]){event.preventDefault();choose(results()[0].code);}
    if(event.key==='ArrowDown'){event.preventDefault();list.querySelector('button,input')?.focus();}
  });
  list.addEventListener('keydown', event => {
    if(!['ArrowDown','ArrowUp'].includes(event.key))return;
    const nodes=[...list.querySelectorAll('button,input')],index=nodes.indexOf(document.activeElement);
    event.preventDefault();nodes[index+(event.key==='ArrowDown'?1:-1)]?.focus();
  });
  picker.addEventListener('toggle', () => {if(picker.open){search.value='';renderList();positionMenu();search.focus();}});
  window.addEventListener('resize', positionMenu);
  window.addEventListener('scroll', positionMenu, {passive:true});
  document.addEventListener('click', event => {if(picker.open && !picker.contains(event.target) && !busy)picker.open=false;});
  document.addEventListener('keydown', event => {if(event.key==='Escape' && drag){event.preventDefault();stopDrag();return;}if(event.key==='Escape' && picker.open){picker.open=false;get('stock-picker-summary').focus();}});
  render();
  return {setStocks(updated) {
    const old=JSON.stringify(stocks().map(stock=>[stock.code,stock.name]));
    if(old!==JSON.stringify(updated.map(stock=>[stock.code,stock.name]))) {
      library.stocks=updated;render();
      const version = revision;
      fetch('/api/stock-groups',{cache:'no-store'}).then(response=>{if(!response.ok)throw new Error('读取分组失败');return response.json();}).then(data=>{if(!busy && revision===version){library=data;selected=data.selected_group;render();}}).catch(error=>{if(revision===version)message(error.message);});
    }
    if(!visited && updated.some(stock=>stock.code===state.code) && !busy){visited=true;change({action:'visit',code:state.code}).then(ok=>{if(!ok)visited=false;});}
  }};
};
