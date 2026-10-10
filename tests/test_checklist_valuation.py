import copy
import json
from uuid import uuid4
import pytest
from quarterly_dashboard.analysis_repository import AnalysisRepository
from quarterly_dashboard.analysis_snapshot import encoded
from quarterly_dashboard.checklist_snapshot import capture
from quarterly_dashboard.checklist_validation import prompt,validate_output
from quarterly_dashboard.checklist_valuation import valuation_item
from test_ai_assessment import database
from test_checklist import result


def evidence(metric,percentile,current=16.09,**extra):
    return {'id':f'valuation.{metric}.10y','metric':metric,'years':10,'observed_on':'2026-10-09',
            'value':{'current':current,'current_date':'2026-10-09','percentile':percentile,'count':121,**extra}}


def test_small_percentage_and_snapshot_dates(database):
    data=capture(database,'600900',as_of='2026-10-02')['input']
    assert all(e['value']['percentile_unit']=='%' for e in data['evidence'] if e['metric'] in ('pe','pb'))
    data['evidence']=[evidence('pe',0.8),evidence('pb',21.5,11.8)]
    data['allowed_evidence_ids']=['context.data_quality']+[e['id'] for e in data['evidence']]
    out=result(data);out['items'][15]['conclusion']='历史分位80%，处于相对高位。'
    checked=validate_output(json.dumps(out),data)['items'][15]
    assert '历史分位0.8%，处于极低位' in checked['conclusion']
    assert '历史分位21.5%，处于偏低位' in checked['conclusion']
    assert '2026-10-09' in checked['conclusion']
    assert checked['evidence_ids']==['valuation.pe.10y','valuation.pb.10y']
    assert checked['status']=='ready'


@pytest.mark.parametrize('percentile,position',[(0,'极低位'),(5,'极低位'),(20,'低位'),(21.5,'偏低位'),(40,'中位'),(60,'中位'),(79.9,'偏高位'),(80,'高位'),(95,'极高位'),(100,'极高位')])
def test_position_matches_trend_page(percentile,position):
    item=valuation_item({'evidence':[evidence('pe',percentile),evidence('pb',percentile)]})
    assert f'历史分位{percentile:g}%，处于{position}' in item['conclusion']


@pytest.mark.parametrize('value',[None,-1,101,float('nan'),True])
def test_invalid_percentile_is_not_a_position(value):
    item=valuation_item({'evidence':[evidence('pe',value)]})
    assert item['status']=='missing'
    assert '处于' not in item['conclusion']


def test_negative_pe_sparse_and_missing_pb():
    negative=valuation_item({'evidence':[evidence('pe',0,-2,unavailable_reason='最新值非正数，不能解释正值历史分位')]})
    assert '最新值非正数' in negative['conclusion'] and '极低位' not in negative['conclusion']
    sparse=valuation_item({'evidence':[evidence('pe',0.8,sparse=True)]})
    assert sparse['status']=='limited'
    assert '历史观测较稀疏' in sparse['conclusion'] and 'PB资料不足' in sparse['conclusion']


def test_history_corrected_without_writing_or_using_current_data(database):
    snapshot=capture(database,'600900',as_of='2026-10-02')
    snapshot['input']['evidence']=[evidence('pe',0.8),evidence('pb',21.5,11.8)]
    historical=result(snapshot['input'])
    historical['items'][15]['conclusion']='PE历史分位80%，相对高位。'
    original=copy.deepcopy(historical)
    repo=AnalysisRepository(database)
    run=repo.enqueue(snapshot,uuid4().hex,'test-model','test-account',prompt())
    repo.transition(run['id'],'queued','running')
    repo.transition(run['id'],'running','validating')
    repo.transition(run['id'],'validating','succeeded',result_json=encoded(historical),verdict='checklist',summary='历史摘要')
    displayed=repo.run(run['id'])
    assert '历史分位0.8%，处于极低位' in displayed['result']['items'][15]['conclusion']
    assert displayed['input']==snapshot['input']
    stored=repo.run(run['id'],internal=True)
    assert json.loads(stored['result_json'])==original
    assert json.loads(stored['input_json'])==snapshot['input']
