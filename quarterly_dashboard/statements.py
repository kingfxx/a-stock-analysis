"""Read-only financial statement views over saved Sina facts; no source requests."""
from __future__ import annotations

import json
from datetime import date

from .fundamental_service import SOURCE
from .sources import _number, normalize_code


# Exact field codes matter: gjzb.BIZTOTCOST is total operating cost, despite its title.
FIELDS = {
    'assets': ('fzb', 'TOTASSET'), 'liabilities': ('fzb', 'TOTLIAB'),
    'equity': ('fzb', 'RIGHAGGR'), 'cash': ('fzb', 'CURFDS'),
    'receivables': ('fzb', 'ACCORECE'), 'inventory': ('fzb', 'INVE'),
    'goodwill': ('fzb', 'GOODWILL'),
    'revenue': ('lrb', 'BIZINCO'), 'total_revenue': ('lrb', 'BIZTOTINCO'),
    'cost': ('lrb', 'BIZCOST'), 'total_cost': ('lrb', 'BIZTOTCOST'),
    'operating_profit': ('lrb', 'PERPROFIT'), 'pretax': ('lrb', 'TOTPROFIT'),
    'tax': ('lrb', 'INCOTAXEXPE'), 'net_profit': ('lrb', 'NETPROFIT'),
    'parent_profit': ('lrb', 'PARENETP'), 'minority_profit': ('lrb', 'MINYSHARRIGH'),
    'deducted_profit': ('ysb', 'NPCUT'),
    'cfo': ('llb', 'MANANETR'), 'cfi': ('llb', 'INVNETCASHFLOW'),
    'cff': ('llb', 'FINNETCFLOW'), 'fx': ('llb', 'CHGEXCHGCHGS'),
    'cash_increase': ('llb', 'CASHNETR'), 'cash_start': ('llb', 'INICASHBALA'),
    'cash_end': ('llb', 'FINALCASHBALA'), 'capex': ('llb', 'ACQUASSETCASH'),
    'sale_cash': ('llb', 'LABORGETCASH'),
}
DEBT = ('SHORTTERMBORR', 'SHORTTERMBDSPAYA', 'DUENONCLIAB', 'LONGBORR', 'BDSPAYA', 'LEASELIAB')
BALANCE = ('CURFDS', 'PLAC', 'RECFINANC', 'NOTESACCORECE', 'PREP', 'INVE', 'OTHERCURRASSE', 'EQUIINVE',
           'FIXEDASSECLEATOT', 'CONSPROGTOT', 'INTAASSET', 'GOODWILL',
           'OTHERNONCASSE',
           'SHORTTERMBORR', 'NOTESACCOPAYA', 'CONTRACTLIAB', 'ADVAPAYM',
           'COPEWORKERSAL', 'TAXESPAYA', 'OTHERCURRELIABI', 'DUENONCLIAB', 'LONGBORR', 'BDSPAYA',
           'LEASELIAB', 'OTHERNONCLIABI')
FINANCIAL_BALANCE = ('CURFDS', 'CASHCENBANK', 'PLAC', 'FINCOSTSPAIDCASH', 'LENDANDLOANCUST',
                     'LENDANDLOAN', 'LOANADVANCES', 'TRADFINASSET', 'HOLDINVEDUE', 'INVE',
                     'CLIEDEPO', 'DEPOSIT', 'FDSBORR', 'BDSPAYA', 'TOTLIAB', 'TOTSHAREQUI')
PROFIT = ('BIZTOTINCO', 'BIZINCO', 'BIZTOTCOST', 'BIZCOST', 'BIZTAX', 'SALESEXPE',
          'MANAEXPE', 'DEVEEXPE', 'FINEXPE', 'OTHERINCO', 'INVEINCO', 'VALUECHGLOSS',
          'CREDITIMPLOSSEPROFIT', 'ASSEIMPALOSSPROFIT', 'ASSETSDISLINCO', 'PERPROFIT',
          'NONOREVE', 'NONOEXPE', 'TOTPROFIT', 'INCOTAXEXPE', 'NETPROFIT', 'PARENETP', 'MINYSHARRIGH')
