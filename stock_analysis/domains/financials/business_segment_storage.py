"""Publish verified segment versions while preserving their actual source."""

import hashlib

from stock_analysis.domains.stocks.sync_support import encoded


def business_content(rows):
    return sorted(({k: v for k, v in row.items() if k not in
                    ('display_rank', 'source_id', 'obtained_at')} for row in rows),
                  key=lambda row: (row['report_date'], row['dimension_type'], row['item_key']))


def publish_versions(conn, by_period, old_by_period, identity, source, definition, run, batch, fields, *, currency=None):
    from stock_analysis.collection.publication_counts import counts, observe, published
    counters = counts(sum(map(len, by_period.values())))
    for period, items in sorted(by_period.items()):
        names = {r['report_name'] for r in items}
        if len(names) != 1:
            raise ValueError('主营构成同一期报告名称冲突')
        content = {'definition': definition, 'items': business_content(items)}
        if currency:
            content['currency'] = currency
        fingerprint = hashlib.sha256(encoded(content).encode()).hexdigest()
        old = old_by_period.get(period)
        if old and old['content_hash'] == fingerprint:
            counters['unchanged'] += len(items)
            continue
        previous_items = {(r['dimension_type'],r['item_key']):r for r in old['items']} if old else {}
        item_fields = ('item_code','item_name',*fields)
        for item in items:
            previous = previous_items.get((item['dimension_type'],item['item_key']))
            observe(counters, (old['report_name'], *(previous[f] for f in item_fields)) if previous else None,
                    (items[0]['report_name'], *(item[f] for f in item_fields)))
        if old:
            conn.execute('UPDATE business_segment_versions SET is_current=0 WHERE id=?', (old['id'],))
        version = conn.execute('INSERT INTO business_segment_versions(instrument_id,source,report_date,report_name,revision,supersedes_id,is_current,content_hash,definition_version,row_count,run_id,batch_id,obtained_at,report_currency) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?)',
            (identity, source, period, items[0]['report_name'], old['revision'] + 1 if old else 1,
             old['id'] if old else None, fingerprint, definition, len(items), run, batch, max(r['obtained_at'] for r in items), currency)).lastrowid
        columns = ('dimension_type', 'item_key', 'item_code', 'item_name', *fields, 'display_rank', 'source_id')
        for item in items:
            conn.execute('INSERT INTO business_segment_items(version_id,' + ','.join(columns) + ',batch_id) VALUES (' + ','.join('?' for _ in range(len(columns) + 2)) + ')',
                         (version, *(item[c] for c in columns), batch))
    return published(counters)
