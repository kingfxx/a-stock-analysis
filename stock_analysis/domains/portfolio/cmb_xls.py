"""Parse the two observed CMB Securities BIFF8 exports from bytes."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

import xlrd


SNAPSHOT_FIELDS = {'币种', '余额', '可用', '可取', '参考市值', '资产'}
POSITION_FIELDS = {'证券代码', '证券名称', '证券数量', '成本价', '价格币种', '成本金额', '最新市值'}
HISTORY_FIELDS = {'证券代码', '证券名称', '成交日期', '成交时间', '买卖标志', '成交价格', '成交数量',
                  '成交金额', '成交编号', '委托编号'}
EVENT_TYPES = {'买入': 'buy', '卖出': 'sell', '股息入账': 'dividend', '股息红利税补缴': 'dividend_tax',
               '交收资金修正': 'settlement_adjustment', '交收资金修正取消': 'settlement_cancellation'}
BIFF_SIGNATURE = bytes.fromhex('d0cf11e0a1b11ae1')
MAX_ROWS = 20000


def decimal_value(value):
    value = str(value).strip()
    if value in ('', '--', '—'):
        return None
    if len(value) > 40 or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value.replace(',', '')):
        raise ValueError('金额或价格字段格式无效')
    try:
        return Decimal(value.replace(',', ''))
    except InvalidOperation as exc:
        raise ValueError('金额或价格字段格式无效') from exc


def decimal_text(value):
    number = decimal_value(value)
    if number is None:
        return None
    if not number:
        return '0'
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def quantity(value, *, signed=False):
    number = decimal_value(value)
    if number is None:
        return None
    if number != number.to_integral_value() or abs(number) > 9_000_000_000_000 or (not signed and number < 0):
        raise ValueError('证券数量格式无效')
    return int(number)

def percent_text(value):
    return decimal_text(str(value).strip().removesuffix('%'))


def identity(value, currency=''):
    code = str(value).strip()
    if code and (not code.isascii() or not code.isdigit() or len(code) not in (5, 6)):
        return code, None, 'unknown'
    if currency in ('港币', '港元', 'HKD') or len(code) == 5:
        return code, 'hk', 'hong_kong'
    if currency not in ('', '人民币', 'CNY', 'RMB'):
        return code, None, 'unknown'
    if code.startswith(('51', '56', '58')):
        return code, 'sh', 'etf'
    if code.startswith('15'):
        return code, 'sz', 'etf'
    if code.startswith(('600', '601', '603', '605', '688', '689')):
        return code, 'sh', 'stock'
    if code.startswith(('000', '001', '002', '003', '300', '301')):
        return code, 'sz', 'stock'
    if code.startswith(('43', '83', '87', '88', '92')):
        return code, 'bj', 'stock'
    return code, None, 'unknown'


def _cells(sheet, row):
    return [str(value).strip() for value in sheet.row_values(row)]


def _header(sheet, row, required):
    fields = _cells(sheet, row)
    return fields if required.issubset(fields) else None


def _records(sheet, first, stop, columns):
    for row in range(first, stop):
        values = sheet.row_values(row)
        if any(value != '' for value in values):
            yield row + 1, dict(zip(columns, values))


def _currency(value):
    value = str(value).strip()
    return {'人民币': 'CNY', 'RMB': 'CNY', '港币': 'HKD', '港元': 'HKD'}.get(value, value)


def _snapshot(sheet, funds_row, positions_row):
    funds_cols = _header(sheet, funds_row, SNAPSHOT_FIELDS)
    pos_cols = _header(sheet, positions_row, POSITION_FIELDS)
    funds = [row for _, row in _records(sheet, funds_row + 1, positions_row, funds_cols)
             if _currency(row['币种']) == 'CNY']
    if len(funds) != 1:
        raise ValueError('需有一行人民币资金汇总')
    summary = {'currency': 'CNY', **{
        key: decimal_text(funds[0][label]) for key, label in (
            ('broker_balance', '余额'), ('broker_available', '可用'), ('broker_withdrawable', '可取'),
            ('broker_market_value', '参考市值'), ('broker_assets', '资产'))}}
    summary['broker_daily_pnl'] = decimal_text(funds[0].get('今日盈亏', ''))
    summary['broker_daily_pnl_rate'] = percent_text(funds[0].get('今日盈亏比例', ''))
    positions = []
    for number, row in _records(sheet, positions_row + 1, sheet.nrows, pos_cols):
        currency = _currency(row['价格币种'])
        code, exchange, asset_type = identity(row['证券代码'], currency)
        shares = quantity(row['证券数量'])
        if not code or not currency or shares is None:
            raise ValueError(f'第 {number} 行缺少证券代码、价格币种或证券数量')
        positions.append({
            'source_row': number, 'code': code, 'exchange': exchange, 'asset_type': asset_type,
            'name': str(row['证券名称']).strip(), 'currency': currency, 'quantity': shares,
            'available_quantity': quantity(row.get('可卖数量', '')),
            'price': decimal_text(re.sub(r'\s*(?:港元|港币|人民币|HKD|CNY|元)$', '', str(row.get('当前价', '')))),
            'cost_price': decimal_text(row['成本价']), 'cost_amount': decimal_text(row['成本金额']),
            'market_value': decimal_text(row['最新市值']), 'holding_pnl': decimal_text(row.get('持仓盈亏', '')),
            'holding_pnl_rate': percent_text(row.get('持仓盈亏比例(%)', '')),
            'daily_pnl': decimal_text(row.get('今日盈亏', '')),
            'daily_pnl_rate': percent_text(row.get('今日盈亏比例(%)', '')),
            'exchange_rate': _display_exchange_rate(row.get('参考汇率中间价', '')),
        })
    return {'kind': 'snapshot', 'summary': summary, 'positions': positions}


def _display_exchange_rate(value):
    # Some exports append a currency-pair label to the displayed rate.
    token = re.match(r'\s*([0-9]+(?:\.[0-9]+)?)', str(value))
    return decimal_text(token[1]) if token else None


def _event_date(value, datemode):
    if isinstance(value, (int, float)) and 0 < value < 100000:
        return xlrd.xldate_as_datetime(value, datemode).date().isoformat()
    day = str(int(value) if isinstance(value, (int, float)) else value).strip().replace('/', '-')
    if not re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', day):
        raise ValueError('历史成交日期无效')
    return date.fromisoformat(day).isoformat()


def _event_time(value):
    text = str(value).strip()
    if not text:
        return None
    if isinstance(value, (int, float)) and 0 <= value < 1:
        seconds = round(value * 86400)
        if seconds < 86400:
            return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
    return datetime.strptime(text, '%H:%M:%S').time().isoformat()


def _history(sheet, header_row, datemode):
    headers = _header(sheet, header_row, HISTORY_FIELDS)
    entries = []
    for number, row in _records(sheet, header_row + 1, sheet.nrows, headers):
        label = str(row['买卖标志']).strip()
        code, exchange, asset_type = identity(row['证券代码'])
        event = EVENT_TYPES.get(label, 'unknown')
        shares = quantity(row['成交数量'], signed=True)
        price = decimal_text(row['成交价格'])
        if not label or (event in ('buy', 'sell') and (not code or not shares or not price or Decimal(price) <= 0)):
            raise ValueError(f'第 {number} 行缺少业务标志或成交必要字段')
        entry = {
            'event_date': _event_date(row['成交日期'], datemode), 'event_time': _event_time(row['成交时间']),
            'event_type': event, 'source_label': label, 'exchange': exchange, 'code': code,
            'name': str(row['证券名称']).strip(), 'asset_type': asset_type, 'price': price, 'quantity': shares,
            'gross_amount': decimal_text(row['成交金额']),
            'execution_reference': str(row['成交编号']).strip() or None,
            'order_reference': str(row['委托编号']).strip() or None,
        }
        # The broker's holder identifier is unnecessary for display or the persisted import.
        fingerprint = sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        entries.append({**entry, 'net_cash': None, 'source_row': number, 'fingerprint': fingerprint})
    if not entries:
        raise ValueError('历史成交文件没有明细')
    return {'kind': 'history', 'entries': entries}


def parse_export(content):
    if not content.startswith(BIFF_SIGNATURE):
        raise ValueError('请选择招商证券导出的原生 Excel XLS 文件')
    try:
        book = xlrd.open_workbook(file_contents=content, on_demand=True)
        try:
            if book.biff_version != 80 or book.nsheets != 1:
                raise ValueError('请选择本功能支持的单工作表招商证券 XLS')
            sheet = book.sheet_by_index(0)
            if sheet.nrows > MAX_ROWS or sheet.ncols > 120:
                raise ValueError('导出文件过大，请拆分后再导入')
            funds = positions = history = None
            for row in range(min(sheet.nrows, 16)):
                if _header(sheet, row, SNAPSHOT_FIELDS):
                    funds = row
                if _header(sheet, row, POSITION_FIELDS):
                    positions = row
                if _header(sheet, row, HISTORY_FIELDS):
                    history = row
            if funds is not None and positions is not None and funds < positions:
                return _snapshot(sheet, funds, positions)
            if history is not None:
                return _history(sheet, history, book.datemode)
            raise ValueError('未找到招商持仓或历史成交表头')
        finally:
            book.release_resources()
    except (xlrd.XLRDError, AssertionError, IndexError, OverflowError, UnicodeError) as exc:
        raise ValueError('无法读取 Excel 文件，请重新导出') from exc
