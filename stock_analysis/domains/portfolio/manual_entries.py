"""Local suggestions and explicit account-scoped ledger writes."""

from hashlib import sha256
import json

from stock_analysis.domains.portfolio.ledger_storage import archive, comparable_entries, save_entries
from stock_analysis.domains.portfolio.manual_fields import BUSINESSES, canonical_json, clean, normalize_entry, require_input
from stock_analysis.infrastructure.storage import utc_now


def _accounts(conn):
    return [dict(row) for row in conn.execute('SELECT id,broker FROM portfolio_accounts ORDER BY id')]


def _securities(conn, query, account_id):
    recent = conn.execute("""
        SELECT code,name,exchange,asset_type FROM (
            SELECT p.code,p.name,p.exchange,p.asset_type,
                   CASE WHEN ? IS NULL OR s.account_id=? THEN 0 ELSE 2 END priority,s.id position
            FROM portfolio_snapshot_positions p JOIN portfolio_snapshots s ON s.id=p.snapshot_id
            WHERE p.asset_type IN ('stock','etf')
            UNION ALL
            SELECT code,name,exchange,asset_type,CASE WHEN ? IS NULL OR account_id=? THEN 1 ELSE 3 END,id
            FROM portfolio_ledger_entries WHERE duplicate_of_id IS NULL AND asset_type IN ('stock','etf')
        ) WHERE name<>'' AND (priority<2 OR ?<>'') AND (instr(code,?) OR instr(name,?))
        GROUP BY code,name,exchange,asset_type,priority ORDER BY priority,max(position) DESC LIMIT 30
    """, (account_id, account_id, account_id, account_id, query, query, query))
    suggestions = {}
    for row in recent:
        suggestions.setdefault(row['code'], dict(row))
    if query:
        for row in conn.execute("""
            SELECT code,name,exchange,'stock' AS asset_type FROM instruments
            WHERE name IS NOT NULL AND (instr(code,?) OR instr(lower(name),?) OR instr(search_initials,?) OR instr(search_pinyin,?))
            ORDER BY code=? DESC,code LIMIT 20
        """, (query, query, query, query, query)):
            suggestions.setdefault(row['code'], dict(row))
    return sorted(suggestions.values(), key=lambda row: (row['code'] != query,
                  not (query and row['name'] == query)))[:20]


def manual_options(db, today, query='', account_id=None):
    if len(query) > 60:
        raise ValueError('搜索文字过长')
    with db.connection() as conn:
        return {'today': today, 'accounts': _accounts(conn), 'securities': _securities(conn, clean(query).lower(), account_id),
                'businesses': {key: [dict(group=group, label=label, mode=mode, aliases=aliases)
                                     for group, label, _, mode, aliases in rows] for key, rows in BUSINESSES.items()}}


def save_manual_entry(db, root, kind, body, today):
    require_input(body)
    name = clean(body.get('broker', ''))
    if not name or len(name) > 50 or any(ord(char) < 32 for char in name):
        raise ValueError('请选择券商或填写有效名称')
    source_name = f"manual/{body['request_id']}.json"
    with db.connection(write=True) as conn:
        accounts = _accounts(conn)
        exact = next((row for row in accounts if row['broker'] == name), None)
        aliases = [row for row in accounts if row['broker'].removesuffix('证券') == name.removesuffix('证券')]
        account = exact or (aliases[0] if len(aliases) == 1 else None)
        if account is None and (body.get('new_broker') != name or aliases):
            raise ValueError('请选择券商，或确认新增券商')
        broker = account['broker'] if account else name
        code = clean(body.get('code', ''))
        # Persist the submitted values as well as the derived ledger fields.
        submitted = {key: clean(value) for key, value in body.items()
                     if key not in ('confirmation', 'new_broker')}
        existing = conn.execute('SELECT id,source_sha256 FROM portfolio_ledger_entries WHERE source_name=?',
                                (source_name,)).fetchone()
        if existing:
            # Compare the archived submission because local security metadata can change.
            saved = (root / f"{existing['source_sha256']}.json").read_bytes()
            if sha256(saved).hexdigest() == existing['source_sha256']:
                if json.loads(saved)['submitted'] == submitted:
                    return {'id': existing['id'], 'event_date': submitted['event_date'], 'already_saved': True}
            raise ValueError('表单提交标识已使用，请重新打开表单')
        security = next((row for row in _securities(conn, code, None) if row['code'] == code), {}) if code else {}
        entry = normalize_entry(kind, body, today, security.get('name'))
        content = canonical_json({'submitted': submitted, 'broker': broker, 'entry': entry})
        digest = sha256(content).hexdigest()
        candidates = list(comparable_entries(conn, account['id'], entry)) if account else []
        confirmation = sha256(canonical_json([digest, [row['id'] for row in candidates]])).hexdigest()
        if candidates and body.get('confirmation') != confirmation:
            return {'confirmation': confirmation,
                    'candidates': [{key: row[key] for key in ('event_date','event_time','source_label','code','quantity','gross_amount')}
                                   for row in candidates[:8]]}
        now = utc_now()
        archive(root, content, digest, 'json')
        account_id = account['id'] if account else conn.execute(
            'INSERT INTO portfolio_accounts(broker,created_at) VALUES (?,?)', (broker, now)).lastrowid
        save_entries(conn, account_id, {'kind': kind, 'entries': [entry]}, {}, digest, source_name, now)
        identity = conn.execute('SELECT id FROM portfolio_ledger_entries WHERE account_id=? AND source_sha256=?',
                                (account_id, digest)).fetchone()[0]
        return {'id': identity, 'event_date': entry['event_date']}
