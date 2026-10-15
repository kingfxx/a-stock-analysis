// Standalone Node checks; the DOM harness supplies only the browser APIs used by the module.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const file=path.join(__dirname,'../web/dcf.js');
const {calculate,wacc}=require(file);
const close=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-8*Math.max(1,Math.abs(expected)),`${actual} != ${expected}`);
// Constant cash flow with zero terminal growth is a perpetuity worth FCF/r.
const constant=calculate(100,0,10,0,10,50);
close(constant.total,1000);close(constant.perShare,100);close(constant.priceGap,.5);
assert.equal(constant.rows.length,10);close(constant.terminalValue,1000);
close(constant.rows[0].pv,100/1.1);
const growing=calculate(1e8,3,9,2,1e8,10);
close(growing.rows[9].fcf,1e8*1.03**10);
close(growing.terminalValue,1e8*1.03**10*1.02/.07);
close(growing.terminalPV,growing.terminalValue/1.09**10);
close(growing.total,growing.explicit+growing.terminalPV);
close(growing.perShare,growing.total/1e8);
for(const args of [[null,3,9,2],[NaN,3,9,2],[100,Infinity,9,2],[100,3,2,2],[100,3,1,2],[100,-100,9,2],[100,3,0,-1],[100,3,9,-100],[1e308,200,9,2]]) assert.ok(calculate(...args).error);
assert.equal(calculate(-100,3,9,2,10,10).priceGap,null);
assert.ok(calculate(-100,3,9,2).total<0);
assert.equal(calculate(0,3,9,2,10,10).priceGap,null);
assert.equal(calculate(100,3,9,2,0).perShare,null);
assert.equal(calculate(100,3,9,2,null).perShare,null);
assert.equal(calculate(100,3,9,2,10,null).priceGap,null);
close(wacc({rf:2,beta:1,erp:5,cost:4,tax:25,weight:20}).value,6.2);
for(const values of [{},{rf:2,beta:1,erp:5,cost:4,tax:101,weight:20},{rf:2,beta:-1,erp:5,cost:4,tax:25,weight:20},{rf:2,beta:1,erp:5,cost:4,tax:25,weight:-1}]) assert.ok(wacc(values).error);

