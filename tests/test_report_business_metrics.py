import json
import pytest
from quarterly_dashboard.report_business_metrics import business_metrics
from quarterly_dashboard.checklist_validation import prompt
from test_ai_assessment import database

PROFIT = """2026年半年度分部信息
单位：千元 币种：人民币
总部及其他
项目 氧化铝板块 原铝板块 营销板块 能源板块 板块间抵销 合计
营运板块
分部收益（亏损以“-”号表示） -817,658 26,615,434 1,056,607 91,864 -716,134 - 26,230,113
所得税费用 -4,733,925
净利润 21,496,188
"""
REGION = """单位：千元 币种：人民币
对外交易收入 本期发生额 上期发生额（经重述）
中国大陆 123,402,880 115,157,887
中国大陆以外 2,010,306 1,250,765
合计 125,413,186 116,408,652
非流动资产
中国大陆 999 888
中国大陆以外 333 222
合计 1332 1110
"""


def test_segment_profit_negative_contributions_and_wrapped_headers():
    p=business_metrics(PROFIT,'2026-06-30','本集团按分部税前利润评价经营业绩')['profit_mix']
    rows={r['name']:r for r in p['rows']}
    assert p['basis']=='分部税前利润' and p['unit']=='千元'
    assert p['denominator']==26230113
    assert rows['原铝板块']['share_pct']==pytest.approx(101.469)
    assert rows['氧化铝板块']['share_pct']<0
    assert rows['总部及其他营运板块']['profit']==-716134
    assert rows['板块间抵销']['profit'] is None
    assert rows['板块间抵销']['share_pct'] is None


def test_comparative_segment_period_and_no_assumed_tax_basis():
    p=business_metrics(PROFIT.replace('2026年半年度','2025年半年度'),'2026-06-30')['profit_mix']
    assert p['period']=='2025-06-30'
    assert '税前' not in p['basis']


def test_region_same_period_yoy_denominator_and_stop_before_assets():
    p=business_metrics(REGION,'2026-06-30')['region_mix']
    assert p['rows'][1]['period']=='2025-06-30'
    assert p['rows'][0]['foreign_share_pct']==pytest.approx(1.6029)
    assert p['rows'][0]['domestic_share_pct']==pytest.approx(98.3971)
    assert p['foreign_share_change_yoy_pp']==pytest.approx(.5284)
    assert p['rows'][0]['foreign_revenue']==2010306


@pytest.mark.parametrize('text',[
    PROFIT.replace('26,230,113','0'),
    PROFIT.replace('26,230,113','88,000,000'),
    PROFIT.replace('单位：千元','单位：'),
    PROFIT.replace('币种：人民币','币种：美元'),
    PROFIT.replace('原铝板块','原铝'),
])
def test_unsupported_or_unreconciled_segment_tables_do_not_invent_ratios(text):
    assert 'profit_mix' not in business_metrics(text,'2026-06-30')


@pytest.mark.parametrize('text',[
    REGION.replace('125,413,186','125,000,000'),
    REGION.replace('中国大陆以外','其他地区'),
    REGION.replace('对外交易收入','分部间交易收入'),
])
def test_region_scope_and_reconciliation_are_required(text):
    assert 'region_mix' not in business_metrics(text,'2026-06-30')


def test_new_prompt_retains_simple_schema_and_distinguishes_numeric_scope():
    p=prompt()
    assert p['version']=='stock_checklist_prompt_v7'
    assert p['output_version']=='stock_checklist_output_v1'
    for phrase in ['分部税前利润','百分点','非同比','生命周期','产能排名','business_metrics']:
        assert phrase in p['instructions']


def test_cached_excerpts_keep_both_reports_and_wrapped_ranking(database,tmp_path,monkeypatch):
    import pdfplumber
    from quarterly_dashboard.company_report_service import CompanyReportService
    from quarterly_dashboard.checklist_snapshot import capture
    text=''
    class Page:
        def __init__(self,value):self.value=value
        def extract_text(self):return self.value
    class PDF:
        def __init__(self):self.pages=[Page(text),Page('界第一。公司披露的产能排名。'+('经营模式 产品定价 '*80))]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    calls=[]
    monkeypatch.setattr(pdfplumber,'open',lambda path:(calls.append(path) or PDF()))
    svc=CompanyReportService(database)
    for period,kind in [('2025-12-31','年度报告'),('2026-06-30','半年度报告')]:
        text='600900 '+period[:4]+kind+'\n公司主要业务。产能均位居世\n'+REGION+(' 公司简介 供应链 核心竞争力 '*80)
        file=tmp_path/(period+'.pdf');file.write_bytes(b'%PDF-'+period.encode())
        doc=svc.import_report('600900',period,file,published_on='2026-09-01')
        svc.parse(doc)
        assert svc.parse(doc)['cache_hit']
    data=capture(database,'600900',as_of='2026-10-02')['input']
    pages=[e for e in data['evidence'] if e['metric']=='report_excerpt']
    regional=[e for e in pages if any(p.get('business_metrics',{}).get('region_mix') for p in e['value'])]
    assert {e['observed_on'] for e in regional}=={'2025-12-31','2026-06-30'}
    assert any('competition' in e['topics'] and '界第一' in e['value'][0]['text'] for e in pages)
    assert len(calls)==2  # Cache reuse does not parse PDFs again.


