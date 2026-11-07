"""Application server lifecycle, scheduler ownership and restore reloads."""
import json
from contextlib import ExitStack

from stock_analysis.infrastructure.storage import Database, instance_lock
from stock_analysis.maintenance.database_restore import RestoreManager, RequestGate
from stock_analysis.collection.collection_management import Scheduler
from stock_analysis.collection.source_archive_service import SourceArchiveService
from stock_analysis.infrastructure.memory_reclaim import IdleMemoryReclaimer


def run_server(port, open_browser, *, database_path, handler, server_factory,
               initialize, activity_check, management_factory, clear_state, open_url, idle_cleanup=None,
               control=None, shutdown_activity=None):
    """Run one local listener and scheduler; drain both before restore/reload."""
    db = Database(database_path)
    manager = RestoreManager(db, activity_check=activity_check)
    reloaded = False
    while True:
        with instance_lock(database_path):
            server = server_factory(('127.0.0.1', port), handler)
            server.restore_manager = manager
            server.request_gate = RequestGate()
            server.memory_reclaimer = IdleMemoryReclaimer()
            with ExitStack() as cleanup:
                def close_reader():
                    if hasattr(server, 'index_reader'):
                        server.index_reader.close()
                cleanup.callback(_close_resource, control, close_reader)
                cleanup.callback(_close_resource, control, server.server_close)
                try:
                    _serve_instance(server, db, reloaded, initialize, management_factory,
                                    idle_cleanup, cleanup, control, shutdown_activity or activity_check,
                                    open_browser, open_url, port)
                except KeyboardInterrupt:
                    return
            if not manager.pending:
                return
            result = manager.execute()
            print('数据库恢复：' + json.dumps(result, ensure_ascii=False), flush=True)
            clear_state()
            reloaded = True


def _serve_instance(server, db, reloaded, initialize, management_factory, idle_cleanup,
                    cleanup, control, activity_check, open_browser, open_url, port):
    """Keep cleanup registered before each resource's initialization can fail."""
    initialize(db, skip_legacy=reloaded)
    server.source_archive = SourceArchiveService(db, server.request_gate)
    scheduler = Scheduler(management_factory(db), gate=server.request_gate, archive=server.source_archive,
                          idle_cleanup=(lambda: idle_cleanup(server)) if idle_cleanup else None)
    def close_scheduler():
        thread = getattr(scheduler, 'thread', None)
        # Scheduler.start() recovers metadata before starting its thread.
        # If recovery fails, close() would join an unstarted thread and mask it.
        if thread is None or thread.ident is not None:
            scheduler.close()
    cleanup.callback(_close_resource, control, close_scheduler)
    scheduler.start()
    print(f'季度分析页面：http://127.0.0.1:{port}/', flush=True)
    if control:
        control.bind(server, activity_check)
        cleanup.callback(lambda: control.detach(bool(server.restore_manager.pending)))
    if open_browser and not reloaded:
        open_url(f'http://127.0.0.1:{port}/')
    server.serve_forever()


def _close_resource(control, close):
    try:
        close()
    except BaseException as exc:
        if control:
            with control.lock:
                control.cleanup_error = str(exc)
        raise
