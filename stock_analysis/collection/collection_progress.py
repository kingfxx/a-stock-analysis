"""Read-only publication counters for the shared collection progress template."""
from __future__ import annotations

import json

# Only source/publication counts are projected; manifests and field coverage stay
# in the explicit evidence endpoint. Missing counters remain unknown, never zero.
FIELDS = ('inserted', 'new', 'revised', 'enriched', 'unchanged', 'published',
          'returned', 'rows', 'total', 'visible_points', 'price_counts',
          'reused_batch_id', 'kind', 'row_count', 'candidate_count', 'metadata_rows', 'mode', 'disclosure_counts', 'unrecorded_stock_members')
PROJECTION = 'json_object(' + ','.join(
    "'" + key + "',CASE WHEN json_type(result_json,'$." + key + "') IN ('true','false') "
    "THEN NULL ELSE json_extract(result_json,'$." + key + "') END" for key in FIELDS) + ", 'changed_type',json_type(result_json,'$.changed'))"
INDEX_DATASETS = {'index.cs.history', 'index.cs.dashboard', 'index.cs.indicator',
                  'index.sw.analysis', 'index.legu.pe', 'index.legu.pb'}
LABELS = {
    'stock.financial.lrb': '利润表', 'stock.financial.fzb': '资产负债表', 'stock.financial.llb': '现金流量表',
    'stock.financial.gjzb': '财务关键指标', 'stock.business_segments': '主营构成', 'stock.announcements': '公告元数据',
    'stock.dividends': '分红', 'stock.shareholders': '股东人数', 'stock.financing': '融资融券',
    'stock.prices_raw': '未复权价格', 'stock.prices_adjusted': '复权价格',
    'stock.valuation.pe': 'PE', 'stock.valuation.pb': 'PB', 'stock.valuation.market_cap': '市值',
    'stock.valuation.industry': '同行估值',
    'market.financial.income': '利润表', 'market.financial.balance': '资产负债表',
    'market.financial.cashflow': '现金流量表', 'market.financial.performance': '业绩指标',
    'market.trade_snapshot': '交易快照', 'bond_zh_us_rate': '国债收益率',
    'market.legu.congestion': '拥挤度 API', 'market.legu.congestion.page': '拥挤度页面',
    'shipping.ccfi': 'CCFI 周度', 'shipping.scfi': 'SCFI 周度',
    'shipping.mot_rates': '航运月度指数', 'shipping.mot_analysis': '航运吞吐量',
    'shipping.excel': '研究资料指数导入',
}
COUNTERS = ('returned', 'written', 'inserted', 'revised', 'enriched', 'unchanged')


def number(value):
    return value if type(value) is int and value >= 0 else None


def normalize(batch, values, label):
    financial = batch['dataset'].startswith('market.financial.')
    inserted = number(values.get('inserted'))
    if inserted is None:
        inserted = number(values.get('new'))
    revised = number(values.get('revised'))
    enriched = number(values.get('enriched'))
    written = number(values.get('published'))
    if inserted is not None and revised is not None and (not financial or enriched is not None):
        written = inserted + revised + (enriched or 0)
    returned = number(values.get('returned'))
    dataset = batch['dataset']
    if returned is None and (financial or dataset in {'market.trade_snapshot', 'market.legu.congestion', 'market.legu.congestion.page'}):
        returned = number(values.get('rows'))
    if returned is None and dataset.startswith('stock.'):
        returned = number(values.get('row_count'))
        if returned is None:
            returned = number(values.get('candidate_count'))
        if returned is None:
            returned = number(values.get('metadata_rows'))
        if dataset == 'stock.prices_adjusted' and values.get('mode') == 'unchanged' and values.get('returned') is None:
            returned = None  # Old row_count=0 meant no publication, not an empty response.
    if dataset == 'stock.valuation.industry' and values.get('changed_type') in ('true','false'):
        returned = 1
        if written is None:
            written = int(values['changed_type'] == 'true')
        if values.get('unchanged') is None:
            values = {**values, 'unchanged':int(values['changed_type'] == 'false')}
    if dataset == 'bond_zh_us_rate':
        returned = number(values.get('total'))
    if dataset in {'shipping.ccfi', 'shipping.scfi'}:
        returned = number(values.get('visible_points'))
    reused = number(values.get('reused_batch_id')) is not None
    reused = reused or batch['status'] in ('reused','skipped')
    if reused:
        # Reuse is not another response or another publication of old facts.
        returned, written, inserted, revised, enriched = None, 0, None, None, None
    empty = returned == 0 and batch['status'] in ('complete', 'failed') and dataset != 'stock.announcement_dates'
    return dict(provider=batch['source_system'], label=label, returned=returned,
                written=written, inserted=inserted, revised=revised, enriched=enriched,
                unchanged=number(values.get('unchanged')) if not reused else None,
                segments=1, unrecorded_segments=int(written is None or bool(number(values.get('unrecorded_stock_members')))), reused_segments=int(reused),
                empty_returns=int(empty), empty_start=batch['scope_start'] if empty else None,
                empty_end=batch['scope_end'] if empty else None)


