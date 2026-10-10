const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const decoder = page.slice(page.indexOf('  function expandValuationPayload('), page.indexOf('  const state ='));
const source = decoder + page.slice(page.indexOf('  let valuationMetric ='), page.indexOf('  // Chip assessment charts.'));

function fixture(range = '3', persistedFrequency = null, persistedBenchmark = null) {
  const elements = new Map(), plots = [], requests = [], storage = new Map();
  if (persistedBenchmark) storage.set('investment-valuation-benchmark-v1', persistedBenchmark);
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

// Five-year weekly percentile samples stay fixed when the chart frequency changes.
{
  const weeklyUi = fixture('5');
  weeklyUi.context.state.valuation.views['5'].pe.percentile_frequency = 'week';
  weeklyUi.render();
  const history = weeklyUi.element('valuation-history').textContent;
  assert.match(history, /周样本（24 个）/);
  for (const frequency of ['day','month','week']) {
    weeklyUi.change('valuation-frequency', frequency);
    assert.equal(weeklyUi.element('valuation-history').textContent, history);
    assert.equal(weeklyUi.element('valuation-high').textContent, '30.00 倍');
  }
  assert.equal(weeklyUi.requests.length, 0);
}

// Manual changes use the in-memory response, preserve percentile values, and survive a reload.
{
  const priceUi = fixture('5');
  priceUi.element('valuation-price-overlay').setAttribute('aria-pressed', 'true');
  priceUi.context.state.valuation.views['5'].pe.rows_by_frequency.week = [
    {date:'2026-02-15',pe:6,qfq_close:13.87,qfq_close_date:'2026-02-13',qfq_no_trades:false},
    {date:'2026-02-22',pe:6,qfq_close:null,qfq_no_trades:true},
    {date:'2026-03-01',pe:6,qfq_close:14.57,qfq_close_date:'2026-02-27',qfq_no_trades:false},
    {date:'2026-03-08',pe:6,qfq_close:null,qfq_no_trades:false}
  ];
  priceUi.render();
  const traces = priceUi.plots.at(-1).traces;
  const price = traces.find(trace => trace.name === '前复权股价');
  assert.deepEqual(Array.from(price.x), ['2026-02-15','2026-03-01','2026-03-08']);
  assert.deepEqual(Array.from(price.y), [13.87,14.57,null]);
  assert.equal(price.connectgaps, false);
  assert.equal(price.customdata[0][0], '2026-02-13');
  assert.equal(traces[0].x.length, 4);
}

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

// A later row for another metric must not clear the yield card; unavailable percentiles never print null%.
{
  const yieldUi = fixture('3');
  const yieldView = structuredClone(yieldUi.context.state.valuation.views['3'].pe);
  Object.assign(yieldView,{current:3.50385423966363,current_date:'2026-09-30',high:3.56,median:3.42,low:3,
    count:36,percentile:66.7,industry:null});
  const rows=[{date:'2026-09-30',dividend_yield:3.50385423966363,dividend_yield_date:'2026-09-30'},
    {date:'2026-10-01',pb:3.23}];
  yieldView.rows_by_frequency={day:rows,week:rows,month:rows};
  yieldUi.context.state.valuation.views['3'].dividend_yield=yieldView;
  vm.runInContext("valuationMetric='dividend_yield';renderValuation();",yieldUi.context);
  assert.equal(yieldUi.element('valuation-current').textContent,'3.50%');
  assert.equal(yieldUi.element('valuation-current-date').textContent,'2026-09-30');
  const history=yieldUi.element('valuation-history').textContent;
  assert.match(history,/66.7%/);
  for(const frequency of ['week','month','day']){
    yieldUi.change('valuation-frequency',frequency);
    assert.equal(yieldUi.element('valuation-current').textContent,'3.50%');
    assert.equal(yieldUi.element('valuation-history').textContent,history);
  }
  yieldView.current=null;yieldView.current_date=null;yieldView.percentile=null;
  yieldUi.render();
  assert.equal(yieldUi.element('valuation-current').textContent,'—');
  assert.match(yieldUi.element('valuation-history').textContent,/暂无法计算当前分位/);
  assert.doesNotMatch(yieldUi.element('valuation-history').textContent,/null%|undefined%|NaN/);
  yieldView.current=0;yieldView.current_date='2026-09-30';yieldView.percentile=0;
  yieldUi.render();
  assert.equal(yieldUi.element('valuation-current').textContent,'0.00%');
  assert.match(yieldUi.element('valuation-history').textContent,/0%/);
  assert.equal(yieldUi.requests.length,0);
}

// Historical position follows the selected metric/range, including exact boundaries and zero yield.
{
  const positionUi = fixture('3');
  const cases = [[0,0],[3.3,0],[5,0],[5.1,0],[20,0],[20.1,1],[39.9,1],[40,2],[50,2],[60,2],[60.1,3],[79.9,3],[80,4],[94.9,4],[95,4],[100,4]];
  for (const metric of ['pe','pb','ps','dividend_yield']) {
    const view = structuredClone(positionUi.context.state.valuation.views['3'].pe);
    positionUi.context.state.valuation.views['3'][metric] = view;
    const labels = metric === 'dividend_yield' ? ['低息','偏低','中值','偏高','高息'] : ['低位','偏低','中值','偏高','高位'];
    for (const [percentile,band] of cases) {
      view.percentile = percentile;
      vm.runInContext(`valuationMetric='${metric}';renderValuation();`,positionUi.context);
      assert.equal(positionUi.element('valuation-position').textContent,percentile <= 5 ? '极低' : percentile >= 95 ? '极高' : labels[band]);
      assert.equal(positionUi.element('valuation-position-basis').textContent,`近 3 年 · 历史分位 ${percentile.toFixed(1)}%`);
      const rank = percentile <= 5 ? 0 : percentile >= 95 ? 6 : band + 1;
      const tones = ['extreme-low','low','slightly-low','middle','slightly-high','high','extreme-high'];
      const tone = tones[metric === 'dividend_yield' ? 6 - rank : rank];
      assert.equal(positionUi.element('valuation-position').getAttribute('data-tone'),tone);
    }
    assert.equal(positionUi.plots.at(-1).traces[1].line.color,metric === 'dividend_yield' ? '#6ac9aa' : '#ed8180');
    assert.equal(positionUi.plots.at(-1).traces[3].line.color,metric === 'dividend_yield' ? '#ed8180' : '#6ac9aa');
    assert.equal(positionUi.element('valuation-high-dot').getAttribute('style'),'--level-color:' + positionUi.plots.at(-1).traces[1].line.color);
    assert.equal(positionUi.element('valuation-low-dot').getAttribute('style'),'--level-color:' + positionUi.plots.at(-1).traces[3].line.color);
  }
  const view = positionUi.context.state.valuation.views['3'].dividend_yield;
  view.current = 0;view.percentile = 0;
  positionUi.render();
  assert.equal(positionUi.element('valuation-position').textContent,'极低');
  assert.equal(positionUi.element('valuation-current').textContent,'0.00%');
  for (const change of [{percentile:null},{percentile:NaN},{percentile:-1},{percentile:101},{count:0},{current:null}]) {
    Object.assign(view,{current:0,count:24,percentile:0},change);
    positionUi.render();
    assert.equal(positionUi.element('valuation-position').textContent,'待判断');
    assert.equal(positionUi.element('valuation-position').getAttribute('data-tone'),'');
    assert.match(positionUi.element('valuation-position-basis').textContent,/暂无有效分位/);
  }
  Object.assign(view,{current:3.5,count:36,percentile:72.2});
  positionUi.context.state.valuation.views['5'].dividend_yield = {...view,percentile:50};
  positionUi.context.state.valuation.views['10'].dividend_yield = {...view,percentile:18};
  for (const [range,label] of [['3','偏高'],['5','中值'],['10','低息']]) {
    positionUi.change('valuation-range',range);
    assert.equal(positionUi.element('valuation-position').textContent,label);
    assert.match(positionUi.element('valuation-position-basis').textContent,new RegExp(`近 ${range} 年`));
    for (const frequency of ['day','week','month','auto']) {
      positionUi.change('valuation-frequency',frequency);
      assert.equal(positionUi.element('valuation-position').textContent,label);
    }
  }
  assert.equal(positionUi.requests.length,0);
}

// Negative PE periods stay marked across frequencies and clear when another metric is selected.
{
  const negativeUi = fixture('3');
  const peView = negativeUi.context.state.valuation.views['3'].pe;
  peView.negative_pe_ranges = [{start:'2025-08-22',end:'2026-02-27'}];
  peView.rows_by_frequency.day = [
    {date:'2025-08-21',pe:254.93}, {date:'2025-08-22',pe:null},
    {date:'2026-02-27',pe:null}, {date:'2026-02-28',pe:84.35}];
  negativeUi.render();
  const plot = negativeUi.plots.at(-1);
  assert.deepEqual(Array.from(plot.traces[0].y), [254.93,null,null,84.35]);
  assert.equal(plot.traces[0].connectgaps, false);
  assert.equal(plot.layout.shapes[0].x0, '2025-08-22');
  assert.equal(plot.layout.shapes[0].x1, '2026-02-27');
  const message = negativeUi.element('valuation-negative-note').textContent;
  assert.match(message, /2025-08-22 至 2026-02-27/);
  assert.match(message, /不参与走势与估值分位计算/);
  for (const frequency of ['week','month','day']) {
    negativeUi.change('valuation-frequency', frequency);
    assert.equal(negativeUi.element('valuation-negative-note').textContent, message);
    assert.equal(negativeUi.element('valuation-high').textContent, '30.00 倍');
  }
  negativeUi.context.state.valuation.views['3'].pb = structuredClone(peView);
  vm.runInContext("valuationMetric = 'pb'; renderValuation();", negativeUi.context);
  assert.equal(negativeUi.element('valuation-negative-note').textContent, '');
  assert.equal(negativeUi.plots.at(-1).layout.shapes.length, 0);
  peView.negative_pe_ranges = [];
  vm.runInContext("valuationMetric = 'pe'; renderValuation();", negativeUi.context);
  assert.equal(negativeUi.element('valuation-negative-note').textContent, '');
  assert.equal(negativeUi.plots.at(-1).layout.shapes.length, 0);
  assert.equal(negativeUi.requests.length, 0);
}

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

// Benchmark switches use server statistics, persist, and leave display sampling and percentiles intact.
{
  const ui = fixture('5');
  const view = ui.context.state.valuation.views['5'].pe;
  Object.assign(view, {mean:20, stddev:25, stddev_high:45, stddev_low:-5});
  ui.render();
  const position = ui.element('valuation-position-basis').textContent;
  ui.change('valuation-benchmark', 'stddev');
  assert.equal(ui.element('valuation-high').textContent, '45.00 倍');
  assert.equal(ui.element('valuation-median-label').textContent, '均值');
  assert.equal(ui.element('valuation-low').textContent, '-5.00 倍');
  assert.equal(ui.element('valuation-position-basis').textContent, position);
  assert.deepEqual(Array.from(ui.plots.at(-1).traces.slice(1), t => Array.from(t.y)), [[45,45],[20,20],[-5,-5]]);
  assert.match(ui.element('valuation-note').textContent, /总体标准差/);
  ui.change('valuation-frequency', 'day');
  assert.equal(ui.element('valuation-high').textContent, '45.00 倍');
  assert.equal(ui.storage.get('investment-valuation-benchmark-v1'), 'stddev');
  assert.equal(fixture('5', null, 'stddev').element('valuation-benchmark').value, 'stddev');
  ui.change('valuation-benchmark', 'percentile');
  assert.equal(ui.element('valuation-high').textContent, '30.00 倍');
  assert.equal(ui.element('valuation-median-label').textContent, '中值（50% 分位）');
  ui.change('valuation-benchmark', 'stddev');
  ui.context.state.valuation.views['5'].pe = {count:0};
  ui.render();
  assert.equal(ui.element('valuation-high').textContent, '—');
  assert.equal(ui.plots.at(-1).traces.length, 1);
  assert.equal(ui.requests.length, 0);
}
