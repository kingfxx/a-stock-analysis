const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const start = page.indexOf('  // Background data loading.');
assert.ok(start >= 0, 'The page must support background chart loading');
const header = page.slice(page.indexOf('<header>'), page.indexOf('</header>'));
assert.match(header, /刷新数据[\s\S]*id="full-audit"[^>]*type="button">全量刷新/);
assert.equal((page.match(/id="full-audit"/g) || []).length, 1);
const decoder = page.slice(page.indexOf('  function expandValuationPayload('), page.indexOf('  const state ='));
const source = decoder + page.slice(start, page.lastIndexOf("  if (typeof Plotly !== 'undefined') { render(); renderValuation(); }"));
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup(loading, views = {quarter:[{profit:10}]}, extra = {}) {
  const pending = new Map(), elements = new Map(), rendered = [], events = [];
  const context = {
    state: {code:'601600', views, valuation:{views:{old:true}}, loading,
      warnings:[], cached_stocks:[], ...extra},
    error: {textContent:'', classList:{remove() {}}},
    Plotly: {}, render: () => rendered.push('financial'),
    renderValuation: () => rendered.push('valuation'), renderChips: section => rendered.push(section), renderCachedStocks() {},
    renderWarnings() {}, renderCurrentStock() {},
    document: {getElementById: id => {
      if (!elements.has(id)) elements.set(id, {textContent:id === 'refresh' ? '刷新数据' : '', attributes:{}, listeners:{},
        setAttribute(name, value) { this.attributes[name] = value; },
        addEventListener(name, handler) { this.listeners[name] = handler; }, replaceChildren() {}});
      return elements.get(id);
    }},
    confirm: () => false,
    window: {dispatchEvent(event) { if (event.type === 'dashboard-data-state') events.push(event.detail.updating); }},
    CustomEvent: class { constructor(type, init) { this.type=type; this.detail=init.detail; } },
    fetch: url => new Promise(resolve => pending.set(url, resolve))
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  function respond(section, data, ok = true, status = ok ? 200 : 503) {
    const key = [...pending.keys()].find(url => url.includes('/api/' + section));
    assert.ok(key, 'Expected a request for ' + section);
    pending.get(key)({ok, status, json:async () => data});
    pending.delete(key);
  }
  return {context, pending, elements, rendered, respond, events};
}

(async () => {
  // Switching to a fully fresh stock neither flashes "updating" nor reloads prices.
  const fresh = {financial:false,dividends:false,valuation:false,shareholders:false,financing:false};
  for (const extra of [{}, {price_version:1,price_needs_update:false}]) {
    const cached = setup(fresh, undefined, extra);
    const button = cached.context.document.getElementById('refresh');
    const loaded = cached.context.loadDashboard();
    assert.equal(button.textContent, '刷新数据');
    assert.equal(button.attributes['aria-disabled'], undefined);
    assert.equal(cached.pending.size, 0);
    await loaded;
    assert.equal(cached.pending.size, 0);
    assert.equal(cached.rendered.length, 0);
    assert.deepEqual(cached.events, [], 'Fresh cached data must not leave AI waiting for an update');
    for (const section of Object.keys(fresh)) {
      assert.equal(cached.elements.get(section + '-status').textContent, '');
    }
  }

  // A slow valuation source cannot hold the financial chart hostage.
  let fixture = setup({financial:true, dividends:false, valuation:true});
  let completion = fixture.context.loadDashboard();
  assert.deepEqual(fixture.events, [true]);
  await fixture.context.loadDashboard();
  assert.deepEqual(fixture.events, [true], 'Duplicate loading must not emit an unmatched update event');
  assert.equal(fixture.pending.size, 1);
  assert.match(fixture.elements.get('financial-status').textContent, /后台更新中/);
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
  assert.deepEqual(fixture.events, [true, false]);
  assert.ok(fixture.context.state.valuation.views.new);
  assert.equal(fixture.elements.get('financial-status').textContent, '');
  assert.equal(fixture.elements.get('valuation-status').textContent, '');

  // Failed refreshes keep both existing charts and clearly indicate failure.
  fixture = setup({financial:false, dividends:false, valuation:false});
  completion = fixture.context.loadDashboard(true);
  fixture.respond('financial', {error:'数据源超时'}, false);
  fixture.respond('shareholders', {rows:[],warnings:[]});
  fixture.respond('financing', {rows:[],warnings:[]});
  await completion;
  assert.equal(fixture.context.state.views.quarter[0].profit, 10);
  assert.deepEqual(fixture.context.state.valuation.views, {old:true});
  assert.match(fixture.elements.get('financial-status').textContent, /失败/);
  assert.match(fixture.elements.get('valuation-status').textContent, /保留/);
  completion = fixture.context.requestSection('financial', true);
  assert.match(fixture.elements.get('financial-status').textContent, /后台更新中/);
  fixture.respond('financial', {views:{quarter:[{profit:20}]},warnings:[],needs_dividends:false});
  await completion;
  assert.equal(fixture.elements.get('financial-status').textContent, '');

  // A cold stock must supply financial reports before computing valuation PS.
  fixture = setup({financial:true, dividends:true, valuation:true}, {});
  completion = fixture.context.loadDashboard();
  assert.equal(fixture.pending.size, 1);
  fixture.respond('financial', {code:'601600', views:{quarter:[{profit:30}]},
    warnings:['价格快照获取失败'], cached_stocks:[], needs_dividends:true});
  await tick();
  assert.equal(fixture.pending.size, 2);
  assert.ok(fixture.rendered.includes('financial'));
  assert.match(fixture.elements.get('financial-status').textContent, /更新未全部完成/);
  fixture.respond('dividends', {code:'601600', views:{quarter:[{profit:30,cash_dividend:8}]},
    warnings:[], cached_stocks:[], needs_dividends:false});
  fixture.respond('valuation', {views:{pe:true}, warnings:[]});
  await completion;
  assert.equal(fixture.context.state.views.quarter[0].cash_dividend, 8);
  assert.ok(fixture.context.state.warnings.includes('价格快照获取失败'));
  assert.ok(fixture.context.state.valuation.views.pe);
  // Chip charts load even if financial loading fails or is slow.
  fixture = setup({financial:true,dividends:true,valuation:true,shareholders:true,financing:true}, {});
  completion = fixture.context.loadDashboard();
  assert.equal(fixture.pending.size, 3);
  fixture.respond('shareholders', {rows:[{date:'2026-06-30',holders:10000}],stored_count:1,warnings:[]});
  fixture.respond('financing', {rows:[{date:'2026-09-29',margin_balance:100}],stored_count:300,warnings:[]});
  await tick();
  assert.ok(fixture.rendered.includes('shareholders'));
  assert.ok(fixture.rendered.includes('financing'));
  fixture.respond('financial', {error:'offline'}, false);
  await completion;
  assert.equal(fixture.context.state.financing.stored_count, 300);

  // Facts remain independently visible while every price consumer waits for one bundle.
  fixture = setup({financial:false,dividends:false,valuation:false,shareholders:false,financing:false},
    {quarter:[{period:'2026-06-30',profit:10,qfq_close:11}]}, {price_version:1,price_needs_update:true});
  completion = fixture.context.loadDashboard(true);
  assert.equal(fixture.pending.size, 4);
  assert.ok([...fixture.pending.keys()].filter(url => !url.includes('/prices')).every(url => url.includes('price_version=1')));
  fixture.respond('prices', {price_version:2,warnings:[]});
  fixture.respond('financial', {error:'offline'}, false);
  fixture.respond('shareholders', {price_version:1,rows:[{holders:120,qfq_close:11}],stored_count:1,warnings:[]});
  fixture.respond('financing', {price_version:1,rows:[{margin_balance:100,qfq_close:11}],stored_count:1,warnings:[]});
  await tick();
  assert.equal(fixture.context.state.price_version, 1);
  assert.equal(fixture.context.state.shareholders.rows[0].holders, 120);
  assert.match([...fixture.pending.keys()][0], /version=2/);
  const bundle = {price_version:2,financial:{views:{quarter:[{period:'2026-06-30',profit:10,qfq_close:22}]}},
    valuation:{views:{'10':{pe:{rows:[{qfq_close:22}]}}}},
    shareholders:{price_version:2,rows:[{holders:120,qfq_close:22}]},
    financing:{price_version:2,rows:[{margin_balance:100,qfq_close:22}]}};
  function verifyCoherent() {
    const state = fixture.context.state;
    assert.equal(state.price_version, 2);
    assert.equal(state.views.quarter[0].qfq_close, 22);
    assert.equal(state.valuation.views['10'].pe.rows[0].qfq_close, 22);
    assert.equal(state.shareholders.price_version, 2);
    assert.equal(state.financing.price_version, 2);
  }
  fixture.context.render = fixture.context.renderValuation = fixture.context.renderChips = verifyCoherent;
  fixture.respond('prices', bundle);
  await completion;
  verifyCoherent();

  // Cancellation starts no work; confirmation fully refreshes every current-stock dataset.
  fixture = setup(fresh, undefined, {price_version:1,price_needs_update:false,name:'中国铝业'});
  const fullButton = fixture.elements.get('full-audit');
  let confirmations = 0;
  fixture.context.confirm = message => {
    confirmations++;
    assert.match(message, /601600 · 中国铝业/);
    assert.match(message, /仅刷新当前股票/);
    assert.match(message, /更新本地数据/);
    return false;
  };
  fullButton.listeners.click();
  assert.equal(confirmations, 1);
  assert.equal(fixture.pending.size, 0);
  fixture.context.confirm = () => { confirmations++; return true; };
  completion = fullButton.listeners.click();
  assert.equal(fullButton.disabled, true);
  assert.equal(fixture.pending.size, 4);
  assert.ok([...fixture.pending.keys()].every(url => url.includes('code=601600') && url.includes('&refresh=1&full=1')));
  fullButton.listeners.click();
  assert.equal(confirmations, 2, 'An active update must not open another confirmation');
  fixture.respond('prices', {price_version:2,warnings:[]});
  fixture.respond('financial', {views:{quarter:[{profit:20}]},needs_dividends:true,warnings:[]});
  fixture.respond('shareholders', {rows:[],warnings:[]});
  fixture.respond('financing', {rows:[],warnings:[]});
  await tick();
  assert.equal(fixture.pending.size, 2);
  assert.ok([...fixture.pending.keys()].every(url => url.includes('&refresh=1&full=1')));
  fixture.respond('dividends', {views:{quarter:[{profit:20}]},needs_dividends:false,warnings:[]});
  fixture.respond('valuation', {views:{},warnings:[]});
  await tick();
  fixture.respond('prices', bundle);
  await completion;
  assert.equal(fullButton.disabled, false);

  // Stale prices still refresh even when every other dataset is fresh.
  fixture = setup(fresh, undefined, {price_version:1,price_needs_update:true});
  completion = fixture.context.loadDashboard();
  assert.equal(fixture.pending.size, 1);
  assert.equal(fixture.elements.get('refresh').textContent, '更新中…');
  fixture.respond('prices', {price_version:2,warnings:[]});
  await tick();
  assert.match([...fixture.pending.keys()][0], /version=2/);
  fixture.respond('prices', bundle);
  await completion;
  assert.equal(fixture.context.state.price_version, 2);
  assert.equal(fixture.elements.get('refresh').textContent, '刷新数据');
  assert.equal(fixture.elements.get('refresh').attributes['aria-disabled'], 'false');

  // Expired contexts explicitly reload a checked generation instead of mixing versions.
  fixture = setup({...fresh,shareholders:true}, undefined,
    {price_version:1,price_needs_update:false});
  completion = fixture.context.loadDashboard();
  fixture.respond('shareholders', {rows:[],warnings:[]});
  await tick();
  fixture.respond('prices', {error:'expired'}, false, 409);
  await tick();
  assert.ok([...fixture.pending.keys()][0].includes('/api/prices?code='));
  assert.ok(![...fixture.pending.keys()][0].includes('&version='));
  fixture.respond('prices', bundle);
  await completion;
  assert.equal(fixture.context.state.price_version, 2);

  // Responses for a previous stock cannot overwrite the newly selected stock.
  fixture = setup({financial:true,dividends:false,valuation:false}, undefined,
    {price_version:1,price_needs_update:true});
  completion = fixture.context.loadDashboard();
  fixture.context.state.code = '000001';
  fixture.respond('financial', {name:'previous stock',views:{quarter:[{profit:99}]}});
  fixture.respond('prices', bundle);
  await completion;
  assert.equal(fixture.context.state.views.quarter[0].profit, 10);
  assert.equal(fixture.context.state.price_version, 1);
  assert.equal(fixture.pending.size, 0);
  console.log('Independent chart loading, failure retention and PS dependency passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
