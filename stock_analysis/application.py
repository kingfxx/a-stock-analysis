"""Application assembly and shared service lifecycle. No HTTP Handler dependency."""
from __future__ import annotations

import sqlite3
import secrets
from pathlib import Path
from stock_analysis.infrastructure.storage import Database, StorageError
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.domains.indices.index_collection import recover_operations
from stock_analysis.maintenance.stock_cleanup import StockCleanupService
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.domains.research.assessment.analysis_service import AnalysisService
from stock_analysis.domains.research.assessment.checklist_snapshot import capture as checklist_capture
from stock_analysis.domains.research.assessment.checklist_validation import prompt as checklist_prompt, validate_output as validate_checklist, OUTPUT_VERSION
from stock_analysis.domains.research.ai.ai_provider import ChatGPTProvider
from stock_analysis.domains.stocks.source_parsers import normalize_code
from stock_analysis.infrastructure.app_settings import AppSettings
from stock_analysis.domains.stocks.data_service import StockDataService


settings = AppSettings()

data = StockDataService(settings)

_SERVICE_LOCK = data._SERVICE_LOCK

_AI_SERVICES = {}

_INDUSTRY_SERVICES = {}

_MAINTENANCE_SERVICES = {}

LOCAL_SESSION_TOKEN = secrets.token_urlsafe(32)

def industry_service():
    with _SERVICE_LOCK:
        key = str(Path(settings.DATABASE_PATH).resolve())
        if key not in _INDUSTRY_SERVICES:
            db = Database(settings.DATABASE_PATH)
            db.initialize()
            _INDUSTRY_SERVICES[key] = IndustryService(db)
        return _INDUSTRY_SERVICES[key]

def ai_service():
    key = str(Path(settings.DATABASE_PATH).resolve())
    with _SERVICE_LOCK:
        if key not in _AI_SERVICES:
            db = Database(settings.DATABASE_PATH)
            _AI_SERVICES[key] = AnalysisService(db, ChatGPTProvider(db.path.parent / "ai-private"), snapshotter=checklist_capture, prompt_factory=checklist_prompt, validator=validate_checklist, output_version=OUTPUT_VERSION)
        return _AI_SERVICES[key]

def business_service():
    shared = ai_service()
    key = str(Path(settings.DATABASE_PATH).resolve()) + ':business'
    with _SERVICE_LOCK:
        if key not in _AI_SERVICES:
            from stock_analysis.domains.research.assessment import business_judgment as business
            _AI_SERVICES[key] = AnalysisService(shared.db, shared.provider, snapshotter=business.capture,
                prompt_factory=business.prompt, validator=business.validate, output_version=business.OUTPUT_VERSION, shared=shared)
        return _AI_SERVICES[key]

def _initialize_runtime(db, skip_legacy=False):
    runtime=db.initialize();StockCleanupService(db).recover();db.check();db.daily_backup()
    recover_operations(db)
    recovered=db.recover_interrupted_runs()
    industry_recovered=IndustryService(db).recover_interrupted_runs()
    ai_recovered=ai_service().repository.recover()
    chips,_=data.services(initialized=True)
    # After restoring, do not silently import newer external cache files into the snapshot.
    if not skip_legacy:
        for section in ('financing','shareholders'):
            for path in sorted((settings.CHIP_CACHE/section).glob('*.json')):
                try:chips.import_legacy(normalize_code(path.stem),section)
                except (ValueError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                    print(f'{path.name} {section} 迁移未完成，原文件保留：{exc}',flush=True)
        for warning in data.import_p4_legacy(db):print(warning,flush=True)
    print(f"SQLite {runtime['sqlite_version']} / {runtime['journal_mode']} / "
          f"结构版本 {runtime['schema_version']}；恢复中断任务 {recovered} 个",flush=True)
    print(f'AI 中断任务 {ai_recovered} 个；仅手动分析使用 ChatGPT 额度',flush=True)
    print(f'行业中断任务 {industry_recovered} 个；行业仅通过独立入口更新',flush=True)

def _runtime_worker_activity():
    """Check in-memory workers without opening SQLite during exclusive maintenance."""
    with _SERVICE_LOCK:
        for service in _INDUSTRY_SERVICES.values():
            with service._lock:
                if service._status['running']:
                    return True
        return any(
            s.worker and s.worker.is_alive() for s in _AI_SERVICES.values())

def _restore_activity():
    with Database(settings.DATABASE_PATH).connection() as conn:
        index_running=bool(conn.execute("SELECT 1 FROM index_collection_operations WHERE status='running'").fetchone())
    return index_running or _runtime_worker_activity()

def _clear_runtime_services():
    with _SERVICE_LOCK:
        data._SERVICES.clear(); _AI_SERVICES.clear(); _INDUSTRY_SERVICES.clear(); _MAINTENANCE_SERVICES.clear()
        data._DATA_LOCKS.clear()


def maintenance_service():
    with _SERVICE_LOCK:
        key = str(settings.DATABASE_PATH)
        if key not in _MAINTENANCE_SERVICES:
            _MAINTENANCE_SERVICES[key] = MaintenanceService(Database(settings.DATABASE_PATH))
        return _MAINTENANCE_SERVICES[key]
