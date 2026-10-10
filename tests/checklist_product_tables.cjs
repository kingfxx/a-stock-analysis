const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const path=require('node:path');
const window={};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/ai-checklist.js'),'utf8'),{window});
const parse=window.checklistProductBlocks;
for(const period of ['[2026年上半年]','【2026年上半年】','［2026年上半年］','2026年上半年｜','2026年上半年 |','2026年上半年：']){
 const text='定性说明。\n'+period+'主营业务收入合计1119.22亿元，含抵销；航运—1072.98亿元—收入占比95.87%；抵销—-17.04亿元—-1.52%。\n'+period+'主营业务毛利合计205.26亿元，含抵销；航运—184.45亿元—毛利贡献89.86%；抵销—3.32亿元—1.62%；毛利贡献不等于净利润贡献。';
 const blocks=parse(text),tables=blocks.filter(b=>b.type==='table');
 assert.equal(tables.length,1,period);
 assert.equal(tables[0].headers[1],'收入（亿元）');
 assert.equal(tables[0].headers[3],'毛利（亿元）');
 assert.equal(tables[0].rows[1][1],'-17.04');
 assert.equal(tables[0].rows[1][2],'-1.52%');
 assert.ok(blocks.some(b=>b.type==='text' && b.text==='毛利贡献不等于净利润贡献。'));
 assert.equal(blocks[0].text,'定性说明。');
}
assert.equal(parse('仅有定性说明。')[0].text,'仅有定性说明。');
assert.equal(parse('2026年上半年｜只有说明，没有数字表格。')[0].type,'text');
console.log('Product table period formats, negative values and unmatched notes verified');

const varied=parse('定性说明。\n2026年1-6月｜分部净利润口径；光模块业务 — 148.06亿元 — 占比未披露；其他 — -0.73亿元 — 占比未披露。\n2025年 | 营业收入口径，分母为收入合计；光通信收发模块 — 374.57亿元（97.95%）；其他 — 7.83亿元 (2.05%)。');
const variedTables=varied.filter(b=>b.type==='table');
assert.equal(variedTables.length,2);
assert.equal(variedTables[0].headers[1],'分部净利润（亿元）');
assert.equal(variedTables[0].rows[1][1],'-0.73');
assert.equal(variedTables[1].rows[0][2],'97.95%');
assert.equal(variedTables[1].rows[1][2],'2.05%');

const trusted=[{id:'company.page.test',metric:'report_excerpt',value:[{business_metrics:{profit_mix:{period:'2026-06-30',unit:'元',basis:'分部净利润（按报告原口径）',denominator:14733426763.13,rows:[{name:'光模块',profit:14805981317.21,share_pct:100.4924},{name:'其他',profit:-72555222.21,share_pct:-.4925},{name:'抵销',profit:668.13,share_pct:0}]}}}]}];
const verified=parse('2026年1-6月｜分部净利润；光模块—148.06亿元—未披露；其他—-0.73亿元—未披露。',trusted).find(b=>b.type==='table');
assert.equal(verified.rows[0][2],'100.49%');
assert.equal(verified.rows[1][2],'-0.49%');
assert.equal(verified.rows[2][1],'668.13元');
assert.equal(verified.evidenceId,'company.page.test');
// An unrelated period cannot silently update an immutable historical snapshot.
const old=parse('2025年｜分部净利润；光模块—100亿元—未披露。',trusted).find(b=>b.type==='table');
assert.equal(old.rows[0][2],'未披露');

const telecom=parse('2025年｜主营业务口径；电信服务—5239.25亿元—收入占主营业务100%，毛利率29.1%。').find(b=>b.type==='table');
assert.equal(telecom.headers[1],'收入（亿元）');
assert.equal(parse('2025年｜业务口径；电信服务—5239.25亿元—未披露。')[0].headers[1],'金额（亿元）');
const incomeEvidence=[{id:'telecom.page',metric:'report_excerpt',value:[{business_metrics:{revenue_mix:{period:'2026-06-30',unit:'亿元',basis:'主营收入构成',denominator:2441,rows:[{name:'通信收入',revenue:2130,share_pct:87.2593},{name:'智能收入',revenue:311,share_pct:12.7407}],note:'天翼云618亿元另口径，不能相加；报告智能收入占比12.8%'}}}]}];
const telecomIncome=parse('2026年上半年｜主营收入口径；通信收入—2130亿元—未披露；智能收入—311亿元—12.8%；天翼云—618亿元—未披露。',incomeEvidence).find(b=>b.type==='table');
assert.equal(telecomIncome.rows.length,2);
assert.equal(telecomIncome.rows[0][2],'87.26%');
assert.equal(telecomIncome.rows[1][2],'12.74%');
assert.ok(telecomIncome.note.includes('天翼云618亿元另口径'));

