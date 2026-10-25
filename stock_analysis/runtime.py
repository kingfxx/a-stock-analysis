"""Application server lifecycle, scheduler ownership and restore reloads."""
import json

from stock_analysis.infrastructure.storage import Database, instance_lock
from stock_analysis.maintenance.database_restore import RestoreManager, RequestGate
from stock_analysis.collection.collection_management import Scheduler


def run_server(port, open_browser, *, database_path, handler, server_factory,
               initialize, activity_check, management_factory, clear_state, open_url):
    """Run one local listener and scheduler; drain both before restore/reload."""
    db = Database(database_path)
    manager = RestoreManager(db, activity_check=activity_check)
    reloaded = False
    while True:
        with instance_lock(database_path):
            server = server_factory(('127.0.0.1', port), handler)
            server.restore_manager = manager
            server.request_gate = RequestGate()
            scheduler = None
            try:
                initialize(db, skip_legacy=reloaded)
                scheduler = Scheduler(management_factory(db), gate=server.request_gate)
                scheduler.start()
                print(f'季度分析页面：http://127.0.0.1:{port}/', flush=True)
                if open_browser and not reloaded:
                    open_url(f'http://127.0.0.1:{port}/')
                server.serve_forever()
            except KeyboardInterrupt:
                return
            finally:
                if scheduler is not None:
                    scheduler.close()
                server.server_close()
                if hasattr(server, 'index_reader'):
                    server.index_reader.close()
            if not manager.pending:
                return
            result = manager.execute()
            print('数据库恢复：' + json.dumps(result, ensure_ascii=False), flush=True)
            clear_state()
            reloaded = True
