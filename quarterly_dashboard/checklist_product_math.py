"""Display-only product arithmetic from saved evidence; never rewrite history."""
import copy
import re
from decimal import Decimal


def product_components(text):
    """Read explicitly headed product income/cost or income/margin tables."""
    from .report_business_metrics import _operating_rows
    found=[]
    headings=list(re.finditer(r'(?m)^\s*分产品(?:或服务)?\s*$', text))
    for heading in headings:
        header=text[max(0,heading.start()-500):heading.start()]
        units=list(re.finditer(r'单位[：:]\s*(千元|万元|亿元|元)',header))
        if not units or not re.search(r'营业收入\s+营业成本\s+毛利率',header):continue
        section=text[heading.end():]
        end=re.search(r'(?m)^\s*(?:分地区|分业务|分行业|分销售模式|[（(]\d+[)）]|\d+、)',section)
        if end:section=section[:end.start()]
        rows,_,_=_operating_rows(section)
        if not rows or len({row['name'] for row in rows})!=len(rows):continue
        if any(row['revenue']<0 or row['cost']<0 for row in rows):continue
        found.append({'unit':units[-1][1], 'rows':rows})
    return found


def _number(value):
    return Decimal(str(value))


def enrich_products(result, snapshot):
    """Fill absent values only, matching source period, unit, name AND revenue."""
    result=copy.deepcopy(result)
    item=next((item for item in result.get('items',[]) if item['id']=='products'),None)
    if not item or not isinstance(item.get('tables'),list):return result
    evidence=snapshot.get('evidence',[])
    for income in list(item['tables']):
        if income['measure']!='income':continue
        rows=income['rows']
        candidates=[]
        for entry in evidence:
            if entry.get('metric')!='report_excerpt' or entry.get('observed_on')!=income['period']:continue
            if entry['id'] not in item['evidence_ids']:continue
            for page in entry.get('value',[]):
                for source in product_components(page.get('text','')):
                    if source['unit']!=income['unit']:continue
                    lookup={row['name']:row for row in source['rows']}
                    if all(row['name'] in lookup and row['amount'] is not None and
                           _number(row['amount'])==_number(lookup[row['name']]['revenue']) for row in rows):
                        candidates.append((entry['id'],lookup))
        gross=next((table for table in item['tables'] if table['period']==income['period'] and
                    table['classification']==income['classification'] and table['measure']=='gross_profit' and table['unit']==income['unit']),None)
        derived={}
        methods=set()
        if candidates and all({name:row['cost'] for name,row in match.items() if name in {r['name'] for r in rows}}==
                              {name:row['cost'] for name,row in candidates[0][1].items() if name in {r['name'] for r in rows}} for _,match in candidates):
            source_id,lookup=candidates[0]
            derived={row['name']:float(_number(row['amount'])-_number(lookup[row['name']]['cost'])) for row in rows}
            methods.add('收入−营业成本（计算）')
        else:
            source_id=None
            for row in rows:
                if row['amount'] is None:continue
                if row.get('cost') is not None:
                    derived[row['name']]=float(_number(row['amount'])-_number(row['cost']))
                    methods.add('收入−营业成本（计算）')
                elif row.get('gross_margin_pct') is not None:
                    derived[row['name']]=float(_number(row['amount'])*_number(row['gross_margin_pct'])/100)
                    methods.add('收入×披露毛利率（估算，毛利率已取整）')
        if derived:
            if gross is None:
                gross={key:copy.deepcopy(income[key]) for key in ('period','classification','unit','evidence_ids')}
                gross.update(measure='gross_profit',basis='',denominator=None,
                    rows=[{'name':row['name'],'amount':None,'share_pct':None} for row in rows])
                item['tables'].append(gross)
            changed=False
            for row in gross['rows']:
                if row['amount'] is None and row['name'] in derived:
                    row['amount']=derived[row['name']]; changed=True
                    row['amount_method']='估算' if '估算' in ''.join(methods) else '计算'
            if changed:
                gross['basis']='毛利：'+'；'.join(sorted(methods))
                if source_id and source_id not in gross['evidence_ids']:gross['evidence_ids'].append(source_id)
        for table in (income,gross):
            if not table or table['denominator'] is not None:continue
            if any(row['amount'] is None or row['share_pct'] is not None for row in table['rows']):continue
            if re.search(r'重叠|非互斥|不可加总|不能相加|子公司',table['basis']):continue
            total=sum((_number(row['amount']) for row in table['rows']),Decimal(0))
            if total<=0:continue
            table['denominator']=float(total)
            basis=re.sub(r'[，；]?未披露该分类合计分母','',table['basis'])
            table['basis']=basis+'；占比按所列产品'+('收入' if table['measure']=='income' else '毛利')+'合计计算，不代表全公司占比'
    return result
