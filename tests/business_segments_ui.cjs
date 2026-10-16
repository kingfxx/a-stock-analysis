const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const context={window:{}};vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/business-segments.js'),'utf8'),context);
const {items,growth,periodsFor,series,yoySeries,percent,esc}=context.window.businessSegmentPresentation;
const row=(key,name,value,extra={})=>({item_key:key,item_name:name,revenue:value,...extra});
const period=(date,rows)=>({report_date:date,dimensions:{'2':rows}});
const periods=[
  period('2022-12-31',[row('a','业务A',100),row('offset','抵销',-10)]),
  period('2023-12-31',[row('a','业务A',0),row('offset','抵销',-20)]),
  period('2024-06-30',[row('a','业务A',99)]),
  period('2025-12-31',[row('a','业务A',150,{gross_margin:0.2}),row('renamed','新业务',40),row('total','合计',190)])
];
const annual=periodsFor(periods,'12-31',5);
assert.equal(annual.length,4);
assert.equal(annual[2].missing,true,'missing annual report has an empty calendar slot');
assert.equal(items(periods.at(-1),'2').length,2,'explicit total excluded from chart');
assert.equal(periods.at(-1).dimensions['2'].length,3,'source total preserved');
const hierarchy=period('2020-12-31',[row('parent','能量饮料',100),row('child','其中:500ml金瓶',70),row('child2','其中：250ml金瓶',30),row('other','其他饮料',10)]);
assert.equal(items(hierarchy,'2').length,2,'explicit children never stack with parent');
assert.equal(items(hierarchy,'2').reduce((sum,r)=>sum+r.revenue,0),110);
assert.equal(hierarchy.dimensions['2'].length,4,'all hierarchy facts remain in source');
const selected=new Set(['a','offset','renamed']);
const plotted=series(annual,'2','revenue',selected);
assert.deepEqual(Array.from(plotted.find(s=>s.key==='a').y),[100,0,null,150]);
assert.deepEqual(Array.from(plotted.find(s=>s.key==='offset').y),[-10,-20,null,null]);
assert.deepEqual(Array.from(plotted.find(s=>s.key==='renamed').y),[null,null,null,40]);
assert.equal(series(annual,'2','cost',selected).every(s=>s.y.every(v=>v===null)),true);
assert.equal(growth(0,100),-100);assert.ok(Math.abs(growth(-10,100)+110)<1e-10);
for(const value of [null,undefined,0,-1])assert.equal(growth(100,value),null);
assert.equal(growth(null,100),null);
const comparable=[period('2023-06-30',[row('a','业务A',10)]),period('2024-06-30',[row('a','业务A',20)]),period('2024-12-31',[row('a','业务A',100)]),period('2025-06-30',[row('renamed','新业务',30)])];
const mid=periodsFor(comparable,'06-30',3);
const yoy=yoySeries(mid,comparable,'2','revenue',new Set(['a','renamed']));
assert.equal(yoy[1].y[0],100,'interim compares interim, never full year');
assert.equal(yoy[2].y.every(v=>v===null),true,'rename is not stitched to previous item');
assert.equal(percent(null),'—');assert.equal(percent(0),'0.00%');assert.equal(percent(-0.1),'-10.00%');
assert.equal(esc('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');
assert.equal(periodsFor([],'12-31',10).length,0);
console.log('business segment presentation checks passed');
