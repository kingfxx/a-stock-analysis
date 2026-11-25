"""Read local forecast facts and append immutable, reproducible snapshots."""
from hashlib import sha256
import hmac
import json
from pathlib import Path

from stock_analysis.collection.collection import encoded
from stock_analysis.domains.valuation.dividend_forecast import calculate_scenario, read_dividend_forecast
from stock_analysis.infrastructure.storage import utc_now

from .market_references import MarketReferences
from .model import (LEGACY_MODEL_VERSION, MODEL_VERSION, PREVIOUS_MODEL_VERSION, QUOTE_BASES, REMAINING_QUARTERS,
                    SCENARIOS, SCHEMA_VERSION, automatic_draft, calculate_profit, remaining_prices, validate)
from .quotes import AluminumQuotes, PRODUCTS
from .sensitivity import attributable_sensitivity


CODE = '601600'
TABLE = 'research_aluminum_forecast_versions'


def _normalize(value):
    # JSON numbers round-trip through the browser as one type.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _identifier(value):
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError('版本编号无效')
    return value


def _current_year_facts(reports, year, annual):
    periods = {row['period']: dict(period=row['period'], profit=row['profit'], detail=row['sources']['profit'])
               for row in reports if row['profit'] is not None and row['period'][5:] in REMAINING_QUARTERS}
    current = [row for row in periods.values() if row['period'][:4] == str(year)]
    if not current:
        return None
    actual = max(current, key=lambda row: row['period'])
    suffix = actual['period'][4:]
    prior = {str(row['year']): periods[f"{row['year']}{suffix}"] for row in annual
             if row['year'] < year and f"{row['year']}{suffix}" in periods}
    return dict(year=year, actual=actual, prior=prior)


def calculate(base, start, scenarios, current=None, annual_equity=None):
    years = list(range(start, start + 3))
    results = {}
    for key, inputs in scenarios.items():
        equity = (annual_equity or {}).get(str(start - 1), {}).get('value')
        profits = [calculate_profit(base, row, current if current and year == current['year'] else None)
                   for year, row in zip(years, inputs)]
        dividend = calculate_scenario(years, dict(mode='linked', base_profit=base['profit'], rows=[
            dict(profit=profit['profit'], payout_ratio=row['payout'], shares=row['shares'])
            for profit, row in zip(profits, inputs)
        ]), base['share_price'])
        for row, profit in zip(dividend['rows'], profits):
            row['parts'] = profit['parts']
            if 'progress' in profit:
                row['progress'] = profit['progress']
            if annual_equity is not None:
                # Roll full-year retained profit from the previous December close.
                equity = equity + row['profit'] - row['cash'] if None not in (equity, row['profit'], row['cash']) else None
                price, shares = base['share_price'], row['shares']
                market_cap = price * shares if price and price > 0 and shares and shares > 0 else None
                row['parent_equity'] = equity
                row['pe'] = market_cap / row['profit'] if market_cap is not None and row['profit'] and row['profit'] > 0 else None
                row['pb'] = market_cap / equity if market_cap is not None and equity and equity > 0 else None
        cash = [row['cash'] for row in dividend['rows']]
        dividend['cumulative_cash'] = sum(cash) if None not in cash else None
        results[key] = dividend
    return results


