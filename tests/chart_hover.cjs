const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Exercise the real renderer, capturing the Plotly traces at its boundary.
const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const metricCode = page.slice(page.indexOf('  const metrics = {'), page.indexOf('  let selectedPeriod ='));
const renderCode = page.slice(page.indexOf('  function label(row)'), page.indexOf('  seriesControls.forEach(series => {'));

for (const period of ['quarter', 'year', 'ttm']) {
  for (const kind of ['bar', 'line']) {
    for (const key of ['profit', 'cash_dividend', 'qfq_price', 'market_cap', 'profit_growth', 'gross_margin', 'roic']) {
      const elements = new Map();
      const context = {
        state: {views: {[period]: [{period:'2025-12-31', publish_date:'2026-04-30',
          profit:100e8, cash_dividend:10e8, qfq_price:20, market_cap:500e8,
          profit_growth:20, gross_margin:30, roic:17.28, cash_dividend_details:'2025-12-31 → 2026-06-05',
          yoy: {profit:'同比 +20.00%', cash_dividend:'同比 +10.00%（仅已实施口径）'}}]}},
        selectedPeriod: period, comparisonRange: {value:'all'},
        seriesControls: [{label:'A', color:'#38c4dc', kind, control:{value:key}}],
        modeOf: series => series.kind, note: {},
        document: {getElementById: id => {
          if (!elements.has(id)) elements.set(id, {classList:{toggle() {}}, textContent:''});
          return elements.get(id);
        }},
        Plotly: {react: (id, traces) => { context.traces = traces; }}
      };
      vm.createContext(context);
      vm.runInContext(metricCode + renderCode + '\nrender();', context);
      const trace = context.traces[0];
      const hover = trace.hovertemplate.replace(/%\{customdata\[(\d+)\]\}/g,
        (_, index) => trace.customdata[0][index]);
      if (key === 'profit') assert.match(hover, /亿元；同比 \+20\.00%/);
      else if (key === 'cash_dividend') {
        assert.match(hover, /亿元；同比 \+10\.00%（仅已实施口径）/);
        assert.match(hover, /2025-12-31 → 2026-06-05/);
      } else assert.doesNotMatch(hover, /；同比/);
      if (key === 'roic') {
        assert.match(hover, new RegExp(period === 'year' ? 'ROIC（年度，简化估算）' : 'ROIC（TTM，简化估算）'));
        assert.equal(trace.y[0], 17.28);
        assert.match(context.note.textContent, /全部货币资金/);
      }
      assert.match(hover, /报告期：2025-12-31/);
      assert.match(hover, /披露日：2026-04-30/);
    }
  }
}
console.log('42 chart hover combinations passed');
