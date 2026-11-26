(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const formatter = new Intl.NumberFormat('zh-CN', {
    signDisplay: 'exceptZero', minimumFractionDigits: 2, maximumFractionDigits: 2
  });
  const money = (value) => `${formatter.format(Number(value))} 元`;
  const rate = (value) => `${formatter.format(Number(value) * 100)}%`;
  let data;
  let report;
  let byDate;
  let loading = false;
  let plotlyPromise;
  let chartRequest = 0;

  function element(tag, text = '') {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  }

  function carriedCloses(record) {
    return (record.estimated_closes || []).map(({code, date, price_date}) =>
      `${code} 在 ${date} 沿用 ${price_date} 收盘价`).join('、');
  }

  function showDay(record) {
    const detail = $('calendar-day-detail');
    if (record.pnl == null) {
      detail.textContent = `${record.date} · 缺少数据 · ${record.issues.join('、')}`;
      return;
    }
    const dailyRate = record.return_rate == null ? '收益率待核对' : `收益率 ${rate(record.return_rate)}`;
    const estimated = Number(record.estimated_dividend) ? ` · 其中 ETF 公告估算 ${money(record.estimated_dividend)}` : '';
    const carried = carriedCloses(record);
    detail.textContent = `${record.date} · 估算盈亏 ${money(record.pnl)} · ${dailyRate} · 买卖 ${record.trades} 笔 · 股息及补税 ${money(record.dividend_net)}${estimated}${carried ? ` · ${carried}` : ''}`;
  }

  function summarize(rows) {
    const compound = $('performance-return-mode').value === 'compound';
    let amount = 0;
    let accumulated = compound ? 1 : 0;
    let complete = true;
    let rateDays = 0;
    let covered = 0;
    let estimatedDays = 0;
    const points = rows.map((row) => {
      if (row.pnl != null) {
        amount += Number(row.pnl);
        covered++;
        if (row.estimated_closes?.length) estimatedDays++;
      }
      const noCapital = row.pnl != null && Number(row.pnl) === 0 &&
        row.return_base != null && Number(row.return_base) === 0;
      if (row.return_rate == null && !noCapital) complete = false;
      if (complete && row.return_rate != null) {
        const day = Number(row.return_rate);
        accumulated = compound ? accumulated * (1 + day) : accumulated + day;
        rateDays++;
      }
      return { date: row.date, amount: row.pnl == null ? null : amount,
        rate: complete && rateDays ? accumulated - (compound ? 1 : 0) : null };
    });
    return { amount: covered ? amount : null, rate: points.at(-1)?.rate ?? null, points, covered, estimatedDays };
  }

  function calendarMetric() {
    return $('calendar-metric').value;
  }

  function calendarText(value) {
    if (value == null) return '缺数据';
    return calendarMetric() === 'rate' ? rate(value) : formatter.format(value);
  }

  function calendarClass(value) {
    return value > 0 ? 'calendar-gain' : value < 0 ? 'calendar-loss' : 'calendar-flat';
  }

  function calendarCell(month, day) {
    const date = `${month}-${String(day).padStart(2, '0')}`;
    const record = byDate.get(date);
    const cell = element('td');
    const number = element('span', String(day));
    number.className = 'calendar-day-number';
    if (!record) {
      const status = date > data.as_of ? '未覆盖' : '休市';
      cell.className = 'calendar-no-result';
      cell.append(number, element('small', status));
      return cell;
    }
    const button = element('button');
    button.type = 'button';
    const useRate = calendarMetric() === 'rate';
    const value = useRate ? record.return_rate : record.pnl;
    const label = calendarText(value);
    const carried = value == null ? '' : carriedCloses(record);
    button.append(number, element('small', carried ? `≈ ${label}` : label));
    button.setAttribute('aria-label', `${date} ${label}${value == null || useRate ? '' : ' 元'}${carried ? '（含沿用收盘价）' : ''}`);
    button.title = value == null ? (record.issues.join('、') || '没有可计算的收益率') : `${date} · ${label}${carried ? ` · ${carried}` : ''}`;
    cell.className = calendarClass(value);
    button.addEventListener('click', () => {
      $('calendar-body').querySelector('[aria-pressed="true"]')?.setAttribute('aria-pressed', 'false');
      button.setAttribute('aria-pressed', 'true');
      showDay(record);
    });
    button.setAttribute('aria-pressed', 'false');
    cell.append(button);
    return cell;
  }

  function renderCalendar() {
    const yearly = $('calendar-view').value === 'year';
    $('calendar-year-control').hidden = !yearly;
    $('calendar-month-controls').hidden = $('calendar-month-grid').hidden = yearly;
    $('calendar-year-grid').hidden = !yearly;
    if (yearly) {
      renderYearCalendar();
      return;
    }
    const select = $('calendar-month');
    const month = select.value;
    const [year, numericMonth] = month.split('-').map(Number);
    const monthRecords = data.records.filter((row) => row.date.startsWith(month));
    setCalendarSummary(monthRecords);
    $('calendar-prev').disabled = select.selectedIndex === 0;
    $('calendar-next').disabled = select.selectedIndex === select.options.length - 1;
    $('calendar-day-detail').textContent = '选择日期查看盈亏明细。';
    const length = new Date(Date.UTC(year, numericMonth, 0)).getUTCDate();
    const offset = new Date(Date.UTC(year, numericMonth - 1, 1)).getUTCDay();
    const rows = [];
    for (let week = 0; week < Math.ceil((offset + length) / 7); week++) {
      const row = element('tr');
      for (let weekday = 0; weekday < 7; weekday++) {
        const day = week * 7 + weekday + 1 - offset;
        row.append(day >= 1 && day <= length ? calendarCell(month, day) : element('td'));
      }
      rows.push(row);
    }
    $('calendar-body').replaceChildren(...rows);
  }

  function setCalendarSummary(records) {
    const summary = summarize(records);
    const useRate = calendarMetric() === 'rate';
    const value = useRate ? summary.rate : summary.amount;
    const measure = useRate ? ($('performance-return-mode').value === 'simple' ? '单利估算' : '复利估算')
      : summary.covered === records.length ? '累计估算' : '已覆盖日期合计';
    const visible = value == null ? '暂无可计算结果' : `${measure} ${useRate ? rate(value) : money(value)}`;
    $('calendar-total').textContent = `${visible} · ${summary.covered}/${records.length} 个交易日${summary.estimatedDays ? ` · ${summary.estimatedDays} 天沿用收盘价` : ''}`;
  }

  function renderYearCalendar() {
    const year = $('calendar-year').value;
    const yearRecords = data.records.filter((row) => row.date.startsWith(`${year}-`));
    setCalendarSummary(yearRecords);
    $('calendar-day-detail').textContent = '选择月份查看交易日明细。';
    const cards = [];
    for (let month = 1; month <= 12; month++) {
      const key = `${year}-${String(month).padStart(2, '0')}`;
      const records = yearRecords.filter((row) => row.date.startsWith(key));
      const summary = summarize(records);
      const value = calendarMetric() === 'rate' ? summary.rate : summary.amount;
      const card = element('button');
      card.type = 'button';
      card.className = calendarClass(value);
      card.disabled = !records.length;
      card.append(element('span', `${month} 月`), element('strong', records.length ? calendarText(value) : '未覆盖'));
      card.title = records.length ? `已覆盖 ${summary.covered}/${records.length} 个交易日` : '未覆盖';
      card.addEventListener('click', () => {
        $('calendar-month').value = key;
        $('calendar-view').value = 'month';
        renderCalendar();
      });
      cards.push(card);
    }
    $('calendar-year-grid').replaceChildren(...cards);
  }

  function loadPlotly() {
    if (window.Plotly) return Promise.resolve(window.Plotly);
    plotlyPromise ??= new Promise((resolve, reject) => {
      const script = element('script');
      script.src = '/plotly.min.js';
      script.onload = () => resolve(window.Plotly);
      script.onerror = () => reject(new Error('暂时无法加载累计盈亏曲线'));
      document.head.append(script);
    });
    return plotlyPromise;
  }

  function chartLayout(key, percent = false) {
    return {
      uirevision: key, font: { color: '#b2c2c9', size: 12 },
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      showlegend: false,
      margin: { l: 70, r: 18, t: 18, b: 38 }, hovermode: 'x unified',
      xaxis: { type: 'date', tickformat: '%Y-%m', hoverformat: '%Y-%m-%d', gridcolor: '#354853' },
      yaxis: { tickformat: percent ? '.1f' : ',.0f', ticksuffix: percent ? '%' : ' 元',
        gridcolor: '#354853', zerolinecolor: '#607985' }
    };
  }

  function splitAtZero(dates, values) {
    const x = [], y = [];
    for (let index = 0; index < values.length; index++) {
      const left = values[index - 1], right = values[index];
      if (left != null && right != null && left * right < 0) {
        const start = Date.parse(dates[index - 1]), end = Date.parse(dates[index]);
        const crossing = start + (end - start) * Math.abs(left) / (Math.abs(left) + Math.abs(right));
        x.push(new Date(crossing).toISOString());
        y.push(0);
      }
      x.push(dates[index]);
      y.push(right);
    }
    return { x, y };
  }

  async function drawCharts(rows, summary, bounds) {
    const request = ++chartRequest;
    const useRate = $('performance-chart-metric').value === 'rate';
    const Plotly = await loadPlotly();
    if (request !== chartRequest) return;
    const metric = useRate ? 'rate' : 'amount';
    const label = useRate ? '累计收益率' : summary.covered < rows.length ? '已覆盖累计盈亏' : '累计盈亏';
    const suffix = useRate ? '%' : ' 元';
    const x = summary.points.map((point) => point.date);
    const y = summary.points.map((point) => point[metric] == null ? null : point[metric] * (useRate ? 100 : 1));
    const segments = splitAtZero(x, y);
    const traces = [
      { sign: 1, color: '#ff949b', fill: 'rgba(255,148,155,.16)' },
      { sign: -1, color: '#78d7ad', fill: 'rgba(120,215,173,.16)' }
    ].map(({ sign, color, fill }) => ({
      type: 'scatter', mode: 'lines', x: segments.x,
      y: segments.y.map((value) => value != null && value * sign >= 0 ? value : null),
      line: { color, width: 2.4 }, fill: 'tozeroy', fillcolor: fill, connectgaps: false, hoverinfo: 'skip'
    }));
    traces.push({
      type: 'scatter', mode: 'markers', x, y, marker: { opacity: .001, size: 6 }, name: label,
      hovertemplate: `${label}：%{y:,.2f}${suffix}<extra></extra>`
    });
    const config = { responsive: true, displayModeBar: false };
    const key = bounds.join('/');
    await Plotly.react($('performance-chart'), traces, chartLayout(`${key}/${metric}`, useRate), config);
    if (request !== chartRequest) return;
    const flows = data.transfers.filter((transfer) => transfer.date >= bounds[0] && transfer.date <= bounds[1]);
    const assets = rows.map((row) => row.market_value == null ? null : Number(row.market_value));
    const marketValues = new Map(rows.map((row, index) => [row.date, assets[index]]));
    const assetsLabel = `${data.scopeLabel}收盘市值`;
    const assetsTraces = [{
      type: 'scatter', mode: 'lines', name: assetsLabel, x: rows.map((row) => row.date), y: assets,
      line: { color: '#e5aa75', width: 2.4 }, connectgaps: false,
      hovertemplate: `${assetsLabel}：%{y:,.2f} 元<extra></extra>`
    }];
    const totals = [];
    for (const [direction, title, color] of [['in', '转入', '#ff949b'], ['out', '转出', '#80dce3']]) {
      totals.push(`${title} ${formatter.format(flows.reduce((total, flow) => total + Number(flow[direction]), 0)).replace(/^\+/, '')} 元`);
      const visible = flows.filter((flow) => Number(flow[direction]) > 0 && marketValues.get(flow.date) != null);
      if (visible.length) {
        assetsTraces.push({
          type: 'scatter', mode: 'markers', x: visible.map((flow) => flow.date),
          y: visible.map((flow) => marketValues.get(flow.date)), customdata: visible.map((flow) => Number(flow[direction])),
          name: title, marker: { size: 8, color, symbol: direction === 'in' ? 'triangle-up' : 'triangle-down' },
          hovertemplate: `${title}：%{customdata:,.2f} 元<extra></extra>`
        });
      }
    }
    $('asset-flow-summary').textContent = `招商账户银证转账参考 · ${totals.join(' · ')}`;
    await Plotly.react($('performance-assets'), assetsTraces, chartLayout(key), config);
  }

  function showMetric(id, value, format) {
    $(id).textContent = value == null ? '—' : format(value);
    $(id).classList.toggle('gain', value > 0);
    $(id).classList.toggle('loss', value < 0);
  }

  async function renderRange() {
    if (!data) return;
    const kind = $('performance-range').value;
    let start = `${data.year}-01-01`;
    let end = data.as_of;
    $('performance-dates').hidden = kind !== 'custom';
    if (kind === 'month') start = `${data.as_of.slice(0, 7)}-01`;
    if (kind === 'all') start = data.start;
    if (kind === 'custom') {
      start = $('performance-start').value;
      end = $('performance-end').value;
      if (!start || !end || start > end || start < data.start || end > data.as_of) {
        $('performance-period').textContent = `请选择 ${data.start} 至 ${data.as_of} 内的有效日期，图表保留上次选择。`;
        return;
      }
    }
    const rows = data.records.filter((record) => record.date >= start && record.date <= end);
    $('performance-period').textContent = kind === 'all'
      ? `${start} 至 ${end} · 从本地最早流水起算` : `${start} 至 ${end}`;
    const summary = summarize(rows);
    $('performance-total-label').textContent = rows.length && summary.covered === rows.length ? '累计盈亏估算' : '已覆盖日期盈亏';
    $('performance-return-label').textContent = `收益率估算 · ${$('performance-return-mode').value === 'simple' ? '单利' : '复利'}`;
    showMetric('performance-total', summary.amount, money);
    showMetric('performance-return', summary.rate, rate);
    $('performance-positive').textContent = `${rows.filter((row) => row.pnl > 0).length}／${rows.filter((row) => row.pnl < 0).length}`;
    $('performance-covered').textContent = `${summary.covered}／${rows.length} 个交易日`;
    await drawCharts(rows, summary, [start, end]);
  }

  async function selectScope() {
    if (!report) return;
    const stocksOnly = $('performance-security-scope').value === 'stocks';
    const scopeLabel = stocksOnly ? 'A 股' : 'A 股及 ETF';
    data = { ...report, ...(stocksOnly ? report.stocks : {}), scopeLabel };
    byDate = new Map(data.records.map((row) => [row.date, row]));
    const status = $('performance-status');
    const blocked = data.blocked_dates.length > 0;
    status.hidden = !blocked;
    if (blocked) {
      status.textContent = `这些日期的成交或持仓需要核对：${data.blocked_dates.join('、')}`;
    }
    $('performance-scope').textContent = `${data.broker} · ${scopeLabel} · 截至 ${data.as_of}`;
    $('performance-assets-scope').textContent = `招商${scopeLabel}收盘市值 · 港股及现金未计入`;
    $('performance-basis').textContent = '日收益率按当日盈亏除以（前收证券市值＋当日买入资金）估算。单利累加日收益率，复利连乘日增长因子。' +
      (data.fee_gap_count ? `${data.fee_gap_count} 笔买卖缺费用或净交收金额，估算未计这部分费用。` : '') +
      (stocksOnly ? '股票股息和补税按券商流水。' : '股票股息和补税按券商流水。ETF 分红计入除息日，优先使用实际到账金额。');
    renderCalendar();
    await renderRange();
  }

  async function loadPerformance() {
    if (loading) return;
    if (report) {
      for (const id of ['performance-chart', 'performance-assets']) {
        if ($(id).data) window.Plotly.Plots.resize($(id));
      }
      return;
    }
    loading = true;
    const status = $('performance-status');
    status.hidden = false;
    status.textContent = '正在读取本地成交与行情。';
    try {
      const response = await fetch('/api/portfolio/performance');
      const result = await response.json();
      if (!response.ok || result.error) throw new Error(result.error || '无法读取盈亏数据');
      if (!result.records.length && !result.stocks.records.length) {
        const blocked = [...new Set([...result.blocked_dates, ...result.stocks.blocked_dates])];
        throw new Error(blocked.length ? `这些日期的成交或持仓需要核对：${blocked.join('、')}` : '暂无可复算的交易日');
      }
      report = result;
      for (const id of ['performance-start', 'performance-end']) {
        $(id).min = report.start;
        $(id).max = report.as_of;
      }
      $('performance-start').value = report.start;
      $('performance-end').value = report.as_of;
      const records = report.records.length ? report.records : report.stocks.records;
      const months = [...new Set(records.map((row) => row.date.slice(0, 7)))];
      const years = [...new Set(months.map((month) => month.slice(0, 4)))];
      $('calendar-month').replaceChildren(...months.map((month) => new Option(month, month)));
      $('calendar-month').value = months.at(-1);
      $('calendar-year').replaceChildren(...years.map((year) => new Option(year, year)));
      $('calendar-year').value = years.at(-1);
      if (!report.records.length) $('performance-security-scope').value = 'stocks';
      $('performance-data').hidden = false;
      await selectScope();
    } catch (error) {
      status.textContent = error.message;
      status.hidden = false;
    } finally {
      loading = false;
    }
  }

  for (const id of ['calendar-month', 'calendar-year', 'calendar-view', 'calendar-metric']) $(id).addEventListener('change', renderCalendar);
  $('performance-security-scope').addEventListener('change', () => selectScope().catch((error) => {
    $('performance-status').hidden = false;
    $('performance-status').textContent = error.message;
  }));
  for (const id of ['performance-range', 'performance-start', 'performance-end',
    'performance-return-mode', 'performance-chart-metric']) {
    $(id).addEventListener('change', () => {
      if (id === 'performance-return-mode') renderCalendar();
      renderRange().catch((error) => {
        $('performance-status').hidden = false;
        $('performance-status').textContent = error.message;
      });
    });
  }
  for (const [id, delta] of [['calendar-prev', -1], ['calendar-next', 1]]) {
    $(id).addEventListener('click', () => {
      $('calendar-month').selectedIndex += delta;
      renderCalendar();
    });
  }
  document.addEventListener('portfolio:performance', loadPerformance);
})();
