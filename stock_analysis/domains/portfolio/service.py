"""Private snapshot imports, provenance and read-only portfolio queries."""

from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from stock_analysis.domains.portfolio.calculations import (
    combine_positions, daily_change, fill_snapshot_daily_pnl, reconcile, snapshot_totals, sum_known,
)
from stock_analysis.domains.portfolio.cmb_transfers import TRANSFER_TYPES, parse_transfers
from stock_analysis.domains.portfolio.cmb_xls import BIFF_SIGNATURE, decimal_text, parse_export
from stock_analysis.domains.portfolio.huatai_snapshot import HUATAI, parse_transcription
from stock_analysis.domains.portfolio.performance_read import read_performance
from stock_analysis.domains.portfolio.reference import previous_market_day
from stock_analysis.infrastructure.storage import StorageError, utc_now


BROKER = '招商证券'
POSITION_COLUMNS = ('source_row', 'exchange', 'code', 'name', 'asset_type', 'currency', 'quantity',
                    'available_quantity', 'price', 'cost_price', 'cost_amount', 'market_value',
                    'holding_pnl', 'exchange_rate', 'holding_pnl_rate', 'daily_pnl', 'daily_pnl_rate')
EVENT_COLUMNS = ('event_date', 'event_time', 'event_type', 'source_label', 'exchange', 'code', 'name',
                 'asset_type', 'price', 'quantity', 'gross_amount', 'net_cash', 'execution_reference',
                 'order_reference', 'source_row', 'fingerprint')


class ImportConflict(ValueError):
    pass


def local_day():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def check_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value or value > local_day():
        raise ValueError('请输入有效的历史日期（北京时间）')
    return value


def _archive(root, content, digest, extension):
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


