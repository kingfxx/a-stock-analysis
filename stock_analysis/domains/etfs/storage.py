"""Publish source-specific ETF facts and revisions."""

from stock_analysis.infrastructure.storage import utc_now
from .parsers import DIVIDEND_FIELDS, PRICE_FIELDS


def latest(conn, table, exchange, code, provider, key, start=None, end=None):
    condition = f' AND {key} BETWEEN ? AND ?' if start is not None else ''
    params = (exchange,code,provider)
    if condition:
        params += (start,end)
    rows = conn.execute(f"""
        SELECT * FROM {table} WHERE exchange=? AND code=? AND source=? {condition}
        ORDER BY {key},revision
    """, params)
    return {row[key]: row for row in rows}


def prices(conn, symbol, provider, start, end):
    return latest(conn,'etf_daily_prices',symbol[:2],symbol[2:],provider,'trade_date',start,end)


def span(conn, symbol, provider):
    return tuple(conn.execute("""
        SELECT min(trade_date),max(trade_date) FROM etf_daily_prices WHERE exchange=? AND code=? AND source=?
    """, (symbol[:2],symbol[2:],provider)).fetchone())


def insert_versions(conn, table, fields, rows, previous, batch, scope, source_id, key):
    stamp = utc_now()
    changes = []
    for event_key, values in rows:
        old = previous.get(event_key)
        if old is not None and all(old[field] == values[field] for field in fields):
            continue
        if old is not None and any(old[field] is not None and values[field] is None for field in fields):
            raise ValueError('来源历史值变为空，需要核实撤回')
        changes.append((scope['exchange'],scope['code'],event_key,batch['source_system'],
                        old['revision'] + 1 if old is not None else 1,
                        *(values[field] for field in fields),stamp,batch['id'],source_id))
    columns = ','.join(('exchange','code',key,'source','revision',*fields,'updated_at','batch_id','source_id'))
    placeholders = ','.join('?' for _ in columns.split(','))
    conn.executemany(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})',changes)
    return len(changes)


def save_prices(conn, batch, scope, observations, source_id):
    previous = prices(conn, scope['exchange']+scope['code'], batch['source_system'],
                      batch['scope_start'],batch['scope_end'])
    return insert_versions(conn,'etf_daily_prices',PRICE_FIELDS,observations.items(),previous,
                           batch,scope,source_id,'trade_date')


def save_dividends(conn, batch, scope, records, source_id):
    previous = latest(conn,'etf_cash_dividend_events',scope['exchange'],scope['code'],
                      'eastmoney','event_key')
    by_dates = {(row['registration_date'],row['ex_date']):key for key,row in previous.items()}
    official = official_events(conn,scope)
    matched = set()
    output = []
    for row in records:
        dates = (row['registration_date'],row['ex_date'])
        event_key = by_dates.get(dates)
        confirmed = [old for old in official.values() if all(old[field]==row[field] for field in
                     ('registration_date','ex_date','payment_date','cash_per_unit'))]
        if len(confirmed)>1:
            raise ValueError('分红与多个正式公告匹配，需要人工核验')
        if confirmed:
            event_key = confirmed[0]['event_key']
        if event_key is None:
            if any(shares_date(old,row) for old in previous.values()):
                raise ValueError('分红日期发生变化，待核对基金管理人正式公告')
            event_key = '/'.join(dates)
        elif event_key in previous:
            old = previous[event_key]
            if not confirmed and any(old[field] != row[field] for field in ('payment_date','cash_per_unit')):
                raise ValueError('分红发放日或金额发生变化，待核对基金管理人正式公告')
            matched.add(event_key)
        output.append((event_key,row))
    if set(previous) - matched:
        raise ValueError('F10 不再返回已发布事件，待核对基金管理人正式公告')
    if len({key for key,_ in output}) != len(output):
        raise ValueError('多条 F10 明细匹配同一分红事件，待核对正式公告')
    return insert_versions(conn,'etf_cash_dividend_events',DIVIDEND_FIELDS,output,previous,
                           batch,scope,source_id,'event_key')


def shares_date(old, candidate):
    return any(old[field]==candidate[field] for field in ('registration_date','ex_date','payment_date'))


def official_events(conn, scope):
    rows = conn.execute("""
        SELECT * FROM etf_cash_dividend_events WHERE exchange=? AND code=? AND source<>'eastmoney'
        ORDER BY updated_at,revision
    """,(scope['exchange'],scope['code']))
    return {row['event_key']:row for row in rows}


def save_official_dividend(conn, batch, scope, record, source_id):
    previous_f10 = latest(conn,'etf_cash_dividend_events',scope['exchange'],scope['code'],'eastmoney','event_key')
    existing = {**previous_f10,**official_events(conn,scope)}
    candidates = {key for key,old in existing.items() if shares_date(old,record)}
    if len(candidates)>1:
        raise ValueError('正式公告可匹配多笔分红，需要人工核验')
    key = next(iter(candidates),record['registration_date']+'/'+record['ex_date'])
    previous = latest(conn,'etf_cash_dividend_events',scope['exchange'],scope['code'],
                      batch['source_system'],'event_key')
    return insert_versions(conn,'etf_cash_dividend_events',DIVIDEND_FIELDS,[(key,record)],previous,
                           batch,scope,source_id,'event_key')
