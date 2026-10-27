"""Explicit task/provider pacing overrides, using the existing shared request budget."""
import json
import math
from dataclasses import asdict, replace

from stock_analysis.infrastructure.storage import utc_now

FIELDS = {
    'interval': (1, 3600, False), 'jitter': (0, 600, False),
    'window_requests': (1, 60, True), 'rest_every': (1, 1000, True),
    'rest_min': (0, 3600, False), 'rest_max': (0, 3600, False),
    'max_concurrency': (1, 2, True),
}


def concurrency_cap(key, provider):
    return 2 if (key == 'indices' and provider in ('csindex', 'sws')) or key == 'market_financial' else 1


def validate(settings, cap=2):
    if not isinstance(settings, dict) or set(settings) != set(FIELDS):
        raise ValueError('限速设置须包含全部可调参数，不能修改冷却或重试规则')
    result = {}
    for field, (minimum, maximum, integer) in FIELDS.items():
        value = settings[field]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not minimum <= value <= (cap if field == 'max_concurrency' else maximum) or not math.isfinite(value)
                or integer and value != int(value)):
            raise ValueError(f'限速参数 {field} 超出支持范围')
        result[field] = int(value) if integer else value
    if result['rest_max'] < result['rest_min']:
        raise ValueError('休息最长时间不能小于最短时间')
    return result


def fields(policy):
    return {name: getattr(policy, name) for name in FIELDS}


def baseline(key, provider):
    from stock_analysis.collection.collection import PacingPolicy, pacing_policy
    base = PacingPolicy(max_attempts=1 if key == 'market_congestion' else 3)
    return pacing_policy(provider, base, index_parallel=key == 'indices',
                         industry_profile={'market_financial': 'financial', 'trade_snapshot': 'snapshot'}.get(key))


def describe(conn, key, providers):
    from stock_analysis.collection.collection import PacingPolicy
    result = []
    for provider in providers:
        current = baseline(key, provider)
        saved = conn.execute('SELECT settings_json,updated_at FROM collection_rate_limits WHERE task_key=? AND provider=?',
                             (key, provider)).fetchone()
        default = fields(PacingPolicy())
        fast = fields(current)
        if key in ('stocks', 'market_congestion', 'sovereign_yields'):
            fast.update(interval=2, jitter=3, window_requests=30, rest_min=10, rest_max=10)
        result.append(dict(provider=provider, current=json.loads(saved['settings_json']) if saved else fields(current),
                           default=default, faster=fast, max_concurrency=concurrency_cap(key, provider),
                           max_attempts=current.max_attempts, updated_at=saved['updated_at'] if saved else None))
    return result


def save(conn, key, provider, settings):
    from stock_analysis.collection.collection_management import task
    if not isinstance(key, str) or not isinstance(provider, str):
        raise ValueError('任务及提供方须为登记的名称')
    spec = task(key)
    if spec.get('manual_only') or provider not in spec['providers']:
        raise ValueError('该任务不支持此网络提供方的限速配置')
    settings = validate(settings, concurrency_cap(key, provider))
    # Same write transaction as configuration save and operation reservations.
    if conn.execute("SELECT 1 FROM collection_operations WHERE status='running' LIMIT 1").fetchone():
        raise ValueError('有采集任务正在运行，请待结束后保存限速设置')
    stamp = utc_now()
    conn.execute('INSERT INTO collection_rate_limits VALUES(?,?,?,?) ON CONFLICT(task_key,provider) DO UPDATE SET '
                 'settings_json=excluded.settings_json,updated_at=excluded.updated_at',
                 (key, provider, json.dumps(settings, separators=(',', ':'), allow_nan=False), stamp))
    return dict(saved=True, updated_at=stamp)


def task_for_batch(conn, batch_id, provider):
    batch = conn.execute('SELECT domain,dataset,operation_id,scope_json FROM collection_batches WHERE id=?', (batch_id,)).fetchone()
    if batch and batch['domain'] == 'stock':
        return 'stocks' if json.loads(batch['scope_json']).get('batch_pacing') else None
    if not batch:
        return None
    operation = batch['operation_id'] or ''
    if operation.startswith('sw_update_runs:'):
        row = conn.execute('SELECT action FROM collection_operations WHERE id=?', (operation.split(':', 1)[1],)).fetchone()
        return {'financial_period': 'market_financial', 'cap_quarter': 'trade_snapshot'}.get(row['action']) if row and provider == 'eastmoney' else None
    row = conn.execute('SELECT scope_json FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()
    if not row:
        return None  # Unregistered CLI backfills retain their existing strategy.
    if batch['dataset'].startswith('bond_zh_us_rate') and provider == 'eastmoney':
        return 'sovereign_yields'
    scope = json.loads(row['scope_json'])
    if (scope.get('dataset') == 'market_congestion' or batch['dataset'].startswith('market.legu.congestion')) and provider == 'legu':
        return 'market_congestion'
    for key, source in (('shipping_monthly', 'mot'), ('shipping_weekly', 'sse')):
        if scope.get('dataset') == key and provider == source:
            return key
    if not scope.get('dataset') and provider in ('csindex', 'legu', 'sws'):
        return 'indices'
    return None


def for_batch(db, batch_id, provider, policy):
    """Resolve once per batch/provider; keep a compact audit snapshot in batch scope."""
    with db.connection() as conn:
        key = task_for_batch(conn, batch_id, provider)
        saved = conn.execute('SELECT settings_json FROM collection_rate_limits WHERE task_key=? AND provider=?',
                             (key, provider)).fetchone() if key else None
    if not saved:
        return policy
    settings = validate(json.loads(saved['settings_json']), concurrency_cap(key, provider))
    selected = replace(policy, **settings, provider='custom')
    with db.connection(write=True) as conn:
        row = conn.execute('SELECT scope_json FROM collection_batches WHERE id=?', (batch_id,)).fetchone()
        scope = json.loads(row['scope_json'])
        scope.setdefault('rate_limits', {})[provider] = dict(task_key=key, **asdict(selected))
        conn.execute('UPDATE collection_batches SET scope_json=? WHERE id=?',
                     (json.dumps(scope, ensure_ascii=False, separators=(',', ':')), batch_id))
    return selected
