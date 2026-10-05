"""Deduplicate chart points on the wire without changing valuation calculations."""
import json


def compact_valuation(payload):
    pool, indexes, views = [], {}, {}

    def references(rows):
        result = []
        for row in rows:
            key = json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if key not in indexes:
                indexes[key] = len(pool)
                pool.append(row)
            result.append(indexes[key])
        return result

    for years, metrics in payload.get('views', {}).items():
        views[years] = {}
        for metric, summary in metrics.items():
            compact = {key: value for key, value in summary.items() if key not in {'rows', 'rows_by_frequency'}}
            if 'rows' in summary:
                compact['row_indices'] = references(summary['rows'])
            if 'rows_by_frequency' in summary:
                compact['row_indices_by_frequency'] = {frequency: references(rows)
                    for frequency, rows in summary['rows_by_frequency'].items()}
            views[years][metric] = compact
    return {**payload, 'views': views, 'row_pool': pool, 'transport_version': 1}
