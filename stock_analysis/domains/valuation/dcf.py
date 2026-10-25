"""Transparent, read-only inputs and pure arithmetic for simplified DCF.

Amounts are CNY yuan, shares are actual shares, and all rates are percentages.
Discounted CFO less Capex is not an enterprise or parent equity valuation.
"""
from __future__ import annotations

import json
import math
from datetime import date
from statistics import median, pstdev

from stock_analysis.domains.financials.core import build_period_rows, view_rows
from stock_analysis.domains.financials.fundamental_service import FundamentalService, SOURCE
from stock_analysis.domains.stocks.price_service import PriceService
from stock_analysis.domains.stocks.sources import normalize_code
from stock_analysis.domains.financials.statements import metric
from stock_analysis.domains.financials.financial_storage import decompress_json


DEFAULTS = dict(base_window='3', growth_pct=3, discount_pct=9, terminal_growth_pct=2)
GROWTH_DEFAULTS = dict(stage1_pct=15, stage2_pct=8, stage3_pct=4)
FCFF_GAPS = [
    '缺少已核实的利息现金支出归类及实际税盾，不能由简化FCF自动转为FCFF。',
    '经营必需现金、受限现金与超额现金尚未区分，不能把全部货币资金加回。',
    '非经营资产与相关收益未完成一致调整。',
    '有息债务、租赁及一年内到期负债需核实，避免缺项归零或重复扣除。',
    '少数股东权益及其他优先索取权缺少可靠估值。',
]


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def forecast_standard(base_fcf, growth_pct):
    """Keep the original standard forecast's power calculation unchanged."""
    base, growth = _finite(base_fcf), _finite(growth_pct)
    if base is None or growth is None or growth <= -100:
        raise ValueError('FCF和增长率必须为有限数值，增长率必须大于-100%')
    try:
        flows = [base * (1 + growth / 100)**t for t in range(1, 11)]
    except OverflowError:
        raise ValueError('DCF计算溢出，请缩小参数范围') from None
    if not all(math.isfinite(v) for v in flows):
        raise ValueError('DCF计算溢出，请缩小参数范围')
    return flows


def forecast_three_stage(base_fcf, stage1_pct, stage2_pct, stage3_pct):
    """Compound from the preceding year; fixed boundaries are 3/4 and 7/8."""
    values = [_finite(v) for v in (base_fcf, stage1_pct, stage2_pct, stage3_pct)]
    if any(v is None for v in values) or any(v <= -100 for v in values[1:]):
        raise ValueError('FCF和阶段增长率必须为有限数值，增长率必须大于-100%')
    current, first, second, third = values
    flows = []
    for growth in [first] * 3 + [second] * 4 + [third] * 3:
        current *= 1 + growth / 100
        if not math.isfinite(current):
            raise ValueError('DCF计算溢出，请缩小参数范围')
        flows.append(current)
    return flows


def discount_cash_flows(fcfs, discount_pct, terminal_growth_pct, *, shares=None, price=None):
    """Discount ten yuan cash flows at year end, allowing effective negative rates."""
    if not isinstance(fcfs, (list, tuple)) or len(fcfs) != 10:
        raise ValueError('DCF需要完整10年现金流序列')
    flows = [_finite(v) for v in fcfs]
    rate, terminal = _finite(discount_pct), _finite(terminal_growth_pct)
    if any(v is None for v in flows) or rate is None or terminal is None:
        raise ValueError('DCF参数必须是有效有限数值')
    rate, terminal = rate / 100, terminal / 100
    if rate <= -1 or terminal <= -1 or rate <= terminal:
        raise ValueError('折现率和永续增长率必须大于-100%，且折现率必须大于永续增长率')
    try:
        rows = [dict(year=t, fcf=flow, present_value=flow / (1 + rate)**t)
                for t, flow in enumerate(flows, 1)]
        explicit = sum(r['present_value'] for r in rows)
        terminal_value = rows[-1]['fcf'] * (1 + terminal) / (rate - terminal)
        terminal_pv = terminal_value / (1 + rate)**10
        total = explicit + terminal_pv
    except (OverflowError, ZeroDivisionError):
        raise ValueError('DCF计算溢出，请缩小参数范围') from None
    if not all(math.isfinite(v) for v in [explicit, terminal_value, terminal_pv, total,
                                         *[v for row in rows for v in row.values()]]):
        raise ValueError('DCF计算溢出，请缩小参数范围')
    count, quote = _finite(shares), _finite(price)
    per_share = total / count if count is not None and count > 0 else None
    difference = (1 - quote / per_share) * 100 if per_share is not None and per_share > 0 and quote is not None and quote > 0 else None
    if per_share is not None and not math.isfinite(per_share) or difference is not None and not math.isfinite(difference):
        raise ValueError('每股换算溢出，请核对股数与价格单位')
    return dict(rows=rows, explicit_value=explicit, terminal_value=terminal_value,
                terminal_present_value=terminal_pv, total_value=total,
                terminal_share_pct=terminal_pv / total * 100 if total else None,
                per_share=per_share, reference_difference_pct=difference,
                mode='simplified', warnings=['基准FCF非正，不适合持续增长DCF。'] if flows[0] <= 0 else [])


