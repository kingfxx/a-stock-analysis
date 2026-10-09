"""Conservative arithmetic for explicitly labelled report tables; no model guesses.

Unsupported layouts retain their original excerpts for qualitative analysis.
All amounts keep the report unit; ratios require a reconciled positive total.
"""
import re
from decimal import Decimal

_NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[-—–]"


def _values(line):
    tail = re.search(r"\s+(?=-?\d|[-—–](?:\s|$))", line)
    if not tail:return []
    tokens = line[tail.start():].split()
    if not tokens or any(not re.fullmatch(_NUMBER, t) for t in tokens):return []
    return [None if t in ('-', '—', '–') else float(t.replace(',', '')) for t in tokens]


def product_display_corrections(snapshot):
    """Repair this known extraction defect for display, preserving saved evidence."""
    corrections=[]
    for entry in snapshot.get('evidence',[]):
        if entry.get('metric')!='report_excerpt':continue
        for page in entry.get('value',[]):
            old=page.get('business_metrics',{})
            for metric in old.values():
                if not any(re.search(r'\d[\d,]*\.\d+[,\d]*\.\d+',row.get('name','')) for row in metric.get('rows',[])):continue
                period=metric['period']
                corrected=business_metrics(page['text'],period,currency_context=metric.get('currency_basis'))
                corrections.append({'evidence_id':entry['id'],'period':period,'metrics':corrected})
                break
    return corrections


def _reconciles(values, total):
    return total is not None and total > 0 and abs(sum(v or 0 for v in values)-total) <= max(1, len(values))


def _operating_rows(section):
    rows=[];pending='';subtotal=None;total=None;wrapped=False
    for line in section.splitlines():
        line=re.sub(r'(?:增加|减少)\s*[-+]?\d+(?:\.\d+)?(?:个?百分点)?','',line).replace('个百分点','').strip()
        # PDF extraction can join adjacent, two-decimal monetary columns.
        # Split only the explicit thousands-grouped layout; never skip a bad
        # amount and mistake the following percentage for revenue.
        line=re.sub(r'(-?\d{1,3}(?:,\d{3})+\.\d{2})(?=-?\d{1,3},\d{3})',r'\1 ',line)
        match=re.search(r'(?:^|\s)(?=-?\d)',line)
        if match:
            inline=re.sub(r'\s+','',line[:match.start()])
            label=inline or pending
            values=line[match.start():].split()
            if not label:continue
            if len(values)<2 or any(not re.fullmatch(_NUMBER,v) or v in ('-', '—', '–') for v in values[:2]):
                return [],None,None
            revenue,cost=(float(v.replace(',','')) for v in values[:2])
            if label=='小计':subtotal=(revenue,cost)
            elif label=='合计':total=(revenue,cost);break
            else:rows.append({'name':label,'revenue':revenue,'cost':cost})
            wrapped=not bool(inline);pending=''
        elif re.fullmatch(r'[\u4e00-\u9fff]{1,12}',line.strip()):
            label=line.strip()
            if label in ('个百分点','营业收入','营业成本','毛利率'):continue
            if rows and (wrapped or label in ('业务','间相互抵销')):
                rows[-1]['name']+=label;wrapped=False
            else:pending=label
        else:wrapped=False
    return rows,subtotal,total


