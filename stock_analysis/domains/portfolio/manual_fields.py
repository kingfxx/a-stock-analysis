"""Normalize two manually entered brokerage forms."""

from datetime import date, time
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import re
from unicodedata import normalize

from stock_analysis.domains.portfolio.cmb_xls import decimal_text, identity, quantity


HISTORY_TYPES = (
    ('买卖', '买入', 'buy', 'trade', ('证券买入', '股票买入')),
    ('买卖', '卖出', 'sell', 'trade', ('证券卖出', '股票卖出')),
    ('分红与税', '股息入账', 'dividend', 'cash', ('现金分红', '股息到账')),
    ('分红与税', '股息红利税补缴', 'dividend_tax', 'cash', ('红利税补缴', '股息补税')),
    ('其他', '红股入账', 'stock_credit', 'credit', ('送股入账',)),
    ('其他', '申购配号', 'unknown', 'ipo', ()),
)
TRANSFER_TYPES = (
    ('转账方向', '转入证券', 'bank_transfer_in', 'cash', ('转入', '银行转证券')),
    ('转账方向', '转出证券', 'bank_transfer_out', 'cash', ('转出', '证券转银行')),
)
BUSINESSES = {'history': HISTORY_TYPES, 'transfers': TRANSFER_TYPES}
INPUT_FIELDS = {
    'broker', 'new_broker', 'request_id', 'confirmation', 'business', 'event_date', 'event_time',
    'code', 'name', 'price', 'quantity', 'gross_amount', 'fees', 'net_cash',
    'execution_reference', 'order_reference',
}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def clean(value):
    return normalize('NFKC', value).strip()


def require_input(body):
    if (not isinstance(body, dict) or body.keys() - INPUT_FIELDS
            or any(not isinstance(value, str) or len(value) > 120 for value in body.values())):
        raise ValueError('手工录入参数无效')
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', body.get('request_id', '')):
        raise ValueError('提交标识无效，请重新打开表单')


def _amount(value, label, *, required=False, signed=False, positive=False):
    number = decimal_text(value)
    if number is None:
        if required:
            raise ValueError(f'请填写{label}')
        return None
    amount = Decimal(number)
    if (abs(amount) >= Decimal('1e15') or amount != amount.quantize(Decimal('0.01'))
            or positive and amount <= 0 or not signed and amount < 0):
        raise ValueError(f'{label}金额无效，最多保留两位小数')
    return number


def normalize_entry(kind, body, today, security_name):
    fields = {key: clean(value) for key, value in body.items()}
    business = fields.get('business', '')
    match = next((row for row in BUSINESSES[kind] if business in (row[1], *row[4])), None)
    if match is None:
        raise ValueError('请选择支持的业务类别')
    _, label, event_type, mode, _ = match
    day = fields.get('event_date', '')
    clock = fields.get('event_time', '')
    try:
        if date.fromisoformat(day).isoformat() != day or day > today:
            raise ValueError
        if clock and not re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?', clock):
            raise ValueError
        event_time = time.fromisoformat(clock).isoformat() if clock else None
    except ValueError:
        raise ValueError('请填写有效的北京时间日期和时间') from None
    code, exchange, asset_type = '', None, 'cash'
    name = ''
    if kind == 'history':
        code, exchange, asset_type = identity(fields.get('code', ''))
        if not re.fullmatch(r'[0-9]{6}', code) or (mode != 'ipo' and exchange not in ('sh', 'sz')):
            raise ValueError('请输入有效的六位沪深证券代码')
        name = security_name or fields.get('name', '')
        if not name and mode != 'ipo':
            raise ValueError('请填写证券名称')
        if mode == 'ipo':
            asset_type = 'unknown'
    units = price = gross = fees = net = None
    if mode in ('trade', 'credit', 'ipo'):
        units = quantity(fields.get('quantity', ''))
        if units == 0 or (units is None and mode != 'ipo'):
            raise ValueError('请填写正整数数量')
        if event_type == 'sell':
            units = -units
    if mode == 'trade':
        price = decimal_text(fields.get('price', ''))
        if price is None or not 0 < Decimal(price) < Decimal('1e9') or len(price.partition('.')[2]) > 8:
            raise ValueError('请填写有效的成交均价')
        amount = (abs(units) * Decimal(price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        gross = _amount(fields.get('gross_amount', '') or str(amount), '成交金额', positive=True)
        fees = _amount(fields.get('fees', ''), '合计手续费')
        net = _amount(fields.get('net_cash', ''), '实际净收付', signed=True)
        signed = Decimal(gross) * (-1 if event_type == 'buy' else 1)
        if net is not None and (Decimal(net) > signed or
                                fees is not None and Decimal(net) != signed - Decimal(fees)):
            raise ValueError('请核对成交金额、费用与实际净收付')
    if mode == 'cash':
        gross = _amount(fields.get('gross_amount', ''), '实际金额', required=True, positive=True)
        net = '-' + gross if event_type in ('dividend_tax', 'bank_transfer_out') else gross
        if kind == 'transfers':
            label = ('转入' if event_type == 'bank_transfer_in' else '转出') + ' · 成功'
    if mode == 'credit':
        gross = net = '0'
    if mode == 'ipo' and units is None and not fields.get('execution_reference'):
        raise ValueError('请填写配号数量或编号')
    entry = dict(event_date=day, event_time=event_time, event_type=event_type, source_label=label,
                 exchange=exchange, code=code, name=name, asset_type=asset_type, price=price, quantity=units,
                 gross_amount=gross, fees=fees, net_cash=net,
                 execution_reference=fields.get('execution_reference') or None,
                 order_reference=(fields.get('order_reference') or None) if kind == 'history' else None,
                 source_row=1)
    entry['fingerprint'] = sha256(canonical_json(entry)).hexdigest()
    return entry
