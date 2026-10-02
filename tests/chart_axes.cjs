const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const metricCode = page.slice(page.indexOf('  const metrics = {'), page.indexOf('  let selectedPeriod ='));
const renderCode = page.slice(page.indexOf('  function label(row)'), page.indexOf('  seriesControls.forEach(series => {'));

function checkAxes(rows, keys, period = 'quarter', range = 'all', kinds = ['bar', 'bar', 'line']) {
  const context = {
    state:{views:{[period]:rows}}, selectedPeriod:period, comparisonRange:{value:range},
    seriesControls:keys.map((key, index) => ({label:'ABC'[index], color:'#38c4dc',
      kind:kinds[index], control:{value:key}})), modeOf:series => series.kind,
    note:{}, document:{getElementById:() => ({classList:{toggle() {}}, textContent:''})},
    Plotly:{react:(id, traces, layout) => { context.traces = traces; context.layout = layout; }}
  };
  vm.createContext(context);
  vm.runInContext(metricCode + renderCode + '\nrender();', context);
  const ranges = Object.entries(context.layout).filter(([key]) => /^yaxis\d*$/.test(key)).map(([, axis]) => axis.range);
  assert.ok(ranges.length > 1);
  if (keys.includes('market_cap')) {
    const market = context.traces.find(trace => trace.name?.includes('披露日估算市值') && trace.showlegend !== false);
    for (const trace of context.traces.filter(trace => /归母净利润|营业收入/.test(trace.name || ''))) {
      assert.notEqual(market.yaxis, trace.yaxis, 'market cap must not share an axis with operating amounts');
    }
    const expectedGroups = new Set(keys.map(key => key === 'market_cap' ? key : key === 'qfq_price' ? 'price' : 'operating'));
    assert.equal(ranges.length, expectedGroups.size, 'dedicated market axis should preserve other grouping');
  }
  let zeroPosition;
  for (const bounds of ranges) {
    assert.ok(bounds && bounds.every(Number.isFinite), 'every axis must have an explicit finite range');
    assert.ok(bounds[1] > bounds[0] && bounds[0] <= 0 && bounds[1] >= 0);
    const position = -bounds[0] / (bounds[1] - bounds[0]);
    if (zeroPosition != null) assert.ok(Math.abs(position - zeroPosition) < 1e-12, 'zero positions must align');
    zeroPosition = position;
  }
  for (const trace of context.traces) {
    const bounds = context.layout[trace.yaxis === 'y' ? 'yaxis' : 'yaxis' + trace.yaxis.slice(1)].range;
    for (const value of trace.y.filter(Number.isFinite)) {
      assert.ok(value >= bounds[0] && value <= bounds[1], 'formal and reference values must not be clipped');
    }
  }
  return zeroPosition;
}

const combinations = [
  [[null, 7, 120], [-20e8, 10e8, 380e8]],
  [[7, 120, 30], [10e8, 380e8, 50e8]],
  [[-30, -10, -20], [-20e8, -10e8, -30e8]],
  [[0, 0, 0], [-20e8, 10e8, 380e8]],
  [[null, null, null], [-20e8, 10e8, 380e8]],
  [[null, null, null], [null, null, null]],
  [[-5, 50, 100], [-300e8, -100e8, -200e8]],
];
for (const period of ['quarter', 'year', 'ttm']) {
  for (const range of ['all', '5', '10']) {
    for (const [roic, profit] of combinations) {
      const rows = roic.map((value, i) => ({period:`${new Date().getFullYear() - 2 + i}-12-31`,
        roic:value, profit:profit[i], qfq_price:[-.1, 10, 30][i], qfq_price_reference:[null, -2, null][i]}));
      for (const keys of [['roic', 'profit'], ['profit', 'roic'], ['roic', 'profit', 'qfq_price'], ['profit', 'qfq_price']]) {
        for (const kinds of [['bar', 'bar', 'line'], ['line', 'bar', 'bar']]) checkAxes(rows, keys, period, range, kinds);
      }
    }
  }
}
// Exercise the boundary where filtering removes the only negative observation.
const currentYear = new Date().getFullYear();
const filteringRows = [{period:`${currentYear-12}-12-31`, roic:5, profit:-10e8},
                       {period:`${currentYear}-03-31`, roic:20, profit:50e8}];
assert.ok(checkAxes(filteringRows, ['roic', 'profit']) > 0);
assert.equal(checkAxes(filteringRows, ['roic', 'profit'], 'quarter', '5'), 0);

const marketRows = [0,1,2].map(i => ({period:`${currentYear-3+i}-12-31`,
  market_cap:[700e8,1800e8,3500e8][i],profit:[-10e8,30e8,100e8][i],
  revenue:[500e8,700e8,800e8][i],qfq_price:[15,25,40][i]}));
for (const period of ['quarter','year','ttm']) {
  for (const keys of [['market_cap','profit','qfq_price'],['profit','market_cap','qfq_price'],
                     ['qfq_price','market_cap','profit'],['market_cap','revenue','profit'],['market_cap','market_cap','profit']]) {
    for (const kinds of [['bar','bar','line'],['line','line','bar']]) checkAxes(marketRows,keys,period,'all',kinds);
  }
}

const realViews = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
for (const [period, rows] of Object.entries(realViews)) {
  for (const range of ['all', '5', '10']) checkAxes(rows, ['roic', 'profit'], period, range);
}
console.log('Multi-axis zero alignment, missing values, filtering and reference bounds passed');
