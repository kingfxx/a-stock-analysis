"""Weekly-window study. Dates are calendar-assisted inferences, not raw dates."""
import json
from datetime import date,timedelta

import numpy as np

from build import ROOT,rate
from calibrate import fit_model,predict

OUT=ROOT/'data/verification/reports/ccfi-profit-lab'
LAGS=list(range(7))
# Official exchange calendars, read through web research; no CCFI values replaced.
CALENDARS={
    2023:dict(origin='2023-01-06',closed=['2023-01-27','2023-10-06'],adjustments={'2023-06-23':'2023-06-21','2023-09-29':'2023-09-28'},url='https://www.sse.net.cn/cninfo/notice/index/202212/t20221220_1372502.jsp'),
    2024:dict(origin='2024-01-05',closed=['2024-02-16','2024-05-03','2024-10-04'],adjustments={'2024-04-05':'2024-04-03'},url='https://www.sse.net.cn/cninfo/notice/index/202311/t20231122_1383770.jsp'),
    2025:dict(origin='2025-01-03',closed=['2025-01-31','2025-10-03'],adjustments={'2025-04-04':'2025-04-03','2025-05-02':'2025-04-30'},url='https://www.sse.net.cn/cninfo/notice/index/202411/t20241129_1397553.jsp'),
    2026:dict(origin='2026-01-09',closed=['2026-02-20'],adjustments={'2026-05-01':'2026-04-30','2026-06-19':'2026-06-18','2026-09-25':'2026-09-24','2026-10-02':'2026-09-30'},url='https://www.sse.sh.cn/cninfo/notice/index/202606/t20260612_1468142.jsp'),
}


def observations(snapshot,offset_days=0,exclude_uncertain=False):
    rows=[]
    for year,c in CALENDARS.items():
        origin=date.fromisoformat(c['origin'])
        for w in snapshot['weekly']:
            nominal=origin+timedelta(weeks=w['week']-1)
            if nominal.year!=year:continue
            scheduled=nominal.isoformat() not in c['closed']
            actual=date.fromisoformat(c['adjustments'].get(nominal.isoformat(),nominal.isoformat()))
            value=w['values'][year-2023]
            note=w['note'] or ''
            uncertainty=[]
            if '标签重叠' in note and str(year) in note:uncertainty.append('overlap_note')
            if year==2026 and w['week']>=39 and ('未展示' in note or '均无标签' in note):uncertainty.append('numeric_value_but_note_says_not_shown')
            if scheduled and value is None:uncertainty.append('scheduled_publication_missing_value')
            if not scheduled and value is not None:uncertainty.append('value_on_official_no_publication_week')
            effective_value=None if exclude_uncertain and uncertainty else value
            rows.append(dict(year=year,graph_week=w['week'],nominal_date=nominal.isoformat(),inferred_publication_date=(actual+timedelta(days=offset_days)).isoformat(),scheduled=scheduled,value=effective_value,original_value=value,note=note,uncertainty=uncertainty,date_status='calendar_assisted_inference',calendar_source=c['url']))
    return sorted(rows,key=lambda r:r['inferred_publication_date'])


def quarter_bounds(year,q):
    start=date(year,(q-1)*3+1,1)
    end=date(year+1,1,1) if q==4 else date(year,q*3+1,1)
    return start,end


def exposure(obs,year,q,lag,method='release_mean'):
    start,end=quarter_bounds(year,q)
    start-=timedelta(weeks=lag);end-=timedelta(weeks=lag)
    # Reject missing years, rather than silently shortening a shifted quarter.
    first=date.fromisoformat(obs[0]['inferred_publication_date'])
    last=date.fromisoformat(obs[-1]['inferred_publication_date'])
    base=dict(start=start.isoformat(),end_exclusive=end.isoformat(),lag_weeks=lag,method=method)
    if start<first or end-timedelta(days=1)>last:return dict(**base,value=None,reason='outside_available_calendar',expected=0,observed=0)
    scheduled=[r for r in obs if r['scheduled']]
    selected=[r for r in scheduled if start<=date.fromisoformat(r['inferred_publication_date'])<end]
    missing=[r for r in selected if r['value'] is None]
    if method=='release_mean':
        val=float(np.mean([r['value'] for r in selected])) if selected and not missing else None
        return dict(**base,value=val,expected=len(selected),observed=len(selected)-len(missing),reason='scheduled_value_missing' if missing else None,points=[dict(date=r['inferred_publication_date'],week=r['graph_week'],year=r['year'],value=r['value']) for r in selected])
    if method!='daily_asof':raise ValueError(method)
    # An index is held over genuine no-publication days. A missing scheduled
    # publication resets it to unknown until the next observed publication.
    index=0;current=None;values=[];day=start
    while index<len(scheduled) and date.fromisoformat(scheduled[index]['inferred_publication_date'])<=day:
        current=scheduled[index]['value'];index+=1
    while day<end:
        while index<len(scheduled) and date.fromisoformat(scheduled[index]['inferred_publication_date'])<=day:
            current=scheduled[index]['value'];index+=1
        values.append(current);day+=timedelta(days=1)
    valid=sum(v is not None for v in values)
    return dict(**base,value=float(np.mean(values)) if valid==len(values) else None,expected=len(values),observed=valid,reason=None if valid==len(values) else 'missing_daily_exposure')