FINANCIAL_PROFIT = ('BIZINCO', 'NETINTEINCO', 'NETPOUNINCO', 'NETINVINCO', 'EARNPREM',
                    'MANAEXPE', 'BIZTAX', 'ASSEIMPALOSS', 'PERPROFIT', 'TOTPROFIT',
                    'INCOTAXEXPE', 'NETPROFIT', 'PARENETP', 'MINYSHARRIGH')
CASH = ('LABORGETCASH', 'LABOPAYC', 'PAYWORKCASH', 'PAYTAX', 'BIZCASHINFL', 'BIZCASHOUTF',
        'MANANETR', 'INVCASHINFL', 'ACQUASSETCASH', 'INVPAYC', 'INVCASHOUTF', 'INVNETCASHFLOW',
        'RECEFROMLOAN', 'DEBTPAYCASH', 'DIVIPROFPAYCASH', 'FINCASHINFL', 'FINCASHOUTF',
        'FINNETCFLOW', 'CHGEXCHGCHGS', 'CASHNETR', 'INICASHBALA', 'FINALCASHBALA')
CASH_STOCKS = {'INICASHBALA': 'FINALCASHBALA', 'CASHOPENBALA': 'CASHFINALBALA',
               'EQUOPENBALA': 'EQUFINALBALA', 'EQAOPENBALA':'EQUFINALBALA',
               'CASHEQUIOPENBALA':'CASHEQUFINBALA'}
CLOSING_CASH = {'FINALCASHBALA', 'CASHFINALBALA', 'EQUFINALBALA', 'CASHEQUFINBALA'}
ALIASES = {'PERPROFIT':('OPERPROFIT',), 'INCOTAXEXPE':('INCOTAX',),
           'PARENETP':('NETPARECOMPPROF',), 'CHGEXCHGCHGS':('EXCHCHGCASHEFFE',),
           'CASHNETR':('CASHEQUINETINCR',), 'INICASHBALA':('CASHEQUIOPENBALA',),
           'FINALCASHBALA':('CASHEQUFINBALA',), 'RIGHAGGR':('TOTSHAREQUI',)}
LABELS = dict(zip(FIELDS, ('资产总额','负债总额','合并股东权益','货币资金','应收账款','存货','商誉',
                         '营业收入','营业总收入','营业成本','营业总成本','营业利润','利润总额','所得税费用',
                         '合并净利润','归母净利润','少数股东损益','扣非归母净利润（来源口径）',
                         '经营净现金流','投资净现金流','筹资净现金流','汇率影响','现金净增加额',
                         '期初现金及等价物','期末现金及等价物','购建长期资产现金支出','销售收现')))


def previous_quarter(period):
    year, month = int(period[:4]), int(period[5:7])
    return f'{year}-{ {6:"03-31", 9:"06-30", 12:"09-30"}[month]}' if month != 3 else None


def compatible(a, b):
    return bool(a and b and a.get('rCurrency') == b.get('rCurrency') == 'CNY'
                and a.get('rType') == b.get('rType') and a.get('rType') == '合并期末')


def items(raw):
    """Deduplicate repeated groups, preserving zero and replacing an invalid duplicate."""
    result = {}
    for item in (raw or {}).get('data', []):
        field = item.get('item_field')
        if not field:
            continue
        key = (item.get('item_source'), field)
        if key not in result or _number(result[key].get('item_value')) is None:
            result[key] = item
    return list(result.values())


def find(raw, source, code):
    for candidate in (code, *ALIASES.get(code, ())):
        found = next((x for x in items(raw) if x.get('item_field') == candidate
                      and x.get('item_source') == source and _number(x.get('item_value')) is not None), None)
        if found:
            return found
    return None


def unit(item):
    if '每股' in item.get('item_title', '') or item.get('item_field') in {'BASICEPS', 'DILUTEDEPS'}:
        return '元/股'
    return '元'