OPERATING = """主营业务分行业情况
单位：元 币种：人民币
分行业 营业收入 营业成本 毛利率（%）
集装箱航运业务 210,731,494,147.06 169,767,735,549.79 19.44 -6.74 6.16
码头业务 12,041,307,540.80 8,921,117,703.55 25.91 11.39 15.72
小计 222,772,801,687.86 178,688,853,253.34 19.79 -5.92 6.60
公司内各业务部
-3,268,996,465.16 -3,194,279,840.46
间相互抵销
合计 219,503,805,222.70 175,494,573,412.88 20.05 -6.14 6.51
主营业务分地区情况
中国大陆 999 888
合计 999 888
"""


def test_main_business_revenue_and_gross_profit_with_wrapped_elimination():
    m=business_metrics(OPERATING,'2025-12-31')
    rev=m['revenue_mix'];profit=m['profit_mix']
    assert rev['denominator']==219503805222.70
    assert rev['rows'][0]['share_pct']==pytest.approx(96.0036)
    assert rev['rows'][1]['share_pct']==pytest.approx(5.4857)
    assert rev['rows'][2]['name']=='公司内各业务部间相互抵销'
    assert rev['rows'][2]['share_pct']<0
    assert sum(r['share_pct'] for r in rev['rows'])==pytest.approx(100)
    assert profit['rows'][0]['profit']==pytest.approx(40963758597.27)
    assert profit['rows'][0]['share_pct']==pytest.approx(93.0799)
    assert '毛利' in profit['basis'] and '非净利润' in profit['basis']


def test_wrapped_business_name_and_product_table_supported():
    t=OPERATING.replace('集装箱航运业务 210','集装箱航运\n210').replace('19.44 -6.74 6.16','19.44 -6.74 6.16\n业务').replace('分行业','分产品')
    m=business_metrics(t,'2025-12-31')
    assert m['revenue_mix']['rows'][0]['name']=='集装箱航运业务'


@pytest.mark.parametrize('text',[
    OPERATING.replace('219,503,805,222.70','220,503,805,222.70'),
    OPERATING.replace('178,688,853,253.34','177,688,853,253.34'),
    OPERATING.replace('12,041,307,540.80 8,921,117,703.55','12,041,307,540.80'),
    OPERATING.replace('营业收入 营业成本','营业成本 营业收入'),
])
def test_main_business_bad_totals_missing_cost_or_column_order_rejected(text):
    assert 'revenue_mix' not in business_metrics(text,'2025-12-31')


def test_business_revenue_evidence_reaches_both_report_snapshots(database,tmp_path,monkeypatch):
    import pdfplumber
    from quarterly_dashboard.company_report_service import CompanyReportService
    from quarterly_dashboard.checklist_snapshot import capture
    text=''
    class Page:
        def extract_text(self):return text
    class PDF:
        pages=[Page()]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(pdfplumber,'open',lambda path:PDF())
    svc=CompanyReportService(database)
    for period,kind in [('2025-12-31','年度报告'),('2026-06-30','半年度报告')]:
        text='600900 '+period[:4]+kind+'\n'+OPERATING+(' 公司简介 '*80)
        file=tmp_path/(period+'.pdf');file.write_bytes(b'%PDF-'+period.encode())
        svc.parse(svc.import_report('600900',period,file,published_on='2026-09-01'))
    data=capture(database,'600900',as_of='2026-10-02')['input']
    pages=[e for e in data['evidence'] if e['metric']=='report_excerpt']
    revenue=[e for e in pages if 'products' in e['topics'] and any(p.get('business_metrics',{}).get('revenue_mix') for p in e['value'])]
    assert {e['observed_on'] for e in revenue}=={'2025-12-31','2026-06-30'}
    assert '子公司归母净利润' in prompt()['instructions']


