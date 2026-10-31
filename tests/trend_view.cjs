const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const plots = [];
const context = {window:{}, Plotly:{react:(chart, traces, layout) => plots.push({chart,traces,layout})}};
vm.runInNewContext(fs.readFileSync('web/features/financials/trend-view.js', 'utf8'), context);
const element = () => ({classList:{toggle(){}}, textContent:'', hidden:false});
const elements = {chart:element(), scroll:element(), hint:element(), title:element(), subtitle:element()};
const rows = [{period:'2025-03-31',profit:-4,revenue:0,period_end_market_cap:10},
  {period:'2025-06-30',profit:6,revenue:8,period_end_market_cap:20}];
const metrics = {profit:{name:'利润',unit:'亿元',scale:1}, revenue:{name:'收入',unit:'亿元',scale:1},
  period_end_market_cap:{name:'市值',unit:'亿元',scale:1}};
const active = ['profit','revenue','period_end_market_cap'].map((key,i)=>({key,metric:metrics[key],
  label:'ABC'[i],color:['red','blue','green'][i],kind:i===2?'line':'bar',values:rows.map(r=>r[key])}));
function draw(series) {
  context.window.TrendView.draw({elements,rows,allRows:rows,active:series,selectedPeriod:'quarter',metrics,
    label:r=>r.period,positions:items=>items.map((_,i)=>i),isPriceOrCap:()=>false,hasMarketComparisons:()=>false});
  return plots.at(-1);
}
const plot=draw(active);
assert.deepEqual(Array.from(plot.traces, t=>t.yaxis), ['y','y','y2']);
const a=plot.layout.yaxis.range,b=plot.layout.yaxis2.range;
assert.ok(Math.abs(a[0]/(a[1]-a[0])-b[0]/(b[1]-b[0]))<1e-12);
assert.ok(a[0]<=-4 && a[1]>=8 && b[0]<=0 && b[1]>=20);
const empty=draw([]);
assert.equal(empty.traces.length,0);
assert.equal(empty.layout.annotations.length,1);
assert.equal(empty.layout.yaxis.visible,false);
console.log('trend view: shared units, separate market cap, zero alignment, empty selection');