def _operating_mix(text, period, unit):
    """Prefer product detail; reconcile parallel industry/product views once."""
    headings=list(re.finditer(r'主营业务分(行业|产品|地区|销售模式)情况',text))
    views={}
    for i,heading in enumerate(headings):
        axis=heading[1]
        if axis not in ('行业','产品'):continue
        section=text[heading.end():headings[i+1].start() if i+1<len(headings) else len(text)]
        if not re.search(r'营业收入\s+营业成本',section):continue
        rows,subtotal,total=_operating_rows(section)
        if not rows or len({r['name'] for r in rows})!=len(rows):continue
        sums=tuple(float(sum(Decimal(str(r[key])) for r in rows)) for key in ('revenue','cost'))
        if total and not all(_reconciles([r[key] for r in rows],total[j]) for j,key in enumerate(('revenue','cost'))):continue
        if subtotal:
            ordinary=[r for r in rows if '抵销' not in r['name']]
            if not all(_reconciles([r[key] for r in ordinary],subtotal[j]) for j,key in enumerate(('revenue','cost'))):continue
        views[axis]=(rows,total,sums)
    if not views:return {}
    axis='产品' if '产品' in views else '行业'
    rows,total,sums=views[axis]
    cross_check=False
    if '产品' in views and '行业' in views:
        cross_check=all(abs(x-y)<=max(1,len(rows)) for x,y in zip(views['产品'][2],views['行业'][2]))
        if not cross_check:return {}
    if not total:
        # A row sum alone may be a partial list. Require an independent business view.
        if not cross_check:return {}
        total=sums
    if total[0]<=0:return {}
    difference=lambda a,b:float(Decimal(str(a))-Decimal(str(b)))
    gross=difference(*total)
    reconciliation='分产品与分行业的收入、成本合计一致；两套分类不相加' if cross_check else '收入和成本与报告合计核对一致'
    note='业务收入含内部交易，除以抵销后的合计；保留抵销项，不冒称外部客户收入占比。' if any('抵销' in r['name'] for r in rows) else '分母为本表主营业务合计，不冒称公司营业总收入。'
    return {
        'revenue_mix':{'period':period,'unit':unit,'currency':'CNY','basis':'主营业务分'+axis+'收入',
            'classification':axis,'reconciliation':reconciliation,
            'denominator':total[0],'denominator_label':'主营业务收入合计（含已列抵销项）',
            'rows':[{'name':r['name'],'revenue':r['revenue'],'share_pct':round(r['revenue']/total[0]*100,4)} for r in rows],'note':note},
        'profit_mix':{'period':period,'unit':unit,'currency':'CNY','basis':'毛利（营业收入减营业成本），非净利润',
            'classification':axis,'reconciliation':reconciliation,
            'denominator':gross,'denominator_label':'主营业务毛利合计（含已列抵销项）',
            'rows':[{'name':r['name'],'profit':difference(r['revenue'],r['cost']),'share_pct':round(difference(r['revenue'],r['cost'])/gross*100,4) if gross>0 else None} for r in rows],
            'note':'毛利贡献不等于净利润贡献；保留亏损及抵销，非正毛利合计不计算贡献比例。'}}


def _segment_net_mix(text, period, unit):
    section=text.split('报告分部的财务信息',1)
    if len(section)!=2:return {}
    section=section[1]
    profit=re.search(r'^净利润(?:[/／]?[（(]损失[）)])?\s+[^\n]+',section,re.M)
    header=re.search(r'^项目\s+([^\n]+)',section,re.M)
    if not profit or not header:return {}
    values=_values(profit[0]);columns=header[1].split()
    if len(columns)+1==len(values):
        before=section[:header.start()].strip().splitlines()
        after=section[header.end():].strip().splitlines()
        if before and after and re.fullmatch(r'[\u4e00-\u9fff]{2,30}',before[-1]) and re.fullmatch(r'[\u4e00-\u9fff]{1,6}',after[0]):
            columns.insert(0,before[-1]+after[0])
    if len(columns)!=len(values) or not columns or columns[-1]!='合计' or any(v is None for v in values):return {}
    if not _reconciles(values[:-1],values[-1]):return {}
    result={'profit_mix':{'period':period,'unit':unit,'currency':'CNY','basis':'分部净利润（按报告原口径，区别于归母净利润）',
        'denominator':values[-1],'denominator_label':'分部净利润合计（含亏损及抵销）',
        'rows':[{'name':n,'profit':v,'share_pct':round(v/values[-1]*100,4)} for n,v in zip(columns[:-1],values[:-1])],
        'note':'按分部净利润合计计算贡献，保留亏损和抵销项，正盈利分部贡献可能超过100%。'}}
    revenue=re.search(r'^(?:主营业务收入|对外交易收入|营业收入)\s+[^\n]+',section,re.M)
    income=_values(revenue[0]) if revenue else []
    if len(income)==len(columns) and all(v is not None for v in income) and _reconciles(income[:-1],income[-1]):
        result['revenue_mix']={'period':period,'unit':unit,'currency':'CNY','basis':'分部收入（按报告表格原口径）',
            'denominator':income[-1],'denominator_label':'分部收入合计（含抵销）',
            'rows':[{'name':n,'revenue':v,'share_pct':round(v/income[-1]*100,4)} for n,v in zip(columns[:-1],income[:-1])],
            'note':'分部收入包含已列抵销，不冒称单一产品或归母利润份额。'}
    return result


