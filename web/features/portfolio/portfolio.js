(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const labels = { stock: 'A 股', etf: 'ETF', hong_kong: '港股', unknown: '待确认' };
  const moneyFormatter = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const decimalFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 });
  const rateFormatter = new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero'
  });
  const display = (value, fmt = decimalFormatter) => value == null ? '—' : fmt.format(Number(value));
  const money = (value) => display(value, moneyFormatter);
  const sessionLabel = (session) => session === 'close' ? '收盘' : '盘中';
  let token;
  let importPreview;
  let snapshotRequest = 0;
  const activity = {
    history: { offset: 0, request: 0, loaded: false },
    transfers: { offset: 0, request: 0, loaded: false }
  };
  let snapshotScope = 'account';
  let positionRows = [];
  let positionSort = { key: 'market_value', direction: -1 };

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  async function request(url, body) {
    const response = await fetch(url, body ? {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Local-Session': token },
      body: JSON.stringify(body)
    } : {});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败，请重试');
    return data;
  }

  function notice(text) {
    $('notice').textContent = text;
    $('notice').hidden = !text;
  }

  function setPnl(element, value) {
    element.textContent = value > 0 ? `+${money(value)}` : money(value);
    element.classList.toggle('gain', value > 0);
    element.classList.toggle('loss', value < 0);
  }

  function setRate(element, rate) {
    element.textContent = rate == null ? '' : `${display(rate, rateFormatter)}%`;
    element.classList.toggle('gain', rate > 0);
    element.classList.toggle('loss', rate < 0);
  }

  function paired(main, secondary) {
    const cell = node('td', main);
    if (secondary != null) cell.append(node('small', secondary, 'subline'));
    return cell;
  }

  function pnlPair(amount, rate, estimated = false) {
    const cell = node('td');
    const main = node('span');
    setPnl(main, amount);
    cell.append(main);
    if (rate != null) {
      const line = node('small', null, 'subline');
      setRate(line, rate);
      cell.append(line);
    }
    if (estimated) {
      cell.append(node('small', '按收盘价估算', 'subline'));
      cell.title = '相邻交易日未复权收盘价差 × 持仓数量';
    }
    return cell;
  }

  function renderFunds(summary, reconciliation, container) {
    const fields = [
      ['券商余额', summary.broker_balance], ['券商可用', summary.broker_available],
      ['券商可取', summary.broker_withdrawable], ['券商参考市值', summary.broker_market_value],
      ['券商资产', summary.broker_assets], ['明细市值与汇总差额', reconciliation.market_difference],
      ['资产减市值和可用', reconciliation.assets_less_market_and_available],
      ['资产减市值和余额', reconciliation.assets_less_market_and_balance]
    ];
    container.replaceChildren(...fields.map(([label, value]) => {
      const item = node('div');
      item.append(node('dt', label), node('dd', money(value)));
      return item;
    }));
  }

  function renderPosition(position) {
    const row = node('tr');
    const name = node('td');
    if (position.asset_type === 'stock' && ['sh', 'sz'].includes(position.exchange)) {
      const link = node('a', position.name);
      link.href = `/?${new URLSearchParams({ code: position.code, tab: 'notes' })}`;
      link.title = '打开个股投资笔记';
      name.append(link);
    } else {
      name.append(node('span', position.name));
    }
    name.append(node('small', [position.code, labels[position.asset_type], position.reference?.industry?.sector]
      .filter(Boolean).join(' · '), 'subline'));
    if (position.accounts?.length) {
      const accounts = node('details', null, 'subline account-breakdown');
      accounts.append(node('summary', position.accounts.length > 1 ? '查看分户持仓' : position.accounts[0].broker));
      for (const part of position.accounts) {
        const cost = part.cost_price == null ? '成本待核对' : `成本 ${display(part.cost_price)}`;
        const daily = part.daily_pnl == null ? '今日盈亏待核对'
          : `今日 ${part.daily_pnl > 0 ? '+' : ''}${money(part.daily_pnl)}${part.daily_pnl_basis ? '（估算）' : ''}`;
        accounts.append(node('div', `${part.broker}　${display(part.quantity)} 股／份　${cost}　市值 ${money(part.market_value)}　持仓盈亏 ${money(part.holding_pnl)}　${daily}`));
      }
      name.append(accounts);
    }
    const weight = node('td', position.weight == null ? '—' : `${position.weight}%`);
    if (position.weight != null && Number(position.weight) >= 0 && Number(position.weight) <= 100) {
      const meter = node('meter');
      meter.max = 100;
      meter.value = position.weight;
      meter.setAttribute('aria-label', `${position.name}仓位`);
      weight.append(meter);
    }
    const reference = node('td');
    if (position.reference) {
      const names = { pe: 'PE（TTM）', pb: 'PB', dividend_yield: '股息率' };
      for (const [key, metric] of Object.entries(position.reference.valuation)) {
        if (metric.value == null) continue;
        const suffix = key === 'dividend_yield' ? '%' : ' 倍';
        const percentile = metric.percentile == null ? '' : ` · 近5年分位 ${display(metric.percentile)}%`;
        const line = node('span', `${names[key]} ${display(metric.value)}${suffix}${percentile}`, 'subline');
        line.append(node('time', ` · ${metric.date}`));
        reference.append(line);
      }
      if (position.reference.close_date) {
        reference.append(node('span', `未复权收盘 ${display(position.reference.close)} · ${position.reference.close_date}`, 'subline'));
      }
    }
    if (!reference.childElementCount) {
      reference.textContent = position.asset_type === 'etf' ? '券商快照 · 暂无本地行情' : '暂无本地估值';
    }
    row.append(name, node('td', money(position.market_value), 'market-column'), node('td', display(position.quantity)),
      paired(display(position.price), display(position.cost_price)),
      pnlPair(position.holding_pnl, position.holding_pnl_rate),
      pnlPair(position.daily_pnl, position.daily_pnl_rate, position.daily_pnl_basis === 'local_close_estimate'),
      weight, reference);
    return row;
  }

  function renderSortedPositions() {
    const { key, direction } = positionSort;
    const sorted = [...positionRows].sort((a, b) => {
      if (a[key] == null && b[key] != null) return 1;
      if (b[key] == null && a[key] != null) return -1;
      return direction * (Number(a[key]) - Number(b[key])) || a.code.localeCompare(b.code);
    });
    $('positions-body').replaceChildren(...sorted.map(renderPosition));
    for (const button of document.querySelectorAll('[data-sort]')) {
      const selected = button.dataset.sort === key;
      if (selected) button.parentElement.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending');
      else button.parentElement.removeAttribute('aria-sort');
      button.querySelector('span').textContent = selected ? (direction === 1 ? '↑' : '↓') : '↕';
    }
  }

  async function loadSnapshot(options = {}) {
    const current = ++snapshotRequest;
    const query = `?${new URLSearchParams(options)}`;
    const data = await request('/api/portfolio/snapshots' + query);
    if (current !== snapshotRequest) return;
    token = data.session_token;
    snapshotScope = data.scope;
    const snapshot = data.snapshot;
    const combined = data.scope === 'combined';
    const accounts = Object.entries(data.accounts);
    $('account-control').hidden = accounts.length < 2;
    $('account-select').replaceChildren(new Option('合并持仓', '', false, combined), ...accounts.map(([id, broker]) =>
      new Option(broker, id, false, !combined && String(snapshot?.account_id) === id)));
    $('snapshot-control').hidden = !snapshot;
    $('holdings-data').hidden = !snapshot;
    $('holdings-empty').hidden = Boolean(snapshot);
    if (!snapshot) {
      $('holdings-empty').textContent = combined ? '暂无可合并的收盘快照，请切换分户查看。' : '导入招商证券持仓表后查看仓位与估值。';
      return;
    }
    $('coverage').textContent = combined ? accounts.map(([, broker]) => broker).join(' · ') : `当前查看 ${snapshot.broker}`;
    $('current-portfolio-name').textContent = snapshot.broker;
    $('current-portfolio-account').textContent = snapshot.snapshot_date;
    const missing = data.missing_accounts?.length ? ` · ${data.missing_accounts.join('、')}缺同日快照` : '';
    $('snapshot-context').textContent = `${snapshot.snapshot_date} · ${sessionLabel(snapshot.session)} · A 股及境内 ETF${missing}`;
    $('snapshot-select').replaceChildren(...(combined
      ? data.days.map((day) => new Option(`${day} 收盘`, day, false, day === snapshot.snapshot_date))
      : data.directory.filter((entry) => entry.account_id === snapshot.account_id).map((entry) => new Option(
        `${entry.snapshot_date} ${sessionLabel(entry.session)}${entry.supersedes_id ? ` · 修订 #${entry.id}` : ''}`,
        entry.id, false, entry.id === snapshot.id
      ))));
    $('market-value').textContent = money(data.totals.market_value);
    $('cash-value').textContent = data.totals.cash == null ? '待核对' : money(data.totals.cash);
    $('asset-value').textContent = money(data.totals.assets ?? snapshot.broker_assets);
    $('asset-source').textContent = data.totals.assets == null && snapshot.broker_assets != null ? '券商原表资产' : '';
    const cashWeight = data.totals.cash_weight;
    $('cash-weight').hidden = $('market-weight').hidden = cashWeight == null;
    $('cash-weight').textContent = cashWeight == null ? '' : `${cashWeight}%`;
    $('market-weight').textContent = cashWeight == null ? '' : `${(100 - Number(cashWeight)).toFixed(2)}%`;
    setPnl($('holding-pnl'), data.totals.holding_pnl);
    setPnl($('daily-pnl'), data.totals.daily_pnl);
    const dailyRate = $('daily-pnl-rate');
    setRate(dailyRate, data.totals.daily_pnl_rate);
    if (data.totals.daily_rate_basis === 'estimated_opening_assets') {
      dailyRate.append(node('span', ' · 估算', 'muted'));
      dailyRate.title = '今日盈亏 ÷（收盘总资产 − 今日盈亏），未调整当日出入金';
    } else {
      dailyRate.removeAttribute('title');
    }
    document.querySelector('[data-sort="weight"]').title = data.totals.weight_basis === 'assets'
      ? '占纳入总资产比例' : data.totals.weight_basis === 'securities' ? '占纳入证券市值比例' : '';
    positionRows = data.positions.filter((row) => ['stock', 'etf'].includes(row.asset_type));
    renderSortedPositions();
    const excluded = data.positions.filter((value) => !['stock', 'etf'].includes(value.asset_type));
    $('excluded').hidden = excluded.length === 0;
    $('excluded-title').textContent = `排除明细（${excluded.length} 条）`;
    $('excluded-list').replaceChildren(...excluded.map((row) => node('li',
      `${row.code} ${row.name} · ${labels[row.asset_type]}`)));
    const funds = combined ? data.funds : [{ snapshot, reconciliation: data.reconciliation }];
    $('funds-details').replaceChildren(...funds.map((entry) => {
      const panel = node('section');
      const fields = node('dl');
      renderFunds(entry.snapshot, entry.reconciliation, fields);
      panel.append(node('h3', entry.snapshot.broker), node('p', entry.snapshot.confirmed_cash == null
        ? '期初现金待核对' : `已确认现金 ${money(entry.snapshot.confirmed_cash)} 元`), fields);
      return panel;
    }));
  }

  function historyRow(entry, preview = false) {
    const row = node('tr');
    const date = [entry.event_date, entry.event_time].filter(Boolean).join(' ');
    const label = [entry.source_label, entry.asset_type === 'hong_kong' ? '港股' : '',
      preview && entry.overlap ? '疑似重复' : ''].filter(Boolean).join(' · ');
    for (const value of [date, label, `${entry.code} ${entry.name}`.trim(),
      display(entry.quantity), display(entry.price), money(entry.gross_amount)]) row.append(node('td', value));
    return row;
  }

  function transferRow(entry, preview = false) {
    const row = node('tr');
    const date = [entry.event_date, entry.event_time].filter(Boolean).join(' ');
    const label = entry.source_label + (preview && entry.overlap ? ' · 疑似重复' : '');
    const cash = entry.net_cash > 0 ? `+${money(entry.net_cash)}` : money(entry.net_cash);
    row.append(...[date, label, money(entry.gross_amount), cash].map((value) => node('td', value)));
    return row;
  }

  async function loadActivity(kind, offset = 0) {
    const state = activity[kind];
    const current = ++state.request;
    const params = new URLSearchParams({ offset });
    for (const [name, value] of new FormData($(`${kind}-filter`))) if (value) params.set(name, value);
    const data = await request(`/api/portfolio/${kind}?${params}`);
    if (current !== state.request) return;
    state.loaded = true;
    state.offset = data.offset;
    const transfers = kind === 'transfers';
    $(`${kind}-count`).textContent = `招商证券 · ${data.total_count} 条` +
      (transfers ? ' · 含作废记录' : ' · 原表金额，费用及净交收待核对');
    const renderRow = transfers ? transferRow : historyRow;
    $(`${kind}-body`).replaceChildren(...data.entries.map((entry) => renderRow(entry)));
    $(`${kind}-empty`).hidden = Boolean(data.entries.length);
    $(`${kind}-pages`).hidden = data.total_count <= 100;
    $(`${kind}-prev`).disabled = state.offset === 0;
    $(`${kind}-next`).disabled = state.offset + 100 >= data.total_count;
  }

  const tabs = Array.from(document.querySelectorAll('.portfolio-nav [role="tab"]'));

  function selectTab(id, { updateUrl = false, focus = false } = {}) {
    for (const tab of tabs) {
      const active = tab.getAttribute('aria-controls') === id;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = !active;
      if (active && focus) tab.focus();
    }
    if (updateUrl && location.hash !== `#${id}`) window.history.pushState(null, '', `#${id}`);
    if (Object.hasOwn(activity, id) && !activity[id].loaded) {
      loadActivity(id).catch((error) => notice(error.message));
    }
    if (id === 'performance') document.dispatchEvent(new Event('portfolio:performance'));
  }

  function selectTabFromLocation() {
    const id = location.hash.slice(1);
    selectTab(id === 'performance' || Object.hasOwn(activity, id) ? id : 'holdings');
  }

  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener('click', () => selectTab(tab.getAttribute('aria-controls'), { updateUrl: true }));
    tab.addEventListener('keydown', (event) => {
      const keys = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length,
        Home: 0, End: tabs.length - 1 };
      if (!Object.hasOwn(keys, event.key)) return;
      event.preventDefault();
      selectTab(tabs[keys[event.key]].getAttribute('aria-controls'), { updateUrl: true, focus: true });
    });
  }
  window.addEventListener('hashchange', selectTabFromLocation);

  function previewTable(headers, rows) {
    const wrapper = node('div', null, 'table-scroll');
    const table = node('table');
    const head = table.createTHead().insertRow();
    head.append(...headers.map((label) => node('th', label)));
    table.createTBody().append(...rows);
    wrapper.append(table);
    return wrapper;
  }

  function renderPreview(data) {
    if (data.kind === 'snapshot') {
      const count = data.positions.filter((position) => ['stock', 'etf'].includes(position.asset_type)).length;
      const caption = node('p', `读取 ${data.positions.length} 条持仓，纳入 ${count} 条。`, 'muted');
      const funds = node('dl');
      renderFunds(data.summary, data.reconciliation, funds);
      const positions = data.positions.map((position) => {
        const row = node('tr');
        row.append(...[`${position.code} ${position.name}`, labels[position.asset_type], display(position.quantity),
          money(position.market_value)].map((value) => node('td', value)));
        return row;
      });
      const reconciliation = node('div', null, 'reconciliation');
      reconciliation.append(funds);
      $('preview-content').replaceChildren(caption, reconciliation,
        previewTable(['证券', '归类', '数量', '券商市值'], positions));
    } else {
      const caption = node('p', `${data.start} 至 ${data.end} · 共 ${data.entry_count} 条` +
        (data.overlap_count ? ` · ${data.overlap_count} 条疑似重复` : '') +
        (data.ignored_count ? ` · 跳过 ${data.ignored_count} 条银行余额查询` : '') +
        (data.entry_count > 100 ? ' · 预览前 100 条' : ''), 'muted');
      const transfers = data.kind === 'transfers';
      const headers = transfers ? ['日期', '方向与状态', '原表金额', '资金变动'] :
        ['日期', '业务', '证券', '数量', '成交价', '原表金额'];
      const renderRow = transfers ? transferRow : historyRow;
      $('preview-content').replaceChildren(caption, previewTable(headers, data.entries.map((entry) => renderRow(entry, true))));
    }
  }

  function importMetadata() {
    return {
      day: $('import-date').value || null,
      session: $('import-session').value || null,
      cash: $('import-cash').value.trim() || null
    };
  }

  function enableConfirm() {
    const data = importPreview?.data;
    const metadata = importMetadata();
    $('confirm-import').disabled = !data || data.already_imported ||
      (data.kind === 'snapshot' && (!metadata.day || !metadata.session ||
        (data.requires_revision && !$('revision-check').checked))) ||
      Boolean(data.overlap_count && !$('overlap-choice').value);
  }

  function encodeFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.onerror = () => reject(new Error('读取文件失败，请重新选择'));
      reader.readAsDataURL(file);
    });
  }

  async function previewFile() {
    const file = $('import-file').files[0];
    if (!file) throw new Error('请先选择招商证券 XLS 文件');
    if (file.size > 4 * 1024 * 1024) throw new Error('文件超过 4 MiB，请拆分后再导入');
    if (!token) throw new Error('请刷新页面后重试');
    importPreview = null;
    enableConfirm();
    const metadata = importMetadata();
    const body = { name: file.name, file: await encodeFile(file), ...metadata };
    const data = await request('/api/portfolio/import/preview', body);
    if (file !== $('import-file').files[0]) return;
    importPreview = { data, body, metadata };
    $('snapshot-fields').hidden = data.kind !== 'snapshot';
    $('revision-field').hidden = !data.requires_revision;
    $('revision-check').checked = false;
    $('overlap-field').hidden = !data.overlap_count;
    $('overlap-choice').value = '';
    $('import-message').textContent = data.already_imported ? '这份记录已导入。' :
      data.kind === 'snapshot' ? '请按实际导出时间确认快照日期和时点。' : '核对明细后确认导入。';
    renderPreview(data);
    enableConfirm();
  }

  async function confirmFile() {
    const { data, metadata } = importPreview;
    const revisedMetadata = importMetadata();
    if (data.kind === 'snapshot' && JSON.stringify(metadata) !== JSON.stringify(revisedMetadata)) {
      await previewFile();
      if (importPreview?.data.requires_revision || importPreview?.data.already_imported || !importPreview) return;
    }
    const current = importPreview;
    const result = await request('/api/portfolio/import/confirm', {
      ...current.body, preview: current.data.preview,
      revise: $('revision-check').checked, overlap: $('overlap-choice').value || null
    });
    importPreview = null;
    $('import-file').value = '';
    $('import-file').dispatchEvent(new Event('change'));
    $('import-dialog').close();
    notice(result.already_imported ? '这份记录已导入。' :
      `已保存 ${result.saved_count} 条${{ snapshot: '持仓', history: '历史成交', transfers: '银证转账' }[result.kind]}。 ` +
      (result.duplicate_count ? `其中 ${result.duplicate_count} 条已标记重复。` : ''));
    if (Object.hasOwn(activity, result.kind)) {
      activity[result.kind].loaded = false;
      selectTab(result.kind, { updateUrl: true, focus: true });
    } else {
      selectTab('holdings', { updateUrl: true, focus: true });
      await loadSnapshot(result.snapshot_id ? { snapshot: result.snapshot_id } : {});
    }
  }

  async function runImport(action) {
    $('preview-import').disabled = true;
    $('confirm-import').disabled = true;
    $('import-file').disabled = true;
    try {
      await action();
    } catch (error) {
      if ($('import-dialog').open) $('import-message').textContent = error.message;
      else notice(error.message);
    } finally {
      $('preview-import').disabled = false;
      $('import-file').disabled = false;
      enableConfirm();
    }
  }

  $('open-import').addEventListener('click', () => $('import-dialog').showModal());
  $('import-file').addEventListener('change', () => {
    importPreview = null;
    $('preview-content').replaceChildren();
    $('snapshot-fields').hidden = $('revision-field').hidden = $('overlap-field').hidden = true;
    $('import-message').textContent = '';
    enableConfirm();
  });
  for (const id of ['import-date', 'import-session', 'import-cash', 'revision-check', 'overlap-choice']) {
    $(id).addEventListener('input', enableConfirm);
  }
  $('preview-import').addEventListener('click', () => runImport(previewFile));
  $('confirm-import').addEventListener('click', () => runImport(confirmFile));
  $('account-select').addEventListener('change', (event) => {
    const account = event.target.value;
    loadSnapshot(account ? { account } : {}).catch((error) => notice(error.message));
  });
  for (const button of document.querySelectorAll('[data-sort]')) {
    button.addEventListener('click', () => {
      const key = button.dataset.sort;
      positionSort = { key, direction: positionSort.key === key ? -positionSort.direction : -1 };
      renderSortedPositions();
    });
  }
  $('snapshot-select').addEventListener('change', (event) => {
    const key = snapshotScope === 'combined' ? 'day' : 'snapshot';
    loadSnapshot({ [key]: event.target.value }).catch((error) => notice(error.message));
  });
  for (const [kind, state] of Object.entries(activity)) {
    $(`${kind}-filter`).addEventListener('submit', (event) => {
      event.preventDefault();
      loadActivity(kind).catch((error) => notice(error.message));
    });
    $(`${kind}-prev`).addEventListener('click', () => loadActivity(kind, state.offset - 100).catch((error) => notice(error.message)));
    $(`${kind}-next`).addEventListener('click', () => loadActivity(kind, state.offset + 100).catch((error) => notice(error.message)));
  }
  selectTabFromLocation();
  loadSnapshot().catch((error) => notice(error.message));
})();
