import pytest
from quarterly_dashboard.analysis_facts import financial_facts


def row(day,values):
    return dict(id='financial.quarter.'+day, metric='financial_period',period_type='quarter',observed_on=day,source='test',value=values)


def test_profit_sign_and_margin_comparison_have_explicit_bases():
    facts={e['id']:e for e in financial_facts([
        row('2025-06-30',{'profit':100,'gross_margin':34.2}),
        row('2026-03-31',{'profit':500,'gross_margin':38.4}),
        row('2026-06-30',{'profit':15,'gross_margin':34.1})])}
    assert facts['financial.fact.quarter.2026-03-31.profit.level']['direction']=='positive'
    yoy=facts['financial.fact.quarter.2026-06-30.profit.yoy']
    assert yoy['value']==-85 and yoy['direction']=='down'
    margin=facts['financial.fact.quarter.2026-06-30.gross_margin.qoq']
    assert margin['value']==pytest.approx(-4.3) and margin['unit']=='百分点'
    assert margin['baseline_on']=='2026-03-31'
    assert margin['baseline_value']==38.4


def test_missing_quarter_and_nonpositive_base_do_not_create_fake_growth():
    facts={e['id']:e for e in financial_facts([
        row('2025-06-30',{'profit':0}),row('2025-12-31',{'profit':-100}),row('2026-06-30',{'profit':10})])}
    assert not any(e['comparison']=='qoq' for e in facts.values())
    assert not any(e['comparison']=='yoy' for e in facts.values())
    assert facts['financial.fact.quarter.2025-12-31.profit.level']['direction']=='negative'