class AluminumForecast:
    def __init__(self, db, secret, quotes=None):
        self.db = db
        self.quotes = quotes or AluminumQuotes(db)
        self.references = MarketReferences(db)
        self._secret = secret.encode()
        resources = Path(__file__).with_name('resources')
        self._reference = json.loads((resources / 'sensitivity.json').read_text(encoding='utf-8'))
        self._automatic = json.loads((resources / 'automatic.json').read_text(encoding='utf-8'))

    def expire_idle(self):
        return self.quotes.expire_idle() + self.references.expire_idle()

    def _sign(self, kind, value):
        return hmac.new(self._secret, encoded([kind, _normalize(value)]).encode(), sha256).hexdigest()

    def _verify(self, kind, value, proof):
        if not isinstance(proof, str) or not hmac.compare_digest(self._sign(kind, value), proof):
            raise ValueError('输入依据无法核验，请重新载入或明确改用手填值')

    def _quote_state(self, state):
        return dict(state, quotes={key: dict(value=quote, proof=self._sign('quote', quote))
                                   for key, quote in state['quotes'].items()})

    def refresh_quotes(self):
        state = self._quote_state(self.quotes.refresh())
        return dict(state, references=self.references.refresh())

    def directory(self, before=None):
        where, params = (' WHERE id<?', (_identifier(before),)) if before is not None else ('', ())
        with self.db.connection() as conn:
            rows = conn.execute(f'SELECT id,name,forecast_start_year,model_version,schema_version,'
                                f'created_at,parent_version_id FROM {TABLE}{where} ORDER BY id DESC LIMIT 101',
                                params).fetchall()
        return dict(versions=[dict(row) for row in rows[:100]], next_before=rows[99]['id'] if len(rows) > 100 else None)

    def read(self):
        facts = read_dividend_forecast(self.db, CODE, include_annual_equity=True)
        annual = [dict(year=int(row['period'][:4]), profit=row['profit'], detail=row['sources']['profit'])
                  for row in facts['reports'] if row['period'].endswith('-12-31') and row['profit'] is not None]
        market = facts['market']
        year = facts['years'][0]
        progress = _current_year_facts(facts['reports'], year, annual)
        evidence = dict(annual=annual, market=market, sensitivity=self._reference, automatic=self._automatic,
                        as_of=facts['as_of_date'], current_year=progress, annual_equity=facts['annual_equity'])
        state = self.quotes.latest()
        defaults = automatic_draft(annual, self._automatic, state['quotes'], market, year, progress)
        return dict(self.directory(), code=CODE, evidence=evidence, proof=self._sign('facts', evidence),
                    defaults=defaults, quotes=dict(self._quote_state(state), references=self.references.latest()),
                    warnings=facts['warnings'],
                    dividends=facts['history'], model_version=MODEL_VERSION, schema_version=SCHEMA_VERSION)

    def read_version(self, version_id):
        with self.db.connection() as conn:
            row = conn.execute(f'SELECT * FROM {TABLE} WHERE id=?', (_identifier(version_id),)).fetchone()
        if not row:
            raise LookupError('版本不存在')
        version = dict(row)
        version['snapshot'] = snapshot = json.loads(version.pop('snapshot_json'))
        compatible = (version['model_version'] in (MODEL_VERSION, PREVIOUS_MODEL_VERSION, LEGACY_MODEL_VERSION)
                      and version['schema_version'] == SCHEMA_VERSION)
        if compatible:
            context = snapshot['context']
            evidence = dict(annual=[context['financial']] if context['financial'] else [],
                            market=context['market'], sensitivity=context['sensitivity'], as_of=context['as_of'])
            if 'automatic' in context:
                evidence['automatic'] = context['automatic']
            if 'current_year' in context:
                evidence['current_year'] = context['current_year']
            if 'annual_equity' in context:
                evidence['annual_equity'] = context['annual_equity']
            registry = {key: dict(value=quote, proof=self._sign('quote', quote))
                        for key, quote in context['quote_registry'].items()}
            version.update(evidence=evidence, proof=self._sign('facts', evidence), quote_registry=registry)
        return dict(version, compatible=compatible)

    def _verified_quotes(self, command, scenarios):
        registry = {}

        def add(product, entry, *, inputs=None):
            if product not in PRODUCTS or not isinstance(entry, dict) or set(entry) != {'value', 'proof'}:
                raise ValueError('行情依据无效')
            quote = entry['value']
            self._verify('quote', quote, entry['proof'])
            if (not quote['contract'].lower().startswith(product)
                    or inputs is not None and quote['price'] != inputs[product + '_price']):
                raise ValueError('所采用行情与预测均价不一致')
            key = f"{quote['source_id']}:{quote['contract']}"
            registry[key] = quote
            return key

        quotes, adopted = command['quotes'], command['adopted']
        if not isinstance(quotes, dict) or not isinstance(adopted, dict) or set(adopted) != set(SCENARIOS):
            raise ValueError('行情依据无效')
        current = {product: add(product, entry) for product, entry in quotes.items()}
        used = {}
        for key, rows in adopted.items():
            if not isinstance(rows, list) or len(rows) != 3 or any(not isinstance(row, dict) for row in rows):
                raise ValueError('年度行情依据无效')
            used[key] = [{product: add(product, entry, inputs=inputs)
                          for product, entry in row.items()} for row, inputs in zip(rows, scenarios[key])]
        return registry, current, used

    def save(self, command):
        fields = {'name', 'note', 'parent_version_id', 'start', 'base', 'scenarios',
                  'evidence', 'proof', 'quotes', 'adopted'}
        if not isinstance(command, dict) or set(command) != fields:
            raise ValueError('保存参数无效')
        name, note, parent = command['name'], command['note'], command['parent_version_id']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('请输入 1–100 字的方案名称')
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError('备注最多 2000 字')
        if parent is not None:
            _identifier(parent)
        start, base, scenarios = command['start'], command['base'], command['scenarios']
        validate(base, start, scenarios)
        evidence = command['evidence']
        self._verify('facts', evidence, command['proof'])
        reference = evidence['sensitivity']
        for rows in scenarios.values():
            for row in rows:
                if row['reference'] is not None:
                    if not reference['available']:
                        raise ValueError('当前无可采用的历史参考值')
                    expected = attributable_sensitivity(reference['slope'], **row['reference'])
                    if row['al_sensitivity'] != expected:
                        raise ValueError('历史参考值与采用的假设不一致；手动调整后按手填保存')
        registry, quotes, adopted = self._verified_quotes(command, scenarios)
        if base['price_basis'] not in QUOTE_BASES and any(row for rows in adopted.values() for row in rows):
            raise ValueError('采用期货报价须选择与上期所一致的价格口径')
        progress = evidence.get('current_year')
        if (progress and start <= progress['year'] <= start + 2
                and REMAINING_QUARTERS[progress['actual']['period'][5:]]):
            comparison = progress['prior'].get(str(base['year']))
            if not comparison:
                raise ValueError('所选基准年缺少同报告期归母净利润，无法计算当年剩余季度')
            prices = remaining_prices(base, comparison)
            if any(prices.get(key) is None for key in ('al_price', 'ao_price')):
                raise ValueError('请补齐基准年剩余期间的沪铝和氧化铝均价')
        equity = evidence.get('annual_equity')
        results = calculate(base, start, scenarios, progress, equity)
        if any(row['profit'] is None for result in results.values() for row in result['rows']):
            raise ValueError('请补齐三个情景共九年的利润参数')
        anchor = next((row for row in evidence['annual'] if row['year'] == base['year']), None)
        context = dict(code=CODE, start=start, base=base, financial=anchor, market=evidence['market'],
                       sensitivity=reference, as_of=evidence['as_of'], quotes=quotes,
                       quote_registry=registry, adopted=adopted,
                       profit_origin='本地财报' if anchor and anchor['profit'] == base['profit'] else '手填',
                       share_price_origin='本地收盘价' if evidence['market']['price'] == base['share_price'] else '手填')
        if 'automatic' in evidence:
            context['automatic'] = evidence['automatic']
        if equity is not None:
            context['annual_equity'] = {key: value for key, value in equity.items() if key == str(start - 1)}
        if progress and start <= progress['year'] <= start + 2:
            context['current_year'] = dict(progress, prior={
                key: value for key, value in progress['prior'].items() if key == str(base['year'])})
        input_hash = sha256(encoded(_normalize([MODEL_VERSION, SCHEMA_VERSION, context, scenarios])).encode()).hexdigest()
        snapshot = dict(context=context, scenarios=scenarios, results=results, note=note)
        created_at = utc_now()
        with self.db.connection(write=True) as conn:
            version_id = conn.execute(f'INSERT INTO {TABLE}(name,forecast_start_year,model_version,schema_version,'
                                      'input_hash,snapshot_json,created_at,parent_version_id) VALUES(?,?,?,?,?,?,?,?)',
                                      (name.strip(), start, MODEL_VERSION, SCHEMA_VERSION, input_hash,
                                       encoded(snapshot), created_at, parent)).lastrowid
        version = dict(id=version_id, name=name.strip(), forecast_start_year=start, model_version=MODEL_VERSION,
                       schema_version=SCHEMA_VERSION, created_at=created_at, parent_version_id=parent)
        return dict(id=version_id, version=version, results=results)

    def delete(self, version_id):
        with self.db.connection(write=True) as conn:
            deleted = conn.execute(f'DELETE FROM {TABLE} WHERE id=?', (_identifier(version_id),))
            if deleted.rowcount != 1:
                raise LookupError('版本不存在')
        return dict(deleted=version_id)
