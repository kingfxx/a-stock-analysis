"""Local database inventory and explicitly requested space compaction."""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from time import monotonic
import shutil
import sqlite3
import tempfile

from .sqlite_sizes import physical_sizes


def compact_database(db, validate):
    """VACUUM outside a transaction, while holding an exclusive SQLite lock."""
    with db.connection():
        pass  # Validate the project schema before opening an autocommit connection.
    conn=sqlite3.connect(db.path.resolve().as_uri()+'?mode=rw',uri=True,timeout=1,isolation_level=None)
    conn.row_factory=sqlite3.Row
    try:
        if conn.execute('PRAGMA journal_mode').fetchone()[0]!='delete':
            raise ValueError('当前仅支持回滚日志模式的数据库空间整理')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA locking_mode=EXCLUSIVE')
        conn.execute('BEGIN EXCLUSIVE')
        validate(conn)
        before=db.path.stat().st_size
        required=2*before
        for folder in (db.path.parent,tempfile.gettempdir()):
            if shutil.disk_usage(folder).free<required:
                raise ValueError('数据库目录或临时目录磁盘空间不足，需至少预留当前数据库大小的两倍空闲空间')
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('数据库完整性检查未通过，未执行空间整理')
        conn.execute('COMMIT')  # EXCLUSIVE locking mode retains the lock until close.
        conn.execute('VACUUM')
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('整理后完整性检查未通过，请检查后台日志')
        after=db.path.stat().st_size
        return {'before_bytes':before,'after_bytes':after,'released_bytes':max(0,before-after),
                'free_bytes':conn.execute('PRAGMA freelist_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]}
    finally:
        conn.close()

DESCRIPTIONS={
 'instruments':('个股数据','股票代码、交易所与名称'),
 'financial_reports':('个股数据','新浪关键指标和三表原始财报，按公司及报告期保存'),
 'raw_daily_prices':('个股数据','不复权日线行情及来源记录'),
 'adjusted_daily_prices':('个股数据','各版本复权日线价格'),
 'adjusted_price_versions':('个股数据','复权价格版本及覆盖范围'),
 'valuation_observations':('个股数据','市值、估值、股息率等时点观测'),
 'dividend_events':('个股数据','分红方案及实施事件'),
 'financing_daily':('个股数据','融资融券日度数据'),
 'shareholder_observations':('个股数据','股东户数观测及公告信息'),
 'industry_snapshots':('个股数据','个股历史行业分类快照'),
 'sw_financial_facts':('行业数据','全市场轻量财务：六项金额、每股指标、加权 ROE、来源毛利率、股息率和公告日期，含修订版本'),
 'market_financial_income':('行业数据','全 A 股东财摘要利润表全部返回字段、来源引用及真实修订版本'),
 'market_financial_balance':('行业数据','全 A 股东财摘要资产负债表全部返回字段、来源引用及真实修订版本'),
 'market_financial_cashflow':('行业数据','全 A 股东财摘要现金流量表全部返回字段、来源引用及真实修订版本'),
 'market_financial_performance':('行业数据','全 A 股东财业绩指标全部返回字段（含每股指标、加权 ROE）、来源及真实修订版本'),
 'market_financial_industry':('行业数据','行业页面最新东财财务字段逻辑视图；不存储副本，不含行业或周期计算'),
 'market_financial_batches':('运行与配置','全市场三表及业绩指标采集批次、覆盖统计、状态与错误'),
 'market_financial_sources':('行业数据','全市场财务分页请求、gzip 原始响应路径、内容哈希及采集时间'),
 'sw_financial_provenance':('行业数据','共享财务来源说明；明细通过来源 ID 引用'),
 'sw_cap_facts':('行业数据','公司季末总市值明细，含来源与修订版本'),
 'sw_cap_provenance':('行业数据','共享市值来源说明；明细通过来源 ID 引用'),
 'sw_industry_cap_quarters':('行业数据','三级行业季度市值汇总及覆盖家数'),
 'sw_industries':('行业数据','申万三级行业分类及父子关系'),
 'sw_imports':('行业数据','行业采集批次、来源清单与导入版本'),
 'sw_memberships':('行业数据','行业成分公司名单、分类版本及 A 股上市日期'),
 'sw_listing_sources':('行业数据','上市日期共享来源响应、字段定义及采集时间'),
 'sw_membership_checks':('运行与配置','全市场任务前每日名单检查、结果及失败记录'),
 'sw_membership_history':('行业数据','来源提供的历史分类变更'),
 'sw_cap_quarter_rosters':('行业数据','固定季度成员版本、实际季末交易日和统计口径'),
 'sw_cap_quarter_members':('行业数据','固定季度的公司及行业归属'),
 'sw_update_runs':('运行与配置','独立行业更新任务、进度、结果与错误'),
 'ai_analysis_runs':('分析与财报','Checklist 和历史分析任务、结果、用量'),
 'ai_analysis_snapshots':('分析与财报','分析输入快照及质量、来源信息'),
 'ai_analysis_preferences':('运行与配置','模型及分析配置'),
 'company_report_documents':('分析与财报','正式财报 PDF 的版本、路径、哈希和披露信息'),
 'company_report_parses':('分析与财报','财报解析版本、页数和质量信息'),
 'company_report_facts':('分析与财报','财报主题提取结果及版本'),
 'sync_runs':('运行与配置','个股数据采集任务及执行结果'),
 'sync_state':('运行与配置','采集覆盖范围、检查时间及成功版本'),
 'schema_migrations':('运行与配置','数据库结构迁移记录及校验值'),
 'report_overrides':('个股数据','财报科目人工修正值及来源'),
 'legacy_imports':('运行与配置','旧数据文件导入记录'),
 'legacy_valuation_snapshots':('个股数据','旧估值月度快照及计算口径'),
 'price_version_leases':('运行与配置','价格版本使用租约及到期时间'),
 'stock_groups':('运行与配置','股票分组及排列顺序'),
 'stock_group_members':('运行与配置','分组内股票及顺序'),
 'stock_picker_preferences':('运行与配置','股票选择器偏好'),
 'stock_recent_views':('运行与配置','最近浏览的股票'),
 'stock_unfollowed':('运行与配置','已取消关注的股票及取消时间；来源数据继续保留'),
 'stock_unfollowed_groups':('运行与配置','取消关注前的分组归属及顺序，用于恢复关注'),
 'stock_cleanup_runs':('运行与配置','未关注股票清理的提交记录、范围、备份及结果；用于中断恢复'),
}
TIME_COLUMNS=('updated_at','obtained_at','checked_at','completed_at','finished_at','validated_at',
              'captured_at','imported_at','applied_at','viewed_at','created_at','unfollowed_at')
PARENT_TIMES={
 'stock_unfollowed_groups':('stock_unfollowed','instrument_id','instrument_id','unfollowed_at'),
 'adjusted_daily_prices':('adjusted_price_versions','version_id','id','created_at'),
 'industry_snapshots':('sync_runs','run_id','id','finished_at'),
 'sw_financial_facts':('sw_imports','import_id','id','obtained_at'),
 'sw_cap_facts':('sw_imports','import_id','id','obtained_at'),
 'sw_industry_cap_quarters':('sw_imports','import_id','id','obtained_at'),
 'sw_memberships':('sw_imports','import_id','id','obtained_at'),
 'sw_membership_history':('sw_imports','import_id','id','obtained_at'),
 'sw_cap_quarter_rosters':('sw_imports','member_import_id','id','obtained_at'),
}


def quote(name):
    return '"'+name.replace('"','""')+'"'


def table_time(conn, name, columns):
    if name=='sw_memberships':
        values=conn.execute('SELECT max(i.obtained_at),max(s.obtained_at) FROM sw_memberships m '
            'JOIN sw_imports i ON i.id=m.import_id LEFT JOIN sw_listing_sources s ON s.id=m.listing_source_id').fetchone()
        return max((v for v in values if v),default=None),'名单批次及上市日期来源采集时间'
    fields=[c for c in TIME_COLUMNS if c in columns]
    if fields:
        # Take max over the separate non-null aggregates.
        values=conn.execute('SELECT '+','.join(f'max({quote(c)})' for c in fields)+' FROM '+quote(name)).fetchone()
        return max((v for v in values if v),default=None),', '.join(fields)
    if name=='sw_cap_quarter_members':
        value=conn.execute('SELECT max(i.obtained_at) FROM sw_cap_quarter_members m '
            'JOIN sw_cap_quarter_rosters r ON r.quarter=m.quarter JOIN sw_imports i ON i.id=r.member_import_id').fetchone()[0]
        return value,'季度名单关联的采集批次时间'
    if name in ('sw_financial_provenance','sw_cap_provenance'):
        facts='sw_financial_facts' if name=='sw_financial_provenance' else 'sw_cap_facts'
        value=conn.execute(f'SELECT max(i.obtained_at) FROM {name} p '
            f'JOIN {facts} f ON f.provenance_id=p.id JOIN sw_imports i ON i.id=f.import_id').fetchone()[0]
        return value,('财务' if name=='sw_financial_provenance' else '市值')+'明细关联的采集批次时间'
    if name in PARENT_TIMES:
        parent,child_key,parent_key,column=PARENT_TIMES[name]
        value=conn.execute(f'SELECT max(p.{quote(column)}) FROM {quote(name)} c JOIN {quote(parent)} p '
                           f'ON c.{quote(child_key)}=p.{quote(parent_key)}').fetchone()[0]
        return value,f'{parent}.{column}（关联批次）'
    return None,'未记录写入时间'


class MaintenanceService:
    def __init__(self, db):
        self.db=db;self._lock=Lock();self._snapshot=None;self._at=0

    def read(self, refresh=False):
        with self._lock:
            if self._snapshot and not refresh and monotonic()-self._at<60:
                return self._snapshot
            data=self._read()
            self._snapshot=data;self._at=monotonic()
            return data

    def _read(self):
        with self.db.connection() as conn:
            objects=[dict(r) for r in conn.execute("SELECT name,type,tbl_name,rootpage FROM sqlite_master "
                                                 "WHERE type IN ('table','index') ORDER BY name")]
            views=[]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"):
                name=row['name']
                columns=[r['name'] for r in conn.execute('PRAGMA table_info('+quote(name)+')')]
                updated,basis=table_time(conn,name,columns)
                category,description=DESCRIPTIONS.get(name,('其他','尚未登记用途'))
                views.append({'name':name,'category':category,'description':description,
                              'updated_at':updated,'time_basis':basis,'total_bytes':0})
            page_size=conn.execute('PRAGMA page_size').fetchone()[0]
            page_count=conn.execute('PRAGMA page_count').fetchone()[0]
            free_bytes=conn.execute('PRAGMA freelist_count').fetchone()[0]*page_size
            sizes,method=physical_sizes(conn,self.db.path)
            indexes={}
            for obj in objects:
                if obj['type']=='index':
                    indexes.setdefault(obj['tbl_name'],[]).append(obj['name'])
            tables=[]
            for obj in objects:
                if obj['type']!='table':continue
                name=obj['name'];category,description=DESCRIPTIONS.get(name,('其他','尚未登记用途'))
                columns=[r['name'] for r in conn.execute(f'PRAGMA table_info({quote(name)})')]
                count=conn.execute('SELECT count(*) FROM '+quote(name)).fetchone()[0]
                updated,basis=table_time(conn,name,columns)
                table_bytes=sizes.get(name);index_names=indexes.get(name,[])
                index_bytes=sum(sizes.get(i,0) for i in index_names) if method!='unavailable' else None
                total=table_bytes+index_bytes if table_bytes is not None and index_bytes is not None else None
                tables.append({'name':name,'category':category,'description':description,'rows':count,
                    'table_bytes':table_bytes,'index_bytes':index_bytes,'total_bytes':total,
                    'index_count':len(index_names),'column_count':len(columns),'updated_at':updated,'time_basis':basis})
            allocated=sum(r['total_bytes'] or 0 for r in tables) if method!='unavailable' else None
            summary={'database_path':str(self.db.path),'database_bytes':page_size*page_count,
                'free_bytes':free_bytes,'page_size':page_size,'schema_version':conn.execute('PRAGMA user_version').fetchone()[0],
                'journal_mode':conn.execute('PRAGMA journal_mode').fetchone()[0],'sqlite_version':sqlite3_version(conn),
                'table_count':len(tables),'row_count':sum(r['rows'] for r in tables),
                'table_bytes':sum(r['table_bytes'] or 0 for r in tables) if allocated is not None else None,
                'index_bytes':sum(r['index_bytes'] or 0 for r in tables) if allocated is not None else None,
                'system_bytes':max(0,page_size*page_count-free_bytes-allocated) if allocated is not None else None,
                'size_method':method}
        backups=list((self.db.path.parent/'backups').glob('*.sqlite3'))
        entries=[(p,p.stat()) for p in backups]
        latest=max(entries,key=lambda pair:pair[1].st_mtime,default=None)
        summary['backups']={'count':len(entries),'total_bytes':sum(s.st_size for _,s in entries),
            'latest':{'name':latest[0].name,'bytes':latest[1].st_size,
                      'created_at':datetime.fromtimestamp(latest[1].st_mtime,timezone.utc).isoformat()} if latest else None}
        return {'generated_at':datetime.now(timezone.utc).isoformat(),'summary':summary,'tables':tables,'views':views,
            'notes':['大小按已分配 SQLite 页统计，包含页内空余；表与索引分别列出。',
                     '记录数包含保留的历史版本；不同表的记录不能简单视为公司数。',
                     '更新时间来自已有采集、写入或关联批次字段，未记录的表不推算。',
                     '此处仅统计 SQLite；原始响应文件、财报 PDF 和备份单独占用磁盘。']}


def sqlite3_version(conn):
    return conn.execute('SELECT sqlite_version()').fetchone()[0]
