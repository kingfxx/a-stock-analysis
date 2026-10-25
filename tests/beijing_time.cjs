const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const time=require('../web/shared/datetime.js');
for(const zone of ['UTC','America/Los_Angeles','Asia/Shanghai']){
  process.env.TZ=zone;
  assert.equal(time.format('2026-10-17T07:29:57.867636+00:00'),'2026-10-17 15:29:57');
  assert.equal(time.format('2026-10-17T15:29:57+08:00'),'2026-10-17 15:29:57');
  assert.equal(time.format('2026-10-17T16:00:00Z'),'2026-10-18 00:00:00');
  assert.equal(time.format('2026-10-17 15:29:57.867636'),'2026-10-17 15:29:57');
  assert.equal(time.format('2026-10-17'),'2026-10-17');
  assert.equal(time.format(null),'—');
  assert.equal(time.format('invalid','未记录'),'未记录');
  assert.equal(time.format(''),'—');
}
const elements=new Map();
const el=id=>{if(!elements.has(id))elements.set(id,{hidden:false});return elements.get(id);};
const script=fs.readFileSync(path.join(__dirname,'../web/features/indices/indices.js'),'utf8');
const render=script.slice(script.indexOf('function renderStatus()'),script.indexOf('const parentOf='));
vm.runInNewContext(render+';renderStatus();',{
  window:{DashboardTime:time},el,data:{operations:[],cooldowns:[{source_system:'legu',reason:'HTTP 429',cooldown_until:'2026-10-17T07:29:57.867636+00:00'}]},
  providers:{legu:'乐咕乐股'},renderWorkProgress(){},clearTimeout(){},setTimeout(){},timer:null
});
assert.equal(el('cooldowns').textContent,'乐咕乐股：HTTP 429，冷却截止（北京时间）2026-10-17 15:29:57');
for(const name of ['index.html','indices.html']){
  const html=fs.readFileSync(path.join(__dirname,'../web/pages',name),'utf8');
  assert.ok(html.indexOf('/datetime.js')<html.indexOf(name==='index.html'?'/dcf.js':'/indices.js'));
}
console.log('Beijing time conversion, midnight, missing values, independent timezone and cooldown UI passed');
