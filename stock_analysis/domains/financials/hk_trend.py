"""Project Hong Kong statement facts into the existing financial trend contract."""

from stock_analysis.domains.stocks.source_parsers import _number

FIELDS = {
    'revenue_ytd': ('lrb', 'BIZINCO'), 'profit_ytd': ('lrb', 'PARENETP'),
    'net_profit_ytd': ('lrb', 'NETPROFIT'), 'total_equity': ('fzb', 'RIGHAGGR'),
    'monetary_funds': ('fzb', 'CURFDS'), 'operating_cash_flow_ytd': ('llb', 'MANANETR'),
    'capex_ytd': ('llb', 'ACQUASSETCASH'),
}


def trend_reports(statements, disclosures):
    rows = []
    for period, reports in sorted(statements.items()):
        # The shared trend calendar currently derives quarters from calendar-year YTD.
        if period[5:] not in ('03-31', '06-30', '09-30', '12-31'):
            continue
        eligible = {kind: raw for kind, raw in reports.items() if raw.get('rCurrency')
                    and raw.get('fiscal_year_end') == '12-31'
                    and (kind == 'fzb' or raw.get('period_start') == period[:4] + '-01-01')}
        currencies = {x['rCurrency'] for x in eligible.values()}
        currency = next(iter(currencies)) if len(currencies) == 1 else None
        result = {'period': period, 'report_currency': currency, 'exchange': 'hk',
                  'publish_date': disclosures.get(period)}
        if currency:
            items = {kind: {item['item_field']: _number(item['item_value']) for item in report['data']}
                     for kind, report in eligible.items()}
            result.update({key: items.get(kind, {}).get(field) for key, (kind, field) in FIELDS.items()})
        rows.append(result)
    return rows