const corrections=[{period:'2025-12-31',evidence_id:'bus.page',metrics:{
 revenue_mix:{period:'2025-12-31',unit:'万元',basis:'主营业务分产品收入',denominator:3622876.43,rows:[{name:'客车产品',revenue:3622876.43,share_pct:100}]},
 profit_mix:{period:'2025-12-31',unit:'万元',basis:'毛利（营业收入减营业成本），非净利润',denominator:910221.88,rows:[{name:'客车产品',profit:910221.88,share_pct:100}]}
}}];
const repaired=parse('定性说明。\n2025年｜主营业务分产品收入；客车产品—25.12万元—100%。\n2025年｜毛利口径；客车产品—8.43—100%，最新中报未披露结构。',[],corrections);
const repairedTables=repaired.filter(b=>b.type==='table');
assert.equal(repairedTables.length,1);
assert.equal(repairedTables[0].rows[0][1],'362.29亿元');
assert.equal(repairedTables[0].rows[0][3],'91.02亿元');
assert.equal(repairedTables[0].rows[0][0],'客车产品');
assert.ok(!JSON.stringify(repaired.filter(b=>b.type==='text')).includes('8.43'));
assert.equal(repaired[0].text,'定性说明。');

// Report basis belongs outside the product name; income and gross profit share a row.
const bus=parse('核心业务说明。\n2025年：主营业务分产品收入，分母为主营业务收入合计（含已列抵销项）：客车产品 — 3,622,876.43万元 — 100.0%。\n2025年：毛利，分母为主营业务毛利合计（含已列抵销项）：客车产品 — 910,221.88万元 — 毛利贡献100.0%。\n非净利润口径。2026年上半年未见产品结构数据。');
const busTables=bus.filter(b=>b.type==='table');
assert.equal(busTables.length,1);
assert.equal(busTables[0].rows[0][0],'客车产品');
assert.equal(busTables[0].headers[1],'收入（万元）');
assert.equal(busTables[0].headers[3],'毛利（万元）');
assert.equal(busTables[0].rows[0][3],'910,221.88');
assert.ok(bus.some(b=>b.type==='text' && b.text.includes('2026年上半年')));

// Missing shares and plain year labels must not suppress the domestic/overseas amounts.
for(const period of ['2025年','2025年度'])for(const label of ['收入','销售收入']) {
 const region=window.checklistMarketBlocks(period+'：国内'+label+'1,540,227.81万元（占比未披露）、海外'+label+'2,110,790.63万元（占比未披露），主营业务分地区口径；分母为地区收入合计，报告未披露占比。2026年上半年：地区收入未披露。');
 const table=region.find(b=>b.type==='table');
 assert.ok(table,period+label);
 assert.equal(table.headers.join(','),'报告期,地区,收入,占比');
 assert.equal(table.rows.length,2);
 assert.equal(table.rows[0][1],'国内');
 assert.equal(table.rows[0][2],'1,540,227.81万元');
 assert.equal(table.rows[0][3],'42.19%（计算）');
 assert.equal(table.rows[1][1],'海外');
 assert.equal(table.rows[1][2],'2,110,790.63万元');
 assert.equal(table.rows[1][3],'57.81%（计算）');
 assert.ok(table.note.includes('分母3,651,018.44万元'));
 assert.ok(region.some(b=>b.type==='text' && b.text.includes('2026年上半年')));
}
const noShare=window.checklistMarketBlocks('2025年度：国内销售收入1540227.81万元、海外销售收入2110790.63万元；年度销售收入同比国内-12.38%、海外+38.87%。');
assert.equal(noShare.find(b=>b.type==='table').rows[1][3],'57.81%（计算）');
assert.ok(noShare.some(b=>b.type==='text' && b.text.includes('+38.87%')));
const shares=window.checklistMarketBlocks('2026年上半年：国内收入146,915,462千元（56.50%）、海外113,127,028千元（43.50%），中报分地区口径，分母为营业收入。').find(b=>b.type==='table');
assert.equal(shares.rows[0][3],'56.50%');
assert.equal(shares.rows[1][3],'43.50%');
const regionRow=(text,index=0)=>window.checklistMarketBlocks(text).find(b=>b.type==='table').rows[index];
assert.equal(regionRow('2025年：国内收入0万元、海外收入100万元。')[3],'0.00%（计算）');
assert.equal(regionRow('2025年：国内收入0万元、海外收入0万元。')[3],'未披露');
assert.equal(regionRow('2025年：国内收入1亿元、海外收入10000万元。')[3],'50.00%（计算）');
assert.equal(regionRow('2025年：国内收入10万元（20%）、海外收入20万元。',1)[3],'未披露');

