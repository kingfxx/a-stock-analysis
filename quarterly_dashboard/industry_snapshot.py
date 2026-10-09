"""Export a consistent, versioned industry baseline without deleting raw evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .storage import DEFAULT_DATABASE


def export(database, directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=False)
    files=[]
    conn=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('BEGIN')
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'sw_*' ORDER BY name")]
        for table in tables:
            # Names come only from this database's schema, never external input.
            quoted='"'+table.replace('"','""')+'"'
            periods=[r[0] for r in conn.execute(f'SELECT DISTINCT period FROM {quoted} ORDER BY period')] if table=='sw_financial_facts' else [None]
            for period in periods:
                path=directory/(f'financial/{period}.json' if period else table+'.json')
                path.parent.mkdir(parents=True,exist_ok=True)
                cursor=conn.execute(f'SELECT f.*,f.import_id=l.id AS is_latest FROM {quoted} f JOIN '
                    f'(SELECT stock_code,max(import_id) id FROM {quoted} WHERE period=? GROUP BY stock_code) l '
                    'ON f.stock_code=l.stock_code WHERE f.period=? ORDER BY f.stock_code,f.import_id',
                    (period,period)) if period else conn.execute(f'SELECT * FROM {quoted} ORDER BY rowid')
                count=0;latest_count=0
                with path.open('w',encoding='utf-8',newline='\n') as out:
                    out.write('[\n')
                    for row in cursor:
                        item=dict(row)
                        if period:
                            item['is_latest']=bool(item['is_latest'])
                            latest_count+=item['is_latest']
                        for key in tuple(item):
                            if key.endswith('_json') and isinstance(item[key],str):
                                item[key]=json.loads(item[key])
                        if count:
                            out.write(',\n')
                        out.write(json.dumps(item,ensure_ascii=False,allow_nan=False,separators=(',',':')))
                        count+=1
                    out.write('\n]\n')
                files.append({'table':table,'period':period,'file':path.relative_to(directory).as_posix(),
                    'rows':count,'latest_rows':latest_count if period else None,
                    'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest={'schema_version':1,'exported_at':datetime.now(timezone.utc).isoformat(),
            'database_schema_version':conn.execute('PRAGMA user_version').fetchone()[0],
            'description':'行业数据一致性快照；财务按报告期保存全部修订，is_latest=true 为当前最新版本。',
            'source_resolution':'provenance_id 引用 sw_financial_provenance.json 或 sw_cap_provenance.json；保留原始响应文件引用，原始响应仍在 industry_sources。',
            'ratio_basis':'每股、ROE 和来源毛利率不做累计差分或 TTM 相加；股息率仅为采集时来源原值，价格基准未核实。',
            'financial_fields':{
                'basic_eps':{'field':'BASIC_EPS','unit':'元/股'},
                'bps':{'field':'BPS','unit':'元/股','basis':'报告期末'},
                'weighted_roe':{'field':'WEIGHTAVG_ROE','unit':'%','basis':'本年累计、加权'},
                'operating_cashflow_per_share':{'field':'MGJYXJJE','unit':'元/股'},
                'deduct_basic_eps':{'field':'DEDUCT_BASIC_EPS','unit':'元/股'},
                'dividend_yield':{'field':'ZXGXL','unit':'%','basis':'来源采集时值，价格基准未核实'},
                'reported_gross_margin':{'field':'XSMLL','unit':'%'},
                'performance_notice_date':{'field':'NOTICE_DATE','unit':'日期'}},
            'raw_evidence_included':False,'files':files}
        (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        return manifest
    finally:
        conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    args=parser.parse_args()
    manifest=export(args.database,args.directory)
    print(json.dumps({'files':len(manifest['files']),'rows':sum(f['rows'] for f in manifest['files'])}))
