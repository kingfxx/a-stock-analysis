"""Versioned qualitative checklist; no investment verdict."""
import json
from pathlib import Path
from .analysis_validation import ReportValidationError
OUTPUT_VERSION="stock_checklist_output_v1"
ITEMS=[("company","公司全称"),("strategy","使命和愿景"),("products","核心产品与定价权"),("market","市场与海外布局"),("control","股权与实际控制人"),("cycle","所属行业与周期"),("competition","行业竞争格局"),("value_chain","产业链位置"),("bargaining","上下游议价能力"),("revenue","营业收入与现金流"),("profit","净利润与盈利质量"),("balance","资产负债情况"),("roe","ROE 与资本回报"),("dividend","分红与持续性"),("market_cap","当前市值"),("valuation","估值水平与历史曲线"),("price","K 线基本趋势"),("chips","筹码评估")]

def prompt():
    instructions=Path(__file__).with_name("prompts").joinpath("checklist_v1.txt").read_text(encoding="utf-8")
    return {"version":"stock_checklist_prompt_v1","output_version":OUTPUT_VERSION,"instructions":instructions}

def validate_output(text, data):
    if not isinstance(text,str) or len(text.encode())>65536:raise ValueError("checklist 输出过大或无效")
    result=json.loads(text)
    if not isinstance(result,dict) or set(result)!={"schema_version","code","items"} or result['schema_version']!=OUTPUT_VERSION or result['code']!=data['instrument']['code']:raise ValueError("checklist 输出协议或股票代码无效")
    items=result['items']
    if not isinstance(items,list) or len(items)!=18:raise ValueError("checklist 必须包含18项")
    allowed=set(data['allowed_evidence_ids']);seen=set()
    missing=set(data['quality'].get('missing_items',[]))
    for item in items:
        if not isinstance(item,dict) or set(item)!={'id','conclusion','evidence_ids','status'}:raise ValueError("checklist 项目结构无效")
        if item['id'] not in dict(ITEMS) or item['id'] in seen:raise ValueError("checklist 项目缺失或重复")
        seen.add(item['id'])
        if not isinstance(item['conclusion'],str) or not item['conclusion'].strip() or len(item['conclusion'])>4000:raise ValueError("checklist 结论文本无效")
        if item['status'] not in {'ready','limited','missing','not_applicable'}:raise ValueError("checklist 资料状态无效")
        ids=item['evidence_ids']
        if not isinstance(ids,list) or not ids or any(not isinstance(x,str) or x not in allowed for x in ids) or len(ids)!=len(set(ids)):
            raise ReportValidationError('items.evidence_ids','checklist 引用缺失、重复或不属于输入资料')
        if item['id'] in missing and item['status']=='ready':raise ValueError("缺失资料的项目不能标为完整")
        if item['status']=='ready' and ids==['context.data_quality']:raise ValueError("完整结论必须有实际资料依据")
    result['items']=sorted(items,key=lambda x:list(dict(ITEMS)).index(x['id']))
    # The existing run table requires summary; create it locally, not a verdict.
    result['summary']="18项定性 checklist · "+str(sum(i['status']=='ready' for i in items))+"项资料完整"
    return result
