"""Historical MOT vocabulary, period/scope identity and offline failure recovery."""
from pathlib import Path
import pytest
from stock_analysis.domains.research.sectors.shipping.shipping_sources import parse_throughput, NoThroughputDisclosure, MOT
from stock_analysis.domains.research.sectors.shipping import shipping_collection
from stock_analysis.collection.collection import RequestCollector, archive_source, begin_batch, finish_batch, parsed_source, reusable_source
from stock_analysis.infrastructure.storage import Database

FIX=Path(__file__).parent/'fixtures/shipping'


@pytest.mark.parametrize('month,period,basis,scope,value,yoy',[
    ('2014-08','2014-07','ytd','全国规模以上',11386.85,5.3),
    ('2016-07','2016-06','monthly','全国规模以上',1846.39,3.7),
    ('2017-07','2017-06','monthly','全国规模以上',2027.89,7.8),
    ('2022-02','2022-01','monthly','全国规模以上',2416,1.6),
    ('2023-03','2023-02','ytd','全国',4414,1.3),
    ('2024-03','2024-02','ytd','全国',4951,12.1),
    ('2024-04','2024-03','ytd','全国',7673,10.0),
    ('2025-12','2025-11','monthly','全国',3052,8.7),
    ('2026-02','2025-12','annual','全国',35447,6.8),
    ('2026-04','2026-03','monthly','全国',3087,4.7),
])
def test_real_historical_period_and_scope(month,period,basis,scope,value,yoy):
    metrics=parse_throughput((FIX/f'analysis-{month}.html').read_text(encoding='utf8'),month)
    assert (metrics[0]['period'],metrics[0]['basis'],metrics[0]['scope'],metrics[0]['value'],metrics[0]['yoy'])==(period,basis,scope,value,yoy)
    if month=='2014-08':
        assert [(r['scope'],r['basis'],r['value']) for r in metrics][1]==('全国主要','monthly',1704.64)
    if month=='2026-04':assert metrics[-1]['yoy']==.1
    if month=='2022-02':assert metrics[-1]['yoy']==-1.9
    if month=='2017-07':assert metrics[-1]['yoy']==23.2


def test_qualitative_only_and_mixed_original_scope_are_not_fabricated():
    with pytest.raises(NoThroughputDisclosure):
        parse_throughput((FIX/'analysis-2024-01.html').read_text(encoding='utf8'),'2024-01')
    metrics=parse_throughput((FIX/'analysis-2022-11.html').read_text(encoding='utf8'),'2022-11')
    assert [r['scope'] for r in metrics]==['全国规模以上','沿海','内河']
    partial=parse_throughput((FIX/'analysis-2024-04.html').read_text(encoding='utf8'),'2024-04')
    assert len(partial)==1  # No invented coastal/inland splits or monthly Q1 amounts.


@pytest.mark.parametrize('change',[
    lambda html:html.replace('2026年4月份中国','2026年5月份中国'),
    lambda html:__import__('re').sub(r'2026-05-09','unknown',html),
    lambda html:html.replace('2026年3月全国','2027年3月全国'),
    lambda html:html.replace('全国港口完成','未知范围完成'),
])
def test_invalid_metadata_or_unrecognized_values_still_fail(change):
    html=(FIX/'analysis-2026-04.html').read_text(encoding='utf8')
    with pytest.raises(ValueError) as exc:parse_throughput(change(html),'2026-04')
    assert not isinstance(exc.value,NoThroughputDisclosure)


def test_managed_failed_archive_reparsed_without_request_or_history_rewrite(tmp_path,monkeypatch):
    db=Database(tmp_path/'analysis.sqlite3');db.initialize()
    url=MOT+'month.html'
    directory=begin_batch(db,'old','shipping.mot_directory','mot')
    source=archive_source(db,directory,'mot','shipping.mot_directory',dict(url=MOT),1,
        raw='<a href="month.html">2024年4月集装箱运输市场分析</a>'.encode())
    parsed_source(db,source,fields=['links']);finish_batch(db,directory,'complete','unknown')
    old=begin_batch(db,'old','shipping.mot_analysis','mot',scope=dict(url=url))
    original=archive_source(db,old,'mot','shipping.mot_analysis',dict(url=url),1,raw=(FIX/'analysis-2024-04.html').read_bytes())
    parsed_source(db,original,error='old parser could not recognize first quarter')
    finish_batch(db,old,'failed','partial')
    with pytest.raises(ValueError):reusable_source(db,old,original)
    class Offline(RequestCollector):
        def get(self,*args,**kw):raise AssertionError('Archived source must avoid network')
    monkeypatch.setattr(shipping_collection,'RequestCollector',Offline)
    with db.connection(write=True) as conn:
        ref=shipping_collection.reserve(conn,'shipping_monthly',dict(mode='backfill',start='2024-04-01',end='2024-04-30'))
    result=shipping_collection.run(db,ref)
    assert result['status']=='complete'
    with db.connection() as conn:
        fact=conn.execute('SELECT * FROM research_shipping_industry_metrics').fetchone()
        assert (fact['basis'],fact['period'],fact['value'])==('ytd','2024-03',7673)
        new=conn.execute('SELECT * FROM collection_sources WHERE id=?',(fact['source_id'],)).fetchone()
        prior=conn.execute('SELECT * FROM collection_sources WHERE id=?',(original,)).fetchone()
        assert new['reused_source_id']==original and new['file_path']==prior['file_path']
        assert new['obtained_at']==prior['obtained_at'] and new['parse_status']=='complete'
        assert prior['parse_status']=='failed'
        assert conn.execute('SELECT status FROM collection_batches WHERE id=?',(old,)).fetchone()[0]=='failed'
        assert conn.execute('SELECT coverage_status FROM collection_batches WHERE id=?',(fact['batch_id'],)).fetchone()[0]=='partial'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
