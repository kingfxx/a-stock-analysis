const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const source = page.slice(page.indexOf('  let valuationMetric ='), page.indexOf('  // Chip assessment charts.'));

function fixture(range = '3', persistedFrequency = null) {
  const elements = new Map(), plots = [], requests = [], storage = new Map();
  if (persistedFrequency) storage.set('investment-valuation-frequency-v1', persistedFrequency);
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value:id === 'valuation-range' ? '10' : id === 'valuation-frequency' ? 'auto' : '',
      textContent:'', attributes:{}, listeners:{},
      setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) { return this.attributes[name] ?? null; },
      addEventListener(name, callback) { this.listeners[name] = callback; }
    });
    return elements.get(id);
  };
  const metricButtons = ['pe','pb','ps','dividend_yield'].map(metric => ({
    dataset:{valuationMetric:metric}, classList:{toggle() {}}, addEventListener() {}
  }));
  const view = {
    frequency:range === '3' ? 'day' : range === '5' ? 'week' : 'month',
    current:20, current_date:'2026-09-29', high:30, median:20, low:10,
    percentile:50, count:24, percentile_frequency:'month', industry:21,
    rows_by_frequency:{
      day:[{date:'2026-09-28',pe:18,pe_date:'2026-09-27',qfq_close:9},
           {date:'2026-09-29',pe:20,pe_date:'2026-09-29',qfq_close:10}],
      week:[{date:'2026-09-29',pe:20,pe_date:'2026-09-27',qfq_close:10}],
      month:[{date:'2026-09-30',pe:20,pe_date:'2026-09-27',qfq_close:10,period_complete:false}]
    },
    sparse_hint:'较早段观测较稀疏；请以悬浮提示中的实际日期为准。'
  };
  const context = {
    state:{valuation:{views:{'3':{pe:view},'5':{pe:view},'10':{pe:view}},warnings:[]}},
    savedSelections:{valuationRange:range}, selectedPeriod:'quarter', comparisonRange:{value:'all'},
    seriesControls:[], modeOf() {},
    document:{getElementById:element,querySelectorAll:selector =>
      selector === '[data-valuation-metric]' ? metricButtons : []},
    window:{addEventListener() {}},
    sessionStorage:{setItem() {}},
    localStorage:{getItem:key => storage.get(key) ?? null,setItem:(key,value) => storage.set(key,value)},
    fetch:url => { requests.push(url); throw new Error('frequency switch requested network'); },
    Plotly:{react:(id,traces,layout) => plots.push({id,traces,layout})}
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const render = () => vm.runInContext('renderValuation()', context);
  const change = (id, value) => {
    element(id).value = value;
    element(id).listeners.change();
  };
  return {context, element, plots, requests, storage, render, change};
}

// A range's automatic frequency chooses the corresponding saved observations.
for (const [range, expectedDate] of [['3','2026-09-28'],['5','2026-09-29'],['10','2026-09-30']]) {
  const ui = fixture(range);
  ui.render();
  assert.equal(ui.plots.at(-1).traces[0].x[0], expectedDate);
}

// Manual changes use the in-memory response, preserve percentile values, and survive a reload.
let ui = fixture('3');
ui.render();
ui.change('valuation-frequency', 'month');
assert.equal(ui.plots.at(-1).traces[0].x[0], '2026-09-30');
assert.equal(ui.element('valuation-high').textContent, '30.00 倍');
assert.equal(ui.element('valuation-history').textContent.includes('50%'), true);
assert.equal(ui.requests.length, 0);
assert.equal(ui.storage.get('investment-valuation-frequency-v1'), 'month');
ui = fixture('3', 'week');
ui.render();
assert.equal(ui.plots.at(-1).traces[0].x[0], '2026-09-29');

// The displayed period and the metric's actual observation date remain distinct.
ui = fixture('10');
ui.render();
const trace = ui.plots.at(-1).traces[0];
assert.equal(trace.x[0], '2026-09-30');
assert.equal(trace.customdata[0][0], '2026-09-27');
assert.equal(trace.customdata[0][1], '（本周期未完结）');
assert.match(trace.hovertemplate, /实际观察日：%\{customdata\[0\]\}/);
assert.match(ui.element('valuation-note').textContent, /较早段观测较稀疏/);

// Delayed API responses render the current frequency, while responses from the old stock are ignored.
(async () => {
  ui = fixture('3');
  ui.context.state.code = '601600';
  const pending = [];
  ui.context.fetch = url => new Promise(resolve => pending.push({url,resolve}));
  const loadingSource = page.slice(page.indexOf('  // Background data loading.'),
    page.indexOf('  async function requestPrices('));
  vm.runInContext(loadingSource, ui.context);
  const request = () => vm.runInContext('requestSection("valuation", false)', ui.context);
  const first = request();
  assert.equal(pending.length, 1);
  ui.change('valuation-frequency', 'week');
  const incoming = structuredClone(ui.context.state.valuation);
  incoming.views['3'].pe.rows_by_frequency.week[0].pe = 25;
  pending.shift().resolve({ok:true,json:async () => incoming});
  await first;
  assert.equal(ui.plots.at(-1).traces[0].y[0], 25);
  assert.equal(ui.plots.at(-1).traces[0].x[0], '2026-09-29');

  const second = request();
  assert.equal(pending.length, 1);
  ui.context.state.code = '000001';
  const plotCount = ui.plots.length;
  const stale = structuredClone(incoming);
  stale.views['3'].pe.rows_by_frequency.week[0].pe = 99;
  pending.shift().resolve({ok:true,json:async () => stale});
  await second;
  assert.equal(ui.context.state.valuation.views['3'].pe.rows_by_frequency.week[0].pe, 25);
  assert.equal(ui.plots.length, plotCount);

  // An unverified price generation keeps the matching cached price in every frequency.
  ui = fixture('3');
  ui.context.state.code = '601600';
  ui.context.state.price_version = 0;
  const oldWeek = ui.context.state.valuation.views['3'].pe.rows_by_frequency.week[0];
  oldWeek.qfq_close = 10;
  const pricePending = [];
  ui.context.fetch = url => new Promise(resolve => pricePending.push({url,resolve}));
  vm.runInContext(loadingSource, ui.context);
  const priceRequest = vm.runInContext('requestSection("valuation", false)', ui.context);
  const fresh = structuredClone(ui.context.state.valuation);
  fresh.views['3'].pe.rows_by_frequency.week[0].qfq_close = 99;
  pricePending.shift().resolve({ok:true,json:async () => fresh});
  await priceRequest;
  assert.equal(ui.context.state.valuation.views['3'].pe.rows_by_frequency.week[0].qfq_close, 10);

  // Legacy monthly caches retain their price when the latest source date shifts within a month.
  ui = fixture('10');
  ui.context.state.code = '601600';
  ui.context.state.price_version = 0;
  ui.context.state.valuation.views['10'].pe = {rows:[{date:'2026-08-30',pe:19,qfq_close:8}]};
  const legacyPending = [];
  ui.context.fetch = url => new Promise(resolve => legacyPending.push({url,resolve}));
  vm.runInContext(loadingSource, ui.context);
  const legacyRequest = vm.runInContext('requestSection("valuation", false)', ui.context);
  const legacyFresh = {views:{'10':{pe:{rows:[{date:'2026-08-31',pe:20,qfq_close:99}]}}}};
  legacyPending.shift().resolve({ok:true,json:async () => legacyFresh});
  await legacyRequest;
  assert.equal(ui.context.state.valuation.views['10'].pe.rows[0].qfq_close, 8);
  console.log('Valuation frequency, persistence, observation dates, sparse hint and stale responses passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
