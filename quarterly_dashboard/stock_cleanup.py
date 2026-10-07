"""Explicit, previewed cleanup of unfollowed stocks; industry stores are excluded."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import uuid4
import hashlib
import json
import re
import shutil

from .company_report_service import CompanyReportService, _PREPARE_LOCK
from .stock_library import StockLibrary
from .storage import utc_now

CATEGORIES = {
    'cache': '可重建缓存', 'financial': '个股财务与行情数据',
    'research': '研究历史与导出研报', 'reports': '正式财报原件',
}
# Never discover deletion scope by a generic instrument_id column or table prefix.
FACT_TABLES = ('financial_reports', 'report_overrides', 'raw_daily_prices',
               'adjusted_daily_prices', 'adjusted_price_versions', 'valuation_observations',
               'dividend_events', 'financing_daily', 'shareholder_observations',
               'legacy_valuation_snapshots', 'industry_snapshots')
DELETE_ORDER = ('ai_analysis_runs', 'ai_analysis_snapshots', 'company_report_facts',
                'company_report_parses', 'company_report_documents', 'sync_state', 'legacy_imports',
                'price_version_leases', 'adjusted_price_versions', 'adjusted_daily_prices', 'financial_reports',
                'report_overrides', 'raw_daily_prices', 'valuation_observations', 'dividend_events',
                'financing_daily', 'shareholder_observations', 'legacy_valuation_snapshots')
FILE_ROOTS = ('fundamentals', 'valuation', 'chips', 'company_reports', 'research_reports')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class StockCleanupService:
    def __init__(self, db, activity_check=lambda: False):
        self.db = db
        self.root = db.path.parent.resolve()
        self.staging = self.root / 'cleanup_staging'
        self.audit = self.root / 'verification' / 'reports' / 'stock_cleanup'
        self.activity_check = activity_check
        self.lock = Lock()
        self.prepared = {}

    def path(self, relative):
        """Validate every component, including Windows junctions/reparse points."""
        part = Path(relative)
        if part.is_absolute() or not part.parts or part.parts[0] not in FILE_ROOTS or '..' in part.parts:
            raise ValueError('清理文件路径超出个股资料目录')
        path = self.root / part
        return self._local_path(path)

    def _local_path(self, path):
        if not path.is_relative_to(self.root) or path==self.root:
            raise ValueError('清理路径超出数据目录')
        current = path
        while current != self.root:
            if current.is_symlink() or (current.exists() and getattr(current.stat(), 'st_file_attributes', 0) & 1024):
                raise ValueError('清理范围包含链接或重解析路径，请先人工核对')
            current = current.parent
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('清理文件路径超出数据目录')
        return path

    def _files(self, directory):
        if not directory.exists():
            return []
        self.path(directory.relative_to(self.root))
        files = []
        for path in sorted(directory.rglob('*')):
            self.path(path.relative_to(self.root))
            if path.is_file():
                files.append(path)
        return files

    def _research_files(self, exchange, code):
        return self._files(self.root / 'research_reports' / (exchange + code))

    def _activity(self, conn, *, ignore_prepare=False):
        reasons = []
        if self.activity_check() or (not ignore_prepare and _PREPARE_LOCK.locked()):
            reasons.append('采集、财报准备或分析任务正在启动／运行')
        for table, states in (('sync_runs', "'running'"), ('sw_update_runs', "'running'"),
                              ('ai_analysis_runs', "'queued','running','validating'")):
            if conn.execute(f'SELECT 1 FROM {table} WHERE status IN ({states}) LIMIT 1').fetchone():
                reasons.append('存在未完成的采集、行业或分析任务')
                break
        return reasons

    def _selection(self, conn, codes, categories):
        if (not isinstance(codes, list) or not 1 <= len(codes) <= 100
                or any(not isinstance(c, str) or not re.fullmatch(r'[036]\d{5}', c) for c in codes)
                or len(set(codes)) != len(codes)):
            raise ValueError('请选择 1–100 只不同的未关注股票')
        if (not isinstance(categories, list) or not categories
                or any(not isinstance(c, str) or c not in CATEGORIES for c in categories)
                or len(set(categories)) != len(categories)):
            raise ValueError('请选择有效的清理类别')
        if 'financial' in categories and 'cache' not in categories:
            raise ValueError('清理财务与行情数据时请同时选择缓存，避免旧缓存重新导入')
        stocks = []
        for code in sorted(codes):
            row = conn.execute('SELECT i.*,u.unfollowed_at FROM instruments i JOIN stock_unfollowed u '
                               'ON u.instrument_id=i.id WHERE i.code=?', (code,)).fetchone()
            if row is None:
                raise ValueError(f'{code} 已恢复关注或不在取消关注列表，请重新预览')
            stocks.append(dict(row))
        return stocks

    def _plan(self, conn, codes, categories, *, fingerprint=True, ignore_prepare=False):
        stocks = self._selection(conn, codes, categories)
        identities = {s['id'] for s in stocks}
        summary = {s['code']:{key:{'rows':0, 'files':0, 'file_bytes':0,
                    'delete_rows':0, 'delete_files':0, 'delete_file_bytes':0, 'reasons':[]} for key in CATEGORIES} for s in stocks}
        rows, files = {}, {}

        def add_rows(table, records, code, category, reason=None):
            item = summary[code][category]
            item['rows'] += len(records)
            if reason:
                if records and reason not in item['reasons']:
                    item['reasons'].append(reason)
            elif category in categories:
                item['delete_rows'] += len(records)
                rows.setdefault(table, []).extend(dict(r) for r in records)

        def add_file(path, code, category, reason=None):
            path = self.path(path.relative_to(self.root))
            if not path.is_file():
                return
            relative = path.relative_to(self.root).as_posix()
            if relative in files:
                return
            item = summary[code][category]
            stat = path.stat()
            item['files'] += 1; item['file_bytes'] += stat.st_size
            deleting = category in categories and not reason
            if reason and reason not in item['reasons']:
                item['reasons'].append(reason)
            if deleting:
                item['delete_files'] += 1; item['delete_file_bytes'] += stat.st_size
            files[relative] = {'category':category, 'code':code, 'bytes':stat.st_size,
                'mtime_ns':stat.st_mtime_ns, 'delete':deleting,
                'sha256':file_hash(path) if fingerprint else None}

        def query(table, where, args):
            return conn.execute(f'SELECT rowid AS cleanup_rowid,* FROM {table} WHERE {where}', args).fetchall()

        member_codes = {r[0] for r in conn.execute('SELECT DISTINCT stock_code FROM sw_memberships')}
        deleting_exports = {p for s in stocks for p in self._research_files(s['exchange'], s['code'])} if 'research' in categories else set()
        export_files = self._files(self.root / 'research_reports')
        text_suffixes = ('.json', '.md', '.html', '.txt')
        reference_texts = [p.read_text(encoding='utf-8-sig', errors='replace') for p in export_files
                           if p not in deleting_exports and p.suffix.lower() in text_suffixes]
        export_references = '\n'.join(reference_texts)
        protected_exports = set()
        while True:
            more = {p for p in deleting_exports-protected_exports
                    if p.relative_to(self.root).as_posix() in export_references or
                    p.relative_to(self.root/'research_reports').as_posix() in export_references}
            if not more:
                break
            protected_exports |= more
            for path in more:
                if path.suffix.lower() in text_suffixes:
                    export_references += '\n'+path.read_text(encoding='utf-8-sig',errors='replace')
        retained_runs = {}
        for stock in stocks:
            if stock['code'] in member_codes:
                row = conn.execute("SELECT id FROM ai_analysis_runs WHERE instrument_id=? AND status='succeeded' "
                                   'ORDER BY created_at DESC,id DESC LIMIT 1', (stock['id'],)).fetchone()
                if row:
                    retained_runs[row[0]] = '行业内对比引用最近成功研究，保留该记录'
        delete_runs = set()
        for stock in stocks:
            for row in query('ai_analysis_runs', 'instrument_id=?', (stock['id'],)):
                reason = retained_runs.get(row['id'])
                if row['id'] in export_references:
                    reason = '其他保留研报引用此研究记录'
                if row['status'] in ('queued', 'running', 'validating'):
                    reason = '运行中或等待中的研究不可清理'
                add_rows('ai_analysis_runs', [row], stock['code'], 'research', reason)
                if 'research' in categories and not reason:
                    delete_runs.add(row['id'])
        snapshots = []
        protected_financial = set()
        for row in query('ai_analysis_snapshots', '1=1', ()):
            used = [r[0] for r in conn.execute('SELECT id FROM ai_analysis_runs WHERE snapshot_id=?', (row['id'],))]
            removing = (row['instrument_id'] in identities and 'research' in categories
                        and all(r in delete_runs for r in used) and row['snapshot_hash'] not in export_references)
            if row['instrument_id'] in identities:
                code = next(s['code'] for s in stocks if s['id']==row['instrument_id'])
                add_rows('ai_analysis_snapshots', [row], code, 'research', None if removing else '保留研究仍引用此快照')
            if not removing:
                snapshots.append(dict(row))
                protected_financial.add(row['instrument_id'])

        # Textual exports can cite documents outside their own stock directory.
        references = export_references+'\n'+'\n'.join(r['input_json']+'\n'+r['source_manifest_json'] for r in snapshots)
        referenced_versions = {int(value) for value in re.findall(r'"(?:qfq_version|price_version)"\s*:\s*(\d+)', references)}
        for snapshot in snapshots:
            manifest = json.loads(snapshot['source_manifest_json'])
            version = manifest.get('price_version')
            if isinstance(version, dict) and type(version.get('id')) is int:
                referenced_versions.add(version['id'])
        documents = [dict(r) for r in query('company_report_documents', '1=1', ())]
        protected_docs = {d['id'] for d in documents if d['content_hash'] in references or d['relative_path'] in references
                          or re.search(r'"document_id"\s*:\s*'+str(d['id'])+r'\b', references)}
        all_parses = [dict(r) for r in conn.execute('SELECT document_id,relative_path FROM company_report_parses')]
        for doc in documents:
            if any(other['id']!=doc['id'] and (other['relative_path']==doc['relative_path'] or
                                             other['content_hash']==doc['content_hash']) for other in documents):
                protected_docs.add(doc['id'])
            paths = {p['relative_path'] for p in all_parses if p['document_id']==doc['id'] and p['relative_path']}
            if any(p['document_id']!=doc['id'] and p['relative_path'] in paths for p in all_parses):
                protected_docs.add(doc['id'])
        # Kept revisions must retain their superseded ancestors (real FK dependency).
        kept_docs = {d['id'] for d in documents if d['instrument_id'] not in identities or 'reports' not in categories} | protected_docs
        while True:
            parents = {d['supersedes_id'] for d in documents if d['id'] in kept_docs and d['supersedes_id'] is not None}
            if parents <= kept_docs:
                break
            kept_docs |= parents

        report_service = CompanyReportService(self.db)
        for stock in stocks:
            identity, code = stock['id'], stock['code']
            own_exports = self._research_files(stock['exchange'], code)
            if own_exports and ('research' not in categories or any(p in protected_exports for p in own_exports)):
                protected_financial.add(identity)
            financial_reason = '保留研究引用该股票来源事实，先清理相应研究历史' if identity in protected_financial else None
            leased = bool(conn.execute('SELECT 1 FROM price_version_leases l JOIN adjusted_price_versions v ON v.id=l.version_id '
                                      'WHERE v.instrument_id=? AND l.expires_at>?', (identity, utc_now())).fetchone())
            price_reference = any(r[0] in referenced_versions for r in conn.execute(
                'SELECT id FROM adjusted_price_versions WHERE instrument_id=?', (identity,)))
            retained_datasets = {'industry'}
            for table in FACT_TABLES:
                where = 'version_id IN (SELECT id FROM adjusted_price_versions WHERE instrument_id=?)' if table=='adjusted_daily_prices' else 'instrument_id=?'
                for row in query(table, where, (identity,)):
                    reason = financial_reason
                    if 'content_hash' in row.keys() and row['content_hash'] in references:
                        reason = '保留研究引用该来源哈希，保留事实记录'
                    # Explicit financial cleanup removes the original rows. Retained
                    # research snapshots embed their inputs; industry facts are independent.
                    if table == 'financial_reports':
                        reason = None
                    elif table == 'industry_snapshots':
                        reason = '行业分类来源保留，不进入个股清理'
                    elif table in ('raw_daily_prices','adjusted_daily_prices','adjusted_price_versions') and (leased or price_reference):
                        reason = '保留研究引用价格版本' if price_reference else '仍有页面使用价格版本，请租约到期后再清理'
                    add_rows(table, [row], code, 'financial', reason)
                    if reason or 'financial' not in categories:
                        if table=='financial_reports': retained_datasets.add('financial:'+row['report_type'])
                        elif table=='adjusted_price_versions': retained_datasets.add('prices_adjusted')
                        elif table=='raw_daily_prices': retained_datasets.add('prices_raw')
                        elif table=='valuation_observations': retained_datasets.add('valuation:'+row['metric'])
                        elif table=='dividend_events': retained_datasets.add('dividends')
                        elif table=='financing_daily': retained_datasets.add('financing')
                        elif table=='shareholder_observations': retained_datasets.add('shareholders')
                        elif table=='report_overrides': retained_datasets.add('report_overrides')
                        elif table=='legacy_valuation_snapshots': retained_datasets.add('valuation_legacy')
            for table in ('sync_state','legacy_imports'):
                for row in query(table, 'instrument_id=?', (identity,)):
                    reason = '保留数据的同步水位／导入来源记录' if row['dataset'] in retained_datasets or financial_reason else None
                    if row['dataset'].startswith('financial:') and 'financial' in categories:
                        reason = None
                    add_rows(table, [row], code, 'financial', reason)
            for directory, fact in (('fundamentals','financial_reports'), ('valuation','valuation_observations'),
                                    ('chips/financing','financing_daily'), ('chips/shareholders','shareholder_observations')):
                available = conn.execute(f'SELECT 1 FROM {fact} WHERE instrument_id=? LIMIT 1', (identity,)).fetchone()
                for suffix in ('.json','.json.bak'):
                    add_file(self.root / directory / (code+suffix), code, 'cache',
                             None if available or 'financial' in categories else '缺少数据库事实，旧缓存可能是唯一资料，保留')
            for path in own_exports:
                reason = '其他保留研究引用此研报文件' if path in protected_exports else None
                add_file(path, code, 'research', reason)
            for doc in (d for d in documents if d['instrument_id']==identity):
                original = report_service.resolve(doc['relative_path'])
                original = self.path(original.relative_to(self.root))
                document_reason = '保留研究或修订版本引用此财报，保留原件与解析' if doc['id'] in kept_docs and 'reports' in categories else None
                if doc['id'] in protected_docs:
                    document_reason = '保留研究或共享文件引用此财报，保留原件与解析'
                deleting_doc = 'reports' in categories and doc['id'] not in kept_docs
                add_rows('company_report_documents', [doc], code, 'reports', document_reason)
                add_file(original, code, 'reports', document_reason)
                rebuildable = original.is_file() and (not fingerprint or file_hash(original)==doc['content_hash'])
                for parse in query('company_report_parses', 'document_id=?', (doc['id'],)):
                    reason = document_reason or (None if rebuildable else '正式原件缺失或哈希异常，解析可能是唯一资料，保留')
                    category = 'reports' if deleting_doc else 'cache'
                    add_rows('company_report_parses', [parse], code, category, reason)
                    add_rows('company_report_facts', query('company_report_facts','parse_id=?',(parse['id'],)), code, category, reason)
                for path in self._files(original.parent / 'parsed'):
                    if path.suffix not in ('.json', '.jsonl'):
                        continue  # Unknown attachments are not assumed to be reproducible parser caches.
                    add_file(path, code, 'reports' if deleting_doc else 'cache', document_reason or
                             (None if rebuildable else '正式原件缺失或哈希异常，保留解析缓存'))
                # Keep the tiny version manifest/directory so deleted vN is never reused.
        for table in rows:
            rows[table].sort(key=lambda r:r['cleanup_rowid'])
        versions = {r['id'] for r in rows.get('adjusted_price_versions', [])}
        for row in query('price_version_leases','1=1', ()):
            if row['version_id'] in versions:
                rows.setdefault('price_version_leases', []).append(dict(row))
                version = next(r for r in rows['adjusted_price_versions'] if r['id']==row['version_id'])
                code = next(s['code'] for s in stocks if s['id']==version['instrument_id'])
                summary[code]['financial']['rows'] += 1
                summary[code]['financial']['delete_rows'] += 1
        plan = {'codes':sorted(codes), 'categories':sorted(categories), 'stocks':stocks,
                'summary':summary, 'rows':rows, 'files':files, 'blocked':self._activity(conn,ignore_prepare=ignore_prepare)}
        plan['fingerprint'] = hashlib.sha256(encoded(plan).encode()).hexdigest() if fingerprint else None
        return plan

    def _public(self, plan):
        rows = sum(c['delete_rows'] for s in plan['summary'].values() for c in s.values())
        files = [dict(path=p, **f) for p,f in plan['files'].items() if f['delete']]
        return {'codes':plan['codes'], 'categories':plan['categories'], 'summary':plan['summary'],
                'delete_rows':rows, 'delete_files':len(files), 'file_bytes':sum(f['bytes'] for f in files),
                'blocked':plan['blocked'], 'tables':{t:len(r) for t,r in plan['rows'].items() if r},
                'files':[{'path':f['path'],'bytes':f['bytes'],'category':f['category']} for f in files],
                'notes':['行业表、行业来源文件、股票基础信息及关注状态不清理。',
                         '勾选个股财务与行情数据会删除所选股票全部 financial_reports 记录及对应同步标记；行业指标与研究快照保留，后续行业采集可使用东方财富。',
                         'SQLite 删除记录通常不缩小文件，空间先转为库内可复用空间；本操作不执行 VACUUM。',
                         '清理前不创建备份；成功清理后无法通过本次操作恢复，请核对预览范围。']}

    def candidates(self):
        with self.db.connection() as conn:
            candidates = []
            for stock in StockLibrary(self.db).unfollowed():
                plan = self._plan(conn, [stock['code']], ['cache'], fingerprint=False)
                stamp = datetime.fromisoformat(stock['unfollowed_at'])
                candidates.append({**stock, 'unfollowed_days':max(0,(datetime.now(timezone.utc)-stamp).days),
                                   'summary':plan['summary'][stock['code']]})
            blocked = self._activity(conn)
        return {'stocks':candidates, 'categories':CATEGORIES, 'defaults':['cache'], 'blocked':blocked}

    def preview(self, command):
        if not isinstance(command, dict) or set(command)!={'codes','categories'}:
            raise ValueError('清理预览须指定股票与类别')
        with self.lock, self.db.connection() as conn:
            plan = self._plan(conn, command['codes'], command['categories'])
            token = uuid4().hex
            self.prepared = {token:{'plan':plan, 'at':monotonic()}}
        return {**self._public(plan), 'token':token, 'expires_seconds':900}

    @contextmanager
    def _freeze(self, gate):
        with gate.lock:
            if gate.blocked or gate.active != 1:
                raise ValueError('还有页面请求正在处理，请稍候再执行清理')
            gate.blocked = True
        try:
            if not _PREPARE_LOCK.acquire(blocking=False):
                raise ValueError('财报准备正在运行，请稍后清理')
            try:
                yield
            finally:
                _PREPARE_LOCK.release()
        finally:
            with gate.lock:
                gate.blocked = False

    def _save(self, path, value):
        self._local_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(encoded(value), encoding='utf-8')
        temporary.replace(path)

    def execute(self, command, gate):
        if not isinstance(command, dict) or set(command)!={'token','confirm'} or command['confirm']!='清理':
            raise ValueError('请输入“清理”确认预览范围')
        with self.lock:
            prepared = self.prepared.get(command['token'])
            if not prepared or monotonic()-prepared['at']>900:
                raise ValueError('清理预览已过期，请重新预览')
            original = prepared['plan']
            with self._freeze(gate):
                with self.db.connection() as conn:
                    plan = self._plan(conn, original['codes'], original['categories'], ignore_prepare=True)
                if plan['blocked']:
                    raise ValueError('采集、行业或分析任务尚未完成，请稍后清理')
                if plan['fingerprint']!=original['fingerprint']:
                    raise ValueError('股票状态、数据、文件或引用发生变化，请重新预览')
                public = self._public(plan)
                if not public['delete_rows'] and not public['delete_files']:
                    raise ValueError('所选范围没有可清理资料；请查看保留原因')
                operation = uuid4().hex
                created_at = utc_now()
                folder = self._local_path(self.staging / operation)
                journal = {'id':operation,'phase':'staging','files':[p for p,f in plan['files'].items() if f['delete']]}
                journal['sha256'] = {p:plan['files'][p]['sha256'] for p in journal['files']}
                self._save(folder / 'journal.json', journal)
                moved = []
                result = {**public, 'id':operation, 'backups':[]}
                try:
                    with self.db.connection(write=True) as conn:
                        fresh = self._plan(conn, original['codes'], original['categories'], ignore_prepare=True)
                        if fresh['fingerprint']!=original['fingerprint']:
                            raise ValueError('执行前数据或引用发生变化，请重新预览')
                        for relative in journal['files']:
                            source = self.path(relative); destination = folder / 'files' / relative
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            source.replace(destination); moved.append(relative)
                            if file_hash(destination)!=journal['sha256'][relative]:
                                raise ValueError('暂存文件与预览哈希不一致，已取消清理')
                        for table in DELETE_ORDER:
                            records = plan['rows'].get(table, [])
                            if table=='company_report_documents':
                                records = sorted(records, key=lambda r:r['id'], reverse=True)
                            conn.executemany(f'DELETE FROM {table} WHERE rowid=?', ((r['cleanup_rowid'],) for r in records))
                        if conn.execute('PRAGMA foreign_key_check').fetchone():
                            raise ValueError('清理存在未处理的共享引用，已取消')
                        result['completed_at'] = utc_now()
                        conn.execute('INSERT INTO stock_cleanup_runs VALUES (?,?,?,?,?,?)',
                                     (operation,encoded(plan['codes']),encoded(plan['categories']),encoded(result),created_at,result['completed_at']))
                except BaseException:
                    for relative in reversed(moved):
                        destination = self.path(relative); destination.parent.mkdir(parents=True,exist_ok=True)
                        (folder / 'files' / relative).replace(destination)
                    shutil.rmtree(folder)
                    raise
                self.prepared.clear()
                self._save(self.audit / (operation+'.json'), result)
                shutil.rmtree(folder)
                return result

    def compact(self, command, gate):
        from .maintenance import compact_database
        if command!={'confirm':'整理'}:
            raise ValueError('请输入“整理”确认数据库空间整理')
        def validate(conn):
            if self._activity(conn,ignore_prepare=True):
                raise ValueError('采集、行业或分析任务尚未完成，请稍后整理')
        with self.lock, self._freeze(gate):
            result=compact_database(self.db,validate)
            self.prepared.clear()  # VACUUM can change implicit rowids in old previews.
            return result

    def recover(self):
        """Called under the app instance lock before legacy imports or requests."""
        if not self.staging.exists():
            return
        self._local_path(self.staging)
        for folder in self.staging.iterdir():
            self._local_path(folder)
            if not folder.is_dir() or not re.fullmatch(r'[0-9a-f]{32}',folder.name) or folder.is_symlink():
                raise ValueError('清理暂存目录异常，需人工核对')
            journal = json.loads((folder / 'journal.json').read_text(encoding='utf-8'))
            if journal['id']!=folder.name:
                raise ValueError('清理暂存记录不一致，需人工核对')
            with self.db.connection() as conn:
                committed = conn.execute('SELECT result_json FROM stock_cleanup_runs WHERE id=?',(folder.name,)).fetchone()
            for relative in journal['files']:
                destination = self.path(relative)
                staged = folder / 'files' / relative
                self._local_path(staged)
                if not staged.resolve().is_relative_to(folder.resolve()) or staged.is_symlink():
                    raise ValueError('清理暂存文件路径异常')
                if staged.exists() and not committed:
                    expected = journal.get('sha256',{}).get(relative)
                    if expected and file_hash(staged)!=expected:
                        raise ValueError('中断清理的暂存文件哈希异常，需人工核对')
                    if destination.exists():
                        raise ValueError('中断清理的文件恢复目标已存在，需人工核对')
                    destination.parent.mkdir(parents=True,exist_ok=True);staged.replace(destination)
            if committed:
                self._save(self.audit / (folder.name+'.json'),json.loads(committed[0]))
            shutil.rmtree(folder)
