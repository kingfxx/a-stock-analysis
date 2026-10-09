const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
assert.equal((page.match(/<option value="period_end_market_cap">季末市值<\/option>/g) || []).length, 3);
const metricCode = page.slice(page.indexOf('  const metrics = {'), page.indexOf('  let selectedPeriod ='));
const renderCode = page.slice(page.indexOf('  function label(row)'), page.indexOf('  seriesControls.forEach(series => {'));
for (const period of ['quarter', 'year', 'ttm']) {
  const row = {period:'2023-12-31', publish_date:'2024-04-20', profit:1e8,
    period_end_market_cap:10e8, period_end_price_date:'2023-12-29',
    market_cap:20e8, price_date:'2024-04-19'};
  const context = {
    state:{views:{[period]:[row]}}, selectedPeriod:period, comparisonRange:{value:'all'},
    seriesControls:['profit','period_end_market_cap','market_cap'].map((key, i) => ({
      label:'ABC'[i], color:'#38c4dc', kind:i ? 'line' : 'bar', control:{value:key}
    })), modeOf:series => series.kind, note:{},
    document:{getElementById:() => ({classList:{toggle() {}}, textContent:''})},
    Plotly:{react:(id, traces) => { context.traces = traces; }}
  };
  vm.createContext(context);
  vm.runInContext(metricCode + renderCode + '\nrender();', context);
  const cap = context.traces.find(t => t.name?.includes('季末市值'));
  const disclosure = context.traces.find(t => t.name?.includes('披露日估算市值'));
  assert.equal(cap.y[0], 10);
  assert.equal(cap.yaxis, disclosure.yaxis);
  assert.notEqual(cap.yaxis, context.traces[0].yaxis);
  const hover = cap.hovertemplate.replace(/%\{customdata\[(\d+)\]\}/g,
    (_, index) => cap.customdata[0][index]);
  assert.match(hover, /价格交易日：2023-12-29/);
  assert.match(context.note.textContent, /不复权收盘价×该报告期末总股本/);
  assert.match(context.note.textContent, /TTM 保留各季末时点值，不加总/);
}
console.log('Season-end market cap options, values, axes and actual price dates passed');