def _narrative_revenue_mix(text, period):
    """Use explicit component definitions, never sum differently scoped businesses."""
    compact=re.sub(r'\s+','',text)
    main=re.search(r'主营(?:业务)?收入(?:\d{1,2})?(?:为|达到|达)人民币([\d,]+(?:\.\d+)?)亿元',compact)
    definition=re.search(r'主营(?:业务)?收入[:：]包括(.{1,180})',compact)
    if not main or not definition:return {}
    names=re.findall(r'(?:^|和|、|及)([\u4e00-\u9fffA-Za-z]{2,15}收入)(?=[（(])',definition[1])
    names=list(dict.fromkeys(names))
    if len(names)<2:return {}
    rows=[];reported=[]
    for name in names:
        found=re.search(re.escape(name)+r'(?:为|达到|达)人民币([\d,]+(?:\.\d+)?)亿元',compact)
        if not found:return {}
        value=float(found[1].replace(',',''));rows.append({'name':name,'revenue':value})
        tail=compact[found.end():found.end()+70]
        ratio=re.search(r'^.*?占收比(?:达到|达|为)(\d+(?:\.\d+)?)%',tail)
        # A ratio after another amount belongs to that later business, not this one.
        if ratio and '收入' not in tail[:ratio.start()+ratio[0].find('占收比')]:reported.append(name+'报告另披露占比'+ratio[1]+'%')
    total=float(main[1].replace(',',''))
    if total<=0 or abs(sum(r['revenue'] for r in rows)-total)>0.5*(len(rows)+1):return {}
    for r in rows:r['share_pct']=round(r['revenue']/total*100,4)
    supplementary=[]
    for match in re.finditer(r'(?:[。；，;]|其中)([\u4e00-\u9fffA-Za-z]{2,15}收入)(?:为|达到|达)人民币([\d,]+(?:\.\d+)?)亿元',compact):
        name=re.sub(r'^(?:(?:本公司|本集团|公司|集团|其中))+','',match[1])
        if name in names or name.endswith('营业收入') or name in ('主营收入','主营业务收入'):continue
        value=float(match[2].replace(',',''))
        supplementary.append(name+match[2]+'亿元，规模相当于主营收入的'+format(value/total*100,'.2f')+'%（另口径，不与主营构成相加）')
    return {'revenue_mix':{'period':period,'unit':'亿元','currency':'CNY','basis':'主营收入构成（按披露整数近似计算）',
        'classification':'收入定义','denominator':total,'denominator_label':'主营收入合计（原文定义组成项）',
        'rows':rows,'note':'；'.join(['披露金额已取整，计算比例为近似值']+reported+list(dict.fromkeys(supplementary)))}}