const optical=window.checklistMarketBlocks('海外客户业务是主要增长来源。\n2025年度：境内收入36.03亿元（9.42%）、境外收入346.37亿元（90.58%）；分地区口径，占营业收入合计。\n2026年1-6月：国内收入未披露金额及占比、海外收入396.15亿元（占比未披露）；报告称海外收入同比增长209.9%，该增速不代表收入占比。');
const opticalTable=optical.find(b=>b.type==='table');
assert.equal(opticalTable.rows.length,4);
assert.equal(opticalTable.rows[0][1],'境内');
assert.equal(opticalTable.rows[0][3],'9.42%');
assert.equal(opticalTable.rows[1][1],'境外');
assert.equal(opticalTable.rows[1][3],'90.58%');
assert.equal(opticalTable.rows[2][2],'未披露');
assert.equal(opticalTable.rows[3][2],'396.15亿元');
assert.equal(opticalTable.rows[3][3],'未披露');
assert.ok(optical.some(b=>b.type==='text' && b.text.includes('209.9%')));
const allMeasures=parse('2025年度｜分部净利润口径；光模块—117.05亿元—101.08%；其他—-1.25亿元—-1.08%。\n2025年｜毛利口径；光模块—150亿元—100%；其他—0亿元—0%。\n2025年度｜分部收入口径；光模块—374.92亿元—98.04%；其他—9.66亿元—2.52%。');
const allTables=allMeasures.filter(b=>b.type==='table');
assert.equal(allTables.length,1);
assert.equal(allTables[0].headers.length,7);
assert.equal(allTables[0].headers[1],'收入（亿元）');
assert.equal(allTables[0].headers[3],'毛利（亿元）');
assert.equal(allTables[0].headers[5],'分部净利润（亿元）');
assert.equal(allTables[0].rows[1][5],'-1.25');
const differentProducts=parse('2025年｜收入口径；光模块—100亿元—100%。\n2025年｜分部净利润；器件—20亿元—100%。');
assert.equal(differentProducts.filter(b=>b.type==='table').length,2);

const railSource=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/checklist-601766-table-source.json'),'utf8'));
const railTables=parse(railSource.conclusions.products,railSource.evidence,railSource.display_corrections).filter(b=>b.type==='table');
assert.equal(railTables.length,2);
for(const table of railTables) {
 assert.equal(table.headers.length,5);
 assert.equal(table.rows.length,4);
 assert.equal(table.rows.map(row=>row[0]).join(','),'铁路装备,城轨与城市基础设施,新产业,现代服务');
 assert.ok(table.headers[1].includes('收入'));
 assert.ok(table.headers[3].includes('毛利'));
}
// Tagged percentage parentheses must preserve the first row without corrections too.
const railRaw=parse(railSource.conclusions.products).filter(b=>b.type==='table');
assert.equal(railRaw.length,2);
assert.equal(railRaw[0].rows[0][0],'铁路装备');
assert.equal(railRaw[0].rows[0][3],'17,275,532');
const railMarket=window.checklistMarketBlocks(railSource.conclusions.market).find(b=>b.type==='table');
assert.equal(railMarket.rows.length,4);
assert.equal(railMarket.rows[0][1],'中国大陆');
assert.equal(railMarket.rows[1][1],'其他国家或地区');
assert.equal(railMarket.rows[1][2],'16,203,642千元');
assert.ok(railMarket.rows.every(row=>row[3].includes('计算')));
assert.ok(!window.checklistMarketBlocks(railSource.conclusions.market).some(b=>b.type==='text' && /交易收入,\d/.test(b.text)));