def extract(raw, item, kind, period, mode, reports):
    value = _number(item.get('item_value'))
    output = {'label': item.get('item_title', item['item_field']), 'field': item['item_field'],
              'source': kind, 'item_source': item.get('item_source'), 'value': value,
              'unit': unit(item), 'method': '直接取数', 'period': period,
              'currency': raw.get('rCurrency'), 'scope': raw.get('rType'), 'reason': None}
    if raw.get('rCurrency') != 'CNY' or raw.get('rType') != '合并期末':
        output.update(value=None, reason='币种或合并范围未经确认')
    elif mode == 'quarter' and kind != 'fzb' and int(period[5:7]) != 3:
        if output['unit'] != '元':
            output.update(value=None, reason='每股指标不按累计值差分')
        elif item['item_field'] not in CLOSING_CASH:
            prior_period = previous_quarter(period)
            prior = reports.get(prior_period, {}).get(kind)
            prior_code = CASH_STOCKS.get(item['item_field'], item['item_field'])
            prior_item = find(prior, item.get('item_source'), prior_code)
            if value is None or not compatible(raw, prior) or prior_item is None:
                output.update(value=None, reason=f'缺少同口径 {prior_period} 有效值')
            else:
                earlier = _number(prior_item['item_value'])
                output.update(value=earlier if item['item_field'] in CASH_STOCKS else value - earlier,
                              method='上季末余额' if item['item_field'] in CASH_STOCKS else '本期累计 − 上季累计',
                              baseline_period=prior_period, baseline_field=prior_code)
    if output['value'] is None and output['reason'] is None:
        output['reason'] = '该报告期缺少有效值'
    return output


def missing(label, period, unit='元'):
    return {'label': label, 'value': None, 'unit': unit, 'method': '待补',
            'reason': '该报告期缺少适用资料', 'period': period, 'source': None, 'field': None}


def calculated(label, inputs, formula, operation, period, unit='元'):
    result = missing(label, period, unit)
    result.update(method='自行计算', formula=formula, inputs=inputs)
    if all(x['value'] is not None for x in inputs):
        result['value'] = operation(*[x['value'] for x in inputs])
        result['reason'] = None if result['value'] is not None else '分母非正，无法计算'
    return result


def metric(reports, period, key, mode):
    source, code = FIELDS[key]
    if mode == 'quarter' and source != 'fzb' and period[5:] != '03-31' and key != 'cash_end':
        current = metric(reports, period, key, 'ytd')
        prior_period = previous_quarter(period)
        previous = metric(reports, prior_period, 'cash_end' if key == 'cash_start' else key, 'ytd')
        if current['value'] is None or previous['value'] is None:
            return {**current, 'value':None, 'reason':f'缺少本期或同口径 {prior_period} 有效值'}
        return {**current, 'value':previous['value'] if key == 'cash_start' else current['value'] - previous['value'],
                'method':'上季末余额' if key == 'cash_start' else '本期累计 − 上季累计',
                'baseline_period':prior_period, 'baseline_field':previous['field'],
                'baseline_source':previous['source'], 'inputs':[current,previous], 'reason':None}
    entry = reports.get(period, {})
    # Only accept exact, applicable gjzb fields; never substitute similarly named totals.
    for kind in ('gjzb', source):
        raw = entry.get(kind)
        if not raw:
            continue
        item = find(raw, source, code)
        if item:
            value = extract(raw, item, kind, period, 'ytd', reports)
            if value['value'] is not None:
                value['label'] = LABELS[key]
                return value
    return missing(LABELS[key], period)


