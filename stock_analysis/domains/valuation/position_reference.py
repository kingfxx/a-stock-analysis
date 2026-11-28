"""Historical valuation references from already-loaded local observations."""

from datetime import date, timedelta

from stock_analysis.domains.valuation.calculations import (
    _nonnegative, _positive, daily_dividend_yield_observations, percentile_position,
    valuation_summary, valuation_summary_observations,
)


def _covered_years(rows, metric, as_of):
    valid_number = _nonnegative if metric == 'dividend_yield' else _positive
    dates = [row['date'] for row in rows if row['date'] <= as_of and valid_number(row.get(metric)) is not None]
    end = date.fromisoformat(as_of)
    for years in (10, 5, 3):
        day = 28 if end.month == 2 and end.day == 29 else end.day
        start = end.replace(year=end.year - years, day=day)
        lower, upper = start.isoformat(), (start + timedelta(days=31)).isoformat()
        if any(lower <= observed <= upper for observed in dates):
            return years
    return None


def summarize_position_metric(rows, metric, as_of, *, observed, trading_dates=None, allow_partial_history=False):
    years = _covered_years(rows, metric, as_of)
    if years is None and allow_partial_history:
        years = 3
    if observed:
        result = valuation_summary_observations(rows, metric, years or 5, {}, as_of=as_of, trading_dates=trading_dates)
    else:
        result = valuation_summary(rows, metric, years or 5, {}, as_of=as_of)
    percentile = result['percentile'] if years and result['count'] > 1 else None
    return {
        'value': result['current'], 'date': result['current_date'], 'percentile': percentile,
        'sample_count': result['count'], 'years': years if percentile is not None else None,
        **percentile_position(percentile, metric),
    }


def summarize_position_valuation(saved, prices, dividends, as_of, *, include_dividend_yield=True):
    reference = {}
    trading_dates = [row['date'] for row in prices]
    observations = saved.get('observation_rows', [])
    yield_rows = daily_dividend_yield_observations(prices, dividends) if dividends is not None else []
    metrics = ('pe', 'pb', 'dividend_yield') if include_dividend_yield else ('pe', 'pb')
    for metric in metrics:
        observed = metric != 'dividend_yield' and bool(observations)
        rows = (observations if observed else yield_rows if metric == 'dividend_yield' and yield_rows else
                [{**row, 'date': row.get(f'{metric}_date') or row['date']} for row in saved.get('rows', [])])
        reference[metric] = summarize_position_metric(rows, metric, as_of, observed=observed, trading_dates=trading_dates)
    return reference
