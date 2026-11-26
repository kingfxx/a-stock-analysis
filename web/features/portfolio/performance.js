(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const formatter = new Intl.NumberFormat('zh-CN', {
    signDisplay: 'exceptZero', minimumFractionDigits: 2, maximumFractionDigits: 2
  });
  const wholeYuanFormatter = new Intl.NumberFormat('zh-CN', {
    signDisplay: 'exceptZero', maximumFractionDigits: 0
  });
  const wholeYuan = (value) => `${wholeYuanFormatter.format(value)} 元`;
  const money = (value) => `${formatter.format(Number(value))} 元`;
  const balance = (value) => money(value).replace(/^\+/, '');
  const rate = (value) => `${formatter.format(Number(value) * 100)}%`;
  const dietzLabel = '单利（修正迪茨）';
  let data;
  let payload;
  let report;
  let byDate;
  let calendarMonths;
  let calendarYears;
  let selectedMonth;
  let pickerYearIndex;
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
    const daily = calendarRate(record.date, record.date);
    const dailyRate = daily == null ? '账户收益率待核对' : `当日账户收益率 ${rate(daily)}`;
    const estimated = Number(record.estimated_dividend) ? ` · 其中 ETF 公告估算 ${money(record.estimated_dividend)}` : '';
    const carried = carriedCloses(record);
    detail.textContent = `${record.date} · 估算盈亏 ${money(record.pnl)} · ${dailyRate} · 买卖 ${record.trades} 笔 · 股息及补税 ${money(record.dividend_net)}${estimated}${carried ? ` · ${carried}` : ''}`;
  }

  function summarize(rows) {
    let amount = 0;
    let covered = 0;
    const points = rows.map((row) => {
      if (row.pnl != null) {
        amount += Number(row.pnl);
        covered++;
      }
      return { date: row.date, amount: row.pnl == null ? null : amount };
    });
    return { amount: covered ? amount : null, points, covered };
  }

  const compoundReturn = () => $('performance-return-mode').value === 'compound';
  const returnLabel = () => compoundReturn() ? '复利（XIRR 年化）' : dietzLabel;
  const accountRates = (start, endpoints) =>
    window.PortfolioReturns.periodReturns(report, start, endpoints, compoundReturn());
  const calendarRate = (start, end) => window.PortfolioReturns.periodReturns(report, start, [end], false).get(end);

  function calendarPeriodRate(records) {
    if (!records.length) return null;
    const end = records.at(-1).date;
    return calendarRate(records[0].date, end);
  }

  function calendarMetric() {
    return $('calendar-metric').querySelector('[aria-pressed="true"]').value;
  }

  function selectCalendarOption(group, value) {
    for (const button of $(group).querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.value === value));
    }
    renderCalendar();
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
      const status = date < data.start || date > data.as_of ? '未覆盖' : '休市';
      cell.className = 'calendar-no-result';
      cell.append(number, element('small', status));
      return cell;
    }
    const button = element('button');
    button.type = 'button';
    const useRate = calendarMetric() === 'rate';
    const value = useRate ? calendarRate(date, date) : record.pnl;
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
    const yearly = $('calendar-view').querySelector('[aria-pressed="true"]').value === 'year';
    $('calendar-year-control').hidden = !yearly;
    $('calendar-month-controls').hidden = $('calendar-month-grid').hidden = yearly;
    $('calendar-year-grid').hidden = !yearly;
    renderSecurityBreakdown();
    if (yearly) {
      renderYearCalendar();
      return;
    }
    const month = selectedMonth;
    const monthIndex = calendarMonths.indexOf(month);
    const [year, numericMonth] = month.split('-').map(Number);
    const monthRecords = data.records.filter((row) => row.date.startsWith(month));
    setCalendarSummary(monthRecords);
    const monthButton = $('calendar-month');
    monthButton.textContent = month;
    monthButton.setAttribute('aria-label', `选择月份，当前 ${year} 年 ${numericMonth} 月`);
    $('calendar-prev').disabled = monthIndex === 0;
    $('calendar-next').disabled = monthIndex === calendarMonths.length - 1;
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

  function renderMonthPicker() {
    const year = calendarYears[pickerYearIndex];
    $('calendar-picker-year').textContent = `${year} 年`;
    $('calendar-picker-prev').disabled = pickerYearIndex === 0;
    $('calendar-picker-next').disabled = pickerYearIndex === calendarYears.length - 1;
    const buttons = [];
    for (let month = 1; month <= 12; month++) {
      const key = `${year}-${String(month).padStart(2, '0')}`;
      const button = element('button', `${month} 月`);
      button.type = 'button';
      button.disabled = !calendarMonths.includes(key);
      button.setAttribute('aria-pressed', String(key === selectedMonth));
      button.addEventListener('click', () => {
        selectedMonth = key;
        renderCalendar();
        $('calendar-month-picker').close();
      });
      buttons.push(button);
    }
    $('calendar-picker-months').replaceChildren(...buttons);
  }

  function setCalendarSummary(records) {
    showMetric('calendar-total-pnl', summarize(records).amount, money);
    showMetric('calendar-total-rate', calendarPeriodRate(records), rate);
  }

  function renderSecurityBreakdown() {
    if (!$('calendar-securities').open || !data) return;
    const yearly = $('calendar-view').querySelector('[aria-pressed="true"]').value === 'year';
    const period = yearly ? $('calendar-year').value : selectedMonth;
    const totals = new Map();
    for (const [month, amounts] of Object.entries(data.monthly_security_pnl || {})) {
      if (yearly ? !month.startsWith(`${period}-`) : month !== period) continue;
      for (const [security, cents] of Object.entries(amounts)) {
        totals.set(security, (totals.get(security) || 0) + cents);
      }
    }
    const records = data.records.filter(({date}) => date.startsWith(period));
    const covered = records.filter(({pnl}) => pnl != null).length;
    $('calendar-security-period').textContent = `${period}${yearly ? ' 年' : ''} · ${covered}/${records.length} 个交易日`;
    for (const [id, sign, cssClass] of [['calendar-gainers', 1, 'gain'], ['calendar-losers', -1, 'loss']]) {
      const securities = [...totals].filter(([, cents]) => cents * sign > 0)
        .sort((left, right) => sign * (right[1] - left[1]) || left[0].localeCompare(right[0]));
      const items = securities.map(([security, cents]) => {
        const code = security.split(':')[1];
        const name = payload.security_names?.[security];
        const label = element('span', name ? `${code} ${name}` : code);
        label.className = 'calendar-security-name';
        const amount = element('strong', money(cents / 100));
        amount.className = `calendar-security-amount ${cssClass}`;
        const item = element('li');
        item.append(label, amount);
        return item;
      });
      $(id).replaceChildren(...(items.length ? items : [element('li', covered ? '暂无记录' : '暂无可复算数据')]));
    }
  }

  function renderYearCalendar() {
    const select = $('calendar-year');
    const year = select.value;
    $('calendar-year-prev').disabled = select.selectedIndex === 0;
    $('calendar-year-next').disabled = select.selectedIndex === select.options.length - 1;
    const yearRecords = data.records.filter((row) => row.date.startsWith(`${year}-`));
    setCalendarSummary(yearRecords);
    $('calendar-day-detail').textContent = '选择月份查看交易日明细。';
    const cards = [];
    for (let month = 1; month <= 12; month++) {
      const key = `${year}-${String(month).padStart(2, '0')}`;
      const records = yearRecords.filter((row) => row.date.startsWith(key));
      const summary = summarize(records);
      const value = calendarMetric() === 'rate' ? calendarPeriodRate(records) : summary.amount;
      const card = element('button');
      card.type = 'button';
      card.className = calendarClass(value);
      card.disabled = !records.length;
      card.append(element('span', `${month} 月`), element('strong', records.length ? calendarText(value) : '未覆盖'));
      card.title = records.length ? `已覆盖 ${summary.covered}/${records.length} 个交易日` : '未覆盖';
      card.addEventListener('click', () => {
        selectedMonth = key;
        selectCalendarOption('calendar-view', 'month');
        $('calendar-view').querySelector('[aria-pressed="true"]').focus();
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
    const theme = getComputedStyle(document.documentElement);
    const gridColor = theme.getPropertyValue('--line').trim();
    return {
      uirevision: key, font: { color: theme.getPropertyValue('--muted').trim(), size: 12 },
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      showlegend: false,
      margin: { l: 70, r: 18, t: 18, b: 38 }, hovermode: 'x unified',
      hoverlabel: { bgcolor: '#23343d', bordercolor: '#607984', align: 'left',
        font: { color: '#f4f8fa', size: 14, family: 'Microsoft YaHei UI, Arial, sans-serif' } },
      xaxis: { type: 'date', tickformat: '%Y-%m', hoverformat: '%Y-%m-%d', gridcolor: gridColor },
      yaxis: { tickformat: percent ? '.1f' : ',.0f', ticksuffix: percent ? '%' : ' 元',
        gridcolor: gridColor, zerolinecolor: '#607985' }
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

  async function drawCharts(rows, summary, bounds, rates, flows) {
    const request = ++chartRequest;
    const useRate = $('performance-chart-metric').value === 'rate';
    const Plotly = await loadPlotly();
    if (request !== chartRequest) return;
    const metric = useRate ? $('performance-return-mode').value : 'amount';
    const label = useRate ? returnLabel() : summary.covered < rows.length ? '已覆盖累计盈亏' : '累计盈亏';
    const suffix = useRate ? '%' : ' 元';
    const x = summary.points.map((point) => point.date);
    const y = summary.points.map((point) => {
      const value = useRate ? rates.get(point.date) : point.amount;
      return value == null ? null : value * (useRate ? 100 : 1);
    });
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
    const assetCount = rows.filter((row) => row.total_assets != null).length;
    const assetLabel = assetCount ? '总资产估算' : '证券市值（现金待核对）';
    const amounts = rows.map((row) => {
      const amount = assetCount ? row.total_assets : row.market_value;
      return amount == null ? null : Number(amount);
    });
    const assetValues = new Map(rows.map((row, index) => [row.date, amounts[index]]));
    const assetHover = assetCount
      ? '总资产估算：%{y:,.2f} 元<br>证券市值：%{customdata[0]:,.2f} 元<br>账户现金：%{customdata[1]:,.2f} 元'
      : '证券市值：%{y:,.2f} 元';
    const assetsTraces = [{
      type: 'scatter', mode: 'lines', name: assetLabel, x: rows.map((row) => row.date), y: amounts,
      customdata: rows.map((row) => [row.market_value == null ? null : Number(row.market_value),
        row.cash_balance == null ? null : Number(row.cash_balance)]),
      line: { color: '#e5aa75', width: 2.4 }, connectgaps: false,
      hovertemplate: assetHover + '<extra></extra>'
    }];
    for (const [direction, title, color] of [['in', '转入', '#ff949b'], ['out', '转出', '#80dce3']]) {
      const visible = flows.filter((flow) => Number(flow[direction]) > 0 && assetValues.get(flow.date) != null);
      if (visible.length) {
        assetsTraces.push({
          type: 'scatter', mode: 'markers', x: visible.map((flow) => flow.date),
          y: visible.map((flow) => assetValues.get(flow.date)), customdata: visible.map((flow) => Number(flow[direction])),
          name: title, marker: { size: 8, color, symbol: direction === 'in' ? 'triangle-up' : 'triangle-down' },
          hovertemplate: `${title}：%{customdata:,.2f} 元<extra></extra>`
        });
      }
    }
    const negativeCash = rows.filter((row) => row.cash_balance != null && Number(row.cash_balance) < 0).length;
    $('asset-cash-summary').textContent = [
      `总资产估算覆盖 ${assetCount}/${rows.length} 个交易日`,
      negativeCash ? `${negativeCash} 天现金倒推为负，待对账` : '',
      !assetCount && rows.length ? '曲线显示已知证券市值' : ''
    ].filter(Boolean).join(' · ');
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
    $('performance-year-control').hidden = kind !== 'by-year';
    if (kind === 'month') start = `${data.as_of.slice(0, 7)}-01`;
    if (kind === 'all') start = data.start;
    if (kind === 'by-year') {
      const year = $('performance-year').value;
      start = `${year}-01-01`;
      end = `${year}-12-31` < data.as_of ? `${year}-12-31` : data.as_of;
    }
    if (start < data.start) start = data.start;
    if (kind === 'custom') {
      start = $('performance-start').value;
      end = $('performance-end').value;
      if (!start || !end || start > end || start < data.start || end > data.as_of) {
        $('performance-period').textContent = `请选择 ${data.start} 至 ${data.as_of} 内的有效日期，图表保留上次选择。`;
        return;
      }
    }
    const rows = data.records.filter((record) => record.date >= start && record.date <= end);
    $('performance-period').textContent = `${start} 至 ${end}`;
    const summary = summarize(rows);
    const closing = rows.at(-1);
    const last = closing?.date;
    const endpoints = $('performance-chart-metric').value === 'rate' ? rows.map((row) => row.date) : last ? [last] : [];
    const rates = accountRates(start, endpoints);
    $('performance-total-label').textContent = rows.length && summary.covered === rows.length ? '累计盈亏估算' : '已覆盖日期盈亏';
    $('performance-return-label').textContent = returnLabel();
    showMetric('performance-total', summary.amount, wholeYuan);
    showMetric('performance-return', rates.get(last), rate);
    $('performance-positive').textContent = `${rows.filter((row) => row.pnl > 0).length}／${rows.filter((row) => row.pnl < 0).length}`;
    $('performance-covered').textContent = `${summary.covered}／${rows.length} 个交易日`;
    const flows = data.transfers.filter(({date}) => date >= start && date <= end);
    const deposits = flows.reduce((total, flow) => total + Number(flow.in), 0);
    const withdrawals = flows.reduce((total, flow) => total + Number(flow.out), 0);
    $('performance-net-investment').textContent = wholeYuan(deposits - withdrawals).replace(/^\+/, '');
    const dividend = rows.length && rows.every((row) => row.dividend_net != null)
      ? rows.reduce((total, row) => total + Math.round(Number(row.dividend_net) * 100), 0) / 100 : null;
    $('performance-dividend-income').textContent = dividend == null ? '—' : wholeYuan(dividend).replace(/^\+/, '');
    const accountMarketValue = Number(report.records.findLast((row) => row.date === last)?.market_value);
    $('performance-dividend-yield').textContent = dividend != null && accountMarketValue > 0
      ? rate(dividend / accountMarketValue).replace(/^\+/, '') : '—';
    $('asset-flow-summary').textContent = `已导入银证转账参考 · 转入 ${balance(deposits)} · 转出 ${balance(withdrawals)}`;
    $('asset-card-date').textContent = last ? `卡片金额取 ${last} 收盘估算。` : '所选区间无交易日。';
    for (const [id, value] of [
      ['asset-total', closing?.total_assets],
      ['asset-market-value', closing?.market_value],
      ['asset-cash', closing?.cash_balance]
    ]) {
      $(id).textContent = value == null ? '—' : balance(value);
    }
    await drawCharts(rows, summary, [start, end], rates, flows);
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
    $('performance-assets-scope').textContent = `${data.broker} · ${scopeLabel}收盘市值＋账户人民币现金估算`;
    $('performance-basis').textContent = (report === payload.combined ? '合并账户汇总各券商的总资产及银证净流量。' : '') +
      '累计证券盈亏为逐日盈亏金额之和。' +
      (data.fee_gap_count ? `${data.fee_gap_count} 笔买卖缺费用或净交收金额，估算未计这部分费用。` : '') +
      (stocksOnly ? '股票股息和补税按券商流水。' : '股票股息和补税按券商流水。ETF 分红计入除息日，优先使用实际到账金额。');
    $('performance-opening-note').textContent = report.opening_assumed_zero
      ? '开户至今的起点按首笔已录净入金前资产为零估算。' : '';
    renderCalendar();
    await renderRange();
  }

  async function selectAccount() {
    const selected = $('performance-account').value;
    const combined = selected === 'combined';
    const account = payload.accounts.find(({broker}) => broker === selected);
    report = combined ? payload.combined : [payload, ...payload.other_accounts].find(({broker}) => broker === selected);
    chartRequest++;
    const coverage = $('performance-account-coverage');
    if (account) {
      const dates = account.trade_count ? `（${account.first_trade} 至 ${account.last_trade}）` : '';
      coverage.textContent = `已导入买卖 ${account.trade_count} 笔${dates} · 有效银证转账 ${account.transfer_count} 笔 · ` +
        (account.snapshot_date ? `最近收盘快照 ${account.snapshot_date}` : '缺少收盘快照');
    } else {
      const brokers = payload.accounts.filter(({snapshot_date}) => snapshot_date).map(({broker}) => broker).join('、');
      const range = report.start ? `历史范围 ${report.start} 至 ${report.as_of}` : '';
      const missing = report.missing_accounts?.length ? `缺收盘快照：${report.missing_accounts.join('、')}` : '';
      coverage.textContent = [brokers, range, missing].filter(Boolean).join(' · ');
    }
    const status = $('performance-status');
    $('performance-scope').textContent = report.broker;
    const records = report.records.length ? report.records : report.stocks?.records;
    if (!records?.length) {
      data = null;
      $('performance-data').hidden = true;
      status.hidden = false;
      const blocked = [...new Set([...(report.blocked_dates ?? []), ...(report.stocks?.blocked_dates ?? [])])];
      const reason = report.error || (blocked.length ? `这些日期的成交或持仓需要核对：${blocked.join('、')}` : '暂无可复算的交易日');
      status.textContent = [reason, coverage.textContent].filter(Boolean).join(' · ');
      return;
    }
    for (const [id, day] of [['performance-start', report.start], ['performance-end', report.as_of]]) {
      $(id).min = report.start;
      $(id).max = report.as_of;
      $(id).value = day;
    }
    calendarMonths = [...new Set(records.map((row) => row.date.slice(0, 7)))];
    calendarYears = [...new Set(calendarMonths.map((month) => month.slice(0, 4)))];
    const yearSelect = $('performance-year');
    const previousYear = yearSelect.value;
    yearSelect.replaceChildren(...calendarYears.toReversed().map((year) => new Option(year, year)));
    yearSelect.value = calendarYears.includes(previousYear) ? previousYear : calendarYears.at(-1);
    selectedMonth = calendarMonths.at(-1);
    $('calendar-year').replaceChildren(...calendarYears.map((year) => new Option(year, year)));
    $('calendar-year').value = calendarYears.at(-1);
    if (!report.records.length) $('performance-security-scope').value = 'stocks';
    $('performance-data').hidden = false;
    await selectScope();
  }

  async function loadPerformance() {
    if (loading) return;
    if (payload) {
      for (const id of ['performance-chart', 'performance-assets']) {
        if (!$('performance-data').hidden && $(id).data) window.Plotly.Plots.resize($(id));
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
      if (!response.ok) throw new Error(result.error || '无法读取盈亏数据');
      payload = result;
      $('performance-account').replaceChildren(new Option(result.combined.broker, 'combined', true, true),
        ...result.accounts.map(({broker}) => new Option(broker, broker)));
      $('performance-account-control').hidden = !result.accounts.length;
      $('performance-range').value = 'all';
      await selectAccount();
    } catch (error) {
      status.textContent = error.message;
      status.hidden = false;
    } finally {
      loading = false;
    }
  }

  $('performance-account').addEventListener('change', () => selectAccount().catch((error) => {
    $('performance-status').hidden = false;
    $('performance-status').textContent = error.message;
  }));
  $('calendar-year').addEventListener('change', renderCalendar);
  $('calendar-securities').addEventListener('toggle', renderSecurityBreakdown);
  for (const id of ['calendar-view', 'calendar-metric']) {
    for (const button of $(id).querySelectorAll('button')) {
      button.addEventListener('click', () => {
        if (button.getAttribute('aria-pressed') === 'false') selectCalendarOption(id, button.value);
      });
    }
  }
  for (const [id, delta] of [['calendar-year-prev', -1], ['calendar-year-next', 1]]) {
    $(id).addEventListener('click', () => {
      $('calendar-year').selectedIndex += delta;
      renderCalendar();
      if ($(id).disabled) $('calendar-year').focus();
    });
  }
  $('calendar-month').addEventListener('click', () => {
    pickerYearIndex = calendarYears.indexOf(selectedMonth.slice(0, 4));
    renderMonthPicker();
    $('calendar-month-picker').showModal();
    $('calendar-picker-months').querySelector('[aria-pressed="true"]').focus();
  });
  for (const [id, delta] of [['calendar-picker-prev', -1], ['calendar-picker-next', 1]]) {
    $(id).addEventListener('click', () => {
      pickerYearIndex += delta;
      renderMonthPicker();
      if ($(id).disabled) $('calendar-picker-months').querySelector('button:enabled').focus();
    });
  }
  $('performance-security-scope').addEventListener('change', () => selectScope().catch((error) => {
    $('performance-status').hidden = false;
    $('performance-status').textContent = error.message;
  }));
  for (const id of ['performance-range', 'performance-year', 'performance-start', 'performance-end',
    'performance-return-mode', 'performance-chart-metric']) {
    $(id).addEventListener('change', () => {
      renderRange().catch((error) => {
        $('performance-status').hidden = false;
        $('performance-status').textContent = error.message;
      });
    });
  }
  for (const [id, delta] of [['calendar-prev', -1], ['calendar-next', 1]]) {
    $(id).addEventListener('click', () => {
      selectedMonth = calendarMonths[calendarMonths.indexOf(selectedMonth) + delta];
      renderCalendar();
    });
  }
  document.addEventListener('portfolio:performance', loadPerformance);
  document.addEventListener('portfolio:ledger-changed', () => {
    payload = report = data = null;
    chartRequest++;
    $('performance-data').hidden = true;
    $('performance-account-control').hidden = true;
    $('performance-account-coverage').hidden = true;
  });
})();
