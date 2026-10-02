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
 assert.equal(tables.length,2,period);
 assert.equal(tables[0].headers[1],'收入（亿元）');
 assert.equal(tables[1].headers[1],'毛利（亿元）');
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