def calculate_dcf(base_fcf, growth_pct, discount_pct, terminal_growth_pct, *, shares=None, price=None):
    """Original standard DCF contract, including its positive discount restriction."""
    values = [_finite(v) for v in (base_fcf, growth_pct, discount_pct, terminal_growth_pct)]
    if any(v is None for v in values):
        raise ValueError('DCF参数必须是有效有限数值')
    base, growth, rate, terminal = values
    growth, rate, terminal = (v / 100 for v in (growth, rate, terminal))
    if growth <= -1 or rate <= 0 or terminal <= -1 or rate <= terminal:
        raise ValueError('增长率必须大于-100%，折现率必须为正且大于永续增长率')
    result = discount_cash_flows(forecast_standard(base, values[1]), values[2], values[3],
                                 shares=shares, price=price)
    result['warnings'] = ['基准FCF非正，不适合持续增长DCF。'] if base <= 0 else []
    return result


def calculate_growth_dcf(base_fcf, stage1_pct, stage2_pct, stage3_pct, discount_pct,
                         terminal_growth_pct, *, shares=None, price=None):
    """Three-stage assumptions change the forecast, never the simplified FCF scope."""
    flows = forecast_three_stage(base_fcf, stage1_pct, stage2_pct, stage3_pct)
    result = discount_cash_flows(flows, discount_pct, terminal_growth_pct, shares=shares, price=price)
    result['warnings'] = ['基准FCF非正，不适合持续增长DCF。'] if float(base_fcf) <= 0 else []
    stages = [float(v) for v in (stage1_pct, stage2_pct, stage3_pct)]
    if any(v > 30 for v in stages):
        result['warnings'].append('阶段增长率超过30%，高增长假设可能难以持续。')
    if stages[1] > stages[0] or stages[2] > stages[1]:
        result['warnings'].append('后续阶段增长率上升，请核对成长路径假设。')
    if float(terminal_growth_pct) > stages[2]:
        result['warnings'].append('永续增长率高于最后阶段增长率，请核对长期假设。')
    if float(discount_pct) < 0:
        result['warnings'].append('使用负折现率，属于非常规假设，未来现金流现值会被放大。')
    if result['terminal_share_pct'] is not None and result['terminal_share_pct'] > 75:
        result['warnings'].append('永续价值占比超过75%，估值高度依赖终值假设。')
    return result


def estimate_wacc(risk_free_pct, beta, equity_risk_premium_pct, debt_cost_pct, tax_rate_pct, debt_weight_pct):
    """Manual assumptions only; never fill missing capital-cost data with zero."""
    values = [_finite(v) for v in (risk_free_pct, beta, equity_risk_premium_pct, debt_cost_pct, tax_rate_pct, debt_weight_pct)]
    if any(v is None for v in values):
        raise ValueError('WACC需要全部六项有效输入')
    rf, b, erp, kd, tax, weight = values
    if not -5 <= rf <= 20 or erp < 0 or kd < 0 or not -10 <= b <= 10 or not 0 <= tax <= 100 or not 0 <= weight <= 100:
        raise ValueError('WACC输入范围无效')
    ke = rf + b * erp
    result = ke * (1 - weight / 100) + kd * (1 - tax / 100) * weight / 100
    if not math.isfinite(result) or result <= 0:
        raise ValueError('WACC必须为有限正数')
    return dict(equity_cost_pct=ke, wacc_pct=result, status='manual_assumptions')


