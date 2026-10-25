"""Resumable backfill of reported performance indicators, preserving existing amounts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from stock_analysis.domains.industries.industry_service import dumps
from stock_analysis.domains.industries.industry_sources import EXTRA_FINANCIAL_FIELDS
from stock_analysis.domains.industries.industry_storage import latest_financial_rows
from stock_analysis.infrastructure.storage import Database, DEFAULT_DATABASE

BASE_FIELDS = ('revenue','parent_profit','notice_date',*EXTRA_FINANCIAL_FIELDS)


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
    raise ValueError('旧业绩指标分页入口已停用；请使用 market_financial.collect_public。')


def report(directory,result):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'performance_fields.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


def run(db, directory, report_directory, *, all_history=False, publish=False):
    """Retired legacy online publisher."""
    raise ValueError('旧业绩指标联网入口已停用；请使用 market_financial_backfill 公共四表入口。')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report-directory',type=Path,required=True)
    parser.add_argument('--all-history',action='store_true')
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--snapshot-directory',type=Path)
    args=parser.parse_args()
    db=Database(args.database)
    try:
        run(db,args.directory,args.report_directory,all_history=args.all_history,publish=args.publish)
    except ValueError as exc:
        parser.error(str(exc))
    if args.snapshot_directory:
        if not args.publish:
            raise ValueError('快照要求先发布补采数据')
        from stock_analysis.domains.industries.industry_snapshot import export
        manifest=export(db.path,args.snapshot_directory)
        print(dumps({'snapshot':str(args.snapshot_directory),'files':len(manifest['files'])}),flush=True)
