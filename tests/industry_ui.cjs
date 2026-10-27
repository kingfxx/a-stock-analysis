const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const windowListeners={};
const context={window:{DashboardTime:require('../web/shared/datetime.js'),addEventListener(name,listener){windowListeners[name]=listener;}},URLSearchParams};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/features/industries/industry.js'),'utf8'),context);
const display=context.window.industryMarketDisplay;
assert.match(display([]).note,/尚无已发布市值汇总/);
assert.equal(display([{quarter:'2026Q3'}]).mode,'markers');
assert.match(display([{quarter:'2026Q3'}]).label,/仅1期/);
assert.match(display([{quarter:'2026Q3'}]).note,/不能形成趋势线/);
const result=display([{quarter:'2025Q4'},{quarter:'2026Q1'},{quarter:'2026Q2'},{quarter:'2026Q3'}]);
assert.equal(result.mode,'lines+markers');
assert.match(result.note,/4 个季度点/);
assert.match(result.note,/2025Q4—2026Q3/);
console.log('market snapshot and history presentation checks passed');
const periods=[];
for(let year=2014;year<=2026;year++)for(const suffix of ['03-31','06-30','09-30','12-31']){
  const period=`${year}-${suffix}`;
  if(period>='2014-09-30'&&period<='2026-06-30')periods.push(period);
}
const filter=context.window.industryPeriodsInRange;
assert.equal(filter(periods,'all').length,48);
assert.equal(filter(periods,'10').length,40);
assert.equal(filter(periods,'10')[0],'2016-09-30');
assert.equal(filter(periods,'5').length,20);
assert.equal(filter(periods,'5')[0],'2021-09-30');
assert.equal(periods.length,48);
const annual=periods.filter(p=>p.endsWith('12-31'));
assert.equal(filter(annual,'10').length,10);
assert.equal(filter(annual,'5').length,5);
assert.equal(filter(annual,'5')[0],'2021-12-31');
assert.equal(filter([],'10').length,0);
assert.equal(filter(['2026-06-30'],'5').length,1);
console.log('industry time range boundaries and annual/quarter counts passed');

class Element {
  constructor(id=''){this.id=id;this.hidden=false;this.listeners={};this.attributes={};this.children=[];this.value='';this.textContent='';this.classList={toggle(){}};}
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  querySelector(){return new Element();}
  append(value){this.children.push(value);}
  replaceChildren(...values){this.children=values;}
  closest(){return null;}
  focus(){}
}
const elements=new Map();
const get=id=>{if(!elements.has(id)){const element=new Element(id);if(id==='industry-panel'||id==='sw-financial-progress'||id==='sw-snapshot-progress')element.hidden=true;
  if(id==='sw-financial-progress-toggle'){element.attributes['aria-expanded']='false';element.textContent='查看财务进度';}
  if(id==='sw-snapshot-progress-toggle'){element.attributes['aria-expanded']='false';element.textContent='查看快照进度';}
  elements.set(id,element);}return elements.get(id);};
context.document={getElementById:get,querySelector:()=>new Element(),createElement:()=>new Element()};
context.window.confirm=()=>false;
context.window.dispatchEvent=()=>{};
context.window.initIndustry({code:'600000'});
const panel=get('industry-panel').innerHTML;
let contextUpdate=null;
context.window.setResearchContext=value=>{contextUpdate=value;};
windowListeners['research-tab-changed']({detail:{tab:'industry'}});
assert.equal(contextUpdate.periodLabel,'全市场报告期');
assert.equal(contextUpdate.updatedLabel,'全市场数据更新');
assert.doesNotMatch(panel,/sw-(?:financial|snapshot)-progress|sw-job-status|sw-updates|前往采集管理/);
assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../web/features/industries/industry.js'),'utf8'),/api\/industry\/status|function poll\(/);
console.log('industry collection progress module is absent from the read-only comparison page');
