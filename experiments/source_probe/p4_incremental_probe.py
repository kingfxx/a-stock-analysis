"""Read-only comparison of P4 full and bounded public-source responses.

Run: python -m experiments.source_probe.p4_incremental_probe
Writes evidence only; never opens the application database or business caches.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quarterly_dashboard.network import create_data_session
from quarterly_dashboard.sources import SINA_URL, symbol_for
from quarterly_dashboard.valuation import BAIDU_URL, BAIDU_INDICATORS, DIVIDEND_URL


class Probe:
    def __init__(self, output):
        self.output = output
        self.session = create_data_session()
        self.calls = []

    def get(self, name, url, params):
        started = time.monotonic()
        response = self.session.get(url, params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()
        self.calls.append(dict(name=name, url=url, params=params,
                               response_bytes=len(response.content),
                               seconds=round(time.monotonic() - started, 3)))
        (self.output / (name + '.json')).write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        time.sleep(1)
        return payload, len(response.content)

    def financial(self, code, kind):
        results = []
        for size, page in ((200, 1), (8, 1), (8, 2)):
            payload, size_bytes = self.get(f'{code}-{kind}-{size}-{page}', SINA_URL,
                dict(paperCode=symbol_for(code), source=kind, type='0', page=page, num=size))
            data = payload['result']['data']
            results.append((data, size_bytes))
        full, first, second = [item[0]['report_list'] for item in results]
        expected = sorted(full, reverse=True)
        return dict(full_count=len(full), reported_count=results[0][0]['report_count'],
            full_complete=len(full) == int(results[0][0]['report_count']),
            page1_periods=list(first), page2_periods=list(second),
            page1_matches_full=all(k in full and v == full[k] for k, v in first.items()),
            page2_matches_full=all(k in full and v == full[k] for k, v in second.items()),
            page_order_matches=list(first) == expected[:8] and list(second) == expected[8:16],
            all_have_update_time=all('update_time' in v for v in first.values()),
            response_bytes=[item[1] for item in results])

    def dividends(self, code):
        results = []
        start = '2025-01-01'
        for column in (None, 'EX_DIVIDEND_DATE', 'NOTICE_DATE', 'PLAN_NOTICE_DATE'):
            filter_ = f'(SECURITY_CODE="{code}")'
            if column:
                filter_ += f"({column}>='{start}')"
            payload, size_bytes = self.get(f'{code}-dividend-{column or "full"}', DIVIDEND_URL,
                dict(reportName='RPT_SHAREBONUS_DET', columns='ALL', filter=filter_,
                     pageNumber=1, pageSize=200, sortTypes='-1',
                     sortColumns='EX_DIVIDEND_DATE', source='WEB', client='WEB'))
            if payload.get('success') is not True:
                raise ValueError(str(payload.get('message')))
            result = payload['result']
            rows = result.get('data') or []
            if len(rows) != int(result['count']):
                raise ValueError('Probe needs more dividend pages')
            results.append((column, rows, size_bytes))
        full = results[0][1]
        encode = lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False)
        keys = [(r.get('REPORT_DATE'), r.get('PLAN_NOTICE_DATE')) for r in full]
        return dict(full_count=len(full), full_bytes=results[0][2],
            source_fields=sorted(full[0]) if full else [],
            progress_values=sorted({str(r.get('ASSIGN_PROGRESS')) for r in full}),
            proposed_identity_unique=len(keys) == len(set(keys)),
            proposed_identity_has_null=any(None in k for k in keys),
            windows=[dict(column=col, start=start, count=len(rows), response_bytes=size,
                matches_full=sorted(map(encode, rows)) == sorted(encode(r) for r in full
                    if str(r.get(col) or '')[:10] >= start)) for col, rows, size in results[1:]])

    def valuations(self, code, metric, indicator):
        results = []
        for window in ('近十年', '近一年'):
            payload, size_bytes = self.get(f'{code}-{metric}-{window}', BAIDU_URL,
                dict(openapi='1', dspName='iphone', tn='tangram', client='app', query=indicator,
                     code=code, word='', resource_id='51171', market='ab', tag=indicator,
                     chart_select=window, industry_select='', skip_industry='1', finClientType='pc'))
            result = payload['Result'][0]['DisplayData']['resultData']['tplData']['result']
            if result['code'] != code or result['chartSelect'] != window:
                raise ValueError('Unexpected stock/window')
            results.append((result['chartInfo'][0]['body'], size_bytes, result['times']))
        full, recent = [dict(item[0]) for item in results]
        common = full.keys() & recent.keys()
        same_value = lambda a, b: Decimal(str(a)) == Decimal(str(b))
        def monthly(values):
            rows = {}
            for day in sorted(values):
                rows[day[:7]] = (day, values[day])
            return rows
        fm, rm = monthly(full), monthly(recent)
        changed = [dict(month=m, full=fm[m], recent=rm[m]) for m in sorted(fm.keys() & rm.keys())
                   if fm[m][0] != rm[m][0] or not same_value(fm[m][1], rm[m][1])]
        value_changes = [m for m in fm.keys() & rm.keys() if not same_value(fm[m][1], rm[m][1])]
        return dict(full_count=len(full), recent_count=len(recent),
            full_bounds=[min(full), max(full)], recent_bounds=[min(recent), max(recent)],
            common_dates=len(common), same_date_value_changes=sum(not same_value(full[d], recent[d]) for d in common),
            monthly_date_or_value_changes=len(changed), monthly_value_changes=len(value_changes),
            monthly_change_examples=changed[:3], response_bytes=[r[1] for r in results],
            advertised_windows=results[1][2])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codes', nargs='+', default=['601919', '600887', '000001'])
    parser.add_argument('--output', type=Path, default=Path('docs/data-sources/p4-incremental-samples.json'))
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    raw_dir = Path('data/verification/samples') / ('p4-incremental-' + stamp)
    raw_dir.mkdir(parents=True, exist_ok=False)
    probe = Probe(raw_dir)
    report = dict(observed_at_utc=stamp, raw_directory=raw_dir.as_posix(), financial={}, dividends={}, valuations={}, errors=[])
    jobs = []
    for code in args.codes:
        jobs.extend(('financial', f'{code}-{kind}', lambda c=code, k=kind: probe.financial(c, k)) for kind in ('lrb', 'fzb', 'llb'))
        jobs.append(('dividends', code, lambda c=code: probe.dividends(c)))
        jobs.extend(('valuations', f'{code}-{metric}', lambda c=code, m=metric, i=indicator: probe.valuations(c, m, i)) for metric, indicator in BAIDU_INDICATORS.items())
    try:
        for section, key, run in jobs:
            try:
                report[section][key] = run()
                print(section, key, json.dumps(report[section][key], ensure_ascii=False), flush=True)
            except Exception as exc:
                report['errors'].append(dict(section=section, key=key, error=str(exc)))
                print('ERROR', section, key, str(exc), flush=True)
            report['calls'] = probe.calls
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        probe.session.close()


if __name__ == '__main__':
    main()