DAIRY = """单位：元 币种：人民币
主营业务分行业情况
分行业 营业收入 营业成本 毛利率（%）
液体乳及乳制
113,013,613,394.45 73,533,117,230.06 34.93 -0.34 -1.60 增加0.83个百分点
品制造业
其他 1,531,679,442.94 1,504,342,743.27 1.78 112.26 172.70 减少21.77个百分点
主营业务分产品情况
分产品 营业收入 营业成本 毛利率（%）
液体乳 70,422,480,013.97 48,288,027,644.55 31.43 -6.11 -6.73 增加0.45个百分点
奶粉及奶制品 32,768,634,710.63 19,144,578,085.02 41.58 10.42 9.38 增加0.56个百分点
冷饮产品 9,822,498,669.85 6,100,511,500.49 37.89 12.63 11.78 增加0.47个百分点
其他 1,531,679,442.94 1,504,342,743.27 1.78 112.26 172.70 减少21.77个百分点
主营业务分地区情况
分地区 营业收入 营业成本
其他 999 888
"""


def test_product_view_without_total_cross_checks_industry_without_double_counting():
    m=business_metrics(DAIRY,'2025-12-31')
    r=m['revenue_mix'];g=m['profit_mix']
    assert r['classification']=='产品'
    assert r['denominator']==114545292837.39
    assert len(r['rows'])==4
    assert r['rows'][0]['name']=='液体乳'
    assert r['rows'][0]['share_pct']==61.48
    assert r['rows'][1]['share_pct']==pytest.approx(28.6076)
    assert g['denominator']==39507832864.06
    assert g['rows'][0]['share_pct']==pytest.approx(56.0255)
    assert g['rows'][1]['share_pct']==pytest.approx(34.4844)
    assert '分行业' in r['reconciliation']
    assert '不相加' in r['reconciliation']


@pytest.mark.parametrize('text',[
    DAIRY.replace('113,013,613,394.45','113,113,613,394.45'),
    DAIRY.replace('73,533,117,230.06','73,433,117,230.06'),
    DAIRY.replace('冷饮产品 9,822,498,669.85 6,100,511,500.49 37.89 12.63 11.78 增加0.47个百分点',''),
])
def test_no_total_inconsistent_or_partial_product_view_is_rejected(text):
    assert 'revenue_mix' not in business_metrics(text,'2025-12-31')


def test_single_view_without_total_does_not_claim_company_total():
    text='单位：元 币种：人民币\n主营业务分产品情况'+DAIRY.split('主营业务分产品情况')[1]
    assert 'revenue_mix' not in business_metrics(text,'2025-12-31')


NET_SEGMENT = """报告分部的财务信息
单位：元
光通信收发模块及器
项目 其他 分部间抵销 合计
件
主营业务收入 41,331,047,920.54 579,611,700.79 -132,797,826.30 41,777,861,795.03
主营业务成本 -22,077,773,817.91 -509,952,061.27 133,037,738.74 -22,454,688,140.44
利润总额 17,328,203,808.54 -97,601,930.83 668.13 17,230,602,545.84
净利润/(损失) 14,805,981,317.21 -72,555,222.21 668.13 14,733,426,763.13
"""
CURRENCY={'pdf_page':73,'text':'本财务报表以人民币列示'}

TELECOM_NARRATIVE = '''公司营业收入为人民币2,590亿元，主营收入为人民币2,441亿元。其中通信收入为人民币2,130亿元，智能收入为人民币311亿元，同比增长7.1%，占收比达到12.8%。天翼云收入达到人民币618亿元。
1 主营收入：包括通信收入（含移动、固网等）和智能收入（含算力、平台、数据、模型及智能应用等）'''


def test_defined_revenue_components_preserve_scope_and_reported_rounding():
    m=business_metrics(TELECOM_NARRATIVE,'2026-06-30')['revenue_mix']
    assert m['denominator']==2441
    assert [r['name'] for r in m['rows']]==['通信收入','智能收入']
    assert m['rows'][0]['share_pct']==pytest.approx(87.2593)
    assert m['rows'][1]['share_pct']==pytest.approx(12.7407)
    assert '12.8%' in m['note'] and '25.32%' in m['note']
    assert '另口径' in m['note'] and '公司营业收入' not in m['note']
    assert '其中通信收入' not in m['note']


