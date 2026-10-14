"""Resumable single-source backfill of four industry financial amounts only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .industry_service import dumps
from .industry_sources import EXTRA_FINANCIAL_FIELDS
from .industry_storage import latest_financial_rows
from .storage import Database, DEFAULT_DATABASE

BASE_FIELDS = ('revenue','parent_profit','notice_date')
EXTRA_COLUMNS = 'SECURITY_CODE,REPORT_DATE,NOTICE_DATE,'+','.join(EXTRA_FINANCIAL_FIELDS.values())


def latest_facts(db, period=None):
    with db.connection() as conn:
        rows = latest_financial_rows(conn,period)
        return {(r['stock_code'],r['period']):dict(r) for r in rows}


def base_fingerprint(facts):
    rows = [(s,p,*(r[f] for f in BASE_FIELDS)) for (s,p),r in sorted(facts.items())
            if any(r[f] is not None for f in BASE_FIELDS)]
    return hashlib.sha256(dumps(rows).encode()).hexdigest()


def collect_period(service, foundation, period, directory, pacer, progress):
    """Retired legacy raw requester; use the shared public financial collector."""
    raise ValueError('旧行业扩展财务分页入口已停用；请使用 market_financial.collect_public。')


def report(directory, result):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'financial_extensions.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines = ['# 行业财务四字段补采报告','',f"状态：{result['status']}；范围：{result['start']}—{result['end']}。",
             '', '单位元，合并／归母按指标定义，本年累计。新增四字段统一取东财；未返回值留空，有效零保留。',
             '当前沪深名单回溯，不包含已退市股票；名单总数不作为历史漏采分母。',
             'OPERATE_INCOME 缺失时保持空，不无条件替换为营业总收入。毛利率未存储，金融行业不直接套用。',
             '下表四个财务字段列为有有效值的公司家数，不是金额；没有返回适用历史财报的公司不计入覆盖，来源提供的上市前财报仍保留。',
             '',f"原有营收、归母利润和披露日保持一致：{result.get('base_metrics_unchanged',False)}。",
             f"备份：{result.get('backup','尚未发布')}。",'',
             '| 报告期 | 来源记录数 | 当前名单匹配（家） | 营业收入覆盖（家） | 营业成本覆盖（家） | 扣非归母利润覆盖（家） | 营业利润覆盖（家） |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for row in result['periods']:
        counts=row['field_counts']
        lines.append('| '+ ' | '.join(map(str,[row['period'],row['source_count'],row['matched_company_count'],
                      *(counts[m] for m in EXTRA_FINANCIAL_FIELDS)]))+' |')
    if result.get('error'):
        lines+=['',f"错误：{result['error']}"]
    (directory/'financial_extensions.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def run(db, directory, report_directory, *, all_history=False, publish=False):
    """Retired legacy online publisher."""
    raise ValueError('旧行业扩展财务联网入口已停用；请使用 market_financial_backfill 公共四表入口。')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report-directory',type=Path,required=True)
    parser.add_argument('--all-history',action='store_true')
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    try:
        run(Database(args.database),args.directory,args.report_directory,all_history=args.all_history,publish=args.publish)
    except ValueError as exc:
        parser.error(str(exc))
