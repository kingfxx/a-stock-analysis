"""Deterministic comparisons for structured model claims; no source requests."""

LABELS = {'revenue':'营业收入', 'profit':'归母净利润', 'gross_margin':'毛利率',
          'net_margin':'净利率', 'operating_cash_flow':'经营现金流', 'free_cash_flow':'简化自由现金流',
          'cash_to_profit':'经营现金流/归母净利润', 'fcf_to_dividend':'简化自由现金流/已实施分红',
          'net_cash':'简化净现金', 'interest_bearing_debt':'简化有息负债', 'roic':'ROIC'}


def financial_facts(evidence):
    output = []
    rows = {kind: sorted((e for e in evidence if e['metric']=='financial_period' and e['period_type']==kind),
                        key=lambda e:e['observed_on']) for kind in ('quarter','ttm','year')}
    def add(row, field, comparison, value, unit, baseline=None, baseline_value=None):
        if value is None: return
        direction = ('positive' if value > 0 else 'negative' if value < 0 else 'zero') if comparison in {'level','coverage'} else ('up' if value > 0 else 'down' if value < 0 else 'flat')
        output.append({'id':f"financial.fact.{row['period_type']}.{row['observed_on']}.{field}.{comparison}",
            'metric':'calculated_fact','field':field,'label':LABELS.get(field,field),
            'value':value,'unit':unit,'direction':direction,'comparison':comparison,
            'observed_on':row['observed_on'],'period_type':row['period_type'],
            'baseline_on':baseline['observed_on'] if baseline else None,'baseline_value':baseline_value,
            'current_value':row['value'].get(field),'source':row['source'],
            'source_evidence_ids':[row['id']] + ([baseline['id']] if baseline else []),
            'methodology':'复用既有周期值；金额变化率仅正基数计算，利润率变化用百分点。合并现金流与归母利润/分红的覆盖倍数仅简化参考，不证明资金可自由分配或分红可持续。'})
    for kind, items in rows.items():
        if not items: continue
        latest = items[-1]
        for row in items[-2:]:
            for field in ('profit','operating_cash_flow','free_cash_flow','net_cash'):
                add(row,field,'level',row['value'].get(field),'元')
        previous = items[-2] if len(items)>1 else None
        if previous and kind != 'year':
            # Do not mistake a missing quarter for the immediately prior quarter.
            year, month = int(latest['observed_on'][:4]), int(latest['observed_on'][5:7])
            previous_expected = f'{year-1}-12-31' if month==3 else f'{year}-{month-3:02d}-' + ('31' if month==6 else '30')
            if previous['observed_on'] != previous_expected: previous = None
        baseline_yoy = next((r for r in items if r['observed_on']==f"{int(latest['observed_on'][:4])-1}{latest['observed_on'][4:]}"),None)
        for comparison, baseline in (('qoq', previous), ('yoy', baseline_yoy)):
            if kind=='year' and comparison=='qoq': continue
            if not baseline: continue
            for field in LABELS:
                current, base = latest['value'].get(field), baseline['value'].get(field)
                if current is None or base is None: continue
                ratio = field in {'gross_margin','net_margin','roic'}
                value = current-base if ratio else (current/base-1)*100 if base>0 else None
                add(latest,field,comparison,value,'百分点' if ratio else '%',baseline,base)
        if kind in {'ttm','year'}:
            values=latest['value']
            for name, numerator, denominator in (
                ('cash_to_profit','operating_cash_flow','profit'),
                ('fcf_to_dividend','free_cash_flow','cash_dividend'),
            ):
                top,bottom=values.get(numerator),values.get(denominator)
                if top is not None and bottom is not None and bottom>0:
                    add(latest,name,'coverage',top/bottom,'倍',baseline_value=bottom)
                    output[-1]['numerator_field']=numerator
                    output[-1]['denominator_field']=denominator
                    output[-1]['current_value']=top
    return output
