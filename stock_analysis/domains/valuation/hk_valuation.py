"""Persist Baidu's observed Hong Kong valuation points without inferring daily coverage."""

from stock_analysis.domains.stocks.collection_plan import selected
from stock_analysis.domains.stocks.fact_revisions import retain_previous
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch, routed_stock_requests
from stock_analysis.domains.stocks.sync_support import data_lock, encoded, instrument_id, mark_failed, sync_state
from stock_analysis.domains.valuation.valuation import BAIDU_INDICATORS, fetch_valuation_indicator
from stock_analysis.infrastructure.network import create_data_session
from stock_analysis.infrastructure.storage import SyncKey, SyncResult

SOURCE = 'baidu:hk-opendata'
VERSION = 'baidu_hk_observations_v1'


def collect(db, collector, code, full):
    identity = db.ensure_instrument(code)
    for metric in BAIDU_INDICATORS:
        if selected('valuation.' + metric, 'opendata'):
            with data_lock(db, 'hk:valuation:' + metric, code):
                _collect_metric(db, collector, code, identity, metric, full)


def _collect_metric(db, collector, code, identity, metric, full):
    dataset = 'valuation.' + metric
    key = SyncKey(identity, 'valuation:' + metric, SOURCE)
    state = sync_state(db, key)
    saved = {row['observed_on']: row for row in db.valuation_observations(identity, SOURCE, metric)}
    window = '全部' if full or not saved else '近一年'
    run = db.start_sync(key, parser_version=VERSION, methodology_version=VERSION,
                        trigger_reason='full' if window == '全部' else 'incremental')
    batch = None
    try:
        batch = begin_stock_batch(db, run, 'baidu', dataset, code,
            scope={'exchange': 'hk', 'source_interface': 'opendata', 'window': window},
            parser_version=VERSION, definition_version=VERSION)
        with routed_stock_requests(db, batch, 'baidu', dataset, collector=collector):
            with create_data_session() as session:
                points = fetch_valuation_indicator(code, session, metric, window, market='hk')
                if saved and window != '全部' and not (saved.keys() & {p['date'] for p in points}):
                    saved_end = max(saved)
                    if min(p['date'] for p in points) > saved_end:
                        window = '全部'
                        points = fetch_valuation_indicator(code, session, metric, window, market='hk')
        rows = [{'observed_on': p['date'], 'value': p['value'], 'sampling_version': VERSION,
                 'source_windows': [p.get('source_window', window)], 'raw_json': encoded(p)}
                for p in points if p['value'] is not None]
        if not rows:
            raise ValueError('百度港股估值没有有效观测')
        start = min(state.get('coverage_start') or rows[0]['observed_on'], *(r['observed_on'] for r in rows))
        end = max(saved.keys() | {r['observed_on'] for r in rows})

        def write(conn):
            for row in rows:
                previous = saved.get(row['observed_on'])
                if previous and previous['value'] != row['value']:
                    retain_previous(conn, row['observed_on'], previous)
            return db.upsert_valuation_observations(conn, key, run, rows)

        db.complete_sync(run, SyncResult(len(rows), start, end, end), write,
                         collection_batch_id=batch, batch_result={'source_window': window})
    except Exception as exc:
        mark_failed(db, run, exc, collection_batch_id=batch)
        raise


def read(db, code):
    identity = instrument_id(db, code)
    if identity is None:
        return {}
    points = {}
    stamps = []
    for metric in ('pe', 'pb'):
        for row in db.valuation_observations(identity, SOURCE, metric):
            day = row['observed_on']
            points.setdefault(day, {'date': day})[metric] = row['value']
            stamps.append(row['obtained_at'][:10])
    return {'observation_rows': [points[day] for day in sorted(points)], 'basis': VERSION,
            'updated_on': max(stamps, default=None), 'rows': [],
            'warnings': ['港股估值按来源实际观测日展示；总市值口径待核实。']} if points else {}
