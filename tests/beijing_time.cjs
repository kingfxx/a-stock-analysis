const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
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
// Cooldown rendering is exercised in the maintenance browser test; this file
// checks the shared formatter independently of the browser's local timezone.
for(const name of ['index.html','indices.html']){
  const html=fs.readFileSync(path.join(__dirname,'../web/pages',name),'utf8');
  assert.ok(html.indexOf('/datetime.js')<html.indexOf(name==='index.html'?'/dcf.js':'/indices.js'));
}
console.log('Beijing time conversion, midnight, missing values and independent timezone passed');
