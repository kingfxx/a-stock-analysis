const assert=require('node:assert/strict');
const {periodKey,chartSeries,leguCloseOverlay,dailyStatistics}=require('../web/features/market/congestion.js');
const channels={api_history:{rows:[
  {observation_date:'2026-07-27',value:.50},{observation_date:'2026-07-30',value:.51},
  {observation_date:'2026-07-31',value:null},{observation_date:'2026-08-03',value:null},
  {observation_date:'2026-08-07',value:null},{observation_date:'2026-08-28',value:null}
]},html_recent_table:{rows:[{observation_date:'2026-08-28',value:.45}]}};
const points=[{observation_date:'2026-07-31',close:3800},{observation_date:'2026-08-07',close:3900},{observation_date:'2026-08-28',close:4000}];
const daily=chartSeries(channels,points,'day');
assert.deepEqual(daily.congestion,[.50,.51,null,null,null,.45]);
const weekly=chartSeries(channels,points,'week');
assert.deepEqual(weekly.dates,['2026-07-31','2026-08-07','2026-08-28']);
assert.deepEqual(weekly.congestion,[null,null,.45]); // Do not carry Thursday's value into a missing Friday.
assert.deepEqual(weekly.points,[3800,3900,4000]);
const monthly=chartSeries(channels,points,'month');
assert.deepEqual(monthly.dates,['2026-07-31','2026-08-28']);
assert.deepEqual(monthly.congestion,[null,.45]);
assert.equal(periodKey('2021-01-01','week'),'2020-12-28');
assert.equal(periodKey('2021-01-03','week'),'2020-12-28');
assert.equal(periodKey('2021-01-04','week'),'2021-01-04');
const overlap={api_history:{rows:[{observation_date:'2026-10-16',value:.41655},{observation_date:'2026-10-15',value:0}]},html_recent_table:{rows:[{observation_date:'2026-10-16',value:.4166},{observation_date:'2026-10-15',value:null}]}};
assert.deepEqual(chartSeries(overlap,[],'day').congestion,[0,.4166]);
assert.equal(chartSeries({api_history:{rows:[]},html_recent_table:{rows:[]}},[],'week').dates.length,0);
const nativeMonthly=chartSeries({api_history:{rows:[{observation_date:'2026-07-29',value:.5},{observation_date:'2026-07-31',value:.6},{observation_date:'2026-08-07',value:.4}]},html_recent_table:{rows:[]}},[{observation_date:'2026-07-29',close:3800}],'week','monthly');
assert.deepEqual(nativeMonthly.pointDates,['2026-07-29']);
assert.deepEqual(nativeMonthly.points,[3800]);
assert.equal(nativeMonthly.dates[0],'2026-07-29');
assert.equal(nativeMonthly.congestion[0],.5);
const sourceCloses=leguCloseOverlay({api_history:{rows:[{observation_date:'2026-07-30',close:3804.69},{observation_date:'2026-07-31',close:null},{observation_date:'2026-10-16',close:null},{observation_date:'2026-10-15',close:3707.48}]},html_recent_table:{rows:[{observation_date:'2026-10-16',close:3716.28},{observation_date:'2026-10-15',close:null}]}});
assert.equal(sourceCloses.available,true);
assert.equal(sourceCloses.first_date,'2026-07-30');
assert.deepEqual(sourceCloses.rows.map(row=>row.close),[3804.69,null,3707.48,3716.28]);
assert.equal(leguCloseOverlay({api_history:{rows:[]},html_recent_table:{rows:[]}}).available,false);
const supplemented={...channels,bigquant_calculated:{rows:[
  {observation_date:'2026-07-31',value:.51,close:3832.26},
  {observation_date:'2026-08-28',value:.48,close:3900}
]}};
assert.equal(chartSeries(supplemented,[],'day').congestion[2],.51);
assert.equal(chartSeries(supplemented,[],'day').congestion.at(-1),.45); // Observed Legu wins.
const derivedCloses=leguCloseOverlay({api_history:{rows:[{observation_date:'2026-07-31',close:null},{observation_date:'2026-08-28',close:3950}]},html_recent_table:{rows:[]},bigquant_calculated:supplemented.bigquant_calculated});
assert.deepEqual(derivedCloses.rows.map(row=>row.close),[3832.26,3950]);
console.log('Congestion display sampling: passed');
const dailyStatsChannels={bigquant_calculated:{rows:[{observation_date:'2026-01-02',value:.9}]},api_history:{rows:[
  {observation_date:'2026-01-01',value:0},{observation_date:'2026-01-02',value:.2},
  {observation_date:'2026-01-03',value:.4},{observation_date:'2026-01-04',value:.6},
  {observation_date:'2026-01-05',value:.8},{observation_date:'2026-01-06',value:null}
]},html_recent_table:{rows:[{observation_date:'2026-01-05',value:.8},{observation_date:'2026-01-03',value:null}]}};
const stats=dailyStatistics(dailyStatsChannels);
assert.equal(stats.count,5);assert.equal(stats.current,.8);assert.equal(stats.currentDate,'2026-01-05');
assert.equal(stats.percentile,80);assert.equal(stats.rank,5);
assert.ok(Math.abs(stats.low-.16)<1e-12);assert.equal(stats.median,.4);
assert.ok(Math.abs(stats.high-.64)<1e-12);assert.equal(stats.mean,.4);
assert.ok(Math.abs(stats.stddev-Math.sqrt(.08))<1e-12);
assert.equal(dailyStatistics(dailyStatsChannels,'2026-01-04','2026-01-05').count,2);
assert.equal(dailyStatistics(dailyStatsChannels,'2027-01-01').count,0);
const singleton=dailyStatistics({api_history:{rows:[{observation_date:'2026-01-01',value:0}]}});
assert.equal(singleton.count,1);assert.equal(singleton.current,0);assert.equal(singleton.stddev,0);assert.equal(singleton.low,0);
console.log('Congestion daily statistics: passed');
