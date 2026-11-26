"""Persist ledger entries and their source evidence."""

from decimal import Decimal
from hashlib import sha256
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from stock_analysis.infrastructure.storage import StorageError


EVENT_COLUMNS = ('event_date', 'event_time', 'event_type', 'source_label', 'exchange', 'code', 'name',
                 'asset_type', 'price', 'quantity', 'gross_amount', 'fees', 'net_cash', 'execution_reference',
                 'order_reference', 'source_row', 'fingerprint')


def archive(root, content, digest, extension):
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'{digest}.{extension}'
    if target.is_symlink():
        raise StorageError('持仓原件路径无效')
    if target.is_file():
        if sha256(target.read_bytes()).hexdigest() != digest:
            raise StorageError('已归档的券商原件校验失败')
        return
    temporary = None
    try:
        with NamedTemporaryFile(dir=root, prefix='import-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_entries(conn, account_id, data, overlaps, digest, name, now):
    entries = data['entries']
    columns = ','.join(EVENT_COLUMNS)
    marks = ','.join('?' for _ in range(len(EVENT_COLUMNS) + 5))
    conn.executemany(f"""
        INSERT INTO portfolio_ledger_entries(account_id,{columns},source_sha256,source_name,imported_at,duplicate_of_id)
        VALUES ({marks})
    """, [(account_id, *(entry.get(col) for col in EVENT_COLUMNS), digest, name, now, overlaps.get(entry['source_row']))
          for entry in entries])
    return {'kind': data['kind'], 'saved_count': len(entries), 'duplicate_count': len(overlaps),
            'ignored_count': data.get('ignored_count', 0)}


def comparable_entries(conn, account_id, entry, *, manual_only=False):
    condition = " AND source_name LIKE 'manual/%'" if manual_only else ''
    candidates = conn.execute("""
        SELECT id,event_date,event_time,source_label,code,quantity,price,gross_amount,
               execution_reference,order_reference
        FROM portfolio_ledger_entries
        WHERE account_id=? AND event_date=? AND (event_type=? OR (source_label='红股入账' AND ?='红股入账'))
        AND code=? AND duplicate_of_id IS NULL
    """ + condition + ' ORDER BY id',
        (account_id, entry['event_date'], entry['event_type'], entry['source_label'], entry['code']))
    for candidate in candidates:
        if candidate['source_label'] != entry['source_label'] and entry['event_type'] == 'unknown':
            continue
        if any(candidate[field] and entry[field] and candidate[field] != entry[field]
               for field in ('event_time', 'execution_reference', 'order_reference')):
            continue
        if abs(candidate['quantity'] or 0) != abs(entry['quantity'] or 0):
            continue
        left, right = candidate['price'], entry['price']
        if entry['event_type'] in ('buy', 'sell'):
            matches = left is not None and right is not None and Decimal(left) == Decimal(right)
        elif entry['source_label'] in ('红股入账', '申购配号'):
            matches = True
        else:
            left, right = candidate['gross_amount'], entry['gross_amount']
            matches = (left is None and right is None) or (
                left is not None and right is not None and abs(Decimal(left)) == abs(Decimal(right)))
        if matches:
            yield candidate


def find_overlaps(conn, account_id, entries):
    candidates, used, overlaps = {}, set(), {}
    for entry in entries:
        fingerprint = entry['fingerprint']
        if fingerprint not in candidates:
            candidates[fingerprint] = iter(conn.execute("""
                SELECT id FROM portfolio_ledger_entries WHERE account_id=? AND fingerprint=?
                AND duplicate_of_id IS NULL ORDER BY id
            """, (account_id, fingerprint)).fetchall())
        match = next((row for row in candidates[fingerprint] if row['id'] not in used), None)
        if match is None:
            match = next((row for row in comparable_entries(conn, account_id, entry, manual_only=True)
                          if row['id'] not in used), None)
        if match is not None:
            used.add(match['id'])
            overlaps[entry['source_row']] = match['id']
    return overlaps