def evaluate(rows,key,target_x,anchor_x,snapshot):
    x=[r['features'][key]['value'] for r in rows]
    result={}
    for field in ['unit_rmb','profit']:
        y=[r[field] for r in rows]
        fitted=fit_model(x,y,'linear')
        errors=[]
        for i in range(6,len(rows)):
            model=fit_model(x[:i],y[:i],'linear')
            level=float(predict(model,x[i])[0])
            bridge=y[i-1]+float(predict(model,x[i])[0]-predict(model,x[i-1])[0])
            errors.append(dict(label=rows[i]['label'],training_end=rows[i-1]['label'],actual=y[i],level_prediction=level,bridge_prediction=bridge,baseline=y[i-1],level_error=level-y[i],bridge_error=bridge-y[i]))
        early=[]
        for i in range(5,len(rows)):
            model=fit_model(x[:i],y[:i],'linear')
            early.append(float(predict(model,x[i])[0])-y[i])
        unit_anchor=next(r for r in snapshot['history'] if r['label']=='2026Q2')
        fitted.update(backtests=errors,level_mae=float(np.mean([abs(r['level_error']) for r in errors])),bridge_mae=float(np.mean([abs(r['bridge_error']) for r in errors])),baseline_mae=float(np.mean([abs(r['baseline']-r['actual']) for r in errors])),level_mae_start5=float(np.mean(np.abs(early))))
        if target_x is not None and anchor_x is not None:
            actual=snapshot['calibration']['target']['anchor_unit_rmb'] if field=='unit_rmb' else unit_anchor['profit']
            growth_delta=float(predict(fitted,target_x)[0]-predict(fitted,anchor_x)[0])
            forecast=actual+growth_delta
            fitted.update(q3_anchored_forecast=forecast)
            if field=='unit_rmb':
                growth=forecast/actual-1;b=snapshot['business']
                revenue=(b['international']*(1+growth)+b['domestic']+b['other'])*1.02
                cost=b['cost']*1.02*1.045
                fitted.update(q3_unit_growth=growth,q3_business_profit=(revenue-cost+b['below'])*b['conversion'])
        result[field]=fitted
    return result


def study(snapshot,obs,method):
    calibration=snapshot['calibration']
    rebuilt_months={r['month']:r['weekly_mean'] for r in monthly_reconciliation(snapshot,obs)}
    rows=[]
    for original in calibration['rows']:
        row={k:original[k] for k in ['label','year','q','period','unit_rmb','profit']}
        row['features']={f'w{lag}':exposure(obs,row['year'],row['q'],lag,method) for lag in LAGS}
        row['features']['m1']=dict(value=original['rates']['1'],method='original_monthly_mean')
        row['features']['m1_rebuilt']=dict(value=rate(rebuilt_months,row['year'],row['q'],1),method='equal_month_weight_from_weekly')
        rows.append(row)
    keys=[f'w{lag}' for lag in LAGS]+['m1','m1_rebuilt']
    common=[r for r in rows if all(r['features'][k]['value'] is not None for k in keys)]
    targets={f'w{lag}':exposure(obs,2026,3,lag,method) for lag in LAGS}
    anchors={f'w{lag}':exposure(obs,2026,2,lag,method) for lag in LAGS}
    targets['m1']=dict(value=snapshot['models']['1']['target'])
    anchors['m1']=dict(value=snapshot['models']['1']['anchor'])
    targets['m1_rebuilt']=dict(value=rate(rebuilt_months,2026,3,1))
    anchors['m1_rebuilt']=dict(value=rate(rebuilt_months,2026,2,1))
    # All candidates must have identical chronological targets and training sets.
    analyses={k:evaluate(common,k,targets[k]['value'],anchors[k]['value'],snapshot) for k in keys} if len(common)>=8 else {}
    return dict(method=method,common_sample=[r['label'] for r in common],rows=rows,targets=targets,anchors=anchors,analyses=analyses)


