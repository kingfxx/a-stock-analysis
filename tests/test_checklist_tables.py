import copy
import json
import pytest
from stock_analysis.domains.research.assessment.checklist_validation import validate_output, prompt
from test_checklist import result


def payload():
    data={'instrument':{'code':'601766'},'allowed_evidence_ids':['page'],'quality':{'missing_items':[]},'evidence':[], 'numeric_tables_version':'v1'}
    out=result(data)
    for item in out['items']:item['evidence_ids']=['page']
    market=next(item for item in out['items'] if item['id']=='market')
    market['tables']=[{'period':'2026-06-30','classification':'地区','measure':'income','basis':'对外收入','unit':'千元','denominator':100,
                       'rows':[{'name':'中国大陆','amount':80,'share_pct':None},{'name':'其他国家或地区','amount':20,'share_pct':None}],'evidence_ids':['page']}]
    return data,out,market['tables'][0]


def test_table_protocol_keeps_names_amounts_null_and_zero():
    data,out,table=payload()
    table['rows'][0]['amount']=0
    table['rows'][1]['amount']=None
    table['rows'][1]['share_pct']=75
    verified=validate_output(json.dumps(out),data)
    market=next(item for item in verified['items'] if item['id']=='market')
    assert market['tables'][0]['rows']==table['rows']
    assert prompt()['version']=='stock_checklist_prompt_v11'


@pytest.mark.parametrize('change',[
    lambda table:table.update(period='2026年上半年'),
    lambda table:table.update(period='2026-02-30'),
    lambda table:table.update(unit='万元人民币'),
    lambda table:table.update(measure='gross_profit'),
    lambda table:table.update(denominator=float('nan')),
    lambda table:table.update(evidence_ids=['unknown']),
    lambda table:table['rows'][0].update(amount=True),
    lambda table:table['rows'][0].update(amount='1,000'),
    lambda table:table['rows'][0].update(share_pct=209.9),
    lambda table:table['rows'][0].update(amount=-1),
    lambda table:table['rows'].append(copy.deepcopy(table['rows'][0])),
])
def test_invalid_numeric_tables_rejected_before_publication(change):
    data,out,table=payload();change(table)
    with pytest.raises(ValueError):validate_output(json.dumps(out),data)


def test_old_contract_without_tables_is_still_supported():
    data,out,_=payload();data.pop('numeric_tables_version')
    for item in out['items']:item.pop('tables',None)
    validate_output(json.dumps(out),data)
    data['numeric_tables_version']='v1'
    with pytest.raises(ValueError,match='tables'):validate_output(json.dumps(out),data)


def test_available_numeric_evidence_cannot_be_silently_dropped():
    data,out,_=payload()
    data['evidence']=[{'id':'page','metric':'report_excerpt','value':[{'business_metrics':{'revenue_mix':{'rows':[{'name':'产品','revenue':10}]}}}]}]
    with pytest.raises(ValueError,match='数字资料'):validate_output(json.dumps(out),data)


def test_duplicate_period_classification_measure_rejected():
    data,out,table=payload()
    next(item for item in out['items'] if item['id']=='market')['tables'].append(copy.deepcopy(table))
    with pytest.raises(ValueError,match='重复'):validate_output(json.dumps(out),data)


def test_valid_table_source_is_merged_into_parent_item():
    data,out,table=payload()
    data['allowed_evidence_ids'].append('table-page')
    table['evidence_ids']=['table-page']
    verified=validate_output(json.dumps(out),data)
    item=next(item for item in verified['items'] if item['id']=='market')
    assert item['evidence_ids']==['page','table-page']
    assert item['tables'][0]['evidence_ids']==['table-page']


def test_unknown_table_source_reports_item_table_and_bad_id():
    from stock_analysis.domains.research.assessment.analysis_validation import ReportValidationError
    data,out,table=payload()
    table['evidence_ids']=['nonexistent-page']
    with pytest.raises(ReportValidationError) as error:
        validate_output(json.dumps(out),data)
    assert error.value.diagnostic['field']=='items.market.tables[0].evidence_ids'
    assert error.value.diagnostic['unknown_evidence_ids']==['nonexistent-page']


@pytest.mark.parametrize('name',['内部抵消数','分部间抵销'])
def test_negative_elimination_cost_preserved(name):
    data,out,table=payload()
    table['measure']='income'
    item=next(item for item in out['items'] if item['id']=='products')
    item['tables']=[copy.deepcopy(table)]
    item['tables'][0]['rows']=[{'name':name,'amount':-100,'share_pct':None,'cost':-80}]
    verified=validate_output(json.dumps(out),data)
    assert next(i for i in verified['items'] if i['id']=='products')['tables'][0]['rows'][0]['cost']==-80


def test_negative_ordinary_product_cost_still_rejected():
    data,out,table=payload()
    item=next(item for item in out['items'] if item['id']=='products')
    item['tables']=[copy.deepcopy(table)]
    item['tables'][0]['rows']=[{'name':'普通产品','amount':100,'share_pct':None,'cost':-80}]
    with pytest.raises(ValueError,match='普通业务营业成本'):
        validate_output(json.dumps(out),data)
