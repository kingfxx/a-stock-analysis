const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const page = fs.readFileSync(path.join(__dirname,'../web/pages/index.html'),'utf8');
assert.match(page, /手动刷新[\s\S]*id="full-audit"[^>]*type="button">全量刷新/);
const source = page.slice(page.indexOf('  function expandValuationPayload('),page.indexOf('  const state =')) + page.slice(page.indexOf('  // Background data loading.'),page.lastIndexOf("  if (typeof Plotly !== 'undefined') { render(); renderValuation(); }"));
const tick = () => new Promise(resolve=>setImmediate(resolve));
function setup(extra={}) {
  const pending=[], elements=new Map(), events=[];
  const context={state:{code:'601600',name:'中国铝业',views:{quarter:[{profit:10}]},valuation:{views:{}},loading:{financial:true},warnings:[],...extra},
    document:{getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'手动刷新',listeners:{},setAttribute(){},addEventListener(k,f){this.listeners[k]=f;}});return elements.get(id);}},
    window:{DashboardTime:require('../web/shared/datetime.js'),dispatchEvent(event){if(event.type==='dashboard-data-state')events.push(event.detail.updating);}},
    CustomEvent:class{constructor(type,init){this.type=type;this.detail=init.detail;}},
    render(){},renderValuation(){},renderChips(){},renderCurrentStock(){},renderCachedStocks(){},renderWarnings(){},renderFollowState(){},
    confirm:()=>false,fetch:(url,options={})=>new Promise(resolve=>pending.push({url,options,resolve}))};
  vm.createContext(context);vm.runInContext(source,context);
  function respond(data,ok=true){const request=pending.shift();assert.ok(request);request.resolve({ok,json:async()=>data});return request;}
  return {context,pending,elements,events,respond};
}
(async()=>{
  for(const extra of [{},{price_version:1,price_needs_update:true},{collection:{needs_initial:false}}]){
    const f=setup(extra);await f.context.loadDashboard();assert.equal(f.pending.length,0);assert.deepEqual(f.events,[]);
  }
  let f=setup({collection:{needs_initial:true}});let work=f.context.loadDashboard();
  assert.equal(f.pending[0].options.method,'POST');assert.equal(JSON.parse(f.pending[0].options.body).initial,true);
  await f.context.loadDashboard();assert.equal(f.pending.length,1);
  f.respond({status:'running',operation_ref:'stock:one'});await tick();
  assert.match(f.pending[0].url,/stock-collection\?code=601600/);
  f.respond({needs_initial:false,operation:{status:'complete'}});await tick();
  const bundle={price_version:2,financial:{views:{quarter:[{profit:20,qfq_close:22}]}},valuation:{views:{'10':{pe:{rows:[{qfq_close:22}]}}}},shareholders:{price_version:2,rows:[]},financing:{price_version:2,rows:[]}};
  assert.equal(f.pending[0].url,'/api/prices?code=601600');f.respond(bundle);await work;
  assert.equal(f.context.state.views.quarter[0].profit,20);assert.equal(f.context.state.price_version,2);
  assert.equal(f.context.state.financing.price_version,2);assert.deepEqual(f.events,[true,false]);
  f=setup();work=f.context.loadDashboard(true);assert.equal(JSON.parse(f.pending[0].options.body).params.mode,'incremental');
  f.respond({error:'source unavailable'},false);await work;
  assert.equal(f.context.state.views.quarter[0].profit,10);assert.match(f.elements.get('stock-collection-status').textContent,/source unavailable/);
  const full=f.elements.get('full-audit');full.listeners.click();assert.equal(f.pending.length,0);
  f.context.confirm=message=>{assert.match(message,/仅刷新当前股票/);return true;};
  work=full.listeners.click();assert.equal(JSON.parse(f.pending[0].options.body).params.mode,'full');
  f.respond({status:'running'});await tick();f.respond({operation:{status:'failed',error:'partial failure'}});await tick();
  f.respond(bundle);await work;assert.match(f.elements.get('stock-collection-status').textContent,/partial failure/);
  assert.equal(full.disabled,false);
  f=setup({collection:{operation:{status:'running'}}});work=f.context.loadDashboard();
  assert.equal(f.pending[0].options.method,undefined);
  f.context.state.code='000001';f.respond({operation:{status:'complete'}});await work;
  assert.equal(f.pending.length,0);assert.equal(f.context.state.views.quarter[0].profit,10);
  console.log('Cache-only visits, initial intent, shared operations, partial failures and stale response guard passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
