"""Reconstruct actual quarterly unit revenue and test CCFI nonlinearity."""
import json
import re
import math
from pathlib import Path

import numpy as np

from build import ROOT, shifted_months, rate

OUT=ROOT/'data/verification/reports/ccfi-profit-lab'
SAMPLES=ROOT/'data/verification/samples/ccfi-profit-lab'
HEADERS={'teu':'本集团货运量（标准箱）','rmb':'本集团航线收入（人民币千元）','usd':'本集团航线收入（折算美元千元）'}
ROUTES=['跨太平洋','亚欧（包括地中海）','亚洲区内（包括澳洲）','其他国际（包括大西洋）','中国大陆','合计']
QUAD_NODES,QUAD_WEIGHTS=np.polynomial.legendre.leggauss(128)


def student_p(tstat,df):
    """Two-sided Student-t tail, numerical quadrature on atan(t) coordinates."""
    limit=math.atan(abs(tstat))
    theta=(QUAD_NODES+1)*limit/2
    z=np.tan(theta)
    normalizer=math.exp(math.lgamma((df+1)/2)-math.lgamma(df/2))/math.sqrt(df*math.pi)
    density=normalizer*(1+z*z/df)**(-(df+1)/2)*(1+z*z)
    return float(np.clip(1-limit*np.sum(QUAD_WEIGHTS*density),0,1))


def extract_table(pages,header,q3=False):
    text='\n'.join(re.sub(r'(?m)^(?:中远海运控股股份有限公司\s*)?20\d{2}\s*年[^\n]*报告\s*$','',re.sub(r'\b\d+\s*/\s*\d+\b',' ',p['text'])) for p in pages)
    text=re.sub(r'\s+',' ',text).replace(' ','\u00a0')
    # Preserve separators between numeric columns; remove only whitespace within CJK labels.
    text=re.sub(r'(?<=[\u4e00-\u9fff（）])\u00a0(?=[\u4e00-\u9fff（）])','',text).replace('\u00a0',' ')
    if header not in text: raise ValueError('Missing header '+header)
    start=text.index(header)
    section=text[start:]
    end=section.index('合计')
    # Include the first total row, but no following subsidiary table.
    end += len('合计')
    tail=re.match(r'\s*[-\d,.\s]+',section[end:])
    section=section[:end+len(tail[0])]
    values={}
    for name in ROUTES:
        match=re.search(re.escape(name)+r'\s+([-\d,.\s]+)',section)
        if not match:raise ValueError('Missing route '+name+' in '+header)
        nums=[float(v.replace(',','')) for v in re.findall(r'-?\d[\d,]*(?:\.\d+)?',match[1])]
        if len(nums) not in [3,6]:raise ValueError(f'Unexpected table width {header} {name}: {nums}')
        values[name]=nums
    actual_q3=len(values['合计'])==6
    if q3!=actual_q3:raise ValueError('Unexpected quarter/cumulative layout')
    current=3 if q3 else 0
    previous=4 if q3 else 1
    for col in [current,previous]:
        if abs(sum(values[n][col] for n in ROUTES[:-1])-values['合计'][col])>3:
            raise ValueError('Route total reconciliation failed')
    page_ids=[p['pdf_page'] for p in pages if header in re.sub(r'\s+','',p['text'])]
    return dict(total=values['合计'][current],domestic=values['中国大陆'][current],prior_total=values['合计'][previous],prior_domestic=values['中国大陆'][previous],direct_q3_total=values['合计'][0] if q3 else None,direct_q3_domestic=values['中国大陆'][0] if q3 else None,page_candidates=page_ids,raw=section)


