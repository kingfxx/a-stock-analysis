"""Validate cached history, stage in isolation, publish, and write a data report."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from quarterly_dashboard.industry_bulk import publish
from quarterly_dashboard.industry_service import IndustryService, dumps
from quarterly_dashboard.storage import Database, DEFAULT_DATABASE

TABLES = ('instruments', 'financial_reports', 'valuation_observations', 'raw_daily_prices', 'ai_analysis_runs')


def fingerprint(db):
    result = {}
    with db.connection() as conn:
        for table in TABLES:
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                digest.update(dumps(list(row)).encode())
                count += 1
            result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def verify(db, collection):
    service = IndustryService(db)
    with db.connection() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    totals = {}
    for level in (1, 2, 3):
        period = max(c['period'] for c in collection['manifest']['checks'] if c['kind'] == 'financial')
        data = service.read(level=level, mode='ytd', period=period)
        totals[level] = sum(r['revenue_known'] or 0 for r in data['ranking'])
    assert max(totals.values()) - min(totals.values()) < max(totals.values()) * 1e-10
    ttm = service.read(level=3, mode='ttm')
    eligible = [r for r in ttm['ranking'] if r['rank_eligible']]
    return {'integrity': 'ok', 'foreign_keys': 'ok', 'revenue_totals_by_level': totals,
            'ttm_default_period': ttm['period'], 'ttm_eligible_industries': len(eligible),
            'ttm_industry_count': len(ttm['ranking']),
            'ttm_top10': [{k: r[k] for k in ('code', 'name', 'revenue_known', 'revenue_yoy',
                         'parent_profit_yoy', 'revenue_matched_coverage', 'parent_profit_matched_coverage')} for r in eligible[:10]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--stage', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    collection = json.loads((args.directory / 'collection.json').read_text(encoding='utf-8'))
    assert len(collection['caps']) == 40
    assert len({(r['stock_code'], r['period']) for r in collection['financials']}) == len(collection['financials'])
    for source in collection['manifest']['files']:
        assert hashlib.sha256(Path(source['file']).read_bytes()).hexdigest() == source['sha256']
    db = Database(args.database)
    before = fingerprint(db)
    report_path = ROOT / 'data/verification/reports/sw_history_20261003.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    if args.stage:
        stage = ROOT / 'data/verification/reports/sw-history-stage.sqlite3'
        if stage.exists():
            raise ValueError('测试库已存在，请确认并清理后再运行')
        db.backup(stage)
        stage_db = Database(stage)
        ids = publish(IndustryService(stage_db), collection)
        assert fingerprint(stage_db) == before
        report['staging'] = {**verify(stage_db, collection), 'import_ids': ids, 'individual_tables_unchanged': True}
        print('隔离库历史导入及向上汇总验证通过', flush=True)
    if args.publish:
        assert report.get('staging', {}).get('integrity') == 'ok'
        backup = db.backup(db.path.parent / 'backups' / ('before-sw-history-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3'))
        ids = publish(IndustryService(db), collection)
        assert fingerprint(db) == before
        report['publication'] = {**verify(db, collection), 'backup': str(backup), 'import_ids': ids,
                                 'individual_tables_unchanged': True, 'individual_tables': before}
        print('日常库行业历史已发布，原个股表哈希一致', flush=True)
    report['manifest'] = {k: v for k, v in collection['manifest'].items() if k != 'files'}
    report['source_files'] = len(collection['manifest']['files'])
    report['financial_records'] = len(collection['financials'])
    report['cap_records'] = sum(len(c['rows']) for c in collection['caps'])
    report['collection_sha256'] = hashlib.sha256((args.directory / 'collection.json').read_bytes()).hexdigest()
    report['missing'] = {}
    universe = {r['stock_code'] for r in collection['foundation']['members']}
    report['unclassified_stocks'] = [r['stock_code'] for r in collection['foundation']['members'] if not r['industry_code']]
    for c in collection['caps']:
        report['missing'][c['quarter']] = sorted(universe - {r['stock_code'] for r in c['rows']})
    report['financial_missing'] = {}
    for c in collection['manifest']['checks']:
        if c['kind'] != 'financial':
            continue
        rows = [r for r in collection['financials'] if r['period'] == c['period']]
        report['financial_missing'][c['period']] = {
            metric: sorted(universe - {r['stock_code'] for r in rows if r[metric] is not None})
            for metric in ('revenue', 'parent_profit')}
    if args.publish:
        with db.connection() as conn:
            report['industry_table_counts'] = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('sw_financial_facts', 'sw_cap_facts', 'sw_industry_cap_quarters',
                              'sw_memberships', 'sw_cap_quarter_rosters', 'sw_cap_quarter_members')}
        report['database_bytes'] = db.path.stat().st_size
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    checks = collection['manifest']['checks']
    lines = ['# 申万行业近十年数据补采报告', '', '采集基准日：2026-10-03。', '',
             '财务：2016Q3—2026Q2，40 个报告期；市值：2016Q4—2026Q3，40 个季末目标。', '',
             f"当前沪深股票名单 {len(universe):,} 家；轻量财务 {report['financial_records']:,} 条，季度市值 {report['cap_records']:,} 条。", '',
             f"当前未分类股票 {len(report['unclassified_stocks'])} 家，不强行归入三级行业；其代码见 JSON 报告。", '',
             '## 来源和口径', '',
             '数据入口：[东方财富数据中心](https://data.eastmoney.com/)；[申万 2021 分类](https://www.swsresearch.com/swindex/pdf/SwClass2021/SwClassCode_2021.xls)。实际请求参数、完整响应文件及 SHA256 见业务来源目录 collection.json 的 manifest.files。', '',
             '- 东财 RPT_DMSK_FN_INCOME：TOTAL_OPERATE_INCOME（合并营业总收入）、PARENT_NETPROFIT（归母净利润），元、本年累计。同一期有效本地新浪指标优先复用。',
             '- 东财 RPT_VALUEANALYSIS_DET：TOTAL_MARKET_CAP（总市值），元；与总股本乘不复权收盘价核对。非交易日选季末之前最后交易日，不跨入下一季度。',
             '- 仅回溯当前沪深名单，排除北交所及已退市公司；来源当前修订值不代表历史时点已知信息，不能作为无偏回测数据。早期季度按当前申万三级分类回填，原已固定的季度分类口径保留。',
             '- 缺失不是零。各期已覆盖合计不能视作完整行业总额；较早年份未上市和来源缺失均可能导致当前名单覆盖低，尚未逐家公司区分原因。',
             '- 窗口最早几期可能缺少单季度差分、TTM 或同比所需的前置基期；这些派生指标继续留空，不自动扩大采集年份。',
             '- 公司仅保存营收、归母利润、季度市值及来源；不增加完整财报。三级市值已有落库汇总，营收、利润仍由轻量明细读取汇总，一级二级向上汇总。', '',
             '## 请求与复用', '', collection['manifest']['pacing'] + '。', '',
             f"本次补采累计至少 {collection['manifest']['network_requests']} 次东财请求（前序断点按成功缓存页计数，本轮按实际尝试计数）；来源分页文件共 {report['source_files']} 个（包括复用最近一年缓存及明确空结果）。", '',
             '财务阶段完成后，腾讯指数请求因缺少末尾复权占位参数返回 bad params；已修正并实测确认 2016-12-30 为该季末最后交易日。断点续跑复用全部已取财务页，没有重复抓取财务数据。', '',
             '早期市值来源空结果实际为 code=9201、message=返回数据为空；已根据保留的真实响应修正识别，停止无意义重试。其他网络／格式失败仍不能冒充空数据。', '',
             '## 财务逐期覆盖', '', '|报告期|来源公司数|当前名单匹配|有效营收|有效归母利润|', '|---|---:|---:|---:|---:|']
    lines.extend(f"|{c['period']}|{c['source_count']}|{c['eligible_count']}|{c['revenue_count']}|{c['profit_count']}|" for c in checks if c['kind'] == 'financial')
    lines.extend(['', '## 季末市值覆盖', '', '|季度|实际交易日|来源公司数|当前名单有效市值|当前名单覆盖|', '|---|---|---:|---:|---:|'])
    lines.extend(f"|{c['quarter']}|{c['trade_date']}|{c['source_count']}|{c['eligible_count']}|{c['eligible_count']/c['expected_count']:.1%}|" for c in checks if c['kind'] == 'cap')
    empty = [c['quarter'] for c in checks if c['kind'] == 'cap' and not c['source_count']]
    lines.extend(['', '来源明确为空的市值季度：' + ('、'.join(empty) or '无') + '；留空，不插值或使用附近不同日期市值代替。', '',
                  '## 验证和存储', '',
                  f"隔离库验证：{'通过' if report.get('staging') else '待完成'}；日常库发布：{'完成' if report.get('publication') else '待完成'}。",
                  '验证包含来源 SHA256、分页数量和重复、精确报告期／交易日、股本乘价格、SQLite 完整性及外键、三级与一级二级营收汇总一致、原五张个股业务表完整内容哈希一致。', '',
                  '行业更新与个股刷新分开，浏览页面不采集；页面更新入口仍是原两个行业试点，全市场补采使用独立命令。历史响应和 collection.json 保存于业务来源目录，详细缺失股票清单见同名 JSON 报告。'])
    if report.get('publication'):
        lines.extend(['', '发布前正式备份：`' + report['publication']['backup'] + '`。'])
        published = report['publication']
        lines.extend(['', '## 最新一期筛选功能验证', '',
                      f"TTM 默认报告期：{published['ttm_default_period']}；同比覆盖率 ≥95% 且可计算增长的三级行业：{published['ttm_eligible_industries']}/{published['ttm_industry_count']}。", '',
                      '以下仅为按当前成分、同批公司营收 TTM 同比排序的筛选结果。利润同比基期非正时留空。', '',
                      '|三级代码|行业|营收TTM同比|归母利润TTM同比|营收可比覆盖|利润可比覆盖|', '|---|---|---:|---:|---:|---:|'])
        for row in published['ttm_top10']:
            profit = '待补／基期非正' if row['parent_profit_yoy'] is None else f"{row['parent_profit_yoy']:.2f}%"
            lines.append(f"|{row['code']}|{row['name']}|{row['revenue_yoy']:.2f}%|{profit}|{row['revenue_matched_coverage']:.1%}|{row['parent_profit_matched_coverage']:.1%}|")
    if report.get('browser'):
        lines.extend(['', '## 页面与回归验收', '',
                      f"相关测试 {report['tests_passed']} 项通过。桌面与手机验收通过，浏览器脚本错误 {len(report['browser']['page_errors'])} 个，模型调用 {report['browser']['model_calls']} 次。",
                      '验证历史图表、三级与上级选择、营收口径、股票对照和个股刷新隔离；行业更新按钮仅模拟请求，没有启动额外真实采集。8765 日常后台已加载最新代码并保持运行。'])
    (report_path.with_suffix('.md')).write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
