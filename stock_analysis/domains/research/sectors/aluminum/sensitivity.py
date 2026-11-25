"""Offline estimation from explicitly verified, aligned sales and SHFE observations."""
from statistics import mean

from .model import number


MIN_TRAIN = 5
MIN_TEST = 2


def actual_price(revenue, volume):
    if revenue is None or volume is None:
        return None
    return number(revenue, '对外销售收入', positive=True) / number(volume, '对外销量', positive=True)


def _slope(rows):
    x = mean(row['market_price'] for row in rows)
    y = mean(row['actual_price'] for row in rows)
    variance = sum((r['market_price'] - x) ** 2 for r in rows)
    if not variance:
        return None
    return sum((r['market_price'] - x) * (r['actual_price'] - y) for r in rows) / variance


def validate_transmission(samples):
    """Each series must have one basis and frequency, with non-overlapping periods."""
    rows = sorted(samples, key=lambda row: row['start'])
    for index, row in enumerate(rows):
        if (row['start'] > row['end'] or row['published_on'] < row['end']
                or index and rows[index - 1]['end'] >= row['start']):
            raise ValueError('测算样本时点无效或期间重叠')
        for field in ('actual_price', 'market_price'):
            if number(row[field], field, positive=True) is None:
                raise ValueError('排除缺少同口径售价或市场均价的期间后再拟合')
    if len({(row['frequency'], row['basis']) for row in rows}) > 1:
        raise ValueError('年度和半年度、不同价格口径须分别检验')
    tests = []
    for index, target in enumerate(rows[MIN_TRAIN:], start=MIN_TRAIN):
        known = [row for row in rows[:index] if row['published_on'] < target['start']]
        if len(known) < MIN_TRAIN:
            continue
        slope = _slope(known)
        if slope is None:
            continue
        anchor = known[-1]
        predicted = anchor['actual_price'] + slope * (target['market_price'] - anchor['market_price'])
        tests.append(dict(period=target['end'], train_end=anchor['end'], train_count=len(known), slope=slope,
                          error=abs(predicted - target['actual_price']),
                          baseline_error=abs(anchor['actual_price'] - target['actual_price'])))
    final_slope = _slope(rows) if len(rows) >= MIN_TRAIN else None
    mae = mean(row['error'] for row in tests) if tests else None
    baseline = mean(row['baseline_error'] for row in tests) if tests else None
    # Positive, broadly stable slopes are required before a reference can be published.
    slopes = [row['slope'] for row in tests] + ([final_slope] if final_slope is not None else [])
    stable = bool(slopes) and min(slopes) > 0 and max(slopes) <= 2 * min(slopes)
    passed = len(tests) >= MIN_TEST and mae < baseline and stable
    return dict(passed=passed, sample_count=len(rows), slope=final_slope, tests=tests,
                mae=mae, baseline_mae=baseline, train_end=rows[-1]['end'] if rows else None)


def attributable_sensitivity(slope, volume, conversion):
    return 100 * slope * volume * conversion


def main():
    """Read a reviewed local sample file and print validation diagnostics."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description='离线核对沪铝对实际售价的历史传导')
    parser.add_argument('samples', type=Path, help='已核对的同频同口径配对样本 JSON 数组')
    args = parser.parse_args()
    samples = json.loads(args.samples.read_text(encoding='utf-8'))
    if not isinstance(samples, list):
        parser.error('样本须为 JSON 数组')
    for row in samples:
        if not row.get('financial_evidence') or not row.get('market_evidence'):
            parser.error('每期须提供财报证据和沪铝均价证据')
        if row.get('actual_price') is None:
            row['actual_price'] = actual_price(row.get('revenue'), row.get('volume'))
    result = validate_transmission(samples)
    result['samples'] = samples
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
