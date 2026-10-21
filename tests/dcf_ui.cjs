// Standalone Node checks; the DOM harness supplies only the browser APIs used by the module.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const file=path.join(__dirname,'../web/dcf.js');
const {calculate,calculateGrowth,forecastStandard,forecastThreeStage,discountCashFlows,wacc}=require(file);
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

// Golden values captured from the original fixed-growth implementation before this change.
for(const [args,expected] of [
  [[100,0,10,0,10,50],[614.456710570468,1000,385.5432894295314,999.9999999999993,99.99999999999993,0.49999999999999967]],
  [[1e8,3,9,2,1e8,10],[742141089.5582396,1958278152.758578,827197854.63295,1569338944.1911895,15.693389441911895,0.36278902419299686]],
  [[-100,-2.5,8.5,-.2,10,10],[-582.0224664983847,-890.5482317410629,-393.8764942859693,-975.8989607843539,-97.58989607843539,null]],
  [[0,3,9,2,10,10],[0,0,0,0,0,null]],
  [[1e8,15,12,2],[1159818385.5915484,4126468890.422065,1328612544.34001,2488430929.9315586,null,null]]
]) {
  const result=calculate(...args);
  assert.deepEqual(['explicit','terminalValue','terminalPV','total','perShare','priceGap'].map(key=>result[key]),expected);
}
const stages=forecastThreeStage(100,15,8,4);
close(stages[0],115);close(stages[2],100*1.15**3);
close(stages[3],stages[2]*1.08);close(stages[6],stages[2]*1.08**4);
close(stages[7],stages[6]*1.04);close(stages[9],100*1.15**3*1.08**4*1.04**3);
const growthModel=calculateGrowth(100,15,8,4,9,2,10,10);
close(growthModel.terminalValue,stages[9]*1.02/.07);
close(growthModel.explicit,stages.reduce((sum,value,i)=>sum+value/1.09**(i+1),0));
close(growthModel.terminalPV,growthModel.terminalValue/1.09**10);
close(growthModel.perShare,growthModel.total/10);close(growthModel.priceGap,1-10/growthModel.perShare);
assert.deepEqual(calculate(100,3,9,2,10,10),discountCashFlows(forecastStandard(100,3),9,2,10,10));
close(calculateGrowth(100,3,3,3,9,2).total,calculate(100,3,9,2).total);
assert.ok(calculateGrowth(100,-2.5,0,1.25,-1,-2).total>0);
assert.ok(calculateGrowth(100,15,8,4,0,-1).total>0);
assert.equal(calculateGrowth(0,15,8,4,9,2).total,0);
assert.equal(calculateGrowth(-100,15,8,4,9,2,10,10).priceGap,null);
for(const args of [[null,15,8,4,9,2],[100,NaN,8,4,9,2],[100,15,Infinity,4,9,2],[100,15,8,-100,9,2],[100,15,8,4,2,2],[100,15,8,4,-100,-101],[100,15,8,4,9,-100],[1e308,200,8,4,9,2]]) assert.ok(calculateGrowth(...args).error);
assert.ok(discountCashFlows([100],9,2).error);
assert.ok(discountCashFlows(Array(10),9,2).error,'sparse arrays are missing cash flows');