class Element {
  constructor(){this.listeners={};this.attrs={};this.value='';this.dataset={};this.nodes={};this.hidden=false;const classes=new Set();this.classList={contains:name=>classes.has(name),toggle(name,on){if(on)classes.add(name);else classes.delete(name);}};}
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(name,value){this.attrs[name]=value;}
  fire(name){return this.listeners[name]?.();}
  set innerHTML(html){
    this.html=html;
    for(const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)){
      const child=new Element();child.value=(match[0].match(/value="([^"]*)"/)||[])[1]||'';
      if(match[1]==='select')child.value='3';
      this.nodes[match[2]]=child;
    }
    this.recommend=[...html.matchAll(/<button[^>]*data-dcf-recommend="([^"]+)"[^>]*data-value="([^"]*)"[^>]*>/g)].map(match=>{const node=new Element();node.dataset={dcfRecommend:match[1],value:match[2]};return node;});
  }
  get innerHTML(){return this.html||'';}
  querySelector(selector){return this.nodes[selector.slice(1)];}
  querySelectorAll(){return [...(this.recommend||[]),...Object.values(this.nodes).flatMap(node=>node.querySelectorAll())];}
}
const fixture={code:'600519',name:'测试 <script>',model_label:'简化DCF（非FCFF/FCFE）',bases:{'3':{value:1e8,available:true,periods:['2023','2024','2025'],source:'saved'},'5':{value:null,available:false,reason:'缺年'}},history:[],market:{total_shares:1e8,price:10},recommendations:{growth:{value_pct:4,basis:'历史中位数',status:'available'}},professional:{available:false,gaps:['利息分类待核对']},applicability:{blocked:false},warnings:[]};
async function domTest(data,failFirst=false){
  const metrics=Array.from({length:4},()=>new Element());metrics[0].classList.toggle('active',true);
  const button=new Element(),head={after(node){panel=node;}},host={querySelector(){return head;},querySelectorAll(){return metrics;}};
  let panel,requests=0;
  button.closest=()=>host;
  const document={readyState:'complete',createElement(){return new Element();},getElementById(id){return id==='dcf-toggle'?button:{textContent:JSON.stringify({code:'600519'})};}};
  const window=new Element();
  vm.runInNewContext(fs.readFileSync(file,'utf8'),{window,document,URLSearchParams,fetch:async url=>{requests++;assert.equal(url,'/api/dcf?code=600519');return {ok:!(failFirst&&requests===1),status:503,json:async()=>data};}});
  assert.equal(requests,0,'page mount must not request facts');
  button.fire('click');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests,1);assert.equal(panel.hidden,false);
  return {panel,button,window,metrics,getRequests:()=>requests};
}
(async()=>{
  const {panel,button,window,metrics,getRequests}=await domTest(fixture);
  const get=id=>panel.querySelector('#dcf-'+id);
  assert.equal(get('base').value,1);assert.equal(get('base').disabled,true);
  assert.equal(get('growth').value,'3');assert.equal(get('discount').value,'9');assert.equal(get('terminal').value,'2');
  assert.equal(button.classList.contains('active'),true);
  assert.ok(metrics.every(item=>!item.classList.contains('active')));
  for(const metric of metrics){
    metric.fire('click');
    assert.equal(panel.hidden,true);assert.equal(button.attrs['aria-expanded'],'false');
    assert.equal(button.classList.contains('active'),false);
    assert.deepEqual(metrics.map(item=>item.classList.contains('active')),metrics.map(item=>item===metric));
    button.fire('click');
    assert.equal(panel.hidden,false);assert.ok(metrics.every(item=>!item.classList.contains('active')));
    assert.equal(getRequests(),1,'switching indicators must preserve cached DCF inputs');
  }
  assert.ok(get('evidence').innerHTML.includes('&lt;script&gt;'));
  const before=get('results').innerHTML;get('growth').value='5';get('growth').fire('input');
  assert.notEqual(get('results').innerHTML,before);assert.equal(getRequests(),1,'assumption changes must remain local');
  get('discount').value='2';get('discount').fire('input');assert.equal(get('results').innerHTML,'');assert.match(get('status').textContent,/必须大于/);
  get('discount').value='9';get('window').value='5';get('window').fire('change');assert.equal(get('base').value,'');assert.equal(get('results').innerHTML,'');
  get('window').value='manual';get('window').fire('change');assert.equal(get('base').disabled,false);get('base').value='-1';get('base').fire('input');assert.match(get('status').textContent,/负/);
  panel.querySelector('#dcf-recommendations').querySelectorAll()[0].fire('click');assert.equal(get('growth').value,'4');
  get('apply-wacc').fire('click');assert.match(get('wacc-result').textContent,/完整填写/);
  for(const [key,value] of Object.entries({rf:2,beta:1,erp:5,cost:4,tax:25,weight:20}))get(key).value=String(value);
  get('apply-wacc').fire('click');assert.match(get('wacc-result').textContent,/来源依据和日期/);
  get('wacc-source').value='人工来源';get('wacc-date').value='2026-10-14';get('apply-wacc').fire('click');assert.equal(get('discount').value,'6.2000');
  button.fire('click');button.fire('click');assert.equal(getRequests(),1,'cached reopening must not request facts again');
  window.listeners['financial-facts-updated']({detail:{code:'600519'}});
  assert.match(get('status').textContent,/财务依据已刷新/);
  get('growth').fire('input');assert.equal(getRequests(),1);assert.match(get('status').textContent,/财务依据已刷新/);
  button.fire('click');button.fire('click');await new Promise(resolve=>setImmediate(resolve));assert.equal(getRequests(),2);
  const financial=await domTest({...fixture,applicability:{financial_company:true}});
  assert.equal(financial.panel.querySelector('#dcf-results').innerHTML,'');assert.match(financial.panel.querySelector('#dcf-status').textContent,/金融企业/);
  const configured=await domTest({...fixture,defaults:{base_window:'1',growth_pct:1,discount_pct:8,terminal_growth_pct:0}});
  assert.equal(configured.panel.querySelector('#dcf-window').value,'1');assert.equal(configured.panel.querySelector('#dcf-growth').value,1);assert.equal(configured.panel.querySelector('#dcf-terminal').value,0);
  const retry=await domTest(fixture,true);assert.match(retry.panel.innerHTML,/HTTP 503/);
  retry.button.fire('click');retry.button.fire('click');await new Promise(resolve=>setImmediate(resolve));assert.equal(retry.getRequests(),2);assert.ok(retry.panel.querySelector('#dcf-results').innerHTML);
  console.log('DCF formula, boundaries, WACC, and DOM interaction checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
