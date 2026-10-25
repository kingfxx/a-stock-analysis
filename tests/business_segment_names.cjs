const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const context={window:{}};vm.createContext(context);
for(const file of ['business-segment-aliases.js','business-segment-names.js','business-segments.js'])
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/features/financials',file),'utf8'),context);
const {normalize}=context.window.businessSegmentNames;
const {series,yoySeries}=context.window.businessSegmentPresentation;
const row=(name,value=100,extra={})=>({item_name:name,item_key:JSON.stringify([extra.item_code??null,name]),revenue:value,cost:60,gross_profit:40,revenue_share:0.5,cost_share:0.4,profit_share:0.6,gross_margin:0.4,...extra});
const p=(date,dimension,rows)=>({report_date:date,dimensions:{[dimension]:rows}});
const original=[p('2020-12-31','2',[row('能量饮料',100,{item_code:'001'})]),p('2021-12-31','2',[row('东鹏特饮',150,{item_code:'001'})]),p('2022-12-31','2',[row('能量饮料',180,{item_code:null})])];
const saved=JSON.stringify(original),energy=normalize('605499',original);
assert.equal(JSON.stringify(original),saved,'raw facts never mutate');
assert.equal(new Set(energy.periods.map(x=>x.dimensions['2'][0].item_key)).size,1);
assert.equal(energy.periods.every(x=>x.dimensions['2'][0].item_name==='能量饮料'),true);
const keys=new Set([energy.periods[0].dimensions['2'][0].item_key]);
assert.equal(series(energy.periods,'2','revenue',keys).length,1,'one trace across renaming');
assert.equal(yoySeries(energy.periods,energy.periods,'2','revenue',keys)[1].y[0],50,'yoy crosses approved alias');
assert.equal(normalize('600000',original).periods[1].dimensions['2'][0].item_name,'东鹏特饮','brand rule scoped to company');
assert.equal(normalize('605499',[p('2021-12-31','3',[row('东鹏特饮')])]).periods[0].dimensions['3'][0].item_name,'东鹏特饮','brand rule scoped to dimension');
const abroad=normalize('000333',[p('2023-12-31','3',[row('国外',100)]),p('2024-12-31','3',[row('海外',120)])]);
assert.equal(abroad.periods.every(x=>x.dimensions['3'][0].item_name==='海外'),true);
const transport=normalize('601919',[p('2023-12-31','2',[row('集装箱航运分部')]),p('2024-12-31','2',[row('集装箱航运及相关业务')]),p('2025-12-31','2',[row('集装箱航运业务')])]);
assert.equal(new Set(transport.periods.map(x=>x.dimensions['2'][0].item_key)).size,1);
const codeReuse=normalize('600000',[p('2020-12-31','2',[row('业务甲',100,{item_code:'001'})]),p('2021-12-31','2',[row('业务乙',200,{item_code:'001'})])]);
assert.notEqual(codeReuse.periods[0].dimensions['2'][0].item_key,codeReuse.periods[1].dimensions['2'][0].item_key,'reused codes never merge unrelated businesses');
const cosmetic=normalize('600000',[p('2020-12-31','2',[row('航运业务分部')]),p('2021-12-31','2',[row('航运业务')])]);
assert.equal(cosmetic.periods[0].dimensions['2'][0].item_key,cosmetic.periods[1].dimensions['2'][0].item_key);
const parenthetical=normalize('600000',[p('2020-12-31','3',[row('中国地区(业务甲)')]),p('2021-12-31','3',[row('中国地区(业务乙)')])]);
assert.notEqual(parenthetical.periods[0].dimensions['3'][0].item_key,parenthetical.periods[1].dimensions['3'][0].item_key,'unknown parenthetical scope never stripped');
const geographic=normalize('600000',[p('2020-12-31','3',[row('华北区域')]),p('2021-12-31','3',[row('华北战区')]),p('2022-12-31','3',[row('华北区域(含北方大区)')])]);
assert.equal(geographic.periods[0].dimensions['3'][0].item_key,geographic.periods[1].dimensions['3'][0].item_key);
assert.notEqual(geographic.periods[1].dimensions['3'][0].item_key,geographic.periods[2].dimensions['3'][0].item_key,'expanded geographic boundary remains distinct');
assert.equal(normalize('600000',[p('2020-12-31','2',[row('干散货航运及相关业务')])]).periods[0].dimensions['2'][0].item_name,'干散货航运及相关业务');

// Same-period semantic collisions block the group across ALL years, even equal amounts.
const collision=normalize('605499',[...original,p('2023-12-31','2',[row('东鹏特饮',100),row('能量饮料',100)])]);
assert.equal(collision.conflicts.length,1);
assert.equal(collision.periods.at(-1).dimensions['2'].length,2);
assert.notEqual(collision.periods[0].dimensions['2'][0].item_key,collision.periods[1].dimensions['2'][0].item_key);
const foreignConflict=normalize('000333',[p('2024-12-31','3',[row('海外'),row('国外')])]);
assert.equal(foreignConflict.periods[0].dimensions['3'].length,2);

