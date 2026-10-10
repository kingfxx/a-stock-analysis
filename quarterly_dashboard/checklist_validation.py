"""Versioned qualitative checklist; no investment verdict."""
import json
import math
from datetime import date
from pathlib import Path
from .analysis_validation import ReportValidationError
OUTPUT_VERSION="stock_checklist_output_v1"
ITEMS=[("company","公司全称"),("strategy","使命和愿景"),("products","核心产品与定价权"),("market","市场与海外布局"),("control","股权与实际控制人"),("cycle","所属行业与周期"),("competition","行业竞争格局"),("value_chain","产业链位置"),("bargaining","上下游议价能力"),("revenue","营业收入与现金流"),("profit","净利润与盈利质量"),("balance","资产负债情况"),("roe","ROE 与资本回报"),("dividend","分红与持续性"),("market_cap","当前市值"),("valuation","估值水平与历史曲线"),("price","K 线基本趋势"),("chips","筹码评估")]

def prompt():
    instructions=Path(__file__).with_name("prompts").joinpath("checklist_v11.txt").read_text(encoding="utf-8")
    return {"version":"stock_checklist_prompt_v11","output_version":OUTPUT_VERSION,"instructions":instructions}


def _validate_tables(item, allowed, required):
    if item['id'] not in ('products','market'):
        if 'tables' in item:raise ValueError('仅产品及地区项目可以提供数字表格')
        return
    if 'tables' not in item:
        if required:raise ValueError('产品及地区项目必须提供 tables，缺少资料时使用空数组')
        return
    tables=item['tables']
    if not isinstance(tables,list) or len(tables)>12:raise ValueError('数字表格列表无效')
    number=lambda value:value is None or (isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value))
    seen=set()
    for table in tables:
        if not isinstance(table,dict) or set(table)!={'period','classification','measure','basis','unit','denominator','rows','evidence_ids'}:raise ValueError('数字表格字段无效')
        try:
            if not isinstance(table['period'],str) or len(table['period'])!=10:raise ValueError()
            date.fromisoformat(table['period'])
        except (ValueError,TypeError):raise ValueError('数字表格报告期须为有效 YYYY-MM-DD')
        for key in ('classification','basis'):
            if not isinstance(table[key],str) or not table[key].strip() or len(table[key])>300:raise ValueError('数字表格分类或口径无效')
        measures={'income'} if item['id']=='market' else {'income','gross_profit','net_profit','pre_tax_profit','profit'}
        if not isinstance(table['measure'],str) or not isinstance(table['unit'],str) or table['measure'] not in measures or table['unit'] not in {'元','千元','万元','亿元'}:raise ValueError('数字表格指标或单位无效')
        identity=(table['period'],table['classification'],table['measure'])
        if identity in seen:raise ValueError('同一期分类与指标表重复')
        seen.add(identity)
        if not number(table['denominator']):raise ValueError('数字表格分母无效')
        ids=table['evidence_ids']
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in allowed or i not in item['evidence_ids'] for i in ids) or len(set(ids))!=len(ids):raise ValueError('数字表格证据无效')
        rows=table['rows']
        if not isinstance(rows,list) or not 1<=len(rows)<=30:raise ValueError('数字表格行无效')
        names=set()
        for row in rows:
            if not isinstance(row,dict) or not {'name','amount','share_pct'}<=set(row) or set(row)-{'name','amount','share_pct','cost','gross_margin_pct'}:raise ValueError('数字表格行字段无效')
            if set(row)&{'cost','gross_margin_pct'} and (item['id']!='products' or table['measure']!='income'):raise ValueError('成本与毛利率仅适用于产品收入表')
            for field in ('cost','gross_margin_pct'):
                if field in row and not number(row[field]):raise ValueError('成本及毛利率须为数字或 null')
            if row.get('cost') is not None and row['cost']<0:raise ValueError('营业成本不能为负')
            if row.get('gross_margin_pct') is not None and row['gross_margin_pct']>100:raise ValueError('毛利率超出有效范围')
            if not isinstance(row['name'],str) or not row['name'].strip() or len(row['name'])>100 or row['name'] in names:raise ValueError('数字表格名称为空或重复')
            names.add(row['name'])
            if not number(row['amount']) or not number(row['share_pct']):raise ValueError('数字表格数值须为数字或 null')
            if item['id']=='market' and ((row['amount'] is not None and row['amount']<0) or (row['share_pct'] is not None and not 0<=row['share_pct']<=100)):raise ValueError('地区收入或占比超出有效范围')

def validate_output(text, data):
    if not isinstance(text,str) or len(text.encode())>65536:raise ValueError("checklist 输出过大或无效")
    result=json.loads(text)
    if not isinstance(result,dict) or set(result)!={"schema_version","code","items"} or result['schema_version']!=OUTPUT_VERSION or result['code']!=data['instrument']['code']:raise ValueError("checklist 输出协议或股票代码无效")
    items=result['items']
    if not isinstance(items,list) or len(items)!=18:raise ValueError("checklist 必须包含18项")
    allowed=set(data['allowed_evidence_ids']);seen=set()
    missing=set(data['quality'].get('missing_items',[]))
    for item in items:
        if not isinstance(item,dict) or not {'id','conclusion','evidence_ids','status'}<=set(item) or set(item)-{'id','conclusion','evidence_ids','status','tables'}:raise ValueError("checklist 项目结构无效")
        if item['id'] not in dict(ITEMS) or item['id'] in seen:raise ValueError("checklist 项目缺失或重复")
        seen.add(item['id'])
        if not isinstance(item['conclusion'],str) or not item['conclusion'].strip() or len(item['conclusion'])>4000:raise ValueError("checklist 结论文本无效")
        if item['status'] not in {'ready','limited','missing','not_applicable'}:raise ValueError("checklist 资料状态无效")
        ids=item['evidence_ids']
        if not isinstance(ids,list) or not ids or any(not isinstance(x,str) or x not in allowed for x in ids) or len(ids)!=len(set(ids)):
            raise ReportValidationError('items.evidence_ids','checklist 引用缺失、重复或不属于输入资料')
        if item['id'] in missing and item['status']=='ready':raise ValueError("缺失资料的项目不能标为完整")
        if item['status']=='ready' and ids==['context.data_quality']:raise ValueError("完整结论必须有实际资料依据")
        _validate_tables(item,allowed,data.get('numeric_tables_version')=='v1')
        if data.get('numeric_tables_version')=='v1' and item['id'] in ('products','market') and not item['tables']:
            keys={'revenue_mix','profit_mix'} if item['id']=='products' else {'region_mix'}
            if any(keys.intersection(page.get('business_metrics',{})) for evidence in data.get('evidence',[])
                   if evidence['id'] in ids and evidence.get('metric')=='report_excerpt' for page in evidence.get('value',[])):
                raise ValueError('已引用的数字资料必须提供表格，不能仅保留结论')
    result['items']=sorted(items,key=lambda x:list(dict(ITEMS)).index(x['id']))
    # The existing run table requires summary; create it locally, not a verdict.
    result['summary']="18项定性 checklist · "+str(sum(i['status']=='ready' for i in items))+"项资料完整"
    from .checklist_valuation import correct_valuation
    return correct_valuation(result,data)
