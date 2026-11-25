(() => {
  const keys = ['bear', 'base', 'bull'];
  const labels = {bear: '保守', base: '中性', bull: '乐观'};
  const products = {al: '沪铝', ao: '氧化铝'};
  const priceBases = ['与上期所报价同口径', '公司实际售价', '其他自定义口径', '年报市场均价与期货代理'];
  const acceptsQuote = basis => basis === priceBases[0] || basis === priceBases[3];
  const units = 1e8;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const format = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 3}) : '—';
  const amount = value => format(finite(value) ? value / units : null);
  const percent = value => finite(value) ? format(100 * value) + '%' : '—';
  const multiple = value => finite(value) ? value.toFixed(2) : '—';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[char]);
  const number = input => input.value === '' || !Number.isFinite(input.valueAsNumber) ? null : input.valueAsNumber;
  const effect = (future, base, sensitivity) => [future, base, sensitivity].every(finite)
    ? (future - base) / 100 * sensitivity : null;
  const sum = values => values.every(finite) ? values.reduce((total, value) => total + value, 0) : null;
  const remainingQuarters = {'03-31': 3, '06-30': 2, '09-30': 1, '12-31': 0};

  function calculate(base, start, scenarios, dividendModel, current, annualEquity) {
    return Object.fromEntries(keys.map(key => {
      const changes = scenarios[key].map(row => ({
        base: base.profit,
        al: effect(row.al_price, base.al_price, row.al_sensitivity),
        ao: effect(row.ao_price, base.ao_price, row.ao_sensitivity),
        volume: row.volume, cost: row.cost, other: row.other,
      }));
      const index = current ? current.year - start : -1;
      let progress;
      if (index >= 0 && index < changes.length) {
        const actual = current.actual;
        const comparison = current.prior[base.year];
        const remaining = remainingQuarters[actual.period.slice(5)];
        const parts = changes[index];
        if (remaining === 0) {
          for (const part of Object.keys(parts)) parts[part] = 0;
        } else {
          parts.base = finite(base.profit) && comparison ? base.profit - comparison.profit : null;
          const prices = comparison && base.remaining_prices?.period === comparison.period
            ? base.remaining_prices : {};
          const assumption = scenarios[key][index];
          for (const commodity of ['al', 'ao']) {
            const field = commodity + '_price';
            const impact = effect(assumption[field], prices[field], assumption[commodity + '_sensitivity']);
            parts[commodity] = finite(impact) ? impact * (remaining / 4) : null;
          }
        }
        progress = {actual_period: actual.period, actual_profit: actual.profit, remaining_quarters: remaining,
          forecast_profit: sum(Object.values(parts))};
        parts.actual = actual.profit;
      }
      const result = dividendModel([start, start + 1, start + 2], {
        mode: 'linked', base_profit: base.profit, rows: scenarios[key].map((row, index) => ({
          profit: sum(Object.values(changes[index])), payout_ratio: row.payout, shares: row.shares,
        })),
      }, base.share_price);
      result.rows.forEach((row, index) => { row.parts = changes[index]; });
      if (progress) result.rows[index].progress = progress;
      if (annualEquity != null) {
        let equity = annualEquity[start - 1]?.value ?? null;
        for (const row of result.rows) {
          equity = [equity, row.profit, row.cash].every(finite) ? equity + row.profit - row.cash : null;
          const marketCap = base.share_price > 0 && row.shares > 0 ? base.share_price * row.shares : null;
          row.parent_equity = equity;
          row.pe = marketCap !== null && row.profit > 0 ? marketCap / row.profit : null;
          row.pb = marketCap !== null && equity > 0 ? marketCap / equity : null;
        }
      }
      result.cumulative_cash = sum(result.rows.map(row => row.cash));
      return [key, result];
    }));
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {calculate};
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const path = '/api/aluminum-forecast';
  const newAdopted = () => Object.fromEntries(keys.map(key => [key, [{}, {}, {}]]));
  let initial, current, draft, historical, parentId, dirty = false, busy = false, token;
  let versions = [], nextBefore, selected = null, quoteState = {quotes: {}, errors: {}}, adopted = newAdopted();
  const fields = [
    ['al_price', '沪铝均价（元/吨）', 1, 0],
    ['ao_price', '氧化铝均价（元/吨）', 1, 0],
    ['al_sensitivity', '沪铝敏感度（亿元）', units],
    ['ao_sensitivity', '氧化铝净敏感度（亿元）', units],
    ['volume', '产销量调整（亿元）', units],
    ['cost', '成本调整（亿元）', units],
    ['other', '其他归母调整（亿元）', units],
    ['payout', '分红比例（%）', .01, 0, 100],
    ['shares', '预计总股本（亿股）', units, 0],
  ];

  function status(message, failure = false) {
    $('status').textContent = message;
    $('status').classList.toggle('error', failure);
  }

  function versionStatus(message, failure = false) {
    $('version-status').textContent = message;
    $('version-status').classList.toggle('error', failure);
  }

  async function api(suffix = '', command) {
    const options = command === undefined ? {} : {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Local-Session': token},
      body: JSON.stringify(command),
    };
    const response = await fetch(path + suffix, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败');
    if (data.session_token) token = data.session_token;
    return data;
  }

  function updateControls() {
    for (const id of ['new-draft', 'load-version', 'delete-version', 'refresh-quotes', 'copy-base',
      'adopt-quotes', 'save', 'fork', 'reprice', 'more-versions']) $(id).disabled = busy;
    $('refresh-quotes').disabled ||= Boolean(historical);
    $('copy-base').disabled ||= Boolean(historical);
    $('adopt-quotes').disabled ||= Boolean(historical) || !Object.keys(quoteState.quotes).length
      || !acceptsQuote(draft?.base.price_basis);
    $('save').disabled ||= Boolean(historical);
    $('load-version').disabled ||= !selected;
    $('delete-version').disabled ||= !selected;
    $('fork').hidden = !historical?.compatible;
    $('reprice').hidden = !historical?.compatible;
    $('more-versions').hidden = !nextBefore;
    $('base').disabled = Boolean(historical) || busy;
    $('remaining-price-base').disabled = Boolean(historical) || busy;
    $('quote-adopt').disabled = Boolean(historical) || busy;
    $('version-inputs').disabled = Boolean(historical) || busy;
    for (const element of $('scenarios').querySelectorAll('fieldset')) element.disabled = Boolean(historical) || busy;
  }

  async function action(run) {
    if (busy) return;
    busy = true;
    updateControls();
    try { await run(); }
    catch (error) { status(error.message, true); }
    finally { busy = false; updateControls(); }
  }

  function dataValue(value, scale) {
    return finite(value) ? value / scale : '';
  }

  function scenarioValue(value, scale, field) {
    const decimals = field === 'al_price' || field === 'ao_price' ? 0 : 2;
    return finite(value) ? (value / scale).toFixed(decimals) : '';
  }

  function input(name, value, attributes = '') {
    return `<input type="number" step="any" aria-label="${esc(name)}" value="${esc(value)}" ${attributes}>`;
  }

  function table(headers, rows) {
    return `<div class="table-scroll"><table><thead><tr>${headers.map(t => `<th scope="col">${esc(t)}</th>`).join('')}</tr></thead><tbody>
      ${rows.map(row => '<tr>' + row.map((t, index) => index === 0
        ? `<th scope="row">${esc(t)}</th>` : `<td>${esc(t)}</td>`).join('') + '</tr>').join('')}</tbody></table></div>`;
  }

  function renderQuotes() {
    $('quotes').innerHTML = Object.entries(products).map(([key, label]) => {
      const quote = quoteState.quotes[key]?.value;
      const age = quote?.archived ? ' · 最近本地归档' : quote?.stale ? ' · 缓存已超过 15 分钟' : '';
      const time = quote?.obtained_at ? window.DashboardTime?.format(quote.obtained_at) || quote.obtained_at : '';
      const rollover = quote?.previous_contract
        ? `<p class="muted">上次参考合约 ${esc(quote.previous_contract)} · ${esc(quote.previous_trade_date)}</p>` : '';
      const error = quoteState.errors[key];
      const source = key === 'al'
        ? '<a href="https://hq.smm.cn/data/futures/shfe-al0000" target="_blank" rel="noopener">上海有色行情</a> · 数据来自上期所'
        : `<a href="${esc(quote?.url)}" target="_blank" rel="noopener">上期所来源</a>`;
      return `<article class="quote-card"><h3>${label}</h3><p class="quote-price">${quote ? format(quote.price) + ' 元/吨' : '—'}</p>
        <p class="muted">${quote ? `${esc(quote.trade_date)} · ${esc(quote.contract)}${age}` : '暂无报价，可刷新或手填。'}</p>
        ${quote ? `<p class="muted">${source} · 获取于 ${esc(time)}</p>` : ''}
        ${rollover}${error ? `<p class="warning">${esc(error)}</p>` : ''}</article>`;
    }).join('');
    const references = quoteState.references || initial?.quotes.references || {};
    const spot = references.spot;
    $('quotes').insertAdjacentHTML('beforeend', `<article class="quote-card" id="spot-quote">
      <h3>电解铝现货 · 华东 A00 铝锭</h3>
      <p class="quote-price">${spot ? format(spot.price) + ' 元/吨' : '—'}</p>
      <p class="muted">${spot ? `${esc(spot.trade_date)} · ${format(spot.low)}–${format(spot.high)} 元/吨${spot.archived ? ' · 最近本地归档' : ''}` : '暂无现货报价，可刷新市场行情。'}</p>
      ${spot ? '<p class="muted"><a href="https://list1.mysteel.com/market/p-975-----020204-0--------1.html" target="_blank" rel="noopener">我的钢铁网行情</a> · 数据为长江有色华东中间价</p>' : ''}
      ${references.errors?.spot ? `<p class="warning">${esc(references.errors.spot)}</p>` : ''}</article>`);
    renderMonthly(references.monthly, references.errors?.monthly);
  }

  function renderMonthly(series, error) {
    const node = $('monthly-chart');
    const note = $('monthly-note');
    if (!series || !window.Plotly) {
      if (node.data) window.Plotly.purge(node);
      node.replaceChildren();
      note.textContent = error || '暂无月线，可刷新市场行情。';
      return;
    }
    window.Plotly.react(node, [{
      x: series.points.map(point => point.trade_date || point.month + '-01'),
      y: series.points.map(point => point.close),
      type: 'scatter', mode: 'lines+markers', name: '沪铝主连月收盘价', connectgaps: false,
      line: {color: '#08635f', width: 2.5},
      hovertemplate: '沪铝主连：%{y:.2f} 元/吨<extra></extra>',
    }], {
      height: 255, margin: {l: 65, r: 16, t: 12, b: 45},
      hovermode: 'x unified', xaxis: {type: 'date', tickformat: '%Y-%m', hoverformat: '%Y-%m-%d'},
    }, {responsive: true, displaylogo: false});
    const current = series.points.at(-1);
    const through = current?.close != null ? `；当月截至 ${esc(current.trade_date)}` : `；最新收盘日 ${esc(series.trade_date)}`;
    note.innerHTML = `<a href="${esc(series.url)}" target="_blank" rel="noopener">${esc(series.source)}</a> · 每月最后一个有效交易日收盘价${through}${series.archived ? ' · 最近本地归档' : ''}${error ? ` <span class="warning">${esc(error)}</span>` : ''}`;
  }

  function renderEvidence() {
    const evidence = current?.evidence;
    const anchor = evidence?.annual.find(row => row.year === draft?.base.year);
    const market = evidence?.market;
    const source = anchor?.detail;
    const text = [];
    if (source) text.push(`${anchor.year} 年归母 ${amount(anchor.profit)} 亿元 · ${source.source}.${source.field} · 披露 ${source.provenance?.publish_date || '—'}`);
    if (market?.price) text.push(`本地参考价 ${format(market.price)} 元 · ${market.price_date} · ${market.price_source}`);
    if (market?.shares) text.push(`总股本 ${format(market.shares / units)} 亿股 · ${market.shares_date}`);
    if (market?.price_date && market?.shares_date && market.price_date !== market.shares_date)
      text.push('股价与股本日期不同，可按需要手动调整');
    $('evidence').textContent = text.join('；');
    const reference = evidence?.sensitivity;
    const automatic = evidence?.automatic;
    const report = automatic ? `<p>${automatic.year} 年报的自产原铝外销量 ${format(automatic.primary_aluminum_sold / 1e4)} 万吨。氧化铝净敞口按冶金级产量减估算耗用，约 ${format((automatic.metallurgical_alumina_produced - automatic.primary_aluminum_produced * automatic.alumina_per_tonne) / 1e4)} 万吨。按年报税前转归母比例、含税报价折净，并假设价格等额传导。</p>
      <p><a href="${esc(automatic.report_url)}" target="_blank" rel="noopener">年报第 ${esc(automatic.report_pages.join('、'))} 页</a> · <a href="${esc(automatic.consumption_url)}" target="_blank" rel="noopener">单位耗用行业依据</a></p>` : '';
    $('sensitivity').innerHTML = report + (reference?.available
      ? `<p>配对样本 ${esc(reference.sample_count)} 期 · 销售价格传导系数 ${format(reference.slope)} · 锚定模型误差 ${format(reference.mae)} 元/吨 · 基线误差 ${format(reference.baseline_mae)} 元/吨</p>`
      : `<p class="muted">实际售价回归：${esc(reference?.reason || '没有已存测算依据。')}</p>`);
    $('scenario-method').textContent = automatic
      ? `默认以本地期货报价作中性参考。剩余季度缺报价时留空，完整预测年度缺报价时用年报均价。两侧按沪铝 ±${format(automatic.al_spread * 100)}%、氧化铝 ±${format(automatic.ao_spread * 100)}% 测算。敏感度来自年报敞口估算，表示价格每变化 100 元／吨的归母影响（亿元），可手动调整。`
      : '敏感度表示价格每变化 100 元／吨的归母影响（亿元）。';
    $('dividends').parentElement.hidden = Boolean(historical);
    $('dividends').innerHTML = table(['财务年度', '每股已实施股息估算（元）', '历史分红率', '覆盖状态'],
      (initial?.dividends || []).map(row => [row.year, format(row.dps), percent(row.payout_ratio), row.state]));
  }

  function renderBase() {
    const base = draft?.base;
    if (!base) { $('base').replaceChildren(); return; }
    const reports = current.evidence.annual.map(row => `<option value="${row.year}">${amount(row.profit)} 亿元</option>`).join('');
    $('base').innerHTML = [
      ['year', '基准财务年', 1, 'min="1900" max="2197" list="report-years"'],
      ['profit', '基准归母净利润（亿元）', units, ''],
      ['al_price', '基准全年沪铝均价（元/吨）', 1, 'min="0"'],
      ['ao_price', '基准全年氧化铝均价（元/吨）', 1, 'min="0"'],
      ['share_price', '参考未复权股价（元）', 1, 'min="0"'],
      ['start', '预测起始年', 1, 'min="1900" max="2197"'],
    ].map(([key, label, scale, bounds]) => `<label>${label}${input(label,
      dataValue(key === 'start' ? draft.start : base[key], scale), `id="${key === 'start' ? 'start' : 'base-' + key}"
      data-base="${key}" data-scale="${scale}" ${bounds}`)}</label>`).join('')
      + `<datalist id="report-years">${reports}</datalist><label>价格口径<select id="price-basis">
      ${priceBases.map(value => `<option${base.price_basis === value ? ' selected' : ''}>${value}</option>`).join('')}
      </select></label><label>基准与手填依据<input id="basis-note" maxlength="1000" value="${esc(base.basis_note)}"></label>`;
  }

  function remainingPricePeriod() {
    const progress = current?.evidence?.current_year;
    if (!draft || !progress || !Number.isInteger(draft.start) || progress.year < draft.start
        || progress.year > draft.start + 2 || !remainingQuarters[progress.actual.period.slice(5)]) return null;
    return progress.prior[draft.base.year]?.period || null;
  }

  function remainingPriceReference(period) {
    return draft.base.price_basis === priceBases[3]
      ? current.evidence.automatic?.remaining_price_references?.[period] : null;
  }

  function renderRemainingPrices() {
    const container = $('remaining-price-base');
    const source = $('remaining-price-source');
    const period = remainingPricePeriod();
    const legacy = historical && !Object.hasOwn(draft?.base || {}, 'remaining_prices');
    container.hidden = source.hidden = !period || Boolean(legacy);
    if (container.hidden) { container.replaceChildren(); return; }
    const reference = remainingPriceReference(period);
    if (!historical && draft.base.remaining_prices?.period !== period && reference) {
      draft.base.remaining_prices = {period, al_price: reference.al_price, ao_price: reference.ao_price};
    }
    const prices = draft.base.remaining_prices?.period === period ? draft.base.remaining_prices : {};
    const quarter = 5 - remainingQuarters[period.slice(5)];
    const range = quarter === 4 ? `${period.slice(0, 4)} 年 Q4` : `${period.slice(0, 4)} 年 Q${quarter}–Q4`;
    container.innerHTML = `<legend>${range}基准均价</legend>`
      + Object.entries(products).map(([key, product]) => {
        const field = key + '_price';
        const label = `${product}均价（元/吨）`;
        return `<label>${label}${input(label, dataValue(prices[field], 1), `min="0" data-remaining-price="${field}"`)}</label>`;
      }).join('');
    source.replaceChildren();
    if (reference) {
      const link = document.createElement('a');
      link.href = reference.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = `${reference.source}，第 ${reference.page} 页`;
      source.append(`默认参考：${reference.basis}。`, link, '。预测价格可用期货报价作代理，也可手填预计均价。');
    } else {
      source.textContent = '请填写与基准利润同期间、与预测价格口径可比的均价。缺价时剩余季度预测留空。';
    }
  }

  function referenceControls(key, index, row) {
    if (!current.evidence.sensitivity.available) return '';
    return `<details><summary>${draft.start + index} 年参考敏感度</summary>
      <label>预计对外销量（万吨）${input('预计对外销量', dataValue(row.reference?.volume, 1e4), 'data-ref="volume" min="0"')}</label>
      <label>税后归母转换比例（%）${input('税后归母转换比例', dataValue(row.reference?.conversion, .01), 'data-ref="conversion" min="0" max="100"')}</label>
      <button type="button" data-reference="${key}" data-index="${index}">${row.reference ? '更新参考值' : '采用参考值'}</button></details>`;
  }

  function renderScenarios() {
    if (!draft) { $('scenarios').replaceChildren(); return; }
    $('scenarios').innerHTML = keys.map(key => `<article class="scenario" data-scenario="${key}"><h3>${labels[key]}</h3>
      <fieldset><div class="table-scroll"><table><thead><tr><th scope="col">假设</th>${draft.scenarios[key].map((_, index) =>
        `<th scope="col" data-year="${index}">${Number.isInteger(draft.start) ? draft.start + index : '—'}</th>`).join('')}</tr></thead>
      <tbody>${fields.map(([field, label, scale, min, max]) => `<tr><th scope="row">${label}</th>${draft.scenarios[key].map((row, index) =>
        `<td>${input(`${labels[key]}第 ${index + 1} 年 ${label}`, scenarioValue(row[field], scale, field),
          `data-scenario="${key}" data-field="${field}" data-index="${index}" data-scale="${scale}"
          ${field.endsWith('sensitivity') ? 'placeholder="待填"' : ''}
          ${min === undefined ? '' : 'min="' + min + '"'} ${max === undefined ? '' : 'max="' + max + '"'}`)}
          ${field === 'al_sensitivity' && row.reference ? '<small>历史测算</small>' : ''}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
      ${draft.scenarios[key].map((row, index) => referenceControls(key, index, row)).join('')}</fieldset></article>`).join('');
  }

  function chart(id, results, field, label, unit, factor) {
    const node = $(id);
    const values = keys.filter(key => results[key]?.rows?.length).map(key => ({
      name: labels[key], x: results[key].rows.map(row => String(row.year)),
      y: results[key].rows.map(row => finite(row[field]) ? row[field] * factor : null),
      type: 'scatter', mode: 'lines+markers', connectgaps: false,
      line: {color: {bear: '#556f92', base: '#08635f', bull: '#a35834'}[key], width: 2.5},
      hovertemplate: `${labels[key]}：%{y:.2f} ${unit}<extra></extra>`,
    }));
    if (!window.Plotly || !values.some(trace => trace.y.some(finite))) {
      if (node.data) window.Plotly.purge(node);
      node.textContent = label + ' · 补齐参数后显示';
      return;
    }
    if (!node.data) node.replaceChildren();
    window.Plotly.react(node, values, {
      title: {text: label, font: {size: 15}}, height: 255, margin: {l: 55, r: 18, t: 38, b: 53},
      xaxis: {type: 'category'}, yaxis: {ticksuffix: unit === '%' ? '%' : ''},
      hovermode: 'x unified', legend: {orientation: 'h', y: -.19},
    }, {responsive: true, displaylogo: false});
  }

  const resultsExpanded = new Map([['base', true]]);
  const simulationFields = [
    {key: 'al_price', label: '电解铝均价（元/吨）', scale: 1, low: 18000, high: 27000, step: 10, price: true},
    {key: 'al_sensitivity', label: '电解铝敏感度（亿元 / 每 1,000 元/吨）', scale: units / 10, low: -60, high: 60, step: .1},
    {key: 'ao_price', label: '氧化铝均价（元/吨）', scale: 1, low: 1500, high: 4500, step: 10, price: true},
    {key: 'ao_sensitivity', label: '氧化铝净敏感度（亿元 / 每 1,000 元/吨）', scale: units / 10, low: -20, high: 20, step: .1},
  ];
  const simulation = {year: null, scenario: 'base', changes: {}, results: {}};
  const simulationInputs = $('simulation-inputs');
  const simulationFieldMarkup = ({key, label, price}) => `<div class="simulation-field">
    <label for="simulation-${key}">${label}</label>
    ${input(label, '', `id="simulation-${key}" data-simulation="${key}" placeholder="待填" ${price ? 'min="1"' : ''}`)}
    <input type="range" step="any" aria-label="${label}滑杆" data-simulation="${key}">
    <span class="simulation-scale" aria-hidden="true"><span></span><span></span></span>
  </div>`;
  simulationInputs.innerHTML = [0, 2].map(start =>
    '<div>' + simulationFields.slice(start, start + 2).map(simulationFieldMarkup).join('') + '</div>').join('');

  function simulationContext() {
    const index = simulation.year - draft.start;
    return {index, row: draft.scenarios[simulation.scenario][index],
      original: simulation.results[simulation.scenario]?.rows[index]};
  }

  function syncSimulationSlider(field, slider, displayed) {
    const bounds = slider.nextElementSibling.children;
    const low = finite(displayed) ? Math.min(field.low, Math.floor(displayed)) : field.low;
    const high = finite(displayed) ? Math.max(field.high, Math.ceil(displayed)) : field.high;
    slider.min = low;
    slider.max = high;
    slider.value = displayed ?? (low + high) / 2;
    bounds[0].textContent = format(low);
    bounds[1].textContent = format(high);
  }

  function syncSimulationInputs() {
    const {row, original} = simulationContext();
    for (const field of simulationFields) {
      const value = Object.hasOwn(simulation.changes, field.key) ? simulation.changes[field.key] : row[field.key];
      const displayed = finite(value) ? value / field.scale : null;
      const box = $('simulation-' + field.key);
      const slider = box.nextElementSibling;
      box.value = displayed === null ? '' : Number(displayed.toFixed(4));
      syncSimulationSlider(field, slider, displayed);
      box.disabled = slider.disabled = original?.progress?.remaining_quarters === 0;
    }
  }

  const trialAmount = (value, signed = false) =>
    finite(value) ? `${signed && value > 0 ? '+' : ''}${amount(value)} 亿元` : '—';

  function renderSimulationResult() {
    const {index, original} = simulationContext();
    const rows = draft.scenarios[simulation.scenario].map((row, i) => i === index ? {...row, ...simulation.changes} : row);
    const result = calculate(draft.base, draft.start, {...draft.scenarios, [simulation.scenario]: rows},
      window.DividendForecast.calculate_scenario, current.evidence.current_year)[simulation.scenario].rows[index];
    const progress = result.progress;
    const partial = Boolean(progress?.remaining_quarters);
    const actual = progress?.remaining_quarters === 0;
    $('simulation-total-label').textContent = `${simulation.year} 年${actual ? '已披露' : '估算'}归母净利润`;
    $('simulation-profit').textContent = trialAmount(result.profit);
    $('simulation-change').textContent = finite(result.profit) && finite(original?.profit)
      ? `较${labels[simulation.scenario]}情景 ${trialAmount(result.profit - original.profit, true)}` : '';
    $('simulation-al').textContent = trialAmount(result.parts.al, true);
    $('simulation-ao').textContent = trialAmount(result.parts.ao, true);
    $('simulation-remaining-row').hidden = !partial;
    $('simulation-remaining-label').textContent = progress?.remaining_quarters === 1 ? '第四季度归母净利润' : '剩余季度归母净利润';
    $('simulation-remaining').textContent = trialAmount(progress?.forecast_profit);
    const prices = partial && draft.base.remaining_prices?.period === remainingPricePeriod()
      ? draft.base.remaining_prices : partial ? {} : draft.base;
    const basis = partial ? `${draft.base.year} 年同期` : `${draft.base.year} 年全年`;
    $('simulation-basis').textContent = actual ? '该年度财报已披露，显示实际利润。'
      : `比较基准为${basis}。电解铝 ${format(prices.al_price)}、氧化铝 ${format(prices.ao_price)} 元/吨。`
        + (partial ? `全年含已披露归母净利润 ${trialAmount(progress.actual_profit)}，滑杆仅改变剩余季度。` : '')
        + (finite(result.profit) ? '' : '请补齐预测价格和基准参数。');
  }

  function renderSimulation(results) {
    const panel = $('profit-simulator');
    panel.hidden = Boolean(historical) || !Number.isInteger(draft?.start);
    if (panel.hidden) return;
    simulation.results = results;
    const years = Array.from({length: 3}, (_, index) => draft.start + index);
    if (!years.includes(simulation.year)) {
      const currentYear = Number(current.evidence.as_of?.slice(0, 4));
      simulation.year = years.find(year => year > currentYear) ?? years[0];
      simulation.changes = {};
    }
    const yearPicker = $('simulation-year');
    yearPicker.replaceChildren(...years.map(year => new Option(String(year), String(year), false, year === simulation.year)));
    $('simulation-scenario').value = simulation.scenario;
    syncSimulationInputs();
    renderSimulationResult();
  }

  $('simulation-year').addEventListener('change', event => {
    simulation.year = Number(event.target.value);
    $('simulation-reset').click();
  });
  $('simulation-scenario').addEventListener('change', event => {
    simulation.scenario = event.target.value;
    $('simulation-reset').click();
  });
  $('simulation-reset').addEventListener('click', () => {
    simulation.changes = {};
    syncSimulationInputs();
    renderSimulationResult();
  });
  simulationInputs.addEventListener('input', event => {
    const field = simulationFields.find(item => item.key === event.target.dataset.simulation);
    if (!field) return;
    let value = number(event.target);
    const box = $('simulation-' + field.key);
    const slider = box.nextElementSibling;
    if (event.target === slider && finite(value)) {
      value = Number((Math.round(value / field.step) * field.step).toFixed(1));
      box.value = slider.value = value;
    } else {
      syncSimulationSlider(field, slider, finite(value) && (!field.price || value > 0) ? value : null);
    }
    simulation.changes[field.key] = finite(value) && (!field.price || value > 0) ? value * field.scale : null;
    renderSimulationResult();
  });

  function renderResults() {
    const panel = $('results');
    for (const detail of panel.querySelectorAll('[data-result-scenario]')) {
      resultsExpanded.set(detail.dataset.resultScenario, detail.open);
    }
    const results = historical ? historical.snapshot.results
      : Number.isInteger(draft?.start) ? calculate(draft.base, draft.start, draft.scenarios,
        window.DividendForecast.calculate_scenario, current.evidence.current_year, current.evidence.annual_equity) : {};
    renderSimulation(results);
    const view = results && keys.some(key => Array.isArray(results[key]?.rows));
    const progress = results?.base?.rows?.find(row => row.progress)?.progress;
    $('actual-progress').hidden = !progress;
    if (progress) {
      $('actual-progress').textContent = `截至 ${progress.actual_period} 已披露累计归母净利润 ${amount(progress.actual_profit)} 亿元。`
        + (progress.remaining_quarters
          ? `其余 ${progress.remaining_quarters} 个季度按各情景测算。表内当年价格和经营调整只用于剩余季度。`
            + (!historical && progress.forecast_profit === null
              ? ' 请核对同期财务并补齐基准与预测均价。' : '')
          : '全年归母净利润已披露。');
    }
    chart('profit-chart', results || {}, 'profit', '归母净利润（亿元）', '亿元', 1 / units);
    chart('yield-chart', results || {}, 'yield_rate', '税前股息率', '%', 100);
    const valuationNote = $('valuation-note');
    valuationNote.hidden = !view;
    if (view) {
      const first = results.base?.rows?.[0];
      const startingEquity = current?.evidence.annual_equity?.[first?.year - 1];
      valuationNote.textContent = first && Object.hasOwn(first, 'pe')
        ? 'PE 和 PB 按参考股价乘当年预计总股本计算市值。PE 除以当年归母净利润，PB 除以预计期末归母净资产。'
          + (startingEquity
            ? `净资产从 ${startingEquity.period} 已披露的 ${amount(startingEquity.value)} 亿元起，逐年加归母利润、减预测现金分红。`
            : '缺少预测首年前一年的年末归母净资产，PB 留空。')
        : '该版本保存时未记录 PE 和 PB。';
    }
    if (!view) {
      panel.innerHTML = historical ? `<details><summary>原保存结果</summary><pre>${esc(JSON.stringify(results, null, 2))}</pre></details>` : '';
      return;
    }
    panel.innerHTML = keys.filter(key => Array.isArray(results[key]?.rows)).map(key => {
      const result = results[key];
      return `<details data-result-scenario="${key}"${resultsExpanded.get(key) ? ' open' : ''}><summary>${labels[key]} · 三年累计分红 ${amount(result.cumulative_cash)} 亿元 · 每股 ${format(result.cumulative_dps)} 元</summary>
        ${table(['年度', ...(progress ? ['已披露归母', '当年剩余预测'] : []),
          '预测基准归母', '沪铝影响', '氧化铝影响', '产销量', '成本', '其他', '归母净利润', '现金分红',
          '税前股息（元/股）', '税前股息率', '预测 PE（倍）', '预测 PB（倍）'], result.rows.map(row => [row.year,
          ...(progress ? [amount(row.progress?.actual_profit), amount(row.progress?.forecast_profit)] : []),
          ...['base', 'al', 'ao', 'volume', 'cost', 'other'].map(part => amount(row.parts?.[part])),
          amount(row.profit), amount(row.cash), format(row.dps), percent(row.yield_rate), multiple(row.pe), multiple(row.pb)]))}
        <p class="muted">利润、调整和现金分红均为亿元。${result.rows.map(row =>
          finite(row.profit) && row.profit <= 0 ? `${row.year} 年归母利润非正，按比例无法确定分红。 `
            : finite(row.profit) && !finite(row.yield_rate) ? `${row.year} 年分红或股息率参数未齐。 ` : '').join('')}</p></details>`;
    }).join('');
  }

  function renderDirectory() {
    $('versions').innerHTML = versions.length ? versions.map(version =>
      `<label class="version-item"><input type="radio" name="version" value="${version.id}"${selected === version.id ? ' checked' : ''}>
      <strong>${esc(version.name)}</strong> <span>${version.forecast_start_year}–${version.forecast_start_year + 2} · #${version.id}</span>
      <time>${esc(window.DashboardTime?.format(version.created_at) || version.created_at)}</time></label>`).join('')
      : '<p class="muted">暂无已保存版本。</p>';
  }

  function render() {
    renderQuotes();
    renderBase();
    renderRemainingPrices();
    renderEvidence();
    renderScenarios();
    renderResults();
    renderDirectory();
    updateControls();
  }

  function reset(currentData) {
    current = currentData;
    quoteState = currentData.quotes;
    draft = structuredClone(currentData.defaults);
    adopted = newAdopted();
    for (const [product, quote] of Object.entries(quoteState.quotes)) {
      for (let index = 0; index < draft.scenarios.base.length; index++) {
        if (draft.scenarios.base[index][product + '_price'] === quote.value.price)
          adopted.base[index][product] = quote;
      }
    }
    historical = null;
    parentId = null;
    dirty = false;
    Object.assign(simulation, {year: null, scenario: 'base', changes: {}});
    $('name').value = '';
    $('note').value = '';
    $('version-detail').textContent = '保存会生成一个独立版本。';
    versionStatus('');
    render();
  }

  function allowSwitch() {
    return !dirty || window.confirm('当前修改尚未保存，是否继续？');
  }

  async function refresh() {
    status('正在查询期货结算价、现货和月线…');
    const state = await api('/refresh-quotes', {});
    quoteState = state;
    if (!state.pending && !dirty && parentId === null && Object.keys(state.quotes).length) {
      initial = await api();
      reset(initial);
    }
    renderQuotes();
    const partial = Object.keys(state.errors).length > 0 || Object.keys(state.references?.errors || {}).length > 0;
    status(state.pending ? '已有行情刷新在运行。' : state.reused && !partial ? '已更新参考行情。'
      : partial ? '部分行情尚未取得，可继续手填或使用已保存方案。' : '已更新参考报价，预测参数仍可单独调整。', partial);
  }

  function fork() {
    const id = historical.id;
    historical = null;
    parentId = id;
    dirty = true;
    Object.assign(simulation, {year: null, scenario: 'base', changes: {}});
    $('version-detail').textContent = `从 #${id} 复制的草稿，保存会生成新版本。`;
    render();
  }

  $('base').addEventListener('input', event => {
    const field = event.target.dataset.base;
    if (field) {
      const val = number(event.target);
      if (field === 'start') draft.start = val;
      else draft.base[field] = val === null ? null : val * Number(event.target.dataset.scale);
      if (field === 'year') {
        delete draft.base.remaining_prices;
        const report = current.evidence.annual.find(row => row.year === val);
        if (report) {
          draft.base.profit = report.profit;
          $('base-profit').value = dataValue(report.profit, units);
        }
      }
      if (field === 'year') renderEvidence();
      if (field === 'start') {
        for (const cell of $('scenarios').querySelectorAll('[data-year]'))
          cell.textContent = Number.isInteger(val) ? val + Number(cell.dataset.year) : '—';
      }
    } else if (event.target.id === 'price-basis') {
      if (draft.base.price_basis !== event.target.value) delete draft.base.remaining_prices;
      draft.base.price_basis = event.target.value;
      if (!acceptsQuote(draft.base.price_basis)) {
        adopted = newAdopted();
        for (const rows of Object.values(draft.scenarios)) rows.forEach(row => { row.reference = null; });
        renderScenarios();
      }
    } else if (event.target.id === 'basis-note') draft.base.basis_note = event.target.value;
    if (field === 'year' || field === 'start' || event.target.id === 'price-basis') renderRemainingPrices();
    dirty = true;
    renderResults();
    updateControls();
  });

  $('remaining-price-base').addEventListener('input', event => {
    const field = event.target.dataset.remainingPrice;
    const period = remainingPricePeriod();
    if (!field || !period) return;
    if (draft.base.remaining_prices?.period !== period)
      draft.base.remaining_prices = {period, al_price: null, ao_price: null};
    draft.base.remaining_prices[field] = number(event.target);
    dirty = true;
    renderResults();
  });

  $('scenarios').addEventListener('input', event => {
    const {scenario, index, field, scale} = event.target.dataset;
    if (!field) return;
    const value = number(event.target);
    const row = draft.scenarios[scenario][index];
    row[field] = value === null ? null : value * Number(scale);
    if (field === 'al_sensitivity' && row.reference) {
      row.reference = null;
      event.target.parentElement.querySelector('small')?.remove();
    }
    if (field === 'al_price' || field === 'ao_price') delete adopted[scenario][index][field.slice(0, 2)];
    dirty = true;
    renderResults();
  });

  $('scenarios').addEventListener('focusout', event => {
    const {scenario, index, field, scale} = event.target.dataset;
    if (field) event.target.value = scenarioValue(draft.scenarios[scenario][index][field], Number(scale), field);
  });

  $('scenarios').addEventListener('click', event => {
    const key = event.target.dataset.reference;
    if (!key) return;
    const index = Number(event.target.dataset.index);
    const inputs = event.target.closest('details');
    const volume = number(inputs.querySelector('[data-ref="volume"]')) * 1e4;
    const conversion = number(inputs.querySelector('[data-ref="conversion"]')) / 100;
    if (draft.base.price_basis !== priceBases[0] || !(volume > 0 && conversion > 0 && conversion <= 1)) {
      status('采用参考值需选择与上期所一致的价格口径，填写正销量与 0–100% 的归母转换比例。', true);
      return;
    }
    const row = draft.scenarios[key][index];
    row.reference = {volume, conversion};
    row.al_sensitivity = 100 * current.evidence.sensitivity.slope * volume * conversion;
    dirty = true;
    renderScenarios();
    renderResults();
  });

  $('name').addEventListener('input', () => { dirty = true; });
  $('note').addEventListener('input', () => { dirty = true; });
  $('refresh-quotes').onclick = () => action(refresh);
  $('new-draft').onclick = () => { if (allowSwitch()) action(async () => {
    initial = await api();
    versions = initial.versions;
    nextBefore = initial.next_before;
    selected = null;
    reset(initial);
    status('已新建草稿。');
  }); };
  $('fork').onclick = () => { fork(); status('已复制为草稿，可修改参数。'); };
  $('reprice').onclick = () => action(async () => { fork(); await refresh(); });
  $('copy-base').onclick = () => {
    for (const key of ['bear', 'bull']) {
      draft.scenarios[key] = structuredClone(draft.scenarios.base);
      adopted[key] = structuredClone(adopted.base);
    }
    dirty = true;
    renderScenarios();
    renderResults();
  };
  $('adopt-quotes').onclick = () => {
    const scenario = $('adopt-scenario').value;
    const year = $('adopt-year').value;
    const productFilter = $('adopt-product').value;
    const quotes = Object.entries(quoteState.quotes).filter(([product]) => !productFilter || product === productFilter);
    if (!quotes.length) { status('所选品种暂无可采用的报价。', true); return; }
    for (const key of scenario ? [scenario] : keys) {
      draft.scenarios[key].forEach((row, index) => {
        if (year !== '' && Number(year) !== index) return;
        for (const [product, quote] of quotes) {
          row[product + '_price'] = quote.value.price;
          adopted[key][index][product] = quote;
        }
      });
    }
    dirty = true;
    status('已将报价填入所选均价，可继续修改。');
    renderScenarios();
    renderResults();
  };
  $('versions').addEventListener('change', event => { selected = Number(event.target.value); updateControls(); });
  $('more-versions').onclick = () => action(async () => {
    const page = await api('?before=' + nextBefore);
    versions.push(...page.versions);
    nextBefore = page.next_before;
    renderDirectory();
  });
  $('load-version').onclick = () => { if (allowSwitch()) action(async () => {
    historical = await api('?version=' + selected);
    dirty = false;
    const snapshot = historical.snapshot;
    current = historical.compatible ? historical : null;
    draft = historical.compatible ? {base: snapshot.context.base, start: snapshot.context.start, scenarios: snapshot.scenarios} : null;
    const registry = historical.quote_registry || {};
    quoteState = {quotes: Object.fromEntries(Object.entries(snapshot.context?.quotes || {})
      .filter(([, key]) => registry[key]).map(([product, key]) => [product, registry[key]])), errors: {}};
    adopted = historical.compatible ? Object.fromEntries(keys.map(key => [key, snapshot.context.adopted[key].map(row =>
      Object.fromEntries(Object.entries(row).map(([product, ref]) => [product, registry[ref]])))])) : newAdopted();
    $('name').value = historical.name;
    $('note').value = snapshot.note || '';
    $('version-detail').textContent = `历史版本 #${historical.id} · ${historical.model_version}`;
    versionStatus('');
    render();
    status(historical.compatible ? '已恢复原始参数与结果。' : '此模型版本可查看已保存的结果。');
  }); };
  $('delete-version').onclick = () => {
    const item = versions.find(version => version.id === selected);
    if (!item || !window.confirm(`删除「${item.name}」#${item.id}？`)) return;
    action(async () => {
      await api('/delete', {id: item.id});
      versions = versions.filter(version => version.id !== item.id);
      if (historical?.id === item.id) reset(initial);
      selected = null;
      renderDirectory();
      status('已删除所选版本。');
    });
  };
  $('save').onclick = () => {
    if (!$('name').value.trim()) { $('name').focus(); versionStatus('请输入方案名称。', true); return; }
    action(async () => {
      try {
        const {id, version} = await api('/save', {name: $('name').value, note: $('note').value, parent_version_id: parentId,
          ...draft, evidence: current.evidence, proof: current.proof, quotes: quoteState.quotes, adopted});
        dirty = false;
        parentId = id;
        selected = id;
        versions.unshift(version);
        $('version-detail').textContent = `已保存版本 #${id}，可继续编辑并另存。`;
        versionStatus(`已保存不可改写版本 #${id}。`);
        renderDirectory();
      } catch (error) { versionStatus(error.message, true); }
    });
  };

  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  action(async () => {
    initial = await api();
    versions = initial.versions;
    nextBefore = initial.next_before;
    $('workspace').hidden = false;
    reset(initial);
    status('已自动生成三情景测算。修改参数可即时试算。');
  });
})();