class Portfolio:
    def __init__(self, db, source_root, reference_reader):
        self.db = db
        self.source_root = source_root
        self.reference_reader = reference_reader

    @staticmethod
    def _assess(conn, broker, data, digest, day, session, cash):
        account = conn.execute('SELECT id FROM portfolio_accounts WHERE broker=?', (broker,)).fetchone()
        account_id = account['id'] if account else None
        if data['kind'] == 'snapshot':
            current = conn.execute("""
                SELECT * FROM portfolio_snapshots
                WHERE account_id=? AND snapshot_date=? AND session=? ORDER BY id DESC LIMIT 1
            """, (account_id, day, session)).fetchone()
            same = bool(current and current['source_sha256'] == digest and current['confirmed_cash'] == cash)
            if same:
                saved = [dict(row) for row in conn.execute(
                    'SELECT ' + ','.join(POSITION_COLUMNS) +
                    ' FROM portfolio_snapshot_positions WHERE snapshot_id=? ORDER BY source_row', (current['id'],))]
                same = all(current[key] == value for key, value in data['summary'].items()) and saved == data['positions']
            return ('snapshot', current['id'] if current else None, same)
        existing = conn.execute('SELECT count(*) FROM portfolio_ledger_entries WHERE account_id=? AND source_sha256=?',
                                (account_id, digest)).fetchone()[0]
        overlaps = {}
        if account_id and not existing:
            matches = {}
            for entry in data['entries']:
                fingerprint = entry['fingerprint']
                if fingerprint not in matches:
                    matches[fingerprint] = iter(conn.execute("""
                    SELECT id FROM portfolio_ledger_entries WHERE account_id=? AND fingerprint=?
                    AND duplicate_of_id IS NULL ORDER BY id
                    """, (account_id, fingerprint)).fetchall())
                match = next(matches[fingerprint], None)
                if match:
                    overlaps[entry['source_row']] = match['id']
        return (data['kind'], overlaps, existing)

    def ingest(self, content, name, *, day=None, session=None, cash=None, confirmation=None,
               revise=False, overlap=None, draft=None):
        if not isinstance(name, str) or not name or len(name) > 255 or any(c in name for c in '\\/\r\n\0'):
            raise ValueError('文件名无效')
        broker = HUATAI if draft is not None else BROKER
        if broker == HUATAI:
            if not name.lower().endswith(('.jpg', '.jpeg')):
                raise ValueError('请选择华泰持仓截图的 JPEG 原件')
            data, cash = parse_transcription(draft, content)
            day, session = draft.get('snapshot_date'), draft.get('snapshot_session')
            if not day or not session:
                raise ValueError('华泰转录需有已确认的日期和时点')
        else:
            data = parse_export(content) if content.startswith(BIFF_SIGNATURE) else parse_transfers(content)
        if data['kind'] == 'snapshot':
            if day is not None:
                day = check_day(day)
            if session is not None and session not in ('close', 'intraday'):
                raise ValueError('请选择盘中或收盘')
            cash = decimal_text(cash) if cash is not None else None
        else:
            day = session = cash = None
            if max(entry['event_date'] for entry in data['entries']) > local_day():
                raise ValueError('导入的历史记录含未来日期')
        digest = sha256(content).hexdigest()
        with self.db.connection() as conn:
            state = self._assess(conn, broker, data, digest, day, session, cash)
        key = sha256(json.dumps([digest, day, session, cash, state, data if draft is not None else None],
                                sort_keys=True).encode()).hexdigest()

        if confirmation is None:
            if data['kind'] == 'snapshot':
                return {**data, 'broker': broker, 'snapshot_date': day, 'session': session,
                        'confirmed_cash': cash, 'preview': key, 'already_imported': state[2],
                        'requires_revision': bool(state[1] and not state[2]),
                        'reconciliation': reconcile(data['summary'], data['positions'])}
            candidates = state[1]
            entries = data['entries']
            visible = entries[:100]
            if data['kind'] == 'transfers':
                private_fields = {'execution_reference', 'order_reference', 'fingerprint'}
                visible = [{key: val for key, val in entry.items() if key not in private_fields} for entry in visible]
            return {'kind': data['kind'], 'preview': key, 'already_imported': bool(state[2]),
                    'entry_count': len(entries), 'start': min(e['event_date'] for e in entries),
                    'end': max(e['event_date'] for e in entries),
                    'ignored_count': data.get('ignored_count', 0),
                    'overlap_count': len(candidates),
                    'entries': [{**entry, 'overlap': entry['source_row'] in candidates} for entry in visible]}

        if data['kind'] == 'snapshot' and (day is None or session is None):
            raise ValueError('确认快照前请选择日期及盘中／收盘')
        if state[2]:
            return {'kind': data['kind'], 'already_imported': True}
        if key != confirmation:
            raise ImportConflict('文件、确认条件或现有记录已变化，请重新预览')
        if data['kind'] == 'snapshot' and state[1] and not revise:
            raise ImportConflict('已有同日快照，请确认是否保存修订')
        if data['kind'] != 'snapshot' and state[1] and overlap not in ('keep', 'mark'):
            raise ImportConflict('请先确认疑似重复记录')

        _archive(self.source_root, content, digest, 'jpg' if broker == HUATAI else 'xls')
        with self.db.connection(write=True) as conn:
            latest = self._assess(conn, broker, data, digest, day, session, cash)
            if latest[2]:
                return {'kind': data['kind'], 'already_imported': True}
            if latest != state:
                raise ImportConflict('已有并发导入，请重新预览')
            now = utc_now()
            conn.execute('INSERT INTO portfolio_accounts(broker, created_at) VALUES (?,?) ON CONFLICT(broker) DO NOTHING',
                         (broker, now))
            account_id = conn.execute('SELECT id FROM portfolio_accounts WHERE broker=?', (broker,)).fetchone()[0]
            if data['kind'] == 'snapshot':
                return self._save_snapshot(conn, account_id, data, day, session, cash, state[1], digest, name, now)
            return self._save_entries(conn, account_id, data, state[1] if overlap == 'mark' else {},
                                      digest, name, now)

    @staticmethod
    def _save_snapshot(conn, account_id, data, day, session, cash, supersedes, digest, name, now):
        summary = data['summary']
        snapshot_id = conn.execute("""
            INSERT INTO portfolio_snapshots(account_id, snapshot_date, session, source_sha256, source_name, supersedes_id,
                currency, broker_balance, broker_available, broker_withdrawable, broker_market_value, broker_assets,
                confirmed_cash, imported_at, broker_daily_pnl, broker_daily_pnl_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (account_id, day, session, digest, name, supersedes, summary['currency'], summary['broker_balance'],
              summary['broker_available'], summary['broker_withdrawable'], summary['broker_market_value'],
              summary['broker_assets'], cash, now, summary['broker_daily_pnl'], summary['broker_daily_pnl_rate'])).lastrowid
        columns = ','.join(POSITION_COLUMNS)
        marks = ','.join('?' for _ in range(len(POSITION_COLUMNS) + 1))
        conn.executemany(f'INSERT INTO portfolio_snapshot_positions(snapshot_id,{columns}) VALUES ({marks})',
                         [(snapshot_id, *(row[col] for col in POSITION_COLUMNS)) for row in data['positions']])
        return {'kind': 'snapshot', 'snapshot_id': snapshot_id, 'saved_count': len(data['positions'])}

    @staticmethod
    def _save_entries(conn, account_id, data, overlaps, digest, name, now):
        entries = data['entries']
        columns = ','.join(EVENT_COLUMNS)
        marks = ','.join('?' for _ in range(len(EVENT_COLUMNS) + 5))
        conn.executemany(f"""
            INSERT INTO portfolio_ledger_entries(account_id,{columns},source_sha256,source_name,imported_at,duplicate_of_id)
            VALUES ({marks})
        """, [(account_id, *(entry[col] for col in EVENT_COLUMNS), digest, name, now, overlaps.get(entry['source_row']))
              for entry in entries])
        return {'kind': data['kind'], 'saved_count': len(entries), 'duplicate_count': len(overlaps),
                'ignored_count': data.get('ignored_count', 0)}

    def snapshot(self, snapshot_id=None, *, account_id=None, day=None):
        if day is not None:
            day = check_day(day)
            if snapshot_id is not None or account_id is not None:
                raise ValueError('合并日期和分户快照请分别选择')
        with self.db.shared_reader() as db:
            conn = db.conn
            directory = [dict(row) for row in conn.execute("""
                SELECT s.id, s.account_id, s.snapshot_date, s.session, s.supersedes_id, a.broker
                FROM portfolio_snapshots s JOIN portfolio_accounts a ON a.id=s.account_id
                ORDER BY s.snapshot_date DESC, s.session='close' DESC, s.id DESC
            """)]
            accounts = {row['account_id']: row['broker'] for row in directory}
            if snapshot_id is None and account_id is None and len(accounts) > 1:
                return self._combined_snapshot(db, directory, accounts, day)
            selected = next((row for row in directory if (snapshot_id is None or row['id'] == snapshot_id)
                             and (account_id is None or row['account_id'] == account_id)), None)
            payload = {'directory': directory, 'accounts': accounts, 'snapshot': None, 'scope': 'account'}
            if not selected and (snapshot_id is not None or account_id is not None):
                raise LookupError('找不到持仓快照')
            if not selected:
                return payload
            snapshot, positions = self._load_snapshot(conn, selected)
            for position in positions:
                position['reference'] = self._reference(db, position, snapshot['snapshot_date'])
            fill_snapshot_daily_pnl(snapshot, positions, previous_market_day(db, snapshot['snapshot_date']))
            return {**payload, 'snapshot': snapshot, 'positions': positions,
                    'totals': snapshot_totals(snapshot, positions),
                    'reconciliation': reconcile(snapshot, positions)}

    def _reference(self, db, position, day):
        if position['asset_type'] == 'stock' and position['exchange'] in ('sh', 'sz'):
            return self.reference_reader(db, position['code'], day)
        return None

    @staticmethod
    def _load_snapshot(conn, entry):
        identity = entry['id']
        snapshot = dict(conn.execute('SELECT * FROM portfolio_snapshots WHERE id=?', (identity,)).fetchone())
        snapshot['broker'] = entry['broker']
        positions = [dict(row) for row in conn.execute(
            'SELECT * FROM portfolio_snapshot_positions WHERE snapshot_id=? ORDER BY source_row', (identity,))]
        return snapshot, positions

    def _combined_snapshot(self, db, directory, accounts, day):
        days = list(dict.fromkeys(row['snapshot_date'] for row in directory if row['session'] == 'close'))
        selected_day = day or (days[0] if days else None)
        selected = {}
        for row in directory:
            if row['snapshot_date'] == selected_day and row['session'] == 'close':
                selected.setdefault(row['account_id'], row)
        if day and not selected:
            raise LookupError('找不到这一天的收盘快照')
        funds, parts = [], []
        previous_day = previous_market_day(db, selected_day) if selected else None
        references = {}
        for entry in selected.values():
            snapshot, positions = self._load_snapshot(db.conn, entry)
            for position in positions:
                key = (position['exchange'], position['asset_type'], position['code'])
                if key not in references:
                    references[key] = self._reference(db, position, selected_day)
                position['reference'] = references[key]
            fill_snapshot_daily_pnl(snapshot, positions, previous_day)
            funds.append({'snapshot': snapshot, 'reconciliation': reconcile(snapshot, positions),
                          'totals': snapshot_totals(snapshot, positions)})
            parts.extend({**position, 'broker': entry['broker']} for position in positions)
        holdings = combine_positions(parts, selected_day)
        cash = sum_known([row['snapshot'] for row in funds], 'confirmed_cash') if len(funds) == len(accounts) else None
        headline = {'broker': '合并持仓', 'snapshot_date': selected_day, 'session': 'close',
                    'confirmed_cash': str(cash) if cash is not None else None}
        missing = [broker for account_id, broker in accounts.items() if account_id not in selected]
        totals = snapshot_totals(headline, holdings)
        daily = sum_known([entry['totals'] for entry in funds], 'daily_pnl') if funds else None
        totals.update(daily_change(totals['assets'], daily))
        return {'scope': 'combined', 'directory': directory, 'accounts': accounts, 'days': days,
                'snapshot': headline if funds else None, 'positions': holdings, 'funds': funds,
                'missing_accounts': missing, 'totals': totals}

    def performance(self):
        return read_performance(self.db, BROKER)

    def history(self, start=None, end=None, offset=0, *, transfers=False):
        start = check_day(start) if start is not None else '0001-01-01'
        end = check_day(end) if end is not None else local_day()
        if start > end or not 0 <= offset <= 1_000_000:
            raise ValueError('成交查询范围无效')
        with self.db.connection() as conn:
            params = (BROKER, start, end, *TRANSFER_TYPES)
            scope = """FROM portfolio_ledger_entries l JOIN portfolio_accounts a ON a.id=l.account_id
                       WHERE a.broker=? AND l.event_date BETWEEN ? AND ? AND l.duplicate_of_id IS NULL"""
            scope += ' AND l.event_type ' + ('IN' if transfers else 'NOT IN') + ' (?,?,?)'
            count = conn.execute('SELECT count(*) ' + scope, params).fetchone()[0]
            entries = conn.execute("""
                SELECT l.id,l.event_date,l.event_time,l.event_type,l.source_label,l.exchange,l.code,l.name,l.asset_type,
                       l.quantity,l.price,l.gross_amount,l.fees,l.net_cash,l.source_name,l.source_row
            """ + scope + ' ORDER BY l.event_date DESC,l.event_time DESC,l.id DESC LIMIT 100 OFFSET ?', (*params, offset))
            return {'total_count': count, 'offset': offset, 'entries': [dict(row) for row in entries]}