def summarize(reports, period, mode, financial):
    values = {key: metric(reports, period, key, mode) for key in FIELDS}
    values['debt'] = calculated('有息负债（简化）', [
        extract(reports[period]['fzb'], item, 'fzb', period, 'ytd', reports) if
        (item := find(reports.get(period, {}).get('fzb'), 'fzb', code)) else missing(code, period)
        for code in DEBT], '短期借款＋短期债券＋一年内到期非流动负债＋长期借款＋应付债券＋租赁负债',
        lambda *v: sum(v), period)
    ratio = lambda a, b: a / b * 100 if b > 0 else None
    for key, label, fields, formula, operation in (
        ('leverage', '资产负债率', ['liabilities', 'assets'], '负债合计 ÷ 资产总计 × 100%', ratio),
        ('gross_margin', '毛利率', ['revenue', 'cost'], '（营业收入 − 营业成本）÷ 营业收入 × 100%',
         lambda r, c: (r-c)/r*100 if r > 0 else None),
        ('net_margin', '合并净利率', ['net_profit', 'revenue'], '合并净利润 ÷ 营业收入 × 100%', ratio),
        ('cash_profit', '经营净现金／合并净利润', ['cfo', 'net_profit'], '经营净现金流 ÷ 合并净利润 × 100%', ratio),
        ('cash_after_capex', '经营净现金减购建支出', ['cfo', 'capex'], '经营净现金流 − 购建长期资产现金支出', lambda a,b:a-b),
    ):
        values[key] = calculated(label, [values[x] for x in fields], formula, operation, period,
                                 '元' if key == 'cash_after_capex' else '%')
    # Source percentages are applicable only in cumulative mode with the same definition.
    for key, field in (('leverage','ASSLIABRT'), ('gross_margin','SGPMARGIN'), ('net_margin','SNPMARGINCONMS')):
        if mode == 'quarter' and key != 'leverage':
            continue
        raw = reports.get(period, {}).get('gjzb')
        item = find(raw, 'ysb', field)
        derived = values[key]['value']
        if item and compatible(raw, raw) and derived is not None and abs(_number(item['item_value']) - derived) < .02:
            values[key] = {**extract(raw, item, 'gjzb', period, 'ytd', reports), 'label': values[key]['label'], 'unit':'%'}
    if financial:
        values['gross_margin'] = missing('毛利率（金融企业不适用）', period, '%')
        values['cash_after_capex'] = missing('经营净现金减购建支出（金融企业不适用）', period)
    return values


def build_section(reports, period, mode, kind, financial):
    raw = reports.get(period, {}).get(kind, {})
    raw_items = items(raw)
    full = [extract(raw, x, kind, period, mode, reports) for x in raw_items]
    if kind == 'fzb':
        sides, side = {}, 'assets'
        liability_codes = set(DEBT) | {'NOTESACCOPAYA','NOTESPAYA','ACCOPAYA','CONTRACTLIAB',
            'ADVAPAYM','COPEWORKERSAL','TAXESPAYA','OTHERCURRELIABI','OTHERNONCLIABI',
            'CLIEDEPO','DEPOSIT','FDSBORR','TOTALCURRLIAB','TOTALNONCLIAB','TOTLIAB'}
        equity_codes = {'RIGHAGGR','TOTSHAREQUI','PARESHARRIGH','MINYSHARRIGH','PAIDINCAPI',
                        'CAPISURP','RESE','UNDIPROF','TREASTK','OCL','OTHEQUIN','GENERISKRESE'}
        asset_codes = set(BALANCE[:BALANCE.index('SHORTTERMBORR')]) | {'TOTASSET','TOTCURRASSET','TOTALNONCASSETS',
                                          'ACCORECE','NOTESRECE','FIXEDASSENET','CONSPROG'}
        for item in raw.get('data', []):
            field, title = item.get('item_field'), item.get('item_title', '')
            if not field:
                if '权益' in title: side = 'equity'
                elif '负债' in title: side = 'liabilities'
                elif '资产' in title: side = 'assets'
                continue
            actual = 'liabilities' if field in liability_codes else 'equity' if field in equity_codes else 'assets' if field in asset_codes else side
            if field in {'TOTLIABSHAREQUI','SHARRIGHTOTAL'}: actual = None
            sides[(item.get('item_source'),field)] = actual
            if field == 'TOTASSET': side = 'liabilities'
            elif field == 'TOTLIAB': side = 'equity'
        for row in full:
            row['balance_side'] = sides.get((row['item_source'],row['field']))
    codes = (FINANCIAL_BALANCE if financial else BALANCE) if kind == 'fzb' else (
        FINANCIAL_PROFIT if financial else PROFIT) if kind == 'lrb' else CASH
    by_field = {x['field']: x for x in full}
    selected = [next((by_field[c] for c in (code,*ALIASES.get(code,())) if c in by_field),
                     missing(code, period)) for code in codes]
    # Missing combined items can fall back to single items without double counting.
    if kind == 'fzb' and not financial:
        fallbacks = {'NOTESACCORECE':('NOTESRECE','ACCORECE'), 'FIXEDASSECLEATOT':('FIXEDASSENET',),
                     'CONSPROGTOT':('CONSPROG',), 'NOTESACCOPAYA':('NOTESPAYA','ACCOPAYA')}
        expanded = []
        for code, item in zip(codes, selected):
            expanded.extend([by_field[x] for x in fallbacks[code] if x in by_field]
                            if code in fallbacks and item['value'] is None else [item])
        selected = expanded
        selected = [x for x in selected if x.get('source') is not None]
    else:
        selected = [x for x in selected if x.get('source') is not None]
    return {'items': selected, 'full_items': full,
            'currency': raw.get('rCurrency'), 'scope': raw.get('rType'),
            'data_source': raw.get('data_source'), 'audit': raw.get('is_audit'),
            'available': bool(raw), 'warnings': [] if raw else ['该报告期尚未保存此表，请刷新财务数据。']}


