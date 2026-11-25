"""Read the CMB Securities tab-separated bank transfer exports."""

from datetime import date, time
from decimal import Decimal
from hashlib import sha256
import json

from stock_analysis.domains.portfolio.cmb_xls import MAX_ROWS, decimal_text


TRANSFER_TYPES = ('bank_transfer_in', 'bank_transfer_out', 'bank_transfer_unposted')
HEADERS = ('转帐日期', '转帐时间', '摘要', '转帐方向', '转帐金额', '银行流水号', '状态说明')


def _unwrap(value):
    value = value.strip()
    if value.startswith('="') and value.endswith('"'):
        return value[2:-1].replace('""', '"')
    return value


def parse_transfers(content):
    try:
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('gb18030')
    except UnicodeDecodeError as exc:
        raise ValueError('请选择招商证券原生 XLS 或银证转账导出文件') from exc
    lines = [(number, line) for number, line in enumerate(text.splitlines(), 1) if line.strip()]
    if len(lines) > MAX_ROWS + 1:
        raise ValueError('银证转账文件过大，请拆分后再导入')
    if not lines or tuple(_unwrap(cell) for cell in lines[0][1].rstrip('\t').split('\t')) != HEADERS:
        raise ValueError('未找到招商银证转账表头')
    entries = []
    ignored = 0
    for number, line in lines[1:]:
        cells = [_unwrap(cell) for cell in line.split('\t')]
        if len(cells) == len(HEADERS) + 1 and cells[-1] == '':
            cells.pop()
        if len(cells) != len(HEADERS):
            raise ValueError(f'第 {number} 行银证转账列数无效')
        day, clock, _, direction, raw_amount, bank_reference, status = cells
        if direction == '查银行余额':
            ignored += 1
            continue
        if direction not in ('转入', '转出') or not status:
            raise ValueError(f'第 {number} 行银证转账方向或状态无效')
        try:
            event_date = date.fromisoformat(day).isoformat()
            event_time = time.fromisoformat(clock).isoformat()
        except ValueError as exc:
            raise ValueError(f'第 {number} 行银证转账日期或时间无效') from exc
        amount = decimal_text(raw_amount)
        if amount is None or Decimal(amount) < 0:
            raise ValueError(f'第 {number} 行银证转账金额无效')
        event = 'bank_transfer_unposted'
        cash = None
        if status == '成功':
            event = 'bank_transfer_in' if direction == '转入' else 'bank_transfer_out'
            cash = f'-{amount}' if direction == '转出' and amount != '0' else amount
        fingerprint = sha256(json.dumps([event, event_date, event_time, direction, amount, bank_reference, status],
                                       ensure_ascii=False).encode()).hexdigest()
        entries.append({
            'event_date': event_date, 'event_time': event_time, 'event_type': event,
            'source_label': f'{direction} · {status}', 'exchange': None, 'code': '', 'name': '',
            'asset_type': 'cash', 'price': None, 'quantity': None,
            'gross_amount': amount, 'net_cash': cash, 'execution_reference': bank_reference or None,
            'order_reference': None, 'source_row': number, 'fingerprint': fingerprint,
        })
    if not entries:
        raise ValueError('文件里没有可导入的银证转账记录')
    return {'kind': 'transfers', 'entries': entries, 'ignored_count': ignored}