def business_metrics(text, period, preceding_text='', *, currency_context=None):
    result = _narrative_revenue_mix(text,period)
    unit_match = re.search(r'单位[：:]\s*(千元|万元|亿元|元)', text)
    # Do not assume the currency/unit from a page header or another table.
    local_currency=re.search(r'币种[：:]\s*([^\s]+)',text)
    confirmed=re.search(r'币种[：:]\s*人民币',text) or (not local_currency and currency_context and re.search(r'本财务报表以人民币列示',currency_context['text']))
    if not unit_match or not confirmed:return result
    unit = unit_match[1]
    profit = re.search(r'^分部(?:收益|利润)[^\n]*', text, re.M)
    if profit:
        values = _values(profit[0])
        header = re.search(r'^项目\s+([^\n]+)', text[:profit.start()], re.M)
        if header:
            columns = re.findall(r'[\w]+板块|(?:分部|板块)间抵销|合计', header[1])
            # PDF text wraps this header across three lines in standard segment notes.
            if '总部及其他' in text[:profit.start()] and '营运板块' in text[:profit.start()] and '总部及其他营运板块' not in columns:
                elimination=next((c for c in columns if c.endswith('间抵销')),None)
                if elimination:columns.insert(columns.index(elimination), '总部及其他营运板块')
            if len(columns)==len(values) and columns and columns[-1]=='合计' and _reconciles(values[:-1], values[-1]):
                tax_before = '分部税前利润' in preceding_text+text[:profit.start()]
                title = re.search(r'(20\d{2})年(半年度|度)分部信息', text[:profit.start()])
                source_period = title[1]+('-06-30' if title[2]=='半年度' else '-12-31') if title else period
                result['profit_mix'] = {
                    'period':source_period, 'unit':unit, 'currency':'CNY',
                    'basis':'分部税前利润' if tax_before else '分部收益/利润（按报告原口径）',
                    'denominator':values[-1], 'denominator_label':'分部收益合计（含亏损及抵销）',
                    'rows':[{'name':name, 'profit':v, 'share_pct':None if v is None else round(v/values[-1]*100, 4)} for name,v in zip(columns[:-1],values[:-1])],
                    'note':'业务板块不等于单一产品；破折号不推断为零，缺失项不计算占比；正盈利板块可能超过100%。'}
    for key,value in _segment_net_mix(text,period,unit).items():result.setdefault(key,value)
    operating=_operating_mix(text,period,unit)
    for key,value in operating.items():result.setdefault(key,value)
    # Only recognize explicit mainland/outside-mainland external revenue tables.
    region_start = text.find('对外交易收入')
    if region_start>=0:
        lines=text[region_start:].splitlines()
        domestic=foreign=total=None
        for line in lines:
            label=re.sub(r'\s+', '', re.split(r'\s+(?=-?\d)',line)[0])
            if label in ('中国大陆','中国大陆以内','中国大陆以外','合计'):
                nums=_values(line)
                if len(nums) not in (1,2):continue
                if label in ('中国大陆','中国大陆以内'):domestic=nums
                elif label=='中国大陆以外':foreign=nums
                elif domestic is not None and foreign is not None:total=nums;break
        if domestic and foreign and total and len(domestic)==len(foreign)==len(total) and all(_reconciles([a,b],c) for a,b,c in zip(domestic,foreign,total)):
            rows=[]
            for i,(a,b,c) in enumerate(zip(domestic,foreign,total)):
                if a is None or b is None:break
                source_period=period if i==0 else str(int(period[:4])-1)+period[4:]
                rows.append({'period':source_period, 'domestic_revenue':a,'foreign_revenue':b,'total_revenue':c,'domestic_share_pct':round(a/c*100,4),'foreign_share_pct':round(b/c*100,4)})
            if rows:
                result['region_mix']={'unit':unit,'currency':'CNY','basis':'地区对外交易收入；中国大陆以内/以外','rows':rows}
                if len(rows)==2:result['region_mix']['foreign_share_change_yoy_pp']=round(rows[0]['foreign_share_pct']-rows[1]['foreign_share_pct'],4)
    if result and not local_currency and currency_context:
        for value in result.values():value['currency_basis']=currency_context
    return result