// Only explicit disjoint regional business partitions are additive.
const shipping=row('中国地区（集装箱航运及相关业务）',100,{cost:80,gross_profit:20,revenue_share:0.4,cost_share:null,profit_share:0.2,gross_margin:0.2});
const port=row('中国地区(码头及相关业务)',50,{cost:30,gross_profit:20,revenue_share:0.2,cost_share:0.15,profit_share:0.2,gross_margin:0.4});
const regionalRaw=[p('2017-12-31','3',[shipping,port]),p('2018-12-31','3',[row('中国地区',180)]),p('2019-12-31','3',[row('中国大陆',200)])];
const regions=normalize('601919',regionalRaw),combined=regions.periods[0].dimensions['3'][0];
assert.equal(combined.item_name,'中国大陆');assert.equal(combined.revenue,150);assert.equal(combined.cost,110);assert.equal(combined.gross_profit,40);
assert.ok(Math.abs(combined.revenue_share-0.6)<1e-12);assert.equal(combined.cost_share,null,'partial share never silently treated as zero');
assert.ok(Math.abs(combined.gross_margin-40/150)<1e-12,'margin weighted by complete gross profit and revenue');
assert.equal(combined.source_items.length,2);assert.equal(combined.calculated_fields.includes('gross_margin'),true);
assert.equal(new Set(regions.periods.map(x=>x.dimensions['3'][0].item_key)).size,1);
assert.equal(regions.periods[0].source_dimensions['3'].length,2,'full original table retained');
const missing=normalize('601919',[p('2017-12-31','3',[shipping,{...port,cost:null,gross_profit:null}])]).periods[0].dimensions['3'][0];
assert.equal(missing.revenue,150);assert.equal(missing.cost,null);assert.equal(missing.gross_profit,null);assert.equal(missing.gross_margin,null);
const totalConflict=normalize('601919',[p('2017-12-31','3',[shipping,port,row('中国地区',150)])]);
assert.equal(totalConflict.periods[0].dimensions['3'].length,3,'parent and partition cannot be added together');
assert.equal(totalConflict.conflicts.length,1);
const samePartition=normalize('601919',[p('2017-12-31','3',[shipping,{...shipping,item_key:'another'}])]);
assert.equal(samePartition.conflicts.length,1,'duplicate partition blocked');
const unrelatedCompany=normalize('600000',[p('2017-12-31','3',[shipping,port])]);
assert.equal(unrelatedCompany.periods[0].dimensions['3'].length,2);
const excluded=normalize('605499',[p('2020-12-31','2',[row('能量饮料'),row('其中:东鹏特饮',60),row('合计')])]);
assert.equal(context.window.businessSegmentPresentation.items(excluded.periods[0],'2').length,1);
assert.equal(normalize('601919',[]).periods.length,0);
console.log('business segment name checks passed: aliases, scope, collisions, partitions, source conservation, missing values, weighted margins, yoy');
if(process.argv.includes('--sample-audit')) {
  const samples=JSON.parse(fs.readFileSync(0,'utf8'));
  const audit=samples.map(sample=>{
    const snapshot=JSON.stringify(sample.periods),result=normalize(sample.code,sample.periods);
    assert.equal(JSON.stringify(sample.periods),snapshot);
    let groups=0;
    for(let i=0;i<sample.periods.length;i++)for(const dimension of ['1','2','3']) {
      const rawRows=sample.periods[i].dimensions[dimension],displayRows=result.periods[i].dimensions[dimension];
      assert.equal(displayRows.reduce((n,r)=>n+r.source_items.length,0),rawRows.length);
      assert.equal(JSON.stringify(displayRows.flatMap(r=>r.source_items.map(x=>x.item_key)).sort()),JSON.stringify(rawRows.map(x=>x.item_key).sort()));
      for(const r of displayRows) {
        if(r.source_items.length<2)continue;groups++;
        for(const field of ['revenue','cost','gross_profit','revenue_share','cost_share','profit_share']) {
          const values=r.source_items.map(x=>x[field]);
          assert.equal(r[field],values.every(x=>typeof x==='number'&&Number.isFinite(x))?values.reduce((a,b)=>a+b,0):null);
        }
      }
    }
    const catalog=d=>context.window.businessSegmentPresentation.catalog(result.periods,d).map(x=>x.item_name);
    if(sample.code==='601919')assert.equal(catalog('2').filter(n=>n.includes('集装箱航运')).length,1);
    if(sample.code==='605499') {
      assert.equal(catalog('2').includes('东鹏特饮'),false);
      const energyKey=result.periods.flatMap(p=>p.dimensions['2']).find(r=>r.item_name==='能量饮料').item_key;
      const annual=context.window.businessSegmentPresentation.periodsFor(result.periods,'12-31',10);
      assert.equal(series(annual,'2','revenue',new Set([energyKey]))[0].y.every(x=>x!==null),true);
    }
    if(sample.code==='000333'){assert.equal(catalog('3').includes('国外'),false);assert.equal(catalog('3').includes('海外'),true);}
    return {code:sample.code,period_count:sample.periods.length,product_names:catalog('2'),region_names:catalog('3'),mapping_count:result.mappings.length,conflicts:result.conflicts,aggregated_groups:groups};
  });
  if(process.env.BUSINESS_NAMES_AUDIT_REPORT)fs.writeFileSync(process.env.BUSINESS_NAMES_AUDIT_REPORT,JSON.stringify({date:'2026-10-16',samples:audit,checks:'source membership conserved; complete additive fields conserved; nulls preserved; confirmed aliases continuous'},null,2)+'\n');
  console.log(JSON.stringify(audit));
}
