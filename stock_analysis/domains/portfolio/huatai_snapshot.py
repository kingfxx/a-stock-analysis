"""Validate a confirmed manual transcription against its screenshot identity."""

from decimal import Decimal
from hashlib import sha256

from stock_analysis.domains.portfolio.cmb_xls import decimal_text, identity, percent_text, quantity


HUATAI = '华泰证券'


def parse_transcription(draft, image):
    try:
        source = draft['source']
        if (draft['account'] != HUATAI or draft['currency'] != 'CNY'
                or source['type'] != 'screenshot_manual_transcription'
                or not image.startswith(b'\xff\xd8\xff') or source['sha256'] != sha256(image).hexdigest()):
            raise ValueError('华泰转录与截图来源不符')
        raw = draft['broker_summary']
        assets = decimal_text(raw['total_assets'])
        market = decimal_text(raw['securities_market_value'])
        cash = decimal_text(draft['cash']['opening_balance'])
        if (draft['cash']['opening_balance_basis'] != 'user_confirmed_assets_less_securities_value'
                or Decimal(assets) - Decimal(market) != Decimal(cash)):
            raise ValueError('华泰期初现金与已确认的差额不符')
        if not isinstance(draft['positions'], list) or len(draft['positions']) > 1000:
            raise ValueError('华泰持仓转录格式无效')
        positions = []
        codes = set()
        for number, row in enumerate(draft['positions'], 1):
            code, exchange, asset_type = identity(row['code'], 'CNY')
            if not code or exchange != row['exchange'] or asset_type not in ('stock', 'etf') or code in codes:
                raise ValueError('华泰持仓证券身份无效或重复')
            codes.add(code)
            shares = quantity(row['quantity'])
            price = decimal_text(row['displayed_price'])
            value = decimal_text(row['market_value'])
            if not shares or not price or Decimal(price) <= 0 or shares * Decimal(price) != Decimal(value):
                raise ValueError('华泰持仓市值与数量及现价不符')
            positions.append({
                'source_row': number, 'code': code, 'exchange': exchange, 'asset_type': asset_type,
                'name': str(row['name']).strip(), 'currency': 'CNY', 'quantity': shares,
                'available_quantity': quantity(row['available_quantity']),
                'price': price, 'cost_price': decimal_text(row['displayed_cost_price']),
                'cost_amount': decimal_text(row['cost_amount']) if row['cost_amount'] is not None else None,
                'market_value': value, 'holding_pnl': decimal_text(row['broker_holding_pnl']), 'exchange_rate': None,
                'holding_pnl_rate': percent_text(row.get('broker_holding_pnl_rate', '')),
                'daily_pnl': None, 'daily_pnl_rate': None,
            })
        if (sum(Decimal(row['market_value']) for row in positions) != Decimal(market)
                or sum(Decimal(row['holding_pnl']) for row in positions) != Decimal(raw['holding_pnl'])):
            raise ValueError('华泰明细市值或盈亏与截图汇总不符')
        summary = {
            'currency': 'CNY', 'broker_balance': None, 'broker_available': None,
            'broker_withdrawable': None, 'broker_market_value': market, 'broker_assets': assets,
            'broker_daily_pnl': decimal_text(raw.get('daily_pnl', '')), 'broker_daily_pnl_rate': None,
        }
        return {'kind': 'snapshot', 'summary': summary, 'positions': positions}, cash
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('华泰转录缺少必要字段') from exc