@pytest.mark.parametrize('text',[
    TELECOM_NARRATIVE.split('1 主营收入')[0],
    TELECOM_NARRATIVE.replace('2,441','2,590'),
    TELECOM_NARRATIVE.replace('智能收入为人民币311亿元','智能收入增长7.1%'),
])
def test_revenue_components_require_definition_amounts_and_reconciled_total(text):
    assert 'revenue_mix' not in business_metrics(text,'2026-06-30')


def test_wrapped_single_product_telecom_table_income_and_gross_profit():
    text='''单位：元 币种：人民币
主营业务分行业情况
分行 营业收入 营业成本 毛利率
业
电信 增加0.42
523,924,731,368.75 371,561,570,703.75 29.1 0.1 (0.5)
业 个百分点
主营业务分产品情况
分产 营业收入 营业成本 毛利率
品
电信 增加0.42
523,924,731,368.75 371,561,570,703.75 29.1 0.1 (0.5)
服务 个百分点'''
    m=business_metrics(text,'2025-12-31')
    assert m['revenue_mix']['rows'][0]['name']=='电信服务'
    assert m['revenue_mix']['rows'][0]['revenue']==523924731368.75
    assert m['revenue_mix']['rows'][0]['share_pct']==100
    assert m['profit_mix']['rows'][0]['profit']==152363160665
    assert '毛利' in m['profit_mix']['basis']


def test_segment_net_profit_wrapped_header_and_explicit_reporting_currency():
    m=business_metrics(NET_SEGMENT,'2026-06-30',currency_context=CURRENCY)
    p=m['profit_mix']
    assert p['denominator']==14733426763.13
    assert p['rows'][0]['name']=='光通信收发模块及器件'
    assert p['rows'][0]['profit']==14805981317.21
    assert p['rows'][0]['share_pct']==pytest.approx(100.4924)
    assert p['rows'][1]['share_pct']<0
    assert p['rows'][2]['profit']==668.13
    assert p['currency_basis']==CURRENCY
    assert '净利润' in p['basis'] and '毛利' not in p['basis']
    assert m['revenue_mix']['denominator']==41777861795.03


@pytest.mark.parametrize('text,context',[
    (NET_SEGMENT,None),
    (NET_SEGMENT.replace('单位：元','单位：元 币种：美元'),CURRENCY),
    (NET_SEGMENT.replace('14,733,426,763.13','14,000,000,000.00'),CURRENCY),
    (NET_SEGMENT.replace('14,805,981,317.21','-'),CURRENCY),
])
def test_net_profit_unknown_currency_mismatch_or_missing_column_is_rejected(text,context):
    assert 'profit_mix' not in business_metrics(text,'2026-06-30',currency_context=context)


JOINED_AMOUNTS='''单位：万元 币种：人民币
主营业务分行业情况
分行业 营业收入 营业成本 毛利率（%）
工业 3,622,876.432,712,654.55 25.12 16.69 13.65 增加2.00个百分点
主营业务分产品情况
分产品 营业收入 营业成本 毛利率（%）
客车产品 3,622,876.432,712,654.55 25.12 16.69 13.65 增加2.00个百分点
主营业务分地区情况'''


def test_joined_monetary_columns_do_not_shift_to_percentages():
    m=business_metrics(JOINED_AMOUNTS,'2025-12-31')
    assert m['revenue_mix']['denominator']==3622876.43
    assert m['revenue_mix']['rows'][0]['name']=='客车产品'
    assert m['profit_mix']['denominator']==910221.88
    assert m['profit_mix']['rows'][0]['share_pct']==100


def test_ambiguous_joined_columns_rejected_instead_of_skipping_amounts():
    text=JOINED_AMOUNTS.replace('3,622,876.432,712,654.55','3622876.432712654.55')
    assert not business_metrics(text,'2025-12-31')


def test_historical_display_repair_preserves_snapshot():
    import copy
    from quarterly_dashboard.report_business_metrics import product_display_corrections
    old={'revenue_mix':{'period':'2025-12-31','rows':[{'name':'客车产品3,622,876.432,712,654.55'}]}}
    snapshot={'evidence':[{'id':'report.page','metric':'report_excerpt','value':[{'text':JOINED_AMOUNTS,'business_metrics':old}]}]}
    saved=copy.deepcopy(snapshot)
    correction=product_display_corrections(snapshot)[0]
    assert correction['metrics']['revenue_mix']['denominator']==3622876.43
    assert correction['evidence_id']=='report.page'
    assert snapshot==saved
    assert not product_display_corrections({'evidence':[]})
