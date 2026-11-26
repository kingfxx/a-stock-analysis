"""Read the Hong Kong panels through the same local dashboard contract."""

from stock_analysis.domains.financials.core import period_end_snapshots
from stock_analysis.domains.financials.hk_statement_service import read_reports
from stock_analysis.domains.financials.hk_trend import trend_reports
from stock_analysis.domains.financials.payloads import _financial_payload
from stock_analysis.domains.stocks.hk_dividends import read as read_dividends
from stock_analysis.domains.stocks.price_projection import financial_prices
from stock_analysis.domains.stocks.price_service import PriceService
from stock_analysis.domains.valuation.hk_valuation import read as read_valuation
from stock_analysis.domains.valuation.payloads import _p4_valuation_payload


def read_panels(db, code, *, version=None):
    with db.connection() as conn:
        instrument = conn.execute("SELECT id,name FROM instruments WHERE exchange='hk' AND code=?", (code,)).fetchone()
        disclosures = {row['period']: row['first_published_on'] for row in conn.execute(
            'SELECT period,first_published_on FROM report_disclosure_dates WHERE instrument_id=? AND is_current=1',
            (instrument['id'],))} if instrument else {}
    reports, updated_at = read_reports(db, code)
    prices = PriceService(db)
    version = (prices.current_version(code) or 0) if version is None else version
    qfq = prices.read(code, 'qfq', version=version, lease=False) if version else []
    raw = prices.read(code, 'raw', lease=False)
    source = {'code': code, 'name': instrument['name'] if instrument else None,
              'reports': trend_reports(reports, disclosures), 'updated_at': updated_at,
              'report_date_basis': 'hkex_disclosure_date_v1'}
    financial = _financial_payload(financial_prices(source, raw, qfq))
    adjusted_by_period = {row['period']: row for row in period_end_snapshots(source['reports'], qfq)}
    for rows in financial['views'].values():
        for row in rows:
            point = adjusted_by_period.get(row['period'], {})
            row['period_end_qfq_price'] = point.get('close')
            row['period_end_qfq_price_date'] = point.get('date')
    latest = {kind: {key: series[-1][key] for key in ('date', 'close')}
              for kind, series in (('raw', raw), ('qfq', qfq)) if series}
    financial.update(exchange='hk', price_currency='HKD' if raw or qfq else None,
                     hk_latest_prices=latest,
                     hk_dividends=read_dividends(db, code), needs_dividends=False,
                     announcement_sync={'has_data': bool(disclosures), 'status': '已保存披露易核验日期'})
    valuation = _p4_valuation_payload(read_valuation(db, code), raw=raw, qfq=qfq, include_dividend_yield=False)
    for views in valuation['views'].values():
        views.pop('ps', None)
    return {'price_version': version, 'financial': financial, 'valuation': valuation,
            'shareholders': {'not_applicable': True}, 'financing': {'not_applicable': True}, 'warnings': []}
