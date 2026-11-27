"""Private snapshot imports, provenance and read-only portfolio queries."""

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from stock_analysis.domains.portfolio.activity_read import read_activity
from stock_analysis.domains.portfolio.calculations import (
    combine_positions, daily_change, fill_snapshot_daily_pnl, position_included, reconcile, snapshot_totals, sum_known,
)
from stock_analysis.domains.portfolio.cmb_transfers import parse_transfers
from stock_analysis.domains.portfolio.cmb_xls import BIFF_SIGNATURE, decimal_text, parse_export
from stock_analysis.domains.portfolio.huatai_snapshot import HUATAI, parse_transcription
from stock_analysis.domains.portfolio.ledger_storage import archive, find_overlaps, save_entries
from stock_analysis.domains.portfolio.manual_entries import manual_options, save_manual_entry
from stock_analysis.domains.portfolio.performance_read import read_performance_reports
from stock_analysis.domains.portfolio.projected_snapshot import (
    book_view, build_book, quote_for_close, read_accounts, read_quotes,
)
from stock_analysis.domains.portfolio.reference import previous_market_day, read_hk_reference
from stock_analysis.domains.portfolio.revaluation import (
    book_quotes, delete_revaluation, display_revaluation, load_saved_revaluation, previous_revaluation_closes, read_revaluation,
    revaluation_directory, saved_revaluation, save_revaluation,
)
from stock_analysis.infrastructure.storage import utc_now


BROKER = '招商证券'
POSITION_COLUMNS = ('source_row', 'exchange', 'code', 'name', 'asset_type', 'currency', 'quantity',
                    'available_quantity', 'price', 'cost_price', 'cost_amount', 'market_value',
                    'holding_pnl', 'exchange_rate', 'holding_pnl_rate', 'daily_pnl', 'daily_pnl_rate')

class ImportConflict(ValueError):
    pass


def local_day():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def check_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value or value > local_day():
        raise ValueError('请输入有效的历史日期（北京时间）')
    return value


