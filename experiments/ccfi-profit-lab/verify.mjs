// Calculation and render-path verification without a browser or network.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',style:{},querySelectorAll:()=>[],querySelector:()=>({style:{}})});return elements.get(id);}
const context=vm.createContext({document:{getElementById:element},structuredClone,console,Blob,URL,setTimeout,location:{hash:''}});
const html=fs.readFileSync(new URL('./index.html',import.meta.url),'utf8');
const script=html.match(/<script>([\s\S]*)<\/script>/)[1];
vm.runInContext(script,context);
vm.runInContext(`
 const assert=(v,msg)=>{if(!v)throw Error(msg)};
 const near=(a,b,t=.0001)=>Math.abs(a-b)<t;
 assert(near(anchor.profit,75.4198,.001),'Q2 parent profit');
 assert(near(anchor.cost,485.296,.001),'Q2 operating cost');
 assert(near(b.international,489.9946,.001),'international revenue definition');
 assert(near(b.teu,5730197,1),'international TEU');
 const unchanged=calc({...defaults.base,rate:D.models['1'].anchor,volume:0,cost:0,fx:0,below:b.below,conversion:b.conversion*100,extra:0,override:0});
 assert(near(unchanged.profit,anchor.profit),'zero-shock bridge reconciles to actual Q2');
 const article=calc({...defaults.base,override:28.6865,volume:2,cost:4.5,below:-14.0143,conversion:73.9857,extra:0});
 assert(near(article.profit,166.7312,.03),'article reproduction');
 const base=calc(defaults.base),bear=calc(defaults.bear),bull=calc(defaults.bull);
 assert(bear.profit<base.profit&&base.profit<bull.profit,'ordered scenarios');
 assert(calc({...defaults.base,cost:10}).profit<base.profit,'higher cost reduces profit');
 assert(calc({...defaults.base,rate:2000}).profit>base.profit,'higher CCFI increases profit');
 assert(near(calc({...defaults.base,extra:1}).profit-base.profit,1),'after-tax additive adjustment');
 assert(calc({...defaults.base,override:0}).growth===0,'zero override is valid');
 for(const lag of ['0','1','2'])for(const bt of D.models[lag].backtests)assert(bt.training_end<bt.label,'no future quarters in regression');
 assert(D.models['1'].n===13&&D.models['1'].backtests.length===7,'sample coverage');
 assert(D.history.every(r=>r.period<='2026-06-30'),'financial cutoff');
 assert(D.calibration.rows.length===14&&D.calibration.common_sample.length===13,'business quarter coverage');
 assert(D.calibration.reports.length===14&&D.calibration.comparative_differences.length===0,'sources and comparative reconciliation');
 assert(defaults.base.revenueModel==='linear','empirical linear default');
 const fitModels=D.calibration.analyses.unit_rmb['1'];
 for(const fit of fitModels){
   const forecast=calc({...defaults.base,revenueModel:fit.kind});
   assert(near(forecast.growth/100,fit.q3_anchored_growth),'frontend matches fitted unit-revenue model '+fit.kind);
   assert(near(forecast.profit,fit.q3_profit_same_cost_assumptions),'frontend matches fitted earnings '+fit.kind);
   assert(near(calibratedGrowth({...defaults.base,revenueModel:fit.kind,rate:D.models['1'].anchor},D.models['1']),0),'Q2 anchoring '+fit.kind);
 }
 assert(near(calc({...defaults.base,revenueModel:'manual'}).profit,base.profit),'equivalent elasticity at default window');
 assert(D.calibration.analyses.unit_rmb['1'][2].rolling_mae_start5===null,'unidentified high-regime model not scored');
 assert(D.weeklyStudy.workbook.hash===D.workbook.hash,'weekly workbook version');
 assert(JSON.stringify(D.weeklyStudy.release_mean.common_sample)===JSON.stringify(D.calibration.common_sample),'identical weekly/monthly quarter sample');
 const ws=D.weeklyStudy.release_mean;
 assert(ws.targets.w2.start==='2026-06-17'&&ws.targets.w2.end_exclusive==='2026-09-17','two-week boundaries');
 assert(ws.targets.w2.expected===13&&ws.targets.w2.observed===13,'complete two-week quarter window');
 assert(near(ws.targets.w2.value,ws.targets.w2.points.reduce((sum,r)=>sum+r.value,0)/13),'two-week mean');
 for(const result of Object.values(ws.analyses))for(const field of ['unit_rmb','profit'])for(const bt of result[field].backtests)assert(bt.training_end<bt.label,'weekly no future financial quarter');
 assert(D.months['2024-01']===1441,'original monthly data retained');
 assert(near(D.weeklyStudy.monthly_reconciliation.find(r=>r.month==='2024-01').weekly_mean,1191.75),'monthly transcription discrepancy exposed');
 $('weeklyMethod').value='daily_asof';$('weeklyLag').value='2';$('weeklyMethod').onchange();
 assert($('weeklySelected').textContent.includes('按')||$('weeklySelected').textContent.includes('提前2周'),'weekly selector render');
 assert($('weeklyResults').innerHTML.includes('240.8'),'daily method results visible');
 $('weeklyMethod').value='release_mean';$('weeklyLag').value='3';$('weeklyLag').onchange();
 assert($('weeklySelected').textContent.includes('提前3周'),'weekly lag change');
 assert(near(calc(defaults.base).profit,171.89088645163466),'monthly default preserved after weekly study');
 // Check annual-report independently disclosed quarter values against extracted DB history.
 const p2024=[67.5500022775,101.1510828654,212.5395239772,109.7643432154];
 D.history.filter(r=>r.year===2024).forEach((r,i)=>assert(near(r.profit,p2024[i]),'2024 annual report reconciliation'));
 const p2025=[116.9453884582,58.4155339518,95.3349767656,37.9855790382];
 D.history.filter(r=>r.year===2025).forEach((r,i)=>assert(near(r.profit,p2025[i]),'2025 annual report reconciliation'));
 $('articlePreset').onclick(); assert(active==='base'&&cases.base.override===28.6865,'article preset');
 $('reset').onclick();assert(cases.base.override===null,'scenario reset');
 console.log(JSON.stringify({scenarios:{bear:bear.profit,base:base.profit,bull:bull.profit},article:article.profit,mae:D.models['1'].mae,regression:base.regression,renderedSections:['cards','bridge','heat','backtest','sourceList'].every(id=>$(id).innerHTML.length>0)},null,2));
`,context);
assert(!html.includes('__DATA__'));
assert(!html.includes('__CALIBRATION_SCRIPT__'));
assert(!html.includes('__WEEKLY_SCRIPT__')&&!html.includes('__WEEKLY_SECTION__'));
for(const source of vm.runInContext('D.calibration.reports',context)){
 const data=fs.readFileSync(new URL('../../'+source.path,import.meta.url));
 assert.equal(createHash('sha256').update(data).digest('hex'),source.sha256,'report hash '+source.period);
}
assert(!/<script[^>]+src=|<link[^>]+href=/.test(html),'offline page must not require external assets');
console.log('PASS: calculations, source reconciliation, scenario/reset actions, render paths, offline dependencies.');
