"""Read-only live comparison of advertised Baidu valuation windows."""
import json
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from experiments.source_probe.p4_incremental_probe import Probe
from quarterly_dashboard.valuation import BAIDU_URL, BAIDU_INDICATORS


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    raw = Path('data/verification/samples') / ('valuation-density-' + stamp)
    raw.mkdir(parents=True, exist_ok=False)
    probe = Probe(raw)
    report = dict(observed_at_utc=stamp, raw_directory=raw.as_posix(), series={}, errors=[])
    output = Path('docs/data-sources/valuation-density-samples.json')
    try:
        for code in ('601919', '600887', '000001'):
            for metric, indicator in BAIDU_INDICATORS.items():
                series, stats = {}, {}
                for window in ('近一年', '近三年', '近五年', '近十年', '全部'):
                    try:
                        payload, size = probe.get(f'{code}-{metric}-{window}', BAIDU_URL,
                            dict(openapi='1', dspName='iphone', tn='tangram', client='app', query=indicator,
                                 code=code, word='', resource_id='51171', market='ab', tag=indicator,
                                 chart_select=window, industry_select='', skip_industry='1', finClientType='pc'))
                        result = payload['Result'][0]['DisplayData']['resultData']['tplData']['result']
                        assert result['code'] == code and result['chartSelect'] == window
                        rows = result['chartInfo'][0]['body']
                        values = dict(rows)
                        days = sorted(values)
                        parsed = [date.fromisoformat(d) for d in days]
                        gaps = Counter((b-a).days for a, b in zip(parsed, parsed[1:]))
                        span = (parsed[-1]-parsed[0]).days + 1
                        stats[window] = dict(count=len(rows), unique_dates=len(values),
                            bounds=[days[0], days[-1]], calendar_span=span,
                            missing_calendar_dates=span-len(values), gap_days=dict(sorted(gaps.items())),
                            weekend_points=sum(d.weekday() >= 5 for d in parsed),
                            null_or_non_numeric=sum(not numeric(v) for v in values.values()),
                            response_bytes=size)
                        series[window] = values
                        print(code, metric, window, json.dumps(stats[window], ensure_ascii=False), flush=True)
                    except Exception as exc:
                        report['errors'].append(dict(code=code, metric=metric, window=window, error=str(exc)))
                comparisons = []
                for a, b in combinations(series, 2):
                    common = sorted(series[a].keys() & series[b].keys())
                    changes = [dict(day=d, a=series[a][d], b=series[b][d]) for d in common
                               if not equal(series[a][d], series[b][d])]
                    comparisons.append(dict(windows=[a,b], common_dates=len(common),
                                            differences=len(changes), examples=changes[:5]))
                report['series'][code+'-'+metric] = dict(windows=stats, comparisons=comparisons)
                report['calls'] = probe.calls
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    finally:
        probe.session.close()


def numeric(value):
    try:
        return Decimal(str(value)).is_finite()
    except Exception:
        return False


def equal(a, b):
    return Decimal(str(a)) == Decimal(str(b)) if numeric(a) and numeric(b) else a == b


if __name__ == '__main__':
    main()