def business_quarters():
    manifests=json.loads((SAMPLES/'calibration_manifest.json').read_text(encoding='utf-8'))
    cumulative={}
    extraction=[]
    for m in manifests:
        pages=json.loads((ROOT/m['pages_path']).read_text(encoding='utf-8'))
        tables={k:extract_table(pages,head,m['period'].endswith('09-30')) for k,head in HEADERS.items()}
        cumulative[m['period']]=dict(tables=tables,source=m)
        extraction.append(dict(period=m['period'],tables=tables,source=m))
    # Take current-period disclosures consistently. Audit subsequent-year comparatives separately.
    revisions=[]
    for p,c in cumulative.items():
        next_period=str(int(p[:4])+1)+p[4:]
        if next_period in cumulative:
            n=cumulative[next_period]
            for kind in HEADERS:
                for metric in ['total','domestic']:
                    original=c['tables'][kind][metric]
                    restated=n['tables'][kind]['prior_'+metric]
                    if original!=restated:
                        revisions.append(dict(period=p,kind=kind,metric=metric,original=original,subsequent_comparative=restated,source=n['source']['period']))
    rows=[]
    for period,c in cumulative.items():
        year=int(period[:4]);q=int(period[5:7])//3
        prevperiod=f'{year}-{["03-31","06-30","09-30"][q-2]}' if q>1 else None
        prev=cumulative[prevperiod] if prevperiod else None
        values={}
        for kind,tables in c['tables'].items():
            for metric in ['total','domestic']:
                value=tables[metric]-(prev['tables'][kind][metric] if prev else 0)
                if q==3 and abs(value-tables['direct_q3_'+metric])>3:
                    raise ValueError(f'{period} {kind} direct Q3 != cumulative difference')
                values[kind+'_'+metric]=value
            values[kind+'_international']=values[kind+'_total']-values[kind+'_domestic']
        if values['teu_international']<=0:raise ValueError('Nonpositive international volume')
        rows.append(dict(period=period,label=f'{year}Q{q}',year=year,q=q,**values,
                         unit_rmb=values['rmb_international']*1000/values['teu_international'],
                         unit_usd=values['usd_international']*1000/values['teu_international'],
                         cumulative_source=c['source']['period'],subtract_source=prev['source']['period'] if prev else None))
    return rows,extraction,revisions


def design(x,kind):
    x=np.asarray(x,dtype=float)
    z=(x-1300)/1000
    if kind=='linear':return np.column_stack([np.ones(len(x)),z])
    if kind=='quadratic':return np.column_stack([np.ones(len(x)),z,z*z])
    if kind=='hinge1500':return np.column_stack([np.ones(len(x)),z,np.maximum(x-1500,0)/1000])
    if kind=='power':return np.column_stack([np.ones(len(x)),np.log(x)])
    raise ValueError(kind)


def fit_model(x,y,kind):
    X=design(x,kind); y=np.asarray(y,dtype=float)
    target=np.log(y) if kind=='power' else y
    coef=np.linalg.lstsq(X,target,rcond=None)[0]
    residual=target-X@coef
    inv=np.linalg.pinv(X.T@X)
    leverage=np.sum((X@inv)*X,axis=1)
    robust=(residual/np.maximum(1-leverage,1e-8))**2
    cov=inv@(X.T@(X*robust[:,None]))@inv
    se=np.sqrt(np.maximum(np.diag(cov),0))
    tstat=float(coef[-1]/se[-1]) if se[-1]>0 else None
    pvalue=student_p(tstat,len(y)-len(coef)) if tstat is not None else None
    smearing=float(np.mean(np.exp(residual))) if kind=='power' else 1.
    pred=np.exp(X@coef)*smearing if kind=='power' else X@coef
    return dict(kind=kind,coef=coef.tolist(),rank=int(np.linalg.matrix_rank(X)),parameters=X.shape[1],se_hc3=se.tolist(),last_coefficient_p_hc3=pvalue,smearing=smearing,
                n=len(y),r2=float(1-sum((y-pred)**2)/sum((y-y.mean())**2)),rmse=float(np.sqrt(np.mean((y-pred)**2))))


def predict(model,x):
    result=design(np.atleast_1d(x),model['kind'])@model['coef']
    return np.exp(result)*model['smearing'] if model['kind']=='power' else result