class Element {
  constructor(){this.listeners={};this.attrs={};this.value='';this.dataset={};this.nodes={};this.hidden=false;const classes=new Set();this.classList={contains:name=>classes.has(name),toggle(name,on){if(on)classes.add(name);else classes.delete(name);}};}
  addEventListener(name,fn){(this.listeners[name]||(this.listeners[name]=[])).push(fn);}
  setAttribute(name,value){this.attrs[name]=value;}
  fire(name){return this.listeners[name]?.map(fn=>fn()).at(-1);}
  set innerHTML(html){
    this.html=html;
    for(const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)){
      const child=new Element();child.value=(match[0].match(/value="([^"]*)"/)||[])[1]||'';
      if(match[1]==='select')child.value=(html.slice(match.index+match[0].length).match(/<option value="([^"]+)"/)||[])[1]||'';
      child.hidden=/\shidden(?:\s|>)/.test(match[0]);
      this.nodes[match[2]]=child;
    }
    this.recommend=[...html.matchAll(/<button[^>]*data-dcf-recommend="([^"]+)"[^>]*data-value="([^"]*)"[^>]*>/g)].map(match=>{const node=new Element();node.dataset={dcfRecommend:match[1],value:match[2]};const id=match[0].match(/\bid="([^"]+)"/);if(id)this.nodes[id[1]]=node;return node;});
  }
  get innerHTML(){return this.html||'';}
  querySelector(selector){return this.nodes[selector.slice(1)]||Object.values(this.nodes).map(node=>node.querySelector(selector)).find(Boolean);}
  querySelectorAll(){return [...(this.recommend||[]),...Object.values(this.nodes).flatMap(node=>node.querySelectorAll())];}
}
const fixture={code:'600519',name:'测试 <script>',model_label:'简化DCF（非FCFF/FCFE）',bases:{'1':{value:1.1e8,available:true,periods:['2025'],source:'saved'},'3':{value:1e8,available:true,periods:['2023','2024','2025'],source:'saved'},'5':{value:null,available:false,periods:['2021','2022','2023','2024','2025'],reason:'缺年'}},history:[{period:'2020-12-31',free_cash_flow:50},{period:'2021-12-31',free_cash_flow:75},{period:'2022-12-31',free_cash_flow:100},{period:'2023-12-31',free_cash_flow:150},{period:'2024-12-31',free_cash_flow:120},{period:'2025-12-31',free_cash_flow:180}],market:{total_shares:1e8,price:10},recommendations:{growth:{value_pct:4,basis:'历史中位数',status:'available'}},professional:{available:false,gaps:['利息分类待核对']},applicability:{blocked:false},warnings:[]};
async function domTest(data,failFirst=false,autoData=null){
  const metrics=Array.from({length:4},()=>new Element());metrics[0].classList.toggle('active',true);
  const button=new Element(),head={after(node){panel=node;}},host={querySelector(){return head;},querySelectorAll(){return metrics;}};
  let panel,requests=0;const plots=[];
  const Plotly={react(target,data,layout){plots.push({target,data,layout});target.plot={data,layout};},purge(target){delete target.plot;}};
  button.closest=()=>host;
  const standard=new Element();
  const document={readyState:'complete',createElement(){return new Element();},getElementById(id){return id==='dcf-toggle'?button:id==='valuation-standard'?standard:{textContent:JSON.stringify({code:'600519'})};}};
  const window=new Element();
  window.dispatchEvent=()=>{};
  window.DashboardTime=require('../web/datetime.js');
  vm.runInNewContext(fs.readFileSync(file,'utf8'),{window,document,Plotly,URLSearchParams,Event,fetch:async url=>{
    if(url.startsWith('/api/dcf/wacc?'))return {ok:true,json:async()=>autoData};
    requests++;assert.equal(url,'/api/dcf?code=600519');return {ok:!(failFirst&&requests===1),status:503,json:async()=>data};
  }});
  assert.equal(requests,0,'page mount must not request facts');
  button.fire('click');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests,1);assert.equal(panel.hidden,false);
  assert.equal(standard.hidden,true,'DCF replaces the ordinary valuation content');
  return {panel,button,window,metrics,plots,standard,getRequests:()=>requests};
}
(async()=>{
  const {panel,button,window,metrics,standard,getRequests}=await domTest(fixture);
  const get=id=>panel.querySelector('#dcf-'+id);
  assert.equal(get('base').value,'1');assert.equal(get('base').disabled,true);
  assert.equal(panel.querySelector('#dcf-growth-reference').textContent,'增长率参考：26.67%');
  assert.match(panel.querySelector('#dcf-growth-reference-basis').textContent,/样本3组/);
  const growthMethod=panel.querySelector('#dcf-growth-method');growthMethod.value='endpoint_cagr';growthMethod.fire('change');
  assert.equal(panel.querySelector('#dcf-growth-reference').textContent,'增长率参考：21.64%');
  growthMethod.value='annual_average';growthMethod.fire('change');
  panel.querySelector('#dcf-recommendations').querySelectorAll()[0].fire('click');assert.equal(get('growth').value,'26.667');get('growth').value='3';get('growth').fire('input');
  assert.equal(get('growth').value,'3');assert.equal(get('discount').value,'9');assert.equal(get('terminal').value,'2');
  assert.equal(button.classList.contains('active'),true);
  assert.ok(metrics.every(item=>!item.classList.contains('active')));
  for(const metric of metrics){
    metric.fire('click');
    assert.equal(panel.hidden,true);assert.equal(button.attrs['aria-expanded'],'false');
    assert.equal(standard.hidden,false);
    assert.equal(button.classList.contains('active'),false);
    assert.deepEqual(metrics.map(item=>item.classList.contains('active')),metrics.map(item=>item===metric));
    button.fire('click');
    assert.equal(panel.hidden,false);assert.ok(metrics.every(item=>!item.classList.contains('active')));
    assert.equal(getRequests(),1,'switching indicators must preserve cached DCF inputs');
  }
  assert.ok(get('evidence').innerHTML.includes('&lt;script&gt;'));
  assert.equal(get('mode').value,'standard');assert.equal(get('stage1-control').hidden,true);
  get('mode').value='three_stage';get('mode').fire('change');
  assert.equal(get('standard-controls').hidden,true);assert.equal(get('stage1-control').hidden,false);
  assert.equal(get('stage1').value,'15');assert.equal(get('stage2').value,'8');assert.equal(get('stage3').value,'4');
  assert.match(get('mode-basis').textContent,/示例/);
  assert.equal(panel.querySelector('#dcf-recommendations').querySelectorAll()[0].disabled,true);
  assert.match(get('results').innerHTML,/高增长期/);assert.match(get('results').innerHTML,/增速放缓期/);assert.match(get('results').innerHTML,/成熟过渡期/);
  const growthPlot=get('forecast-chart').plot;
  close(growthPlot.data[0].y[0],1.15);close(growthPlot.data[0].y[3],1.15**3*1.08);
  assert.equal(growthPlot.layout.xaxis.title.text,'预测年');assert.equal(growthPlot.layout.yaxis.title.text,'FCF（亿元）');
  assert.equal(growthPlot.layout.shapes.length,3);assert.match(growthPlot.layout.annotations[0].text,/高增长期<br>15.00%/);
  assert.equal(growthPlot.layout.yaxis.range,undefined,'negative cash flows must not be cropped');
  get('stage1').value='31.5';get('stage1').fire('input');assert.match(get('status').textContent,/超过30%/);assert.equal(get('stage1').value,'31.5');
  get('stage2').value='35';get('stage2').fire('input');assert.match(get('status').textContent,/突然上升/);
  get('stage3').value='1';get('stage3').fire('input');assert.match(get('status').textContent,/最后阶段/);
  get('discount').value='-1';get('terminal').value='-2';get('discount').fire('input');assert.match(get('status').textContent,/非常规/);assert.ok(get('results').innerHTML);
  get('stage1').value='';get('stage1').fire('input');assert.equal(get('results').innerHTML,'');assert.equal(get('forecast-chart').hidden,true);assert.equal(get('forecast-chart').plot,undefined);
  get('stage1').value='17.25';get('discount').value='9';get('terminal').value='2';get('stage1').fire('input');
  const growthBefore=get('results').innerHTML;
  get('mode').value='standard';get('mode').fire('change');assert.equal(get('forecast-chart').hidden,true);
  assert.equal(get('forecast-chart').plot,undefined);assert.equal(get('growth').value,'3');assert.equal(get('standard-controls').hidden,false);
  get('growth').value='1e20';get('growth').fire('input');
  get('mode').value='three_stage';get('mode').fire('change');assert.equal(get('stage1').value,'17.25');assert.equal(get('results').innerHTML,growthBefore);
  assert.match(get('results').innerHTML,/15.25→33.00→-1.00/,'growth scenarios must apply explicit offsets independent of hidden standard input');
  get('mode').value='standard';get('mode').fire('change');get('growth').value='';get('growth').fire('input');
  get('mode').value='three_stage';get('mode').fire('change');assert.equal(get('results').innerHTML,growthBefore);
  get('growth').value='3';
  metrics[1].fire('click');button.fire('click');assert.equal(get('mode').value,'three_stage');assert.equal(get('stage1').value,'17.25');
  assert.equal(getRequests(),1,'mode, growth and chart changes use saved local data');
  get('mode').value='standard';get('mode').fire('change');

  const before=get('results').innerHTML;get('growth').value='5';get('growth').fire('input');
  assert.notEqual(get('results').innerHTML,before);assert.equal(getRequests(),1,'assumption changes must remain local');
  get('discount').value='2';get('discount').fire('input');assert.equal(get('results').innerHTML,'');assert.match(get('status').textContent,/必须大于/);
  get('discount').value='9';get('window').value='5';get('window').fire('change');assert.equal(get('base').value,'');assert.equal(get('results').innerHTML,'');
  get('window').value='manual';get('window').fire('change');assert.equal(get('base').disabled,false);get('base').value='-1';get('base').fire('input');assert.match(get('status').textContent,/负/);
  get('mode').value='three_stage';get('mode').fire('change');assert.ok(get('forecast-chart').plot.data[0].y.every(value=>value<0));get('mode').value='standard';get('mode').fire('change');
  get('window').value='5';get('window').fire('change');assert.match(panel.querySelector('#dcf-growth-reference-basis').textContent,/样本5组/);assert.equal(panel.querySelector('#dcf-growth-reference').textContent,'增长率参考：32.67%');
  growthMethod.value='endpoint_cagr';growthMethod.fire('change');assert.equal(panel.querySelector('#dcf-growth-reference').textContent,'增长率参考：29.20%');
  get('window').value='1';get('window').fire('change');assert.equal(panel.querySelector('#dcf-growth-reference').textContent,'增长率参考：50.00%');assert.match(panel.querySelector('#dcf-growth-reference-basis').textContent,/首尾复合增长率/);
  get('window').value='3';get('window').fire('change');
  get('apply-wacc').fire('click');assert.match(get('wacc-result').textContent,/完整填写/);
  for(const [key,value] of Object.entries({rf:2,beta:1,erp:5,cost:4,tax:25,weight:20}))get(key).value=String(value);
  get('apply-wacc').fire('click');assert.equal(get('discount').value,'6.2','applying WACC must not require manual source notes or a date');
  button.fire('click');button.fire('click');assert.equal(getRequests(),1,'cached reopening must not request facts again');
  window.listeners['financial-facts-updated'][0]({detail:{code:'600519'}});
  assert.match(get('status').textContent,/财务依据已刷新/);
  get('growth').fire('input');assert.equal(getRequests(),1);assert.match(get('status').textContent,/财务依据已刷新/);
  button.fire('click');button.fire('click');await new Promise(resolve=>setImmediate(resolve));assert.equal(getRequests(),2);
  const financial=await domTest({...fixture,applicability:{financial_company:true}});
  financial.panel.querySelector('#dcf-mode').value='three_stage';financial.panel.querySelector('#dcf-mode').fire('change');
  assert.equal(financial.panel.querySelector('#dcf-forecast-chart').plot,undefined);assert.equal(financial.panel.querySelector('#dcf-forecast-chart').hidden,true);
  assert.equal(financial.panel.querySelector('#dcf-results').innerHTML,'');assert.match(financial.panel.querySelector('#dcf-status').textContent,/金融企业/);
  const configured=await domTest({...fixture,growth_defaults:{stage1_pct:12.5,stage2_pct:6,stage3_pct:2.5},defaults:{base_window:'1',growth_pct:1,discount_pct:8,terminal_growth_pct:0}});
  assert.equal(configured.panel.querySelector('#dcf-window').value,'1');assert.equal(configured.panel.querySelector('#dcf-growth').value,'1');assert.equal(configured.panel.querySelector('#dcf-terminal').value,'0');
  assert.equal(configured.panel.querySelector('#dcf-stage1').value,12.5);assert.equal(configured.panel.querySelector('#dcf-stage3').value,2.5);
  const autoData={as_of_date:'2026-10-15',parameters:{rf:{value:1.678,status:'observed',source:'test',as_of_date:'2026-10-14'},beta:{value:0.1273084078625435,status:'estimated',sample_count:200,start:'2021-10-15',end:'2026-10-15'},erp:{value:6,status:'assumption'},kd:{value:3.2470819173262853,status:'estimated'},tax:{value:16.268795579183625,history_value:16.268795579183625,status:'estimated',history_status:'estimated'},weight:{value:28.865160940429256,status:'estimated'}},warnings:[]};
  const referenceAutomatic=await domTest(fixture,false,autoData);
  await referenceAutomatic.panel.querySelector('#dcf-estimate-wacc-reference').fire('click');
  for(const [id,value] of Object.entries({rf:'1.678',beta:'0.127',erp:'6',cost:'3.247',tax:'16.269',weight:'28.865'}))assert.equal(referenceAutomatic.panel.querySelector('#dcf-'+id).value,value,`reference-card estimation should populate ${id}`);
  assert.equal(referenceAutomatic.panel.querySelector('#dcf-reset-wacc').disabled,false);
  assert.equal(referenceAutomatic.panel.querySelector('#dcf-wacc-as-of').textContent,'2026-10-15');
  assert.match(referenceAutomatic.panel.querySelector('#dcf-wacc-evidence').innerHTML,/历史有效税率，自动用于WACC/);
  assert.match(referenceAutomatic.panel.querySelector('#dcf-discount-reference').textContent,/折现率参考：\d+\.\d+%/);
  const automatic=await domTest(fixture,false,autoData);
  await automatic.panel.querySelector('#dcf-auto-wacc').fire('click');
  assert.equal(automatic.panel.querySelector('#dcf-rf').value,'1.678');assert.equal(automatic.panel.querySelector('#dcf-beta').value,'0.127');
  assert.equal(automatic.panel.querySelector('#dcf-cost').value,'3.247');assert.equal(automatic.panel.querySelector('#dcf-tax').value,'16.269');
  assert.equal(automatic.panel.querySelector('#dcf-weight').value,'28.865');
  assert.equal(automatic.panel.querySelector('#dcf-wacc-as-of').textContent,'2026-10-15');
  assert.match(automatic.panel.querySelector('#dcf-wacc-evidence').innerHTML,/历史有效税率，自动用于WACC/);
  assert.match(automatic.panel.querySelector('#dcf-wacc-result').textContent,/WACC/,'automatic estimates should calculate WACC immediately when all inputs exist');
  assert.match(automatic.panel.querySelector('#dcf-wacc-result').textContent,/WACC \d+\.\d{1,3}%/,'WACC results should display at most three decimal places');
  const referenceEstimate=wacc({rf:1.678,beta:0.1273084078625435,erp:6,cost:3.2470819173262853,tax:16.268795579183625,weight:28.865160940429256}).value;
  await automatic.panel.querySelector('#dcf-estimate-wacc-reference').fire('click');
  assert.equal(automatic.panel.querySelector('#dcf-discount-reference').textContent,`折现率参考：${Number(referenceEstimate.toFixed(3)).toString()}%`);
  automatic.panel.querySelector('#dcf-apply-discount-reference').fire('click');
  assert.equal(automatic.panel.querySelector('#dcf-discount').value,Number(referenceEstimate.toFixed(3)).toString(),'the WACC reference applies only after clicking the card action');
  automatic.panel.querySelector('#dcf-beta').value='1.5';automatic.panel.querySelector('#dcf-beta').fire('input');
  assert.match(automatic.panel.querySelector('#dcf-wacc-evidence').innerHTML,/人工覆盖/);
  automatic.panel.querySelector('#dcf-reset-wacc').fire('click');assert.equal(automatic.panel.querySelector('#dcf-beta').value,'0.127');
  automatic.panel.querySelector('#dcf-tax').value='16.269';automatic.panel.querySelector('#dcf-tax').fire('input');
  assert.match(automatic.panel.querySelector('#dcf-wacc-sensitivity').innerHTML,/Beta/);
  const retry=await domTest(fixture,true);assert.match(retry.panel.innerHTML,/HTTP 503/);
  retry.button.fire('click');retry.button.fire('click');await new Promise(resolve=>setImmediate(resolve));assert.equal(retry.getRequests(),2);assert.ok(retry.panel.querySelector('#dcf-results').innerHTML);
  console.log('DCF formula, boundaries, WACC, and DOM interaction checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
