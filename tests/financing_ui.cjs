const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const source = page.slice(page.indexOf('  // Chip assessment charts.'), page.indexOf('  // Background data loading.'));
assert.match(page, /id="financing-range"><option value="1" selected>1年/);

function fixture(savedSelections = {}) {
  const elements = new Map(), plots = [];
  let saves = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value:id === 'financing-range' ? '1' : id === 'financing-metric' ? 'margin_balance' : id.endsWith('-adjustment') ? 'qfq' : 'all',
      textContent:'', listeners:{},
      addEventListener(name, handler) { this.listeners[name] = handler; }
    });
    return elements.get(id);
  };
  const row = (date, balance, net, price = balance/10) => ({date,margin_balance:balance,total_balance:balance*2,
    net_buy:net,qfq_close:price,raw_close:price == null ? null : price+1,qfq_price_date:date,raw_price_date:date});
  const rows = [row('2021-09-30',10,1),row('2021-10-01',20,2),row('2023-10-01',30,3),
    row('2025-09-30',40,4),row('2025-10-01',50,5),row('2025-12-31',60,6),row('2026-01-02',70,-2),
    row('2026-04-06',80,null),row('2026-04-10',90,9,null),
    row('2026-09-28',100,2),row('2026-09-29',200,-1),row('2026-10-01',300,3),row('2026-10-02',400,4)];
  const context = {savedSelections,state:{financing:{rows,window_end:'2026-10-01',stored_count:rows.length,latest:rows.at(-2)}},
    document:{getElementById:element},saveSelections() { saves++; },
    fetch() { throw new Error('Range changes must not request external data'); },
    Plotly:{react:(id,traces,layout) => plots.push({id,traces,layout})}};
  vm.createContext(context);
  vm.runInContext(source, context);
  return {context,element,plots,rows,saves:() => saves,
    render:() => context.renderChips('financing'),
    change:(id,value) => { element(id).value=value; element(id).listeners.change(); }};
}

const ui = fixture();
const original = JSON.stringify(ui.rows);
ui.render();
let plot = ui.plots.at(-1);
assert.equal(plot.layout.xaxis.range[0], '2025-10-01');
assert.equal(plot.traces[0].x[0], '2025-10-01');
assert.equal(plot.traces[0].x.length, 8);
assert.match(ui.element('financing-description').textContent, /按交易日/);
ui.change('financing-range','3');
plot = ui.plots.at(-1);
assert.equal(plot.layout.xaxis.range[0], '2023-10-01');
assert.equal(plot.traces[0].x[0], '2023-10-01');
assert.equal(plot.traces[0].x.length, 10);
ui.change('financing-range','5');
plot = ui.plots.at(-1);
assert.equal(plot.layout.xaxis.range[0], '2021-10-01');
assert.match(ui.element('financing-description').textContent, /按周/);
assert.equal(plot.traces[0].x.length, 6);
assert.equal(plot.traces[0].x.at(-1), '2026-10-01');
assert.equal(plot.traces[0].y.at(-1), 300/1e8);
assert.equal(plot.traces[1].y.at(-1), 30);
assert.equal(plot.traces[1].line.color, '#f4d35e');
assert.ok(!plot.traces[0].x.includes('2025-12-31'), 'A cross-year week is one sample');
const missingWeek = plot.traces[0].x.indexOf('2026-04-10');
assert.equal(plot.traces[1].y[missingWeek], null, 'Missing closing price must stay missing');
ui.change('financing-metric','net_buy');
plot = ui.plots.at(-1);
assert.equal(plot.traces[0].type, 'bar');
assert.match(plot.traces[0].name, /周合计/);
assert.equal(plot.traces[0].y.at(-1), 4/1e8);
assert.equal(plot.traces[0].y[plot.traces[0].x.indexOf('2026-01-02')], 4/1e8);
assert.equal(plot.traces[0].y[missingWeek], null, 'Incomplete weekly net flows must not become partial totals');
ui.change('financing-adjustment','raw');
assert.equal(ui.plots.at(-1).traces[1].y.at(-1), 31);
ui.change('financing-range','1');
assert.equal(ui.plots.at(-1).traces[0].y.at(-1), 3/1e8);
assert.equal(JSON.stringify(ui.rows), original, 'Weekly aggregation must not change cached daily rows');
assert.equal(ui.saves(), 5);
assert.equal(fixture({financingRange:'3'}).element('financing-range').value, '3');
assert.equal(fixture({financingRange:'invalid'}).element('financing-range').value, '1');
const leap = fixture();
leap.context.state.financing.window_end = '2024-02-29';
leap.render();
assert.equal(leap.plots.at(-1).layout.xaxis.range[0], '2023-02-28');
console.log('Financing range, daily/weekly aggregation and missing data checks passed');