def monthly_reconciliation(snapshot,obs):
    results=[]
    for key,monthly in snapshot['months'].items():
        if monthly is None:continue
        selected=[r for r in obs if r['scheduled'] and r['inferred_publication_date'].startswith(key)]
        known=[r['value'] for r in selected if r['value'] is not None]
        complete=bool(selected) and len(known)==len(selected)
        weekly_mean=float(np.mean(known)) if complete else None
        results.append(dict(month=key,monthly=monthly,weekly_mean=weekly_mean,difference=weekly_mean-monthly if weekly_mean is not None else None,expected=len(selected),observed=len(known),complete=complete))
    return results


def write_report(result):
    release=result['release_mean'];daily=result['daily_asof']
    lines=['# 周度 CCFI 提前两周检验（2026-10-24）','',
           '结论：在官方日历辅助推定的日期及原表整数值下，提前2周在13季共同样本、7季滚动检验中，单箱收入和直接利润水平模型误差较低；利润锚定桥的发布均值口径则是提前3周略好。支持2—3周作为候选窗口，不能确认精确2周是稳定最优，更不能等同于公司每笔订单固定滞后14天。',
           '', '## 窗口、日期及数据质量','',
           '“提前2周”指整个季度起止日一起减14天，再对该窗口内实际发布的CCFI求均值；不是只取季度末两周。日期采用左闭右开区间，例如2026Q3为[2026-06-17,2026-09-17)，即6月17日至9月16日。实际纳入13个发布值，首末发布日为6月18日、9月11日。Q2为[2026-03-18,2026-06-17)，同样13点。',
           '', '原附件只有图中第1—52周，不是带日期的原始历史序列。依据航交所年度发布日公告、图中节假日空档以及已有日期点位辅助推定：2023/2024/2025图中第1周对应1月6日/1月5日/1月3日；2026对应1月9日（1月2日停发，因此不能直接套ISO周号）。中间保留停发周的位置，提前发布采用官方实际日期。该映射仍是有依据的推定，未逐条独立验证全部周值。',
           '', '来源表原值和备注不改动。部分“留空待核对/未展示”备注所在单元格已经有数值，保留矛盾并另做严格剔除敏感性；不把猜测的校正值覆盖原表。',
           '', '2024年1月原月值1441与四周值937/1140/1287/1403的均值1191.75相差249.25点。这不是舍入误差。原月度备注已标重叠待核对，仍需核对原始来源；本次将周数据重建月均作为独立敏感性，不悄悄改写原月值。',
           '', '## 相同样本的滚动检验','',
           '本轮固定线性模型，单独比较时间窗口，避免同时改变频率和非线性形式。所有窗口使用2023Q2—2026Q2共同13季；先用6季训练，此后7次按顺序预测。2023Q1缺少前年末运价，统一排除。周数据只改善解释变量的窗口精度，不把13个季度利润扩充为数百个独立样本。财务和来源采用现有版本，并非完整实时修订快照回测。','',
           '| 窗口 | 单箱收入水平MAE（元/TEU） | 归母利润水平MAE（亿元） | 归母利润锚定桥MAE（亿元） | 同成本业务情景Q3归母（亿元） |',
           '|---|---:|---:|---:|---:|']
    for k,a in release['analyses'].items():
        name={'m1':'原月度提前1月','m1_rebuilt':'周值重建月均，提前1月'}.get(k,'周度提前'+k[1:]+'周')
        forecast=a['unit_rmb'].get('q3_business_profit')
        lines.append(f"| {name} | {a['unit_rmb']['level_mae']:.2f} | {a['profit']['level_mae']:.2f} | {a['profit']['bridge_mae']:.2f} | {forecast:.2f} |")
    lines += ['', '收入水平：用训练样本拟合单箱收入=f(CCFI)，直接预测下一季。利润水平：用训练样本拟合归母利润=f(CCFI)，直接预测下一季。利润锚定桥：上一季实际归母 + 训练期斜率×CCFI变化。三者误差不可混称；表中最后一列的收入成本业务模型也不是直接利润回归。',
              '', '将检验起点提前至2024Q3、增加对高点的外推后，8季收入误差在提前3周时较小（235.34元/TEU），提前2周为285.88元/TEU；利润水平误差则重建月度/提前4周更低。这说明候选窗口排序依赖于有限的检验季度，不能把只在7季检验胜出的2周当作已确定参数。',
              '', '原月度提前1月与周度提前4周不是同一窗口，且前者三个月等权、后者各次发布等权。新增的“周值重建月均”先按推定发布日聚合月均，再按三个月等权，可分辨窗口差异与月度转录差异。该敏感性仍显示2周候选较好，但不能据此断言日期和周值已经准确。',
              '', '## 替代加权、日期与质量敏感性','',
              f"按日暴露：将最近已发布指数用于其后每天，正式停发期间保持有效；若某个应发布日的数值缺失，从该日起留空，不能用旧值冒充新值。该口径提前2周的收入MAE {daily['analyses']['w2']['unit_rmb']['level_mae']:.2f}元/TEU，利润水平MAE {daily['analyses']['w2']['profit']['level_mae']:.2f}亿元，利润桥MAE {daily['analyses']['w2']['profit']['bridge_mae']:.2f}亿元。收入成本情景归母约{daily['analyses']['w2']['unit_rmb']['q3_business_profit']:.2f}亿元。",
              '', '若所有推定日期整体错1周，收入最优窗口也会在1—3周移动；这是日期识别风险，不是额外独立证据。因此在拿到带实际日期的历史原序列前，不把“正好2周”写成确定结论。',
              '', '极保守地排除所有带重叠/未展示矛盾备注的数值，所有候选共同可用季度只剩'+str(len(result['quality_sensitivity']['common_sample']))+'季、4次滚动检验。此时原月度与周度2—3周的排序变化，不能称2周稳健胜出。该剔除只是压力检验，不代表备注中的已有数值已被证实错误。',
              '', '## 对2026Q3测算的影响','']
    a=release['anchors']['w2'];t=release['targets']['w2'];fit=release['analyses']['w2']['unit_rmb']
    lines += [f"周度提前2周：Q2有效CCFI {a['value']:.4f}，Q3 {t['value']:.4f}；以Q2实际单箱收入锚定，收入增幅{fit['q3_unit_growth']*100:.2f}%。保持箱量+2%、单位成本+4.5%及其余中性参数不变，归母约{fit['q3_business_profit']:.2f}亿元。",
              '', f"独立直接利润锚定桥约{release['analyses']['w2']['profit']['q3_anchored_forecast']:.2f}亿元，与收入成本情景是不同方法，不取平均。",
              '', '原网页月度模型默认保留约171.9亿元，不因日期仍为推定、仅7次回测，就自动改称周度模型更可靠。周度对照及窗口明细加入原独立网页。',
              '', '## 官方日历与复算证据','']
    for year,c in CALENDARS.items():lines.append(f"- [{year}年航交所指数发布日公告]({c['url']})。")
    lines += ['', '`weekly.json`保存原始图中周次、推定日期、官方日历链接、质量标记、窗口内点位、覆盖情况、回测及敏感性；不修改原Excel、原月度数据或财报。复算：`python experiments/ccfi-profit-lab/weekly.py`。']
    (OUT/'WEEKLY_RESEARCH.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    snapshot=json.loads((OUT/'snapshot.json').read_text(encoding='utf-8'))
    obs=observations(snapshot)
    release=study(snapshot,obs,'release_mean')
    daily=study(snapshot,obs,'daily_asof')
    sensitivity={}
    for shift in [-7,7]:
        s=study(snapshot,observations(snapshot,offset_days=shift),'release_mean')
        sensitivity[str(shift)]=dict(common_sample=s['common_sample'],analyses=s['analyses'])
    quality=study(snapshot,observations(snapshot,exclude_uncertain=True),'release_mean')
    result=dict(definition='Quarter boundaries shifted earlier by 7*lag days; not just the last 2 weeks of the quarter.',workbook=snapshot['workbook'],financial_cutoff=snapshot['cutoff'],
                calendars=CALENDARS,date_status='calendar-assisted inference; not a fully independently verified dated CCFI history',observations=obs,
                release_mean=release,daily_asof=daily,date_shift_sensitivity=sensitivity,
                quality_sensitivity=dict(common_sample=quality['common_sample'],analyses=quality['analyses'],targets=quality['targets']),
                monthly_reconciliation=monthly_reconciliation(snapshot,obs))
    (OUT/'weekly.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    calendar_evidence=ROOT/'data/verification/samples/ccfi-profit-lab/weekly-calendar-evidence.json'
    calendar_evidence.write_text(json.dumps(dict(reviewed_on='2026-10-24',method='official calendars read via web; normalized rules, not raw HTTP archive',calendars=CALENDARS,date_status=result['date_status']),ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(result)
    for study_ in [release,daily]:
        print(study_['method'],study_['common_sample'])
        for key,a in study_['analyses'].items():print(key,'revenue MAE',round(a['unit_rmb']['level_mae'],2),'profit level',round(a['profit']['level_mae'],2),'profit bridge',round(a['profit']['bridge_mae'],2),'q3',round(a['unit_rmb'].get('q3_business_profit',0),2))
    print('MONTH DIFFERENCES',[(r['month'],round(r['difference'],2)) for r in result['monthly_reconciliation'] if r['difference'] is not None and abs(r['difference'])>10])


if __name__=='__main__':main()
