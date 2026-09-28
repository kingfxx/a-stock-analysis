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
          profit:100e8, cash_dividend:10e8, dividend_payout_ratio:10, qfq_price:20, market_cap:500e8,
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
      if (key === 'cash_dividend' && period === 'year') {
        assert.match(hover, /分红率：10\.00%（仅已实施口径）/);
        assert.match(context.note.textContent, /全年归母净利润/);
        for (const [profit, dividend, ratio, expected] of [
          [100e8, 0, 0, '分红率：0.00%'],
          [100e8, 150e8, 150, '分红率：150.00%'],
          [-10e8, 10e8, null, '分红率：—（归母净利润非正）'],
          [0, 10e8, null, '分红率：—（归母净利润非正）'],
          [null, 10e8, null, '分红率：—（数据缺失）'],
          [100e8, null, null, '分红率：—（数据缺失）'],
        ]) {
          Object.assign(context.state.views.year[0], {profit, cash_dividend:dividend, dividend_payout_ratio:ratio});
          // The payout detail must not depend on a YoY value being available.
          context.state.views.year[0].yoy = {};
          vm.runInContext('render();', context);
          const payoutTrace = context.traces[0];
          const detail = payoutTrace.hovertemplate.replace(/%\{customdata\[(\d+)\]\}/g,
            (_, index) => payoutTrace.customdata[0][index]);
          assert.ok(detail.includes(expected), detail);
          assert.doesNotMatch(detail, /undefined/);
        }
      } else {
        assert.doesNotMatch(hover, /分红率/);
        assert.doesNotMatch(context.note.textContent, /全年归母净利润/);
      }
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
