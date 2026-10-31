/* Adapter for the existing enterprise payload and session selections. */
window.initEnterpriseTrend = function({state, savedSelections, saveSelections}) {
  const metrics = {
    revenue: {name:'营业收入', unit:'亿元', scale:1e8},
    profit: {name:'归母净利润', unit:'亿元', scale:1e8},
    adjusted_profit: {name:'归母扣非净利润', unit:'亿元', scale:1e8},
    net_assets: {name:'净资产（所有者权益合计）', unit:'亿元', scale:1e8},
    revenue_growth: {name:'营收同比增长率', unit:'%', scale:1},
    profit_growth: {name:'归母净利润同比增长率', unit:'%', scale:1},
    roe: {name:'ROE（TTM 估算）', unit:'%', scale:1},
    roic: {name:'ROIC（TTM，简化估算）', unit:'%', scale:1},
    operating_cash_flow: {name:'经营活动现金流净额', unit:'亿元', scale:1e8},
    main_business_cash_flow: {name:'主营现金流', unit:'亿元', scale:1e8},
    free_cash_flow: {name:'简化自由现金流', unit:'亿元', scale:1e8},
    cash_dividend: {name:'估算现金分红（已实施）', unit:'亿元', scale:1e8},
    gross_margin: {name:'毛利率', unit:'%', scale:1},
    net_margin: {name:'净利率', unit:'%', scale:1},
    capex: {name:'资本开支 CapEx', unit:'亿元', scale:1e8},
    interest_bearing_debt: {name:'有息负债（简化）', unit:'亿元', scale:1e8},
    net_cash: {name:'净现金／净负债', unit:'亿元', scale:1e8},
    period_end_market_cap: {name:'季末市值', unit:'亿元', scale:1e8, dateKey:'period_end_price_date'},
    market_cap: {name:'披露日估算市值', unit:'亿元', scale:1e8, dateKey:'price_date', referenceKey:'market_cap_reference'},
    qfq_price: {name:'披露日前复权股价', unit:'元/股', scale:1, dateKey:'qfq_price_date', referenceKey:'qfq_price_reference'}
  };
  const disclosureSourceLabel = value => ({manual:'人工覆盖',cninfo_original:'巨潮原始正式报告',
    cninfo:'巨潮冲突候选',sina_unverified:'新浪校正日期（未核验）',none:'无可用日期'})[value] || '未记录';
  const disclosureStatusLabel = value => ({matched_original:'已匹配原始报告',conflict_retained:'候选冲突，保留原日期',
    pending:'待核验',failed:'同步失败',sina_unverified:'新浪日期未核验',manual_override:'人工确认'})[value] || '未记录';
  let selectedPeriod = ['quarter','ytd','year','ttm'].includes(savedSelections.period) ? savedSelections.period : 'quarter';
  const comparisonRange = document.getElementById('comparison-range');
  comparisonRange.value = ['all','10','5'].includes(savedSelections.comparisonRange) ? savedSelections.comparisonRange : '10';
  const seriesControls = [
    {id:'bar-metric', defaultKind:'bar', label:'A', color:'#38c4dc'},
    {id:'bar-metric-2', defaultKind:null, legacyKind:'bar', label:'B', color:'#b9a0f5'},
    {id:'line-metric', defaultKind:'line', label:'C', color:'#f4d35e'}
  ].map(series => ({...series, control:document.getElementById(series.id),
    buttons:[...document.getElementById(series.id).parentElement.querySelectorAll('[data-series-mode]')]}));
  const modeOf = series => series.buttons.find(button => button.getAttribute('aria-pressed') === 'true')?.dataset.seriesMode || null;
  for (const series of seriesControls) {
    const metric = savedSelections.metrics?.[series.id];
    if ([...series.control.options].some(option => option.value === metric)) series.control.value = metric;
    const savedMode = savedSelections.display?.[series.id];
    const visible = savedSelections.visible?.[series.id];
    const mode = ['bar','line'].includes(savedMode) ? savedMode : savedMode === null ? null
      : typeof visible === 'boolean' ? (visible ? series.legacyKind || series.defaultKind : null) : series.defaultKind;
    series.buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.seriesMode === mode)));
  }
  document.querySelectorAll('#trend-controls-mount [data-period]').forEach(button =>
    button.classList.toggle('active', button.dataset.period === selectedPeriod));
  const trendElements = Object.fromEntries(Object.entries({scroll:'chart-scroll',hint:'chart-scroll-hint',
    title:'chart-title',subtitle:'chart-subtitle',chart:'chart',head:'trend-history-head',
    body:'trend-history-body',footer:'trend-history-footer',caption:'trend-history-caption'})
    .map(([key,id]) => [key, document.getElementById(id)]));
  const note = document.getElementById('chart-note');
  function label(row) {
    if (selectedPeriod === 'year') return row.period.slice(0,4);
    return row.period.slice(0,4) + ' Q' + ({'03':1,'06':2,'09':3,'12':4}[row.period.slice(5,7)] || '?');
  }
  function positions(rows) {
    let yearGap = 0;
    return rows.map((row, index) => {
      if (['quarter','ytd'].includes(selectedPeriod) && index && row.period.slice(0,4) !== rows[index - 1].period.slice(0,4)) yearGap += .48;
      return index + yearGap;
    });
  }
  function render() {
    document.getElementById('shipping-forecast-entry').hidden = state.code !== '601919';
    metrics.roic.name = selectedPeriod === 'year' ? 'ROIC（年度，简化估算）' : 'ROIC（TTM，简化估算）';
    const cutoff = new Date();
    cutoff.setHours(0, 0, 0, 0);
    if (comparisonRange.value !== 'all') cutoff.setFullYear(cutoff.getFullYear() - Number(comparisonRange.value));
    const allRows = state.views?.[selectedPeriod] || [];
    const rangedRows = allRows.filter(row =>
      comparisonRange.value === 'all' || new Date(row.period + 'T00:00:00') >= cutoff);
    const activeControls = seriesControls.filter(series => modeOf(series)).map(series => ({
      ...series, kind:modeOf(series), key:series.control.value, metric:metrics[series.control.value]
    }));
    const rows = activeControls.length ? rangedRows.filter(row => activeControls.some(series =>
      row[series.key] != null || (series.metric.referenceKey && row[series.metric.referenceKey] != null))) : rangedRows;
    const labels = rows.map(label);
    const xs = positions(rows);
    const active = activeControls.map(series => ({
      ...series,
      values:rows.map(row => row[series.control.value] == null ? null : row[series.control.value] / metrics[series.control.value].scale)
    }));
    const {referenceCount, showQoq, comparisonText, periodOffsets} = TrendView.draw({
      elements:trendElements, rows, allRows, active, selectedPeriod, metrics, label, positions,
      isPriceOrCap:key => ['qfq_price','market_cap'].includes(key),
      hasMarketComparisons:key => ['qfq_price','market_cap','period_end_market_cap'].includes(key)
    });
    const selectedMetrics = new Set(active.map(series => series.key));
    const hasFinancial = [...selectedMetrics].some(metric => !['market_cap','period_end_market_cap','qfq_price','interest_bearing_debt','net_cash','cash_dividend'].includes(metric));
    const hasBalance = selectedMetrics.has('interest_bearing_debt') || selectedMetrics.has('net_cash');
    const hasMarket = selectedMetrics.has('market_cap') || selectedMetrics.has('qfq_price');
    const hasNegativeAdjusted = selectedMetrics.has('qfq_price') && rows.some(row => row.qfq_price != null && row.qfq_price < 0);
    const missingNote = active.map(series => {
      const missing = series.values.filter(value => value == null).length;
      return missing ? ' ' + series.label + '有 ' + missing + ' 个正式值缺失。' : '';
    }).join('');
    note.textContent = (!active.length ? '请选择柱形或折线，显示至少一个指标。' : '') +
      (hasFinancial ? (selectedPeriod === 'quarter' ? '单季度财务值由当年累计报表差分得到。' : selectedPeriod === 'ytd' ? '当年累计直接取财报年初至报告期末的累计金额：Q1、Q1+Q2、Q1+Q2+Q3、全年；同比比较去年相同累计期间，财务指标仅显示同比；股价和市值仍按原有时点取值并显示同比、环比。' : selectedPeriod === 'ttm' ? '财务 TTM 为连续四个单季度之和；缺期则留空。' : '年度财务值取当年年报累计数。') : '') +
      (hasMarket ? (hasFinancial ? ' ' : '') + '市场指标正式值取对应报告披露日附近的价格快照。' : '') +
      (selectedMetrics.has('period_end_market_cap') ? ' 季末市值＝季末当日或此前最近交易日的不复权收盘价×该报告期末总股本；价格相隔超过 15 天或股本缺失时留空。年度显示年末值，TTM 保留各季末时点值，不加总。' : '') +
      (selectedMetrics.has('market_cap') ? ' 估算市值按披露日前最近未复权收盘价×报告期末股本计算。' : '') +
      (selectedMetrics.has('qfq_price') ? ' 前复权价格采用数据源当前调整后的历史序列。' : '') +
      (hasNegativeAdjusted ? ' 部分历史前复权值为负，图中保留数据源原值；它不是当时的未复权成交价。' : '') +
      ([...selectedMetrics].some(metric => metric.endsWith('_growth')) ? ' 同比增长率按去年相同报告期、相同周期口径比较；基期小于等于零或缺失时留空。' : '') +
      (selectedMetrics.has('roe') ? ' ROE（TTM 估算）＝连续四季度归母净利润÷本期与上年同期归母净资产均值；非官方加权平均 ROE。' : '') +
      (selectedMetrics.has('roic') ? ' ROIC（简化估算）＝税后经营利润÷期初期末投入资本均值；年度用全年利润，单季度、当年累计与 TTM 视图均用连续四季度利润及上年同期、本期资本。税后经营利润＝（利润总额＋费用化利息－财务费用项下利息收入）×（1－所得税费用÷利润总额）；投入资本＝合并所有者权益＋简化有息负债－全部货币资金，尚未剔除其他非经营性损益。关键字段缺失、利润总额非正、税率异常或平均资本非正时留空。' : '') +
      (selectedMetrics.has('operating_cash_flow') || selectedMetrics.has('free_cash_flow') ? ' 经营现金流来自新浪现金流量表；单季度由累计值差分。简化自由现金流＝经营现金流－购建长期资产支付的现金，不能视为精确可分配现金。' : '') +
      (selectedMetrics.has('gross_margin') ? ' 毛利率＝（营业收入－营业成本）÷营业收入；按所选周期的收入、成本计算。' : '') +
      (selectedMetrics.has('net_margin') ? ' 净利率＝合并净利润÷营业收入，并非归母净利率；按所选周期计算。' : '') +
      (selectedMetrics.has('main_business_cash_flow') ? ' 主营现金流取现金流量表“销售商品、提供劳务收到的现金”（LABORGETCASH），为现金流入金额；单季由累计值差分，当年累计取报告期原值，年度取全年累计，TTM 为连续四季合计。' : '') +
      (selectedMetrics.has('capex') ? ' CapEx 取现金流量表“购建固定资产、无形资产和其他长期资产所支付的现金”；单季由累计值差分。' : '') +
      (selectedMetrics.has('cash_dividend') ? ' 估算现金分红仅计已实施记录，按归属报告期而非实际到账日放置；金额＝税前每股派息×数据源总股本，未扣除不参与分红的回购股等，因此并非公告实付总额。0 表示截至当前缓存暂无该期已实施分红。' : '') +
      (selectedMetrics.has('cash_dividend') && selectedPeriod === 'year' ? ' 分红率＝该年度已实施的估算现金分红÷全年归母净利润；按分红归属年度匹配，后续实施分红会影响该值，净利润非正或数据缺失时无法计算。' : '') +
      (hasBalance ? ' 有息负债（简化）＝短期借款＋应付短期债券＋一年内到期的非流动负债＋长期借款＋应付债券＋租赁负债；净现金＝报告期末货币资金－有息负债，正值为净现金，负值为净负债。均为期末时点值，不随单季／当年累计／年度／TTM 切换而加总；货币资金可能含受限资金，债务科目亦可能含非有息部分。' : '') +
      (referenceCount ? ' 空心圆点为距披露日 16–180 天的最近交易价参考点；与相邻价格点用虚线连接，正常价格点之间仍用实线。' : '') +
      missingNote;
    const disclosureAudit = document.getElementById('disclosure-audit');
    const disclosureAuditRows = document.getElementById('disclosure-audit-rows');
    const auditedRows = rangedRows.filter(row => row.publish_date_source || row.publish_date_status ||
      row.publish_date_reason || row.latest_revision_on);
    disclosureAuditRows.replaceChildren();
    disclosureAudit.hidden = !auditedRows.length;
    for (const row of [...auditedRows].reverse()) {
      const item = document.createElement('div'); item.className = 'disclosure-audit-row';
      const heading = document.createElement('strong'); heading.textContent = row.period + ' · 披露日 ' + (row.publish_date || '未记录'); item.append(heading);
      const sourceStatus = document.createElement('p');
      sourceStatus.textContent = '日期来源：' + disclosureSourceLabel(row.publish_date_source) + '；核验状态：' + disclosureStatusLabel(row.publish_date_status);
      item.append(sourceStatus);
      if (row.latest_revision_on) { const revision = document.createElement('p'); revision.textContent = '最近已知修订：' + row.latest_revision_on; item.append(revision); }
      if (row.publish_date_reason) { const reason = document.createElement('p'); reason.textContent = '说明：' + row.publish_date_reason; item.append(reason); }
      disclosureAuditRows.append(item);
    }
    TrendView.history({elements:trendElements, rows, active, selectedPeriod, label, showQoq, comparisonText, periodOffsets});
  }
  seriesControls.forEach(series => {
    series.control.addEventListener('change', () => { saveSelections(); render(); });
  });
  TrendView.bindShapes(document.getElementById('trend-controls-mount'), () => { saveSelections(); render(); });
  document.querySelectorAll('#trend-controls-mount [data-period]').forEach(button => button.addEventListener('click', () => {
    selectedPeriod = button.dataset.period;
    document.querySelectorAll('#trend-controls-mount [data-period]').forEach(item => item.classList.toggle('active', item === button));
    saveSelections();
    render();
  }));
  comparisonRange.addEventListener('change', () => { saveSelections(); render(); });
  return {render, selections: () => ({
    period:selectedPeriod, comparisonRange:comparisonRange.value,
    metrics:Object.fromEntries(seriesControls.map(series => [series.id, series.control.value])),
    display:Object.fromEntries(seriesControls.map(series => [series.id, modeOf(series)]))
  })};
};