def calculate_beta(stock_prices, market_prices, *, years=5, as_of=None, min_samples=None):
    """Weekly close-to-close beta on matching week-ending trading dates."""
    from datetime import timedelta
    end = date.fromisoformat(as_of) if as_of else date.today()
    start = end - timedelta(days=365 * years)
    min_samples = min_samples or max(52, years * 40)
    # Match by ISO week, not by calendar date; both returns use the same pair of week labels.
    def week_map(rows):
        out = {}
        for row in rows:
            day = row.get('date') or row.get('observation_date')
            value = _finite(row.get('close'))
            if day and value and value > 0 and start.isoformat() <= day <= end.isoformat():
                d = date.fromisoformat(day); label=f'{d.isocalendar().year}-W{d.isocalendar().week:02d}'
                if label not in out or day > out[label][0]: out[label]=(day,value)
        return {k:v[1] for k,v in out.items()}
    sw, mw = week_map(stock_prices), week_map(market_prices)
    labels=sorted(set(sw)&set(mw))
    # Missing either leg removes that paired return.
    def week_start(label):
        year,week=label.split('-W'); return date.fromisocalendar(int(year),int(week),1)
    pairs=[(sw[b]/sw[a]-1,mw[b]/mw[a]-1) for a,b in zip(labels,labels[1:])
           if (week_start(b)-week_start(a)).days==7 and sw[a]>0 and sw[b]>0 and mw[a]>0 and mw[b]>0]
    if len(pairs)<min_samples:
        return dict(value=None,status='unavailable',sample_count=len(pairs),reason=f'共同周收益率样本不足{min_samples}组')
    xs=[p[0] for p in pairs]; ys=[p[1] for p in pairs]; mean_x=sum(xs)/len(xs); mean_y=sum(ys)/len(ys)
    variance=sum((y-mean_y)**2 for y in ys)/len(ys)
    beta=(sum((x-mean_x)*(y-mean_y) for x,y in zip(xs,ys))/len(xs))/variance if variance else None
    return dict(value=beta if beta is not None and math.isfinite(beta) else None,
        status='estimated' if beta is not None and math.isfinite(beta) else 'unavailable',sample_count=len(pairs),
        start=start.isoformat(),end=end.isoformat(),market='沪深300',method='个股前复权及沪深300周末收盘价；共同ISO周对齐；周收益率OLS协方差/方差',
        reason=None if beta is not None and math.isfinite(beta) else '市场周收益率方差为零或结果无效')