def waterfall(values, financial, period):
    def delta(label, keys, operation, formula):
        return calculated(label, [values[k] for k in keys], formula, operation, period)
    if financial:
        profit = [('营业收入', values['revenue'], 'absolute'),
                  ('营业净支出及损益', delta('营业净支出及损益', ['operating_profit','revenue'], lambda a,b:a-b,
                                          '营业利润 − 营业收入'), 'relative')]
    else:
        profit = [('营业总收入', values['total_revenue'], 'absolute'),
                  ('营业总成本', delta('营业总成本', ['total_cost'], lambda c:-c,'−营业总成本'), 'relative'),
                  ('其他营业损益（净额）', delta('其他营业损益（净额）', ['operating_profit','total_revenue','total_cost'],
                                             lambda o,r,c:o-r+c,'营业利润 − 营业总收入 ＋ 营业总成本'), 'relative')]
    profit += [('营业利润',values['operating_profit'],'absolute'),
               ('营业外收支等净额',delta('营业外收支等净额',['pretax','operating_profit'],lambda a,b:a-b,'利润总额 − 营业利润'),'relative'),
               ('利润总额',values['pretax'],'absolute'),
               ('所得税费用',delta('所得税费用',['tax'],lambda t:-t,'−所得税费用'),'relative'),
               ('其他净利润调整',delta('其他净利润调整',['net_profit','pretax','tax'],lambda n,p,t:n-p+t,'净利润 − 利润总额 ＋ 所得税费用'),'relative'),
               ('合并净利润',values['net_profit'],'absolute')]
    cash = [(label,values[key],measure) for label,key,measure in (
        ('期初现金及等价物','cash_start','absolute'),('经营净现金流','cfo','relative'),
        ('投资净现金流','cfi','relative'),('筹资净现金流','cff','relative'),
        ('汇率影响','fx','relative'),('期末现金及等价物','cash_end','absolute'))]
    def serialize(rows):
        return [{'label':label, 'value':x['value'], 'measure':measure, 'detail':x} for label,x,measure in rows
                if not (label == '其他净利润调整' and x['value'] is not None and abs(x['value']) < .01)]
    return {'lrb': serialize(profit), 'llb': serialize(cash)}