const structuredTable=(measure,rows,extra={})=>({period:'2026-06-30',classification:'产品',measure,basis:measure==='net_profit' ? '分部净利润，非归母净利润' : measure,unit:'亿元',denominator:100,rows,evidence_ids:['page'],...extra});
const numericRows=[{name:'产品甲',amount:100,share_pct:null},{name:'产品乙',amount:-1,share_pct:-1}];
const structured=window.checklistStructuredBlocks({id:'products',conclusion:'核心业务。',tables:[structuredTable('net_profit',numericRows),structuredTable('gross_profit',numericRows),structuredTable('income',numericRows)]}).find(b=>b.type==='table');
assert.equal(structured.headers.length,7);
assert.equal(structured.headers[1],'收入金额');
assert.equal(structured.headers[3],'毛利金额');
assert.equal(structured.headers[5],'分部净利润金额');
assert.equal(structured.rows[1][5],'-1亿元');
assert.equal(structured.rows[0][6],'100.00%（计算）');
const multiRegions=window.checklistStructuredBlocks({id:'market',conclusion:'地区定义按报告。',tables:[structuredTable('income',[{name:'亚太（含港澳台）',amount:60,share_pct:null},{name:'欧洲',amount:30,share_pct:30},{name:'美洲',amount:10,share_pct:null}],{classification:'地区',basis:'合并对外收入'})]}).find(b=>b.type==='table');
assert.equal(multiRegions.rows.length,3);
assert.equal(multiRegions.rows[0][1],'亚太（含港澳台）');
assert.equal(multiRegions.rows[0][3],'60.00%（计算）');
assert.equal(multiRegions.rows[1][3],'30.00%');
const partialRegions=window.checklistStructuredBlocks({id:'market',conclusion:'仅海外金额披露。',tables:[structuredTable('income',[{name:'境内',amount:null,share_pct:null},{name:'境外',amount:50,share_pct:null}],{denominator:null})]}).find(b=>b.type==='table');
assert.equal(partialRegions.rows[0][2],'未披露');
assert.equal(partialRegions.rows[1][3],'未披露');
const zeroRegion=window.checklistStructuredBlocks({id:'market',conclusion:'金额为零。',tables:[structuredTable('income',[{name:'国内',amount:0,share_pct:null},{name:'海外',amount:0,share_pct:null}],{denominator:null})]}).find(b=>b.type==='table');
assert.equal(zeroRegion.rows[0][3],'未披露');

// Model wording can add an amount label and use commas instead of a second dash.
for(const prefix of ['', '收入']) {
 const ship=parse('定性说明。\n2026年上半年 | 主营业务收入口径；船舶造修及海洋工程 — '+prefix+'815.25亿元，毛利率17.21%；船舶配套、机电设备及其他 — '+prefix+'90.54亿元，毛利率16.83%；合计 — '+prefix+'905.79亿元，毛利率17.17%，收入占比未披露。\n2025年度 | 主营业务收入口径；船舶造修及海洋工程 — '+prefix+'1312.79亿元，毛利率11.72%；船舶配套、机电设备及其他 — '+prefix+'186.17亿元，毛利率16.15%；合计 — '+prefix+'1498.96亿元，毛利率12.27%。');
 const tables=ship.filter(b=>b.type==='table');
 assert.equal(tables.length,2);
 assert.equal(tables[0].rows.length,3);
 assert.equal(tables[0].headers[1],'收入（亿元）');
 assert.equal(tables[0].rows[0][1],'815.25');
 assert.equal(tables[0].rows[1][0],'船舶配套、机电设备及其他');
 assert.ok(tables[0].rows[0][2].includes('毛利率17.21%'));
 assert.equal(tables[1].rows[0][1],'1312.79');
 assert.equal(ship[0].text,'定性说明。');
}
