"""Historical valuation references from already-loaded local observations."""

from stock_analysis.domains.valuation.calculations import (
    daily_dividend_yield_observations, valuation_summary, valuation_summary_observations,
)


def summarize_position_valuation(saved, prices, dividends, as_of):
    reference = {}
    trading_dates = [row['date'] for row in prices]
    observations = saved['observation_rows']
    yield_rows = daily_dividend_yield_observations(prices, dividends) if dividends is not None else []
    for metric in ('pe', 'pb', 'dividend_yield'):
        daily = observations if metric != 'dividend_yield' else yield_rows
        if metric != 'dividend_yield' and daily:
            result = valuation_summary_observations(daily, metric, 5, {}, as_of=as_of, trading_dates=trading_dates)
        else:
            rows = daily or [{**row, 'date': row.get(f'{metric}_date') or row['date']}
                            for row in saved['rows']]
            result = valuation_summary(rows, metric, 5, {}, as_of=as_of)
        reference[metric] = {
            'value': result['current'], 'date': result['current_date'], 'percentile': result['percentile'],
            'sample_count': result['count'],
        }
    return reference
