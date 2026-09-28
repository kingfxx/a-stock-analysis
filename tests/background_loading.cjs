const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const start = page.indexOf('  // Background data loading.');
assert.ok(start >= 0, 'The page must support background chart loading');
const source = page.slice(start, page.lastIndexOf("  if (typeof Plotly !== 'undefined') { render(); renderValuation(); }"));
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup(loading, views = {quarter:[{profit:10}]}) {
  const pending = new Map(), elements = new Map(), rendered = [];
  const context = {
    state: {code:'601600', views, valuation:{views:{old:true}}, loading,
      warnings:[], cached_stocks:[]},
    error: {textContent:'', classList:{remove() {}}},
    Plotly: {}, render: () => rendered.push('financial'),
    renderValuation: () => rendered.push('valuation'), renderCachedStocks() {},
    renderWarnings() {},
    document: {getElementById: id => {
      if (!elements.has(id)) elements.set(id, {textContent:'',
        setAttribute() {}, addEventListener() {}, replaceChildren() {}});
      return elements.get(id);
    }},
    fetch: url => new Promise(resolve => pending.set(url, resolve))
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  function respond(section, data, ok = true) {
    const key = [...pending.keys()].find(url => url.includes('/api/' + section));
    assert.ok(key, 'Expected a request for ' + section);
    pending.get(key)({ok, json:async () => data});
    pending.delete(key);
  }
  return {context, pending, elements, rendered, respond};
}

(async () => {
  // A slow valuation source cannot hold the financial chart hostage.
  let fixture = setup({financial:true, dividends:false, valuation:true});
  let completion = fixture.context.loadDashboard();
  assert.equal(fixture.pending.size, 1);
  fixture.respond('financial', {code:'601600', name:'中国铝业',
    updated_at:'2026-09-28', views:{quarter:[{profit:20}]}, warnings:[],
    cached_stocks:[], needs_dividends:false});
  await tick();
  assert.equal(fixture.context.state.views.quarter[0].profit, 20);
  assert.ok(fixture.rendered.includes('financial'));
  assert.equal(fixture.pending.size, 1);
  assert.deepEqual(fixture.context.state.valuation.views, {old:true});
  fixture.respond('valuation', {views:{new:true}, updated_on:'2026-09-28', warnings:[]});
  await completion;
  assert.ok(fixture.context.state.valuation.views.new);

  // Failed refreshes keep both existing charts and clearly indicate failure.
  fixture = setup({financial:false, dividends:false, valuation:false});
  completion = fixture.context.loadDashboard(true);
  fixture.respond('financial', {error:'数据源超时'}, false);
  await completion;
  assert.equal(fixture.context.state.views.quarter[0].profit, 10);
  assert.deepEqual(fixture.context.state.valuation.views, {old:true});
  assert.match(fixture.elements.get('financial-status').textContent, /失败/);
  assert.match(fixture.elements.get('valuation-status').textContent, /保留/);

  // A cold stock must supply financial reports before computing valuation PS.
  fixture = setup({financial:true, dividends:true, valuation:true}, {});
  completion = fixture.context.loadDashboard();
  assert.equal(fixture.pending.size, 1);
  fixture.respond('financial', {code:'601600', views:{quarter:[{profit:30}]},
    warnings:['价格快照获取失败'], cached_stocks:[], needs_dividends:true});
  await tick();
  assert.equal(fixture.pending.size, 2);
  assert.ok(fixture.rendered.includes('financial'));
  fixture.respond('dividends', {code:'601600', views:{quarter:[{profit:30,cash_dividend:8}]},
    warnings:[], cached_stocks:[], needs_dividends:false});
  fixture.respond('valuation', {views:{pe:true}, warnings:[]});
  await completion;
  assert.equal(fixture.context.state.views.quarter[0].cash_dividend, 8);
  assert.ok(fixture.context.state.warnings.includes('价格快照获取失败'));
  assert.ok(fixture.context.state.valuation.views.pe);
  console.log('Background chart loading, failure retention and PS dependency passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