class Portfolio:
    def __init__(self, db, source_root, reference_reader, *, reference_cache=None):
        self.db = db
        self.source_root = source_root
        self.reference_reader = reference_reader
        self.reference_cache = reference_cache

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
        overlaps = find_overlaps(conn, account_id, data['entries']) if account_id and not existing else {}
        return (data['kind'], overlaps, existing)

    def manual_options(self, query='', account_id=None):
        return manual_options(self.db, local_day(), query, account_id)

    def save_manual(self, kind, body):
        return save_manual_entry(self.db, self.source_root, kind, body, local_day())

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
                        'positions': [{**row, 'included': position_included(row)} for row in data['positions']],
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

        archive(self.source_root, content, digest, 'jpg' if broker == HUATAI else 'xls')
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
            return save_entries(conn, account_id, data, state[1] if overlap == 'mark' else {},
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

    def snapshot(self, snapshot_id=None, *, account_id=None, day=None, basis_ids=None, close_as_of=None):
        if day is not None:
            day = check_day(day)
            if snapshot_id is not None or account_id is not None:
                raise ValueError('合并日期和分户快照请分别选择')
        with self._reference_snapshot() as (db, reference):
            conn = db.conn
            directory = [dict(row) for row in conn.execute("""
                SELECT s.id, s.account_id, s.snapshot_date, s.session, s.supersedes_id, a.broker
                FROM portfolio_snapshots s JOIN portfolio_accounts a ON a.id=s.account_id
                ORDER BY s.snapshot_date DESC, s.session='close' DESC, s.id DESC
            """)]
            accounts = {row['account_id']: row['broker'] for row in directory}
            if snapshot_id is None and account_id is None and len(accounts) > 1:
                return self._combined_snapshot(db, reference, directory, accounts, day, basis_ids, close_as_of)
            selected = next((row for row in directory if (snapshot_id is None or row['id'] == snapshot_id)
                             and (account_id is None or row['account_id'] == account_id)
                             and (close_as_of is None or row['session'] == 'close'
                                  and row['snapshot_date'] <= close_as_of)), None)
            payload = {'directory': directory, 'accounts': accounts, 'snapshot': None, 'scope': 'account'}
            if not selected and close_as_of is None and (snapshot_id is not None or account_id is not None):
                raise LookupError('找不到持仓快照')
            if not selected:
                return payload
            snapshot, positions = self._load_snapshot(conn, selected)
            for position in positions:
                position['reference'] = reference(position, snapshot['snapshot_date'])
            fill_snapshot_daily_pnl(snapshot, positions, previous_market_day(db, snapshot['snapshot_date']))
            return {**payload, 'snapshot': snapshot, 'positions': positions,
                    'revaluations': revaluation_directory(conn, snapshot['account_id']),
                    'totals': snapshot_totals(snapshot, positions),
                    'reconciliation': reconcile(snapshot, positions)}

    @contextmanager
    def _reference_snapshot(self):
        cache = self.reference_cache
        with self.db.shared_reader() as db:
            revision = cache.revision(db.conn) if cache is not None else None
            yield db, lambda position, day: self._reference(db, position, day, revision)

    def _reference(self, db, position, day, revision):
        if position['asset_type'] == 'stock' and position['exchange'] in ('sh', 'sz'):
            read = self.reference_reader
        elif position['asset_type'] == 'hong_kong' and position_included(position):
            read = read_hk_reference
        else:
            return None
        code = position['code']
        if revision is None:
            return read(db, code, day)
        return self.reference_cache.read(revision, (position['exchange'], code, day), lambda: read(db, code, day))

    @staticmethod
    def _load_snapshot(conn, entry):
        identity = entry['id']
        snapshot = dict(conn.execute('SELECT * FROM portfolio_snapshots WHERE id=?', (identity,)).fetchone())
        snapshot['broker'] = entry['broker']
        positions = [dict(row) for row in conn.execute(
            'SELECT * FROM portfolio_snapshot_positions WHERE snapshot_id=? ORDER BY source_row', (identity,))]
        return snapshot, positions

    def _combined_snapshot(self, db, reference, directory, accounts, day, basis_ids=None, close_as_of=None):
        days = list(dict.fromkeys(row['snapshot_date'] for row in directory if row['session'] == 'close'))
        selected_day = day or (days[0] if days else None)
        selected = {}
        for row in directory:
            matches = row['snapshot_date'] <= close_as_of if close_as_of else row['snapshot_date'] == selected_day
            if (matches and row['session'] == 'close'
                    and (basis_ids is None or row['id'] in basis_ids)):
                selected.setdefault(row['account_id'], row)
        if close_as_of and selected:
            selected_day = max(entry['snapshot_date'] for entry in selected.values())
        if day and not selected:
            raise LookupError('找不到这一天的收盘快照')
        funds, parts = [], []
        previous_day = previous_market_day(db, selected_day) if selected else None
        references = {}
        for entry in selected.values():
            snapshot, positions = self._load_snapshot(db.conn, entry)
            for position in positions:
                key = (position['exchange'], position['asset_type'], position['code'])
                reference_key = (*key, snapshot['snapshot_date'])
                if reference_key not in references:
                    references[reference_key] = reference(position, snapshot['snapshot_date'])
                position['reference'] = references[reference_key]
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
                'revaluations': revaluation_directory(db.conn, None),
                'snapshot': headline if funds else None, 'positions': holdings, 'funds': funds,
                'missing_accounts': missing, 'totals': totals}

    def performance(self):
        return read_performance_reports(self.db, BROKER)

    def current_snapshot(self, account_id=None):
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        today = now.date().isoformat()
        payload = self.snapshot(account_id=account_id, close_as_of=today)
        with self._reference_snapshot() as (reader, reference):
            accounts = read_accounts(reader, payload, today)
            if any(account['ledger_ids'] for account in accounts):
                prices, days = read_quotes(reader, self._projected_securities(accounts), now)
                quote = quote_for_close(accounts, prices, days, today)
                book = build_book(payload, accounts, prices, days, quote, today)
                return book_view(payload, book, quote, reference)
        return self.snapshot(account_id=account_id)

    @staticmethod
    def _projected_securities(accounts):
        return [row for account in accounts for row in account['positions'].values() if position_included(row)]

    def revaluation(self, snapshot_id=None, *, account_id=None, day=None, save=False, current=False):
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        today = now.date().isoformat()
        selection = dict(snapshot_id=snapshot_id, account_id=account_id, day=day)
        payload = self.snapshot(**selection, close_as_of=today if current else None)
        payload, result = self._read_projected_close(payload, selection, now, current)
        if result is None:
            result = read_revaluation(self.db, payload, now)
        if save and not result['missing_accounts']:
            return save_revaluation(self.db, payload, result)
        return result

    def _read_projected_close(self, payload, selection, now, current):
        today = now.date().isoformat()
        with self.db.shared_reader() as reader:
            accounts = read_accounts(reader, payload, today)
            if not any(account['ledger_ids'] for account in accounts):
                return payload, None
            prices, days = read_quotes(reader, self._projected_securities(accounts), now)
            quote = quote_for_close(accounts, prices, days, today)
            if current and quote and quote < payload['snapshot']['snapshot_date']:
                payload = self.snapshot(**selection, close_as_of=quote)
            snapshot = payload['snapshot']
            missing = payload.get('missing_accounts', [])
            if not quote or not snapshot or quote < snapshot['snapshot_date']:
                return payload, dict(quote_date=None, today=today, positions=payload.get('positions', []),
                                     totals={}, missing_accounts=missing)
            accounts = read_accounts(reader, payload, quote)
            extra = [row for row in self._projected_securities(accounts)
                     if (row['exchange'], row['code'], row['asset_type']) not in prices]
            if extra:
                prices.update(read_quotes(reader, extra, now)[0])
            book = build_book(payload, accounts, prices, days, quote, quote)
            return payload, dict(snapshot_date=snapshot['snapshot_date'], quote_date=quote, today=today,
                                 broker=snapshot['broker'], book=book,
                                 positions=book_quotes(book),
                                 totals=book['totals'], missing_accounts=missing)

    def saved_revaluation(self, estimate_id):
        return saved_revaluation(self.db, estimate_id)

    def delete_revaluation(self, estimate_id):
        delete_revaluation(self.db, estimate_id)

    def revaluation_snapshot(self, estimate_id, *, account_id=None):
        saved_account, basis_ids, saved = load_saved_revaluation(self.db, estimate_id)
        if saved_account is not None and account_id not in (None, saved_account):
            raise LookupError('该账户没有此收盘版本')
        payload = (self.snapshot(basis_ids[0]) if saved_account is not None
                   else self.snapshot(day=saved['snapshot_date'], basis_ids=basis_ids,
                                      close_as_of=saved['quote_date'] if 'book' in saved else None))
        inherited = account_id is not None and saved_account is None
        if inherited:
            basis = next((entry['snapshot'] for entry in payload['funds']
                          if entry['snapshot']['account_id'] == account_id), None)
            if basis is None:
                raise LookupError('该账户没有此收盘版本')
        if 'book' in saved:
            with self._reference_snapshot() as (_, reference):
                view = book_view(payload, saved['book'], saved['quote_date'], reference, estimate_id=estimate_id)
        else:
            keys = {(row['asset_type'], row['code']) for row in saved['positions']}
            positions = [row for row in payload['positions'] if position_included(row) and row['quantity'] > 0
                         and (row['asset_type'], row['code']) in keys]
            with self._reference_snapshot() as (db, reference):
                references = [reference(position, saved['quote_date']) for position in positions]
                previous = previous_revaluation_closes(db, positions, saved)
            view = display_revaluation(payload, saved, references, previous)
        return self._inherited_revaluation(view, saved, basis) if inherited else view

    def _inherited_revaluation(self, view, saved, basis):
        payload = self.snapshot(basis['id'])
        positions = [{**part, 'reference': row.get('reference')}
                     for row in view['positions'] for part in row.get('accounts', [row])
                     if part.get('broker') == basis['broker']]
        snapshot = {**basis, 'snapshot_date': saved['quote_date'], 'session': 'close',
                    'broker_assets': None, 'broker_daily_pnl': None, 'broker_daily_pnl_rate': None}
        book = saved.get('book')
        issues = []
        if book is not None:
            balances = book.get('account_balances', {}).get(str(basis['account_id']))
            if balances is None:
                with self.db.shared_reader() as db:
                    account = read_accounts(db, payload, saved['quote_date'], ledger_ids=set(book['ledger_ids']))[0]
                held = {(row['exchange'], row['code']) for row in positions if position_included(row)}
                can_sum = all(event['event_date'] != saved['quote_date']
                              or (event['exchange'], event['code']) in held for event in account['activities'])
                daily = sum_known([row for row in positions if position_included(row)], 'daily_pnl') if can_sum else None
                balances = {**account, 'daily_pnl': daily}
            snapshot['confirmed_cash'] = balances['cash']
            issues = balances['issues']
        totals = snapshot_totals(snapshot, positions)
        if book is not None:
            daily = balances['daily_pnl']
            totals.update(daily_change(totals['assets'], Decimal(daily) if daily is not None else None))
        return {**payload, 'snapshot': snapshot, 'positions': positions, 'totals': totals,
                'estimate_id': saved['saved_id'], 'basis_date': basis['snapshot_date'],
                'quote_date': saved['quote_date'], 'position_date': view.get('position_date'),
                'projection_issues': issues,
                'funds': [{'snapshot': basis, 'reconciliation': payload['reconciliation']}]}

    def history(self, start=None, end=None, offset=0, *, transfers=False, **options):
        return read_activity(self.db, local_day(), start, end, offset, transfers, options)