def merge(entries):
    groups = {}
    for entry in entries:
        key = (entry['provider'], entry['label'])
        if key not in groups:
            groups[key] = dict(entry)
            continue
        group = groups[key]
        for field in (*COUNTERS, 'segments', 'unrecorded_segments', 'reused_segments', 'empty_returns'):
            values = [v for v in (group[field], entry[field]) if v is not None]
            group[field] = sum(values) if values else None
        group['empty_start'] = min(filter(None, (group['empty_start'], entry['empty_start'])), default=None)
        group['empty_end'] = max(filter(None, (group['empty_end'], entry['empty_end'])), default=None)
    return list(groups.values())


def attach_data_counts(conn, rows, providers):
    """Adapt existing batches for every registered task, without rewriting history."""
    if not rows:
        return
    refs = {row['operation_ref']: [] for row in rows}
    batches = conn.execute('SELECT operation_id,source_system,dataset,status,scope_start,scope_end,'
        + PROJECTION + ' AS counters FROM collection_batches WHERE operation_id IN ('
        + ','.join('?' for _ in refs) + ') ORDER BY id', tuple(refs))
    for record in batches:
        batch = dict(record)
        values = json.loads(batch.pop('counters'))
        dataset = batch['dataset']
        if dataset in INDEX_DATASETS:
            refs[batch['operation_id']].append(normalize(batch, values, '估值'))
            if isinstance(values.get('price_counts'), dict):
                refs[batch['operation_id']].append(normalize(batch, values['price_counts'], '行情'))
        elif dataset in LABELS or any(values.get(k) is not None for k in ('inserted', 'new', 'revised', 'published')):
            # Discovery requests are evidence, not another set of published yields.
            if values.get('kind') != 'target_discovery':
                refs[batch['operation_id']].append(normalize(batch, values, LABELS.get(dataset, dataset)))
                if dataset == 'stock.announcements' and isinstance(values.get('disclosure_counts'),dict):
                    refs[batch['operation_id']].append(normalize({**batch,'dataset':'stock.announcement_dates'},values['disclosure_counts'],'公告日期判断（派生）'))
    for row in rows:
        entries = refs[row['operation_ref']]
        has_batches = bool(entries)
        result = row['result']
        for channel in result.get('channels') or []:
            if isinstance(channel, dict) and channel.get('index') in ('CCFI', 'SCFI') and channel.get('skipped'):
                entries.append(dict(provider=providers[0], label=channel['index']+' 周度',
                    returned=None, written=0, inserted=None, revised=None, enriched=None, unchanged=None,
                    segments=0, unrecorded_segments=0, reused_segments=1,
                    empty_returns=0, empty_start=None, empty_end=None))
        if not entries and isinstance(result.get('snapshot'), dict) and result['snapshot'].get('skipped'):
            entries.append(dict(provider=providers[0], label='交易快照', returned=None,
                written=0, inserted=None, revised=None, enriched=None, unchanged=None,
                segments=1, unrecorded_segments=0, reused_segments=1,
                empty_returns=0, empty_start=None, empty_end=None))
        # Image review deliberately publishes under the original image batch;
        # its own operation records this invocation's count in channels instead.
        if not has_batches:
            for channel in result.get('channels') or []:
                if isinstance(channel, dict) and number(channel.get('published')) is not None:
                    batch = dict(dataset='shipping.image_review', source_system=providers[0],
                                 status=row['status'], scope_start=None, scope_end=None)
                    label = channel['index']+' 周度' if channel.get('index') in ('CCFI', 'SCFI') else '资料核验导入'
                    entries.append(normalize(batch, channel, label))
        if not entries:
            for provider in row.get('scope', {}).get('providers') or providers:
                entries.append(dict(provider=provider, label='数据', **dict.fromkeys(COUNTERS),
                    segments=0, unrecorded_segments=0, reused_segments=0,
                    empty_returns=0, empty_start=None, empty_end=None))
        result['data_counts'] = merge(entries)
