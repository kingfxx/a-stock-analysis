function weeklyLabel(key){return ({m1:'原月度提前1月',m1_rebuilt:'周值重建月均，提前1月'})[key]??`周度提前${key.slice(1)}周`;}
function renderWeekly(){const w=D.weeklyStudy;if(!w)return;const mode=$('weeklyMethod').value||'release_mean',lag=Number($('weeklyLag').value||2),study=w[mode],key='w'+lag,a=study.analyses[key],target=study.targets[key],prior=study.anchors[key];
 $('weeklyResults').innerHTML=table(['窗口','单箱收入MAE（元/TEU）','利润水平MAE（亿元）','利润桥MAE（亿元）','固定中性业务归母（亿元）'],Object.entries(study.analyses).map(([k,v])=>[weeklyLabel(k),fmt(v.unit_rmb.level_mae,1),fmt(v.profit.level_mae,2),fmt(v.profit.bridge_mae,2),fmt(v.unit_rmb.q3_business_profit,1)]));
 $('weeklySelected').textContent=`提前${lag}周：Q2窗口 ${prior.start} 至 ${prior.end_exclusive}（不含末日），有效CCFI ${fmt(prior.value)}；Q3窗口 ${target.start} 至 ${target.end_exclusive}（不含末日），有效CCFI ${fmt(target.value)}。收入锚定增幅 ${fmt(a.unit_rmb.q3_unit_growth*100)}%，固定中性业务归母 ${fmt(a.unit_rmb.q3_business_profit,1)} 亿元；独立利润锚定桥 ${fmt(a.profit.q3_anchored_forecast,1)} 亿元。日期为日历辅助推定。`;
 const releasePoints=w.release_mean.targets[key].points??[];
 $('weeklyPoints').innerHTML=table(['Q3窗口内推定发布日','原图年份 / 周次','CCFI（点）'],releasePoints.map(r=>[r.date,`${r.year} / 第${r.week}周`,fmt(r.value,0)]));
 const a2=w.release_mean.analyses.w2,a3=w.release_mean.analyses.w3,n=w.quality_sensitivity.common_sample.length;
 $('weeklyRobustness').textContent=`7季检验中，提前两周收入MAE ${fmt(a2.unit_rmb.level_mae,1)}、三周 ${fmt(a3.unit_rmb.level_mae,1)} 元/TEU；扩大到8季、增加2024Q3高点外推后，两周为 ${fmt(a2.unit_rmb.level_mae_start5,1)}、三周 ${fmt(a3.unit_rmb.level_mae_start5,1)}。日期整体错一周，最优窗口也会在1—3周移动。极保守排除带重叠/未展示备注的数值后，共同样本仅剩${n}季、${n-6}次滚动预测，排序改变。不能证明精确两周稳定最优。`;
 $('weeklyMonthlyCheck').innerHTML=table(['年月','原月均','周值重建月均','差异（周−月）','发布值覆盖'],w.monthly_reconciliation.filter(r=>r.difference!=null&&Math.abs(r.difference)>3).map(r=>[r.month,fmt(r.monthly),fmt(r.weekly_mean),fmt(r.difference),`${r.observed}/${r.expected}`]));
 $('weeklyQuarterRows').innerHTML=table(['季度','窗口起始','窗口末日（不含）','CCFI（点）','覆盖'],study.rows.map(r=>{const f=r.features.w2;return[r.label,f.start,f.end_exclusive,fmt(f.value),`${f.observed}/${f.expected}`];}));
 $('weeklyDateRows').innerHTML=table(['原图年 / 周','推定发布日','是否应发布','原值','质量提示'],w.observations.map(r=>[`${r.year} / ${r.graph_week}`,r.inferred_publication_date,r.scheduled?'是':'否',fmt(r.original_value,0),(r.uncertainty.length?'需核对；':'')+r.note]));
 $('weeklyCalendarSources').innerHTML=Object.entries(w.calendars).map(([year,c])=>`<li><a href="${c.url}" target="_blank" rel="noopener">${year}年航交所指数发布日公告</a>；图中第1周推定起点 ${c.origin}。保留春节等停发空档，并按公告调整提前发布日期。</li>`).join('');
}
$('weeklyMethod').onchange=renderWeekly;$('weeklyLag').onchange=renderWeekly;renderWeekly();