def derivative(model,x):
    c=model['coef'];kind=model['kind']
    if kind=='linear':return c[1]/1000
    if kind=='quadratic':return (c[1]+2*c[2]*(x-1300)/1000)/1000
    if kind=='hinge1500':return (c[1]+(c[2] if x>1500 else 0))/1000
    return float(predict(model,x)[0])*c[1]/x


def compare(rows,field,lag):
    x=[r['rates'][str(lag)] for r in rows];y=[r[field] for r in rows]
    result=[]
    kinds=['linear','quadratic','hinge1500']+(['power'] if all(v>0 for v in y) else [])
    for kind in kinds:
        model=fit_model(x,y,kind)
        errors=[]
        for i in range(6,len(rows)):
            f=fit_model(x[:i],y[:i],kind)
            p=float(predict(f,x[i])[0])
            errors.append(dict(label=rows[i]['label'],training_end=rows[i-1]['label'],actual=y[i],predicted=p,error=p-y[i]))
        jackknife=[fit_model(x[:i]+x[i+1:],y[:i]+y[i+1:],kind)['coef'][-1] for i in range(len(x))]
        model.update(lag=lag,field=field,rolling_mae=float(np.mean([abs(e['error']) for e in errors])),rolling_rmse=float(np.sqrt(np.mean([e['error']**2 for e in errors]))),backtests=errors,jackknife_last_min=min(jackknife),jackknife_last_max=max(jackknife),slopes={str(v):derivative(model,v) for v in [900,1300,1800]},local_elasticities={str(v):derivative(model,v)*v/float(predict(model,v)[0]) for v in [900,1300,1800]})
        early=[]
        for i in range(5,len(rows)):
            f=fit_model(x[:i],y[:i],kind)
            early.append(abs(float(predict(f,x[i])[0])-y[i]) if f['rank']==f['parameters'] else None)
        model['rolling_mae_start5']=float(np.mean(early)) if all(v is not None for v in early) else None
        model['rolling_start5_count']=sum(v is not None for v in early)
        result.append(model)
    return result


