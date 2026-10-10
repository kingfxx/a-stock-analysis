"""Bounded industry reads from the latest Eastmoney financial projection."""
from collections import OrderedDict
import json
import threading

from .statements import previous_quarter


def required_periods(period, mode):
    """Cumulative inputs for a selected flow and its same-period YoY."""
    result = set()
    for p in (period, str(int(period[:4])-1)+period[4:]):
        result.add(p)
        if mode == 'quarter':
            before = previous_quarter(p)
            if before:
                result.add(before)
        elif mode == 'ttm' and not p.endswith('12-31'):
            year = str(int(p[:4])-1)
            result.update((year+'-12-31', year+p[4:]))
    return tuple(sorted(result))


class IndustryFinancialReader:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._facts = OrderedDict()
        self._series = OrderedDict()

    @staticmethod
    def _remember(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def facts(self, member_id, nonfinancial, periods=None, stocks=None):
        with self.db.connection() as conn:
            return self._read(conn, member_id, nonfinancial, periods, stocks)

    @staticmethod
    def _read(conn, member_id, nonfinancial, periods=None, stocks=None):
        sql = ('SELECT f.* FROM market_financial_industry f JOIN sw_memberships m '
               'ON m.stock_code=f.security_code AND m.import_id=? WHERE f.exchange IN (\'sh\',\'sz\')')
        args = [member_id]
        if periods is not None:
            if not periods:
                return {}
            sql += ' AND f.report_date IN ('+','.join('?' for _ in periods)+')'
            args.extend(periods)
        if stocks is not None:
            if not stocks:
                return {}
            sql += ' AND f.security_code IN ('+','.join('?' for _ in stocks)+')'
            args.extend(stocks)
        facts = {}
        for row in conn.execute(sql, args):
            stock = row['security_code']
            values = {k:row[k] for k in ('revenue','parent_profit','operating_revenue','operating_cost',
                                        'weighted_roe','bps','income_source_id','performance_source_id')}
            values['gross_margin_applicable'] = stock in nonfinancial
            # INCOME.TOTAL_OPERATE_INCOME is consolidated cumulative yuan.
            values['gross_margin_total_revenue_equivalent'] = stock in nonfinancial
            facts[(stock,row['report_date'])] = values
        return facts

    def window(self, token, member_id, nonfinancial, period, mode):
        periods = required_periods(period, mode)
        key = (token, member_id, periods)
        with self._lock:
            if key in self._facts:
                self._facts.move_to_end(key)
                return self._facts[key]
            value = self.facts(member_id, nonfinancial, periods=periods)
            # Discard superseded publication/member versions, even before LRU eviction.
            for cache in (self._facts,self._series):
                for old in list(cache):
                    if old[:2] != key[:2]:
                        del cache[old]
            return self._remember(self._facts, key, value, 2)

    def series(self, token, member_id, nonfinancial, stocks, periods, mode, aggregate):
        key = (token, member_id, tuple(stocks), tuple(periods), mode)
        with self._lock:
            if key in self._series:
                self._series.move_to_end(key)
                return self._series[key]
            inputs = sorted({p for period in periods for p in required_periods(period,mode)})
            facts = self.facts(member_id, nonfinancial, periods=inputs, stocks=stocks)
            result = [aggregate(stocks,facts,p,mode) for p in periods]
            return self._remember(self._series,key,result,8)

    def provenance(self, facts, stock, period):
        source_id = facts.get((stock,period),{}).get('income_source_id')
        if source_id is None:
            return {}
        with self.db.connection() as conn:
            source = conn.execute('SELECT * FROM market_financial_sources WHERE id=?',(source_id,)).fetchone()
        if source is None:
            return {}
        common = {'source':'eastmoney:RPT_DMSK_FN_INCOME','report_type':'利润表',
                  'unit':'元','basis':'本年累计','scope':'合并','method':'直接取数',
                  'period':period,'source_id':source_id,'obtained_at':source['obtained_at'],
                  'url':source['url'],'params':json.loads(source['params_json']),
                  'file_path':source['file_path'],'content_hash':source['content_hash']}
        return {metric:{**common,'field':field} for metric,field in
                [('revenue','TOTAL_OPERATE_INCOME'),('parent_profit','PARENT_NETPROFIT')]}
