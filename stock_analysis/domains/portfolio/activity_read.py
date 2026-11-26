"""Filter and page the private portfolio activity ledger."""

from datetime import date

from stock_analysis.domains.portfolio.cmb_transfers import TRANSFER_TYPES
from stock_analysis.domains.portfolio.cmb_xls import decimal_text


TEXT_COLUMNS = {'broker': 'a.broker', 'business': 'l.source_label'}
NUMERIC_COLUMNS = {'quantity': 'l.quantity', 'price': 'l.price',
                   'amount': 'l.gross_amount', 'net_cash': 'l.net_cash'}
ACTIVITY_FIELDS = {'range', 'sort', 'direction', 'security', *TEXT_COLUMNS, *NUMERIC_COLUMNS}


def activity_dates(period, today, start=None, end=None):
    if period == 'all':
        return None, today
    if period in ('month', 'quarter', 'year'):
        now = date.fromisoformat(today)
        month = {'month': now.month, 'quarter': (now.month - 1) // 3 * 3 + 1, 'year': 1}[period]
        return now.replace(month=month, day=1).isoformat(), today
    if period != 'custom':
        raise ValueError('时间范围无效')
    for day in (start, end):
        if day is not None and (date.fromisoformat(day).isoformat() != day or day > today):
            raise ValueError('请输入有效的历史日期（北京时间）')
    return start, end


def read_activity(db, today, start, end, offset, transfers, options):
    if options.keys() - ACTIVITY_FIELDS or not 0 <= offset <= 1_000_000:
        raise ValueError('流水查询参数无效')
    numeric_fields = ('amount', 'net_cash') if transfers else ('quantity', 'price', 'amount')
    if options.keys() & (NUMERIC_COLUMNS.keys() - set(numeric_fields)) or (transfers and 'security' in options):
        raise ValueError('筛选字段不适用于此列表')
    period = options.get('range', 'custom' if start or end else 'all')
    start, end = activity_dates(period, today, start, end)
    bounds = (start or '0001-01-01', end or today)
    if bounds[0] > bounds[1]:
        raise ValueError('开始日期不能晚于结束日期')
    sort = options.get('sort', 'date')
    direction = options.get('direction', 'desc').lower()
    orders = {'date': 'l.event_date', **TEXT_COLUMNS,
              **{key: f'CAST({NUMERIC_COLUMNS[key]} AS NUMERIC)' for key in numeric_fields}}
    if not transfers:
        orders['security'] = 'l.code'
    if sort not in orders or direction not in ('asc', 'desc'):
        raise ValueError('排序字段或方向无效')
    direction = direction.upper()
    if sort == 'date':
        order = f'l.event_date {direction},l.event_time {direction} NULLS LAST,l.id {direction}'
    else:
        order = f'{orders[sort]} {direction} NULLS LAST,l.event_date DESC,l.event_time DESC,l.id DESC'

    source = """FROM portfolio_ledger_entries l JOIN portfolio_accounts a ON a.id=l.account_id
                WHERE l.duplicate_of_id IS NULL AND l.event_type"""
    source += (' IN' if transfers else ' NOT IN') + ' (?,?,?)'
    scope = source + ' AND l.event_date BETWEEN ? AND ?'
    params = [*TRANSFER_TYPES, *bounds]
    for key, column in TEXT_COLUMNS.items():
        value = options.get(key, '').strip()
        if value:
            if len(value) > 100:
                raise ValueError('筛选文字过长')
            scope += f' AND {column}=?'
            params.append(value)
    keyword = options.get('security', '').strip()
    if keyword:
        if len(keyword) > 80:
            raise ValueError('证券筛选文字过长')
        scope += " AND instr(lower(l.code || ' ' || l.name),lower(?))>0"
        params.append(keyword)
    for field in numeric_fields:
        value = options.get(field, '').strip()
        if value:
            number = decimal_text(value)
            if number is None:
                raise ValueError('请输入有效的数字筛选值')
            scope += f' AND CAST({NUMERIC_COLUMNS[field]} AS NUMERIC)=CAST(? AS NUMERIC)'
            params.append(number)

    with db.connection() as conn:
        count = conn.execute('SELECT count(*) ' + scope, params).fetchone()[0]
        rows = conn.execute("""
            SELECT l.id,a.broker,l.event_date,l.event_time,l.event_type,l.source_label,l.exchange,l.code,l.name,l.asset_type,
                   l.quantity,l.price,l.gross_amount,l.fees,l.net_cash,l.source_name,l.source_row
        """ + scope + f' ORDER BY {order} LIMIT 100 OFFSET ?', (*params, offset))
        entries = [dict(row) for row in rows]
        facets = {key: [row[0] for row in conn.execute(
            f'SELECT DISTINCT {column} ' + source + f' ORDER BY {column}', TRANSFER_TYPES)]
                  for key, column in TEXT_COLUMNS.items()}
    return {'total_count': count, 'offset': offset, 'entries': entries, 'facets': facets,
            'period': {'start': start, 'end': end or today}}