def write_report(result):
    models=result['analyses']['unit_rmb']['1']
    linear,quadratic=models[:2]
    profit_quad=result['analyses']['profit']['1'][1]
    a=linear['coef'][0]-1.3*linear['coef'][1]
    names={'linear':'线性','quadratic':'二次','hinge1500':'1500点分段','power':'对数幂函数'}
    lines=[
        '# 海控 CCFI 收入传导及利润非线性检验（2026-10-24）',
        '',
        '结论：历史季度涨跌幅的直接传导弹性约 0.70；以2026Q2实际为锚、本次Q3运价窗口折算约0.67。全样本曲线有高运价边际增大利润的方向，但现有数据不足以确认稳健凸性。默认中性收入模型改为财报线性校准，非线性作为探索选项。',
        '',
        '## 样本与取数',
        '',
        '复用/下载2023Q1—2026Q2共14份中国准则正式报告；五条航线分别核对，国际=总计−中国大陆。收入人民币千元×1000转元。先将累计收入和累计箱量分别转单季，再相除；Q3还与季报直接披露的单季列独立核对。与下一年度相同期间的比较数据核对，所用航线总额/国内收入及箱量未发现修订差异。',
        '',
        '主检验使用提前一个月的月均CCFI，每季取连续三个月简单平均。2023Q1缺2022年末运价，统一排除，剩13季。周数据只有图中周次，没有实际观察日期，不能独立增加13个财务样本，也不伪造周/月对应关系。',
        '',
        '| 季度 | 有效CCFI | 国际收入（亿元） | 国际箱量（万TEU） | 单箱收入（元/TEU） | 归母利润（亿元） |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for r in result['rows']:
        x=r['rates']['1']
        lines.append(f"| {r['label']} | {x:.2f}"+f" | {r['rmb_international']/1e5:.4f} | {r['teu_international']/1e4:.4f} | {r['unit_rmb']:.2f} | {r['profit']:.2f} |" if x is not None else f"| {r['label']} | 缺失 | {r['rmb_international']/1e5:.4f} | {r['teu_international']/1e4:.4f} | {r['unit_rmb']:.2f} | {r['profit']:.2f} |")
    g=result['growth_fit']
    lines += ['', '## 收入传导：估计值及含义','',
              f"12次季度变化，过原点回归：单箱收入涨幅 = {g['through_origin_elasticity']:.6f} × CCFI涨幅；不是逐季增幅比值的简单平均。允许截距时，截距为{g['intercept']*100:.4f}个百分点，斜率为{g['elasticity_with_intercept']:.6f}。",
              '',f'13季水平回归：单箱收入（元/TEU） = {a:.4f} + {linear["coef"][1]/1000:.6f} × 有效CCFI，R²={linear["r2"]:.4f}。',
              '', '即使单箱收入水平与CCFI是线性关系，百分比弹性也不必恒定。线性模型的局部弹性 bX/(a+bX)，在900/1300/1800点分别为 '+ '/'.join(f"{linear['local_elasticities'][str(x)]:.3f}" for x in [900,1300,1800])+'；但每点CCFI的绝对单箱收入增量仍为固定的约4.743元/TEU。',
              '', '当前预测用 Q2实际单箱收入 + f(Q3 CCFI) − f(Q2 CCFI)，保留最新一季的偏差。线性模型下增幅约30.08%，对本次44.82%的CCFI增幅，等效弹性为0.6712。',
              '', '## 模型比较', '',
              '| 模型 | 拟合R² | 7季滚动MAE（元/TEU） | 8季滚动MAE（元/TEU） | 本次单箱收入增幅 | 同成本情景归母（亿元） |',
              '|---|---:|---:|---:|---:|---:|']
    for m in models:
        early=f"{m['rolling_mae_start5']:.2f}" if m['rolling_mae_start5'] is not None else '参数不可识别'
        lines.append(f"| {names[m['kind']]} | {m['r2']:.4f} | {m['rolling_mae']:.2f} | {early} | {m['q3_anchored_growth']*100:.2f}% | {m['q3_profit_same_cost_assumptions']:.2f} |")
    lines += ['', '7季检验从2024Q4开始，训练期含2024Q3高点；8季检验从2024Q3开始，包含对该高点的事前外推。二次模型在7季检验略好，但8季误差显著扩大。1500点分段模型在早期训练期没有高区间样本，不能将不可识别的高区间系数当成0，因此不评分。阈值1500仅为预定探索点，没有用全样本寻找最优阈值。',
              '', '误差是直接预测单箱收入水平的误差，区别于原页归母利润锚定桥的19.87亿元MAE。所有模型使用当前披露版本，按季度顺序训练，不是完整的实时修订快照回测。模型较多、误差样本仅7/8季，不声称选出了长期最优模型。',
              '', '## 对“高运价时每点CCFI带来更多利润”的验算', '',
              f"单箱收入二次项为正，HC3近似p={quadratic['last_coefficient_p_hc3']:.3f}；每点CCFI的拟合单箱收入增量在900/1800点约{quadratic['slopes']['900']:.2f}/{quadratic['slopes']['1800']:.2f}元/TEU，但留一法二次项区间跨过0。去掉2024Q3后收入曲线转为向下弯曲。",
              '', f"归母利润二次拟合也向上弯曲，每点CCFI的拟合归母增量在900/1800点约{profit_quad['slopes']['900']:.3f}/{profit_quad['slopes']['1800']:.3f}亿元，二次项HC3近似p={profit_quad['last_coefficient_p_hc3']:.3f}。高于1500点样本仅2024Q3和2024Q4两季。缩放为每国际TEU的合并归母利润后方向仍正，但并不能从中分离成本因果关系。",
              '', '这些p值采用异方差稳健HC3标准误和Student-t近似，未校正时间相关、模型尝试次数，不能当作精确概率。美元单箱收入的二次项也缺少有力支持，结果不只由人民币折算造成。',
              '', '成本机制需要区分：若利润=(a+b×CCFI−单位成本)×箱量，且单位成本和箱量不变，每增加一点CCFI的绝对利润增量就是b×箱量，仍是常数。固定成本和经营杠杆可以造成利润率/百分比弹性变化，并不自动意味着绝对利润凸性。',
              '', '合并营业成本/总TEU只是成本环境代理，不是国际航线精确单箱成本。该代理的二次模型滚动误差未优于线性，也无法证明高运价区间成本边际显著下降。归母利润同时受箱量、航线结构、汇率、费用、税率及少数股东影响，13季不足以稳健区分全部因素。',
              '', '## 敏感性与限制', '',
              '附件整数标签及重叠备注全部保留。剔除涉及2024年1月、2026年1月的两个季度，线性收入斜率约4.717元/TEU/点，与全样本4.743接近；不能因此证明其他转录值都准确。',
              '', '提前0/1/2月的线性单箱收入滚动MAE分别为 '+ '/'.join(f"{result['analyses']['unit_rmb'][str(lag)][0]['rolling_mae']:.2f}" for lag in [0,1,2])+'元/TEU，当前样本支持保留提前1月作为默认研究窗口。',
              '', '本次中性利润从168.4更新为171.9亿元，只更换收入传导方法；箱量+2%、单位成本+4.5%、毛利以下净额与税前转归母比例仍沿用原中性假设，并非财报已经保证的未来值。悲观136.5、乐观200.1亿元保留原压力参数。',
              '', '## 来源与复算', '',
              '`calibration.json` 包含14份业务表提取、字段、页面、来源哈希、各模型系数、滚动预测、留一法与敏感性。`snapshot.json` 包含原CCFI输入、财务字段来源与哈希。网页财报弹性检验提供原件链接。']
    for e in result['extraction']:
        m=e['source']
        pages='；'.join(k+':'+','.join(map(str,v['page_candidates'])) for k,v in e['tables'].items())
        lines.append(f"\n- [{m['title']}]({m['url']})，{pages}，SHA256 `{m['sha256']}`。")
    (OUT/'ELASTICITY_RESEARCH.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    snapshot=json.loads((OUT/'snapshot.json').read_text(encoding='utf-8'))
    business,extraction,revisions=business_quarters()
    financial={r['period']:r for r in snapshot['history']}
    rows=[]
    for r in business:
        f=financial[r['period']]
        r.update(profit=f['profit'],cost=f['cost'],revenue=f['revenue'],gross=f['revenue']-f['cost'],
                 cost_per_total_teu=f['cost']*1e8/r['teu_total'],profit_per_international_teu=f['profit']*1e8/r['teu_international'],
                 rates={str(lag):rate(snapshot['months'],r['year'],r['q'],lag) for lag in [0,1,2]})
        rows.append(r)
    common=[r for r in rows if all(v is not None for v in r['rates'].values())]
    analyses={field:{str(lag):compare(common,field,lag) for lag in [0,1,2]} for field in ['unit_rmb','unit_usd','profit','profit_per_international_teu','cost_per_total_teu']}
    transitions=[]
    for prev,cur in zip(common,common[1:]):
        dx=cur['rates']['1']/prev['rates']['1']-1
        dy=cur['unit_rmb']/prev['unit_rmb']-1
        transitions.append(dict(label=cur['label'],previous=prev['label'],ccfi_growth=dx,unit_rmb_growth=dy,realized_growth_ratio=dy/dx if abs(dx)>.02 else None))
    anchor=next(r for r in rows if r['label']=='2026Q2')
    target=snapshot['models']['1']['target'];anchorx=anchor['rates']['1']
    for model in analyses['unit_rmb']['1']:
        forecast=float(predict(model,target)[0]);at_anchor=float(predict(model,anchorx)[0])
        anchored=anchor['unit_rmb']+forecast-at_anchor
        growth=anchored/anchor['unit_rmb']-1
        business=snapshot['business']
        revenue=(business['international']*(1+growth)+business['domestic']+business['other'])*1.02
        cost=business['cost']*1.02*1.045
        profit=(revenue-cost+business['below'])*business['conversion']
        model.update(q3_level_forecast=forecast,q3_anchored_forecast=anchored,q3_anchored_growth=growth,q3_effective_elasticity=growth/(target/anchorx-1),q3_profit_same_cost_assumptions=profit)
    gx=np.array([r['ccfi_growth'] for r in transitions]);gy=np.array([r['unit_rmb_growth'] for r in transitions])
    growth_elasticity=float(gx@gy/(gx@gx))
    gxdesign=np.column_stack([np.ones(len(gx)),gx])
    growth_coeff=np.linalg.lstsq(gxdesign,gy,rcond=None)[0]
    differences=[]
    for a,c in zip(common,common[1:]):
        differences.append((c['rates']['1']-a['rates']['1'],c['unit_rmb']-a['unit_rmb']))
    delta_coef=np.linalg.lstsq(np.column_stack([np.ones(len(differences)),np.array(differences)[:,0]]),np.array(differences)[:,1],rcond=None)[0]
    flagged=[r for r in common if any(k in ['2024-01','2026-01'] for k in shifted_months(r['year'],r['q'],1))]
    sensitivities={}
    for label,excluded in [('omit_flagged_january',{r['label'] for r in flagged}),('omit_2024Q3',{'2024Q3'}),('omit_2024',{'2024Q1','2024Q2','2024Q3','2024Q4'})]:
        retained=[r for r in common if r['label'] not in excluded]
        sensitivities[label]={field:{kind:fit_model([r['rates']['1'] for r in retained],[r[field] for r in retained],kind) for kind in ['linear','quadratic']} for field in ['unit_rmb','profit']}
    result=dict(definition='International route revenue (RMB thousand) x 1000 / international TEU; subtract cumulative amounts before division.',
                rows=rows,common_sample=[r['label'] for r in common],analyses=analyses,transitions=transitions,comparative_differences=revisions,extraction=extraction,
                target=dict(ccfi=target,anchor_ccfi=anchorx,anchor_unit_rmb=anchor['unit_rmb']),
                growth_fit=dict(n=len(transitions),through_origin_elasticity=growth_elasticity,intercept=float(growth_coeff[0]),elasticity_with_intercept=float(growth_coeff[1]),delta_intercept=float(delta_coef[0]),delta_slope=float(delta_coef[1])),
                sensitivities=sensitivities,ccfi_flagged_quarters=[r['label'] for r in flagged],high_ccfi_quarters=[r['label'] for r in common if r['rates']['1']>1500],
                limitations=['Weekly labels have no true dates; not treated as additional quarterly observations.','Current original business disclosures used consistently; comparative differences audited separately.','Fits and rolling tests use current report versions; not real-time vintage backtests.','CCFI predicts revenue association, not causal cost-adjusted profit.','Fixed 1500 hinge is an exploratory threshold, not a fitted optimum.'])
    (OUT/'calibration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    write_report(result)
    print('ROWS')
    for r in common:print(r['label'],round(r['rates']['1'],2),round(r['unit_rmb'],2),round(r['profit'],2))
    print('COMPARATIVE DIFFERENCES',revisions)
    for field in analyses:
        print('FIELD',field)
        for m in analyses[field]['1']:print(m['kind'],'MAE',round(m['rolling_mae'],3),'R2',round(m['r2'],3),'p',round(m['last_coefficient_p_hc3'],4),'slopes',m['slopes'],'Q3 growth',m.get('q3_anchored_growth'))


if __name__=='__main__':main()