def build_auto_wacc(db, code, *, years=5, as_of=None):
    """Build evidence-backed estimates only from facts already stored locally."""
    from datetime import timedelta
    from stock_analysis.domains.indices.index_prices import read_prices as read_index_prices
    code=normalize_code(code)
    as_of = as_of or date.today().isoformat()
    if years not in (2, 3, 5):
        raise ValueError('Beta窗口仅支持2、3或5年')
    params = {}
    params['rf'] = read_risk_free_rate(db, as_of)
    params['erp'] = dict(value=6.0,source='模型假设',as_of_date=None,method='可配置情景，默认6%；另提供5%和7%',status='assumption',scenarios=[5,6,7])
    identity=None
    with db.connection() as conn:
        row=conn.execute('SELECT id FROM instruments WHERE code=?',(code,)).fetchone()
        identity=row['id'] if row else None
    stock=[]
    if identity:
        start=(date.fromisoformat(as_of)-timedelta(days=365*years)).isoformat()
        with db.connection() as conn:
            stock=[dict(date=r['trade_date'],close=r['close']) for r in conn.execute(
                "SELECT p.trade_date,p.close FROM adjusted_daily_prices p JOIN adjusted_price_versions v ON v.id=p.version_id "
                "JOIN sync_state s ON s.active_price_version_id=v.id WHERE v.instrument_id=? AND v.status='complete' "
                "AND p.trade_date BETWEEN ? AND ? ORDER BY p.trade_date",(identity,start,as_of))]
    market=read_index_prices(db,index_code='cn:000300',provider='csindex',series_key='history.points',frequency='daily',return_type='unspecified',start=(date.fromisoformat(as_of)-timedelta(days=365*years)).isoformat(),end=as_of,fields=('close',))
    beta=calculate_beta(stock,market,years=years,as_of=as_of)
    beta.update(source='个股 adjusted_daily_prices + index_price_observations(csindex 沪深300)',as_of_date=beta.get('end'),
                method=beta.get('method','已有价格不足或指数序列缺失'),status=beta.get('status','unavailable'))
    if not stock or not market:
        beta.update(value=None,status='unavailable',reason='缺少本地个股前复权日线或沪深300日线；自动估算不触发补采')
    params['beta']=beta
    # Financial statements require full and comparable facts. Reuse the saved JSON only.
    with db.connection() as conn:
        reports=conn.execute('SELECT f.period,f.report_type,f.raw_json,f.obtained_at FROM financial_reports f '
            'JOIN instruments i ON i.id=f.instrument_id WHERE i.code=? AND f.source=? AND f.period<=? ORDER BY f.period',
            (code,SOURCE,as_of)).fetchall()
    raw={}
    for r in reports: raw.setdefault(r['period'],{})[r['report_type']]=json.loads(decompress_json(r['raw_json']))
    def consolidated_cny(doc):
        return bool(doc and doc.get('rCurrency')=='CNY' and doc.get('rType')=='合并期末')
    years_available=sorted(p for p in raw if p.endswith('-12-31') and
        consolidated_cny(raw[p].get('lrb')) and consolidated_cny(raw[p].get('fzb')))
    balance_periods=sorted(p for p in raw if p<=as_of and consolidated_cny(raw[p].get('fzb')))
    def item_value(doc, code):
        for item in (doc or {}).get('data',[]):
            if item.get('item_field')==code:
                return _finite(item.get('item_value'))
        return None
    def debt_for(period):
        document=raw.get(period,{}).get('fzb',{})
        if not consolidated_cny(document): return None,{},list(DEBT_FIELDS)
        result=sum_interest_bearing_debt(document)
        return result['value'],result['components'],result['missing']
    latest=years_available[-1] if years_available else None
    prior=f'{int(latest[:4])-1}-12-31' if latest else None
    ending,components,ending_missing=debt_for(latest) if latest else (None,{},list(DEBT_FIELDS))
    beginning,_,beginning_missing=debt_for(prior) if prior else (None,{},list(DEBT_FIELDS))
    debt_labels=dict(zip(DEBT_FIELDS,('短期借款','应付短期债券','一年内到期的非流动负债','长期借款','应付债券','租赁负债')))
    annual_interest=item_value(raw.get(latest,{}).get('lrb',{}),'INTERESTEXPENSE') if latest else None
    if annual_interest is None and latest: annual_interest=item_value(raw.get(latest,{}).get('lrb',{}),'INTEEXPE')
    if annual_interest is not None: annual_interest=abs(annual_interest)
    kd=calculate_debt_cost(annual_interest,beginning,ending)
    kd_valid=kd is not None
    kd_missing=sorted(set(beginning_missing or []) | set(ending_missing or []))
    params['kd']=dict(value=kd if kd_valid else None,source='financial_reports Sina 合并利润表利息费用 + 资产负债表六项债务',as_of_date=latest,
        method='最近完整年度利息费用 / (上年末与本年末已披露有息债务分项合计的均值)；缺项时为部分估算，不以缺项当零或财务费用替代',
        status='estimated' if kd_valid else 'unavailable',partial=bool(kd_valid and kd_missing),missing_components=kd_missing,
        reason=('债务分项存在未披露项，Kd基于已披露分项部分估算：'+', '.join(debt_labels[k] for k in kd_missing)) if kd_valid and kd_missing else None if kd_valid else '缺少可靠利息费用或两期可用同口径有息债务；不以财务费用替代')
    pbt=item_value(raw.get(latest,{}).get('lrb',{}),'TOTPROFIT') if latest else None
    tax=item_value(raw.get(latest,{}).get('lrb',{}),'INCOTAXEXPE') if latest else None
    effective=calculate_effective_tax(tax,pbt)
    tax_usable=effective is not None and 0<=effective<=100
    params['tax']=dict(value=effective if tax_usable else None,history_value=effective,source='financial_reports Sina 合并利润表',as_of_date=latest,
        method='最近完整年度所得税费用/利润总额；历史有效税率自动作为WACC税率输入，可手动调整',
        status='estimated' if tax_usable else 'unavailable',history_status='estimated' if tax_usable else 'unavailable',
        reason=None if tax_usable else '利润总额非正、字段缺失或有效税率不在0%至100%范围，无法自动填入')
    # Use the latest saved consolidated balance sheet before the valuation date;
    # a semiannual statement is preferable for current capital structure.
    weight_period=balance_periods[-1] if balance_periods else None
    total_debt,weight_components,weight_missing=debt_for(weight_period) if weight_period else (None,{},list(DEBT_FIELDS))
    # Match market capitalization to the balance-sheet reporting date.
    market_cap=None; market_date=None
    snapshot=db.connection()
    with snapshot as conn:
        if weight_period:
            period_date=date.fromisoformat(weight_period)
            quarter_start=date(period_date.year,((period_date.month-1)//3)*3+1,1).isoformat()
            s=conn.execute("SELECT total_market_cap,trade_date FROM market_trade_snapshot WHERE security_code=? AND is_latest=1 AND definition_version IN ('em-valueanalysis-v1','historical-composite-v1') AND total_market_cap IS NOT NULL AND trade_date BETWEEN ? AND ? ORDER BY trade_date DESC,CASE definition_version WHEN 'em-valueanalysis-v1' THEN 0 ELSE 1 END,updated_at DESC,id DESC LIMIT 1",(code,quarter_start,weight_period)).fetchone()
        else:
            s=None
        if s and _finite(s['total_market_cap']) is not None:
            market_cap=_finite(s['total_market_cap']); market_date=s['trade_date']
    weight=total_debt/(total_debt+market_cap)*100 if total_debt is not None and market_cap and total_debt+market_cap>0 else None
    params['weight']=dict(value=weight,source='financial_reports Sina 最近一期合并资产负债表 + market_trade_snapshot.total_market_cap',as_of_date=weight_period,
        method=f'已披露有息债务分项合计 / (有息债务账面值 + A股总市值)；债务账面值近似市场价值；市值取同季度不晚于报告期末的最后交易日 {market_date or "未记录"}',
        status='estimated' if weight is not None else 'unavailable',partial=bool(weight is not None and weight_missing),missing_components=weight_missing or [],
        reason=('债务分项存在未披露项，权重按已披露分项部分估算：'+', '.join(debt_labels[k] for k in weight_missing)) if weight is not None and weight_missing else None if weight is not None else '债务或基准日总市值不可用；A+H结构需人工核验')
    warnings=['简化FCF=CFO−资本开支不等同严格FCFF；应用WACC后现金流与折现率口径仍可能不匹配。',
            '金融企业不适用普通工业企业WACC。']
    if ending == 0: warnings.append('最新完整年度已披露有息债务为零；未将零债务成本视为可用Kd。')
    if latest and latest != as_of[:4]+'-12-31': warnings.append('财务年度与估值基准日不一致。')
    if weight_period and not market_date: warnings.append('缺少与最近一期有息债务资产负债表同季度的市值快照，债务权重不可用。')
    if weight_missing: warnings.append('债务权重基于已披露有息债务分项部分估算，未将缺失分项填零；请核对资产负债表。')
    if kd_missing: warnings.append('Kd基于两个年度已披露有息债务分项部分估算，遗漏项可能影响债务成本。')
    if effective is not None and not 0<=effective<=100: warnings.append('历史有效税率异常，未作为预期税率。')
    warnings.append('利息资本化及租赁利息拆分未从财务附注核验；Kd口径仍需人工复核。')
    return dict(parameters=params,beta_window_years=years,erp_scenarios=[5,6,7],complete=all(_finite(params[k].get('value')) is not None for k in ('rf','beta','erp','kd','tax','weight')),
        as_of_date=as_of,warnings=warnings)


def read_risk_free_rate(db, as_of):
    """Read the latest valid China 10Y revision on or before the valuation date."""
    evidence=dict(value=None,source='本地 sovereign_yield_observations.china_10y / 东方财富',
        as_of_date=None,method='读取 v1 最新修订；取不晚于估值基准日的最近有效中国10年期国债收益率（%）',
        status='unavailable')
    try:
        end=date.fromisoformat(as_of)
    except (TypeError,ValueError):
        return dict(evidence,reason='估值基准日格式无效')
    with db.connection() as conn:
        rows=conn.execute(
            'SELECT observation_date,china_10y,id,source_id,batch_id,version,updated_at '
            "FROM sovereign_yield_observations WHERE is_latest=1 AND definition_version='v1' "
            'AND china_10y IS NOT NULL AND observation_date<=? ORDER BY observation_date DESC',
            (end.isoformat(),))
        for row in rows:
            value=_finite(row['china_10y'])
            if value is None:
                continue
            observed=date.fromisoformat(row['observation_date'])
            return dict(evidence,value=value,as_of_date=observed.isoformat(),status='observed',
                stale=(end-observed).days>7,observation_id=row['id'],source_id=row['source_id'],
                batch_id=row['batch_id'],version=row['version'],updated_at=row['updated_at'],
                definition_version='v1')
    return dict(evidence,reason='本地数据库没有不晚于估值基准日的有效中国10年期国债收益率，请先更新国债数据或手工填写')


DEBT_FIELDS=('SHORTTERMBORR','SHORTTERMBDSPAYA','DUENONCLIAB','LONGBORR','BDSPAYA','LEASELIAB')


def sum_interest_bearing_debt(balance_raw):
    """Sum observed approved debt components; keep missing components explicit for partial estimates."""
    values={field:None for field in DEBT_FIELDS}
    for item in (balance_raw or {}).get('data',[]):
        field=item.get('item_field')
        if field in values and values[field] is None: values[field]=_finite(item.get('item_value'))
    missing=[k for k,v in values.items() if v is None]
    observed=[v for v in values.values() if v is not None]
    total=sum(observed) if observed else None
    return dict(value=total,components=values,missing=missing,complete=not missing and len(observed)==len(values))


def calculate_debt_cost(annual_interest, opening_debt, closing_debt):
    interest,opening,closing=map(_finite,(annual_interest,opening_debt,closing_debt))
    if interest is None or opening is None or closing is None or opening+closing<=0:
        return None
    value=abs(interest)/((opening+closing)/2)*100
    return value if math.isfinite(value) and 0<=value<=50 else None


def calculate_effective_tax(tax_expense, profit_before_tax):
    tax,pbt=map(_finite,(tax_expense,profit_before_tax))
    if tax is None or pbt is None or pbt<=0: return None
    value=tax/pbt*100
    return value if math.isfinite(value) else None


def _growth_recommendation(history):
    metrics = []
    for field, label in [('revenue', '营业收入'), ('profit', '归母净利润'), ('free_cash_flow', '简化FCF')]:
        changes = []
        values = [_finite(r.get(field)) for r in history]
        for old, new in zip(history, history[1:]):
            a, b = _finite(old.get(field)), _finite(new.get(field))
            if int(new['period'][:4]) == int(old['period'][:4]) + 1 and a is not None and a > 0 and b is not None and b >= 0:
                change = (b / a - 1) * 100
                if math.isfinite(change):
                    changes.append(change)
        metrics.append(dict(field=field, label=label, sample_count=len(changes),
                            available_years=sum(v is not None for v in values),
                            median_growth_pct=median(changes) if changes else None,
                            volatility_pct=pstdev(changes) if changes else None,
                            negative_years=sum(v is not None and v < 0 for v in values)))
    reliable = [m['median_growth_pct'] for m in metrics if m['sample_count'] >= 4]
    if not reliable:
        return dict(value_pct=3, status='insufficient', metrics=metrics,
                    basis='不足5年可比数据；3%为配置假设，不使用异常端点CAGR。')
    recommendation = max(-3, min(8, median(reliable)))
    unstable = any(m['negative_years'] or (m['volatility_pct'] or 0) > 40 for m in metrics)
    if unstable:
        recommendation = min(recommendation, 3)
    return dict(value_pct=round(recommendation, 2), status='estimated', metrics=metrics,
                basis='最近最多10个完整年度、相邻正基数同比的中位数；至少4对同比参与指标中位数汇总，范围-3%至8%；负值或同比波动标准差>40个百分点时上限3%。推荐是历史参考。')


def read_dcf(db, code, legacy_root):
    """Read existing facts only. No collector, migration, model, or price lease."""
    code = normalize_code(code)
    financial = FundamentalService(db, legacy_root).read(code)
    reports = financial.get('reports', [])
    with db.connection() as conn:
        raw_rows = conn.execute('SELECT f.* FROM financial_reports f JOIN instruments i ON i.id=f.instrument_id WHERE i.code=? AND f.source=?', (code, SOURCE)).fetchall()
        snapshots = conn.execute("SELECT * FROM market_trade_snapshot WHERE security_code=? AND is_latest=1 AND trade_date<=? AND definition_version IN ('em-valueanalysis-v1','historical-composite-v1') ORDER BY trade_date DESC,CASE definition_version WHEN 'em-valueanalysis-v1' THEN 0 ELSE 1 END,updated_at DESC,id DESC", (code, date.today().isoformat())).fetchall()
        overrides = conn.execute('SELECT o.* FROM report_overrides o JOIN instruments i ON i.id=o.instrument_id WHERE i.code=?', (code,)).fetchall()
        industry = conn.execute('SELECT d.name,m.effective_date FROM sw_memberships m JOIN sw_industries d ON d.code=m.industry_code WHERE m.stock_code=? ORDER BY m.import_id DESC LIMIT 1', (code,)).fetchone()
    raw, metadata = {}, {}
    for r in raw_rows:
        raw.setdefault(r['period'], {})[r['report_type']] = json.loads(decompress_json(r['raw_json']))
        metadata[(r['period'], r['report_type'])] = dict(content_hash=r['content_hash'], obtained_at=r['obtained_at'], run_id=r['run_id'])
    override_map = {(r['period'], r['field_name']): r for r in overrides}
    mappings = [('revenue_ytd', 'revenue'), ('profit_ytd', 'parent_profit'), ('operating_cash_flow_ytd', 'cfo'), ('capex_ytd', 'capex')]
    normalized, evidence = [], {}
    for original in reports:
        row = dict(original)
        period = row['period']
        if period > date.today().isoformat():
            continue
        evidence[period] = {}
        for field, key in mappings:
            detail = metric(raw, period, key, 'ytd')
            selected = raw.get(period, {}).get(detail.get('source'), {})
            detail.update(currency=selected.get('rCurrency'), scope=selected.get('rType'),
                          provenance=metadata.get((period, detail.get('source'))), override=False)
            if period in raw:
                row[field] = detail['value']
            else:
                row[field] = None
                detail = dict(value=None, source='legacy', field=field,
                              period=period, unit='元', method='待补', reason='缺少币种及合并口径原始证据，未使用legacy值')
            override = override_map.get((period, field))
            relevant_kind = 'llb' if key in ('cfo','capex') else 'lrb'
            relevant_raw = raw.get(period, {}).get(relevant_kind, {})
            if override and relevant_raw.get('rCurrency') == 'CNY' and relevant_raw.get('rType') == '合并期末':
                row[field] = _finite(json.loads(override['value_json']))
                detail.update(value=row[field], override=True, method='人工覆盖', override_origin=override['origin'],
                              override_updated_at=override['updated_at'], override_source_path=override['source_path'],
                              currency=relevant_raw['rCurrency'], scope=relevant_raw['rType'])
            evidence[period][field] = detail
        normalized.append(row)
    annual = view_rows(build_period_rows(normalized, [], []), 'year')
    history = []
    latest = max((r['period'] for r in reports if r['period'] <= date.today().isoformat()), default=None)
    last_year = int(latest[:4]) - (latest[5:] != '12-31') if latest else date.today().year - 1
    for row in annual:
        if last_year - 9 <= int(row['period'][:4]) <= last_year:
            sources = evidence[row['period']]
            sources['free_cash_flow'] = dict(method='复用core经营现金流−资本开支', unit='元', inputs=[sources['operating_cash_flow_ytd'], sources['capex_ytd']])
            history.append({k: row.get(k) for k in ('period', 'revenue', 'profit', 'operating_cash_flow', 'capex', 'free_cash_flow')} | dict(sources=sources))
    by_period = {r['period']: r for r in history}
    bases = {}
    for n in (1, 3, 5):
        periods = [f'{y}-12-31' for y in range(last_year - n + 1, last_year + 1)]
        values = [by_period.get(p, {}).get('free_cash_flow') for p in periods]
        available = all(_finite(v) is not None for v in values)
        bases[str(n)] = dict(value=sum(values) / n if available else None, periods=periods,
                             available=available, reason=None if available else '连续完整年度FCF存在缺年或缺值，未跳过缺失年份。',
                             source='现有年度经营现金流−资本开支，元')
    market = dict(total_shares=None, shares_date=None, shares_source=None, price=None, price_date=None, price_source=None)
    warnings = []
    snapshot = next((r for r in snapshots if (_finite(r['total_shares']) or 0) > 0), None)
    price_snapshot = next((r for r in snapshots if (_finite(r['close_price']) or 0) > 0), None)
    if snapshots and snapshot and snapshots[0]['trade_date'] > snapshot['trade_date']:
        warnings.append('最新交易快照缺少有效总股数，使用较早已核实股本并展示日期。')
    if snapshot and (_finite(snapshot['total_shares']) or 0) > 0:
        market.update(total_shares=snapshot['total_shares'], shares_date=snapshot['trade_date'], shares_source='market_trade_snapshot.total_shares（股）',
                      shares_evidence=dict(source_id=snapshot['source_id'], batch_id=snapshot['batch_id'], content_hash=snapshot['content_hash'], definition_version=snapshot['definition_version'], obtained_at=snapshot['obtained_at']))
    elif normalized:
        current = max(normalized, key=lambda r: r['period'])
        # Sina's monetary share-capital line is not automatically a verified share count.
        market.update(shares_date=current['period'], shares_source='最新报表股本（实收资本面值金额，未验证股数）')
        warnings.append('缺少已核实总股数；报表实收资本金额不能自动替代股数，每股参考值留空。')
    prices = PriceService(db).read(code, 'raw', lease=False, end=date.today().isoformat())
    price_row = next((p for p in reversed(prices) if (_finite(p['close']) or 0) > 0), None)
    if price_row and (not price_snapshot or price_row['date'] >= price_snapshot['trade_date']):
        market.update(price=price_row['close'], price_date=price_row['date'], price_source='raw_daily_prices.close / 腾讯未复权收盘价')
    elif price_snapshot:
        market.update(price=price_snapshot['close_price'], price_date=price_snapshot['trade_date'], price_source='market_trade_snapshot.close_price（未复权，较新日期或回退）')
    for field in ('shares_date', 'price_date'):
        if market[field] and (date.today() - date.fromisoformat(market[field])).days > 30:
            warnings.append(('股本' if field == 'shares_date' else '行情') + '数据距今超过30天，请核对时效。')
    if market['shares_date'] and market['price_date'] and market['shares_date'] != market['price_date']:
        warnings.append('股本与行情日期不同，参考换算可能受期间股本变动影响。')
    industry_name = industry['name'] if industry else None
    name = financial.get('name') or ''
    financial_company = any(word in name + (industry_name or '') for word in ('银行', '证券', '保险', '中国平安', '多元金融'))
    if financial_company:
        warnings.append('金融企业不适用本标准DCF模型，结果应禁止展示。')
    elif not industry:
        warnings.append('缺少行业分类，DCF适用性仍需核对。')
    elif any(word in industry_name for word in ('煤炭', '钢铁', '有色', '航运', '石油', '房地产')):
        warnings.append('周期行业需核对跨周期现金流，历史增长可能不可持续。')
    if any(r['free_cash_flow'] is not None and r['free_cash_flow'] <= 0 for r in history):
        warnings.append('历史存在非正FCF年份，请检查投资周期和异常值；不适合直接外推永续增长。')
    return dict(code=code, name=financial.get('name'), mode='simplified', model_label='简化DCF（非FCFF/FCFE）', defaults=dict(DEFAULTS), growth_defaults=dict(GROWTH_DEFAULTS),
                bases=bases, history=history, market=market, updated_at=financial.get('updated_at'), warnings=warnings,
                recommendations=dict(growth=_growth_recommendation(history),
                    discount=dict(value_pct=9, status='assumption', basis='9%为可配置折现率假设；本地缺少已核实的10年国债收益率、Beta、ERP和资本成本数据，非自动WACC。'),
                    terminal=dict(value_pct=2, range_pct=[0, 2], status='assumption', basis='成熟企业0%—2%初步假设区间；企业成熟度未经自动验证，允许覆盖，且必须低于折现率。')),
                applicability=dict(financial_company=financial_company, blocked=financial_company, industry=industry_name,
                                   basis='本地申万行业分类与公司名称关键词' if industry else '公司名称关键词；行业分类不可用'),
                professional=dict(available=False, gaps=list(FCFF_GAPS), adjustments=[]))
