"""Existing indices HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

import sqlite3
from threading import Thread
from urllib.parse import parse_qs
from stock_analysis.infrastructure.storage import Database, StorageError
from stock_analysis.domains.indices.index_collection import start_operation, run_operation, cancel_operation
from stock_analysis.domains.indices.index_repository import read_indices
from stock_analysis.domains.indices.index_read_service import IndexReadService
from stock_analysis.domains.market import market_congestion
from stock_analysis.collection.collection import read_source
from stock_analysis import application as app


class IndicesRoutes:

    def _get_indices_congestion(self, parsed):
        if parsed.path=='/api/indices/congestion':
            try:
                query=parse_qs(parsed.query)
                self._ai_reply(market_congestion.read(Database(app.settings.DATABASE_PATH),start=query.get('start',[None])[0],
                    end=query.get('end',[None])[0],index_code=query.get('code',['cn:000001'])[0]))
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_indices_catalog(self, parsed):
        if parsed.path in {'/api/indices/catalog','/api/indices/status','/api/indices/chart','/api/indices/price','/api/indices/crowding','/api/indices/industry-crowding','/api/indices/equity-bond-spread'}:
            try:
                with app._SERVICE_LOCK:
                    if not hasattr(self.server, 'index_reader'):
                        self.server.index_reader = IndexReadService(Database(app.settings.DATABASE_PATH))
                    reader = self.server.index_reader
                query = parse_qs(parsed.query)
                if parsed.path.endswith('/catalog'):
                    body = reader.catalog()
                elif parsed.path.endswith('/status'):
                    body = reader.status()
                elif parsed.path.endswith('/price'):
                    body = reader.price_overlay(query.get('code',[None])[0])
                elif parsed.path.endswith('/industry-crowding'):
                    body = reader.industry_crowding(query.get('level',[None])[0],query.get('start',[None])[0],query.get('end',[None])[0])
                elif parsed.path.endswith('/equity-bond-spread'):
                    body = reader.equity_bond_spread(query.get('code',[None])[0])
                elif parsed.path.endswith('/crowding'):
                    body = reader.price_overlay(query.get('code',[None])[0],field='amount_ratio')
                else:
                    body = reader.chart(code=query.get('code',[None])[0],provider=query.get('provider',[None])[0],
                        series=query.get('series',[None])[0],definition=query.get('definition',['v1'])[0],metric=query.get('metric',[None])[0])
                self._ai_reply(body)
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_indices(self, parsed):
        if parsed.path=='/api/indices':
            try:
                query=parse_qs(parsed.query)
                data=read_indices(Database(app.settings.DATABASE_PATH),code=query.get('code',[None])[0],kind=query.get('kind',[None])[0],
                    level=query.get('level',[None])[0],provider=query.get('provider',[None])[0],series=query.get('series',[None])[0],
                    definition_version=query.get('definition',['v1'])[0],start=query.get('start',[None])[0],end=query.get('end',[None])[0],weekly=query.get('weekly')==['1'])
                self._ai_reply(data)
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_indices_source(self, parsed):
        if parsed.path=='/api/indices/source':
            try:
                raw,item=read_source(Database(app.settings.DATABASE_PATH),int(parse_qs(parsed.query).get('id',['0'])[0]))
                # Escape arbitrary source text and keep it inert in the browser.
                self._ai_reply({'metadata':item,'body':raw.decode('utf-8',errors='replace') if item['content_type'] and ('json' in item['content_type'] or 'text' in item['content_type']) else '二进制响应；文件路径及双哈希见元数据'})
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _post_indices_congestion_schedule(self, path, command):
        data = market_congestion.configure(Database(app.settings.DATABASE_PATH), command)
        return data

    def _post_indices_collect(self, path, command):
        if not isinstance(command, dict):
            raise ValueError('指数采集请求须为对象')
        db = Database(app.settings.DATABASE_PATH)
        if path.endswith('/cancel'):
            if command:
                raise ValueError('取消请求无需参数')
            cancel_operation(db)
            data = {'cancel_requested': True}
        else:
            if set(command) - {'mode', 'providers', 'start', 'end', 'datasets'}:
                raise ValueError('指数采集参数无效')
            if 'datasets' in command:
                if command['datasets'] == ['market_congestion']:
                    if command.get('providers', ['legu']) != ['legu'] or 'start' in command or 'end' in command:
                        raise ValueError('拥挤度任务范围无效')
                    operation = market_congestion.start(db, command.get('mode', 'incremental'))
                elif command['datasets'] == ['bond_zh_us_rate']:
                    if 'start' in command or 'end' in command:
                        raise ValueError('国债收益率由本地水位决定范围')
                    operation = start_operation(db, command.get('mode', 'incremental'), command.get('providers', ['csindex', 'legu', 'sws']), datasets=command['datasets'])
                else:
                    raise ValueError('指数采集数据集无效')
            else:
                operation = start_operation(db, command.get('mode', 'incremental'), command.get('providers', ['csindex', 'legu', 'sws']), command.get('start'), command.get('end'))
            Thread(target=run_operation, args=(db, operation), daemon=True).start()
            data = {'operation_id': operation, 'status': 'running'}
        return data
