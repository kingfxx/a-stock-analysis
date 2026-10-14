import subprocess
import sys
from pathlib import Path

import pytest

from quarterly_dashboard import industry_bulk, industry_financial_extensions
from quarterly_dashboard import industry_cap_history, industry_performance_backfill, industry_sources, market_financial


@pytest.mark.parametrize(('entry', 'args', 'message'), [
    (industry_sources.download, (None,), 'industry_memberships'),
    (market_financial.collect_dataset, (None, None, 'income', '2026-06-30', None, None), 'collect_public'),
    (industry_financial_extensions.run, (None, None, None), 'market_financial_backfill'),
    (industry_financial_extensions.collect_period, (None, None, '2026-06-30', None, None, None), 'collect_public'),
    (industry_performance_backfill.run, (None, None, None), 'market_financial_backfill'),
    (industry_performance_backfill.collect_period, (None, None, '2026-06-30', None, None, None), 'collect_public'),
    (industry_bulk.collect, (None, None), '已停用'),
])
def test_legacy_online_entrypoints_stop_before_network_or_database(entry, args, message):
    with pytest.raises(ValueError, match=message):
        entry(*args)


def test_offline_financial_parser_remains_available():
    rows = industry_sources.financial_rows([{
        'SECURITY_CODE': '600519', 'REPORT_DATE': '2026-06-30', 'NOTICE_DATE': None,
        'TOTAL_OPERATE_INCOME': 0, 'PARENT_NETPROFIT': -1,
    }], '2026-10-14')
    assert len(rows) == 1
    assert rows[0]['revenue'] == 0
    assert rows[0]['parent_profit'] == -1


def test_legacy_classification_cli_prints_public_entry_guidance(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-m', 'quarterly_dashboard.industry_sources', str(tmp_path / 'unused')],
        cwd=repo, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert 'industry_memberships' in result.stderr


def test_cap_history_requires_local_evidence_instead_of_legacy_baidu_request(tmp_path):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params):
            if 'raw_daily_prices' in sql:
                return [(day,) for day in industry_cap_history.TARGETS]
            return []

    class ReadOnlyDatabase:
        def connection(self):
            return Connection()

    bundle = {
        'members': [{'stock_code': '600519', 'industry_code': industry_cap_history.PILOT_INDUSTRIES[0]}],
        'caps': [{'stock_code': '600519', 'trade_date': '2026-10-13', 'total_cap': 1e12,
                  'provenance': {'source': 'tencent:qt.gtimg.cn', 'field': '45'}}],
    }
    source = tmp_path / 'bundle.json'
    source.write_text(__import__('json').dumps(bundle), encoding='utf-8')
    with pytest.raises(ValueError, match='公共 RequestCollector'):
        industry_cap_history.build(ReadOnlyDatabase(), source, tmp_path / 'output')
