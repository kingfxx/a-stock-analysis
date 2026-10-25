"""OCR cell consensus and the managed HTML/image publication boundary."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from stock_analysis.domains.research.sectors.shipping import shipping_ocr as ocr; from stock_analysis.domains.research.sectors.shipping import shipping_collection
from stock_analysis.domains.research.sectors.shipping.shipping_sources import MOT, monthly_points
from stock_analysis.infrastructure.storage import Database
from stock_analysis.collection.collection import archive_source

FIX=Path(__file__).parent/'fixtures/shipping'


@pytest.fixture
def mock_scans(monkeypatch):
    expected=json.loads((FIX/'2026-08-rows.json').read_text(encoding='utf8'))
    texts=[['航线','2025年','2026年至今','8月','与上月比（%）']]
    for r in expected:
        texts.append([r['route']+'航线' if r['route']!='综合' else '综合指数',
                      *(f'{r[k]:.2f}' for k in ('annual','ytd','monthly')),f"{r['change']:.1f}"])
    scan=[[[dict(text=text,score=.99)] for text in row] for row in texts]
    monkeypatch.setattr(ocr,'recognize_native',lambda *a,**k:dict(cells=texts))
    monkeypatch.setattr(ocr,'model_hashes',lambda:{'fixture.onnx':'hash'})
    scans=[deepcopy(scan) for _ in range(4)]
    call=iter(scans)
    monkeypatch.setattr(ocr,'recognize_rapid',lambda *a:next(call))
    return expected,scans


def test_consensus_preserves_all_rows_decimals_and_negative_sign(mock_scans):
    expected,scans=mock_scans
    rows,evidence=ocr.extract_month_table((FIX/'2026-08.png').read_bytes(),'2026-08')
    assert rows==expected and rows[0]['change']==-1.9
    assert len(monthly_points(rows,'2026-08','2026-09-03'))==39
    assert evidence['extractor']=='mot-local-ocr-v1'


@pytest.mark.parametrize('problem',['missing_decimal','conflict','low_confidence','wrong_header','unknown_route'])
def test_ocr_uncertainty_never_publishes(mock_scans,monkeypatch,problem):
    _,scans=mock_scans
    if problem=='missing_decimal':
        for s in scans:s[1][1][0]['text']='119760'
    elif problem=='conflict':scans[0][1][1][0]['text']='1197.61'
    elif problem=='low_confidence':
        for s in scans:s[1][1][0]['score']=.5
    elif problem=='wrong_header':
        for s in scans:s[0][3][0]['text']='9月'
    else:
        for s in scans:s[1][0][0]['text']='不确定'
        monkeypatch.setattr(ocr,'recognize_native',lambda *a,**k:dict(cells=[]))
    with pytest.raises(ValueError):ocr.extract_month_table((FIX/'2026-08.png').read_bytes(),'2026-08')


@pytest.mark.parametrize('uncertain',[False,True])
def test_managed_image_uses_actual_image_source_and_keeps_failed_evidence(tmp_path,monkeypatch,uncertain):
    db=Database(tmp_path/'ocr.sqlite3');db.initialize()
    rows=json.loads((FIX/'2026-08-rows.json').read_text(encoding='utf8'))
    def extract(raw,month):
        assert raw==(FIX/'2026-08.png').read_bytes() and month=='2026-08'
        if uncertain:raise ValueError('识别不一致，留待核验')
        return rows,dict(extractor='mot-local-ocr-v1',rows=rows,model_hashes={})
    monkeypatch.setattr(ocr,'extract_month_table',extract)
    sent=[]
    class Collector:
        def __init__(self,db,**kw):self.db=db
        def check_cancelled(self):pass
        def report(self):return dict(network_requests=len(sent))
        def get(self,batch,provider,dataset,url,**kw):
            sent.append(url)
            if url==MOT:raw='<a href="month.html">2026年8月中国出口集装箱运价指数</a>'.encode()
            elif url.endswith('month.html'):raw='<title>2026年8月中国出口集装箱运价指数</title><p>2026-09-03</p><div class="article-content"><img src="table.png"></div>'.encode()
            else:raw=(FIX/'2026-08.png').read_bytes()
            source=archive_source(self.db,batch,provider,dataset,dict(url=url),1,raw=raw)
            return type('Response',(),dict(content=raw,text=raw.decode('utf8',errors='replace')))(),source
    monkeypatch.setattr(shipping_collection,'RequestCollector',Collector)
    with db.connection(write=True) as c:ref=shipping_collection.reserve(c,'shipping_monthly',dict(mode='backfill',start='2026-08-01',end='2026-08-31'))
    result=shipping_collection.run(db,ref)
    assert result['status']==('failed' if uncertain else 'complete')
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM research_shipping_rate_points').fetchone()[0]==(0 if uncertain else 39)
        image=c.execute("SELECT * FROM collection_sources WHERE dataset='shipping.mot_image'").fetchone()
        assert image['file_path'] and image['parse_status']==('failed' if uncertain else 'complete')
        if not uncertain:
            assert c.execute('SELECT count(*) FROM research_shipping_rate_points WHERE source_id=?',(image['id'],)).fetchone()[0]==39
            assert c.execute("SELECT count(*) FROM collection_sources WHERE dataset='shipping.mot_ocr' AND parse_status='complete'").fetchone()[0]==1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_historical_analysis_without_disclosure_is_not_a_parse_failure():
    from stock_analysis.domains.research.sectors.shipping.shipping_sources import parse_throughput,NoThroughputDisclosure
    html='<title>2014年1月份中国出口集装箱运输市场分析报告</title><p>2014-02-10</p><div class="article-content">'+('本月航运市场分析，欧洲航线运价及市场需求。'*25)+'</div>'
    with pytest.raises(NoThroughputDisclosure):parse_throughput(html,'2014-01')
    # A truncated/login/wrong-month response must not become a successful empty disclosure.
    for invalid in ['<title>登录</title>2014-02-10',html.replace('2014年1月','2014年2月')]:
        with pytest.raises(ValueError) as error:parse_throughput(invalid,'2014-01')
        assert not isinstance(error.value,NoThroughputDisclosure)