def read_statements(db, code, *, period=None, mode='ytd', comparison='yoy'):
    code = normalize_code(code)
    if mode not in {'ytd','quarter'} or comparison not in {'yoy','previous','year_end'}:
        raise ValueError('财务分析展示口径无效')
    if period:
        date.fromisoformat(period)
        if period[5:] not in {'03-31','06-30','09-30','12-31'}:
            raise ValueError('请选择季度报告期')
    with db.connection() as conn:
        instrument = conn.execute('SELECT id,name FROM instruments WHERE code=?',(code,)).fetchone()
        rows = conn.execute('SELECT report_type,period,raw_json,obtained_at FROM financial_reports '
                            'WHERE instrument_id=? AND source=? AND report_type IN (?,?,?,?) ORDER BY period',
                            (instrument['id'], SOURCE,'gjzb','fzb','lrb','llb')).fetchall() if instrument else []
    reports = {}
    for row in rows:
        if row['period'][5:] in {'03-31','06-30','09-30','12-31'}:
            reports.setdefault(row['period'], {})[row['report_type']] = json.loads(row['raw_json'])
    periods = sorted((p for p,r in reports.items() if any(k in r for k in ('fzb','lrb','llb'))), reverse=True)
    period = period or (periods[0] if periods else None)
    if period and period not in periods:
        raise ValueError('所选报告期尚未保存三表')
    financial = bool(instrument and any(x in (instrument['name'] or '') for x in ('银行','证券','保险','中国平安','中国人寿','中国太保','新华保险')))
    result = {'code':code,'name':instrument['name'] if instrument else None,'periods':periods,
              'period':period,'mode':mode,'comparison':comparison,'financial_company':financial,
              'updated_at':max((x['obtained_at'] for x in rows),default=None),'sections':{},'observations':[]}
    if not period:
        return result
    year = int(period[:4])
    baseline = f'{year-1}{period[4:]}' if comparison == 'yoy' else f'{year-1}-12-31' if comparison == 'year_end' else (
        previous_quarter(period) or f'{year-1}-12-31')
    values = summarize(reports,period,mode,financial)
    old_values = summarize(reports,baseline,mode,financial)
    charts = waterfall(values,financial,period)
    result.update(baseline=baseline, values=values, baseline_values=old_values, charts=charts)
    for kind in ('fzb','lrb','llb'):
        # Flows always compare the same period window; year-end applies only to balances.
        compare_period = baseline if comparison != 'year_end' or kind == 'fzb' else f'{year-1}{period[4:]}'
        section = build_section(reports,period,mode,kind,financial)
        section['baseline'] = compare_period
        section['comparison_items'] = build_section(reports,compare_period,mode,kind,financial)['full_items']
        if kind != 'fzb' and mode == 'ytd' and comparison == 'previous':
            section['warnings'].append('累计期间长度不同，变动额不代表单季环比；可切换单季度比较。')
        result['sections'][kind] = section
    for kind, keys, operation, label in (
        ('fzb', ['assets','liabilities','equity'], lambda a,l,e:a-l-e, '资产与负债＋权益'),
        ('llb', ['cash_start','cfo','cfi','cff','fx','cash_end'], lambda s,o,i,f,x,e:s+o+i+f+x-e,
         '现金余额与活动净现金流')):
        if all(values[key]['value'] is not None for key in keys):
            difference = operation(*[values[key]['value'] for key in keys])
            if abs(difference) > max(1, abs(values[keys[0]]['value']) * 1e-8):
                result['sections'][kind]['warnings'].append(f'{label}勾稽差额 {difference:,.2f} 元，请核对修订与报表范围。')
    result['baseline_values'] = old_values if comparison != 'year_end' else {
        **old_values, **{k:v for k,v in summarize(reports,f'{year-1}{period[4:]}',mode,financial).items()
                      if k not in {'assets','liabilities','equity','cash','receivables','inventory','goodwill','debt','leverage'}}}
    # Facts only, no model calls or investment verdicts.
    for key in ('revenue','receivables','inventory','net_profit','cfo','capex'):
        current = values[key]
        old = metric(reports,f'{year-1}{period[4:]}',key,mode)
        result['observations'].append({'key':key,'current':current,'baseline':old,
            'growth_pct': (current['value']/old['value']-1)*100 if current['value'] is not None
                         and old['value'] is not None and old['value'] > 0 else None})
    return result
