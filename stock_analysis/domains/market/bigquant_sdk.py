"""Read-only BigQuant SDK adapter with public pacing on HTTP and Flight exchanges."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime
import io
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from stock_analysis.collection.collection import RequestCollector, encoded, archive_source, parsed_source, begin_batch, finish_batch, read_source

DATASET='market.trade_snapshot.bigquant_inputs'


class MaterializedReader:
    def __init__(self, table, chunks):self.table=table;self.chunks=iter(chunks)
    def read_all(self):return self.table
    def read_chunk(self):
        try:return next(self.chunks)
        except StopIteration:raise StopIteration from None
    def __iter__(self):
        while True:
            try:yield self.read_chunk()
            except StopIteration:return
    def cancel(self):pass


class BigQuantSDK:
    def __init__(self, db, operation, *, credentials=None, progress=print, cancelled=lambda:False):
        if datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()>'2026-10-17':
            raise ValueError('用户指定的BigQuant访问窗口已结束')
        self.db=db;self.operation=operation
        path=credentials or db.path.parent/'credentials/bigquant.txt'
        secret=Path(path).read_text(encoding='utf-8-sig').strip()
        self.ak,sep,self.sk=secret.partition('.')
        if not sep or not self.ak or not self.sk or '\n' in secret:raise ValueError('BigQuant凭据格式无效')
        self.secrets=(secret,self.ak,self.sk)
        self.collector=RequestCollector(db,progress=progress,cancelled=cancelled)
        self.progress=progress;self.batch=None;self.scope={};self.sources=[]
        self.local=threading.local()

    def sanitized(self, value):
        if isinstance(value,dict):
            return {k:('[REDACTED]' if any(t in str(k).lower() for t in
                ('token','password','authorization','secret','api_key','access_key','signature')) else self.sanitized(v)) for k,v in value.items()}
        if isinstance(value,list):return [self.sanitized(v) for v in value]
        if isinstance(value,str):
            for secret in self.secrets:value=value.replace(secret,'[REDACTED]')
        return value

    def http_evidence(self, response):
        try:return encoded(self.sanitized(response.json())).encode()
        except ValueError:return encoded(dict(status=response.status_code,body_kind='non_json_omitted')).encode()

    def exchange(self, transport, method, callback, serialize, response_of=lambda value:None):
        if getattr(self.local,'active',False):
            # Fail closed instead of silently bypassing shared pacing for nested calls.
            raise RuntimeError('Unexpected nested SDK transport; adapter needs explicit support')
        self.local.active=True
        try:
            value,sid=self.collector.invoke(self.batch,'bigquant',DATASET,
                dict(transport=transport,method=method,scope=self.scope),callback,serialize=serialize,
                response_of=response_of,hash_basis='sanitized_http_json / gzip_file' if transport=='http' else 'sdk_arrow_or_action / gzip_file')
            self.sources.append(sid)
            return value
        finally:self.local.active=False

    @contextmanager
    def instrumented(self):
        import httpx
        import bigquant
        from bigquant_core.flight_client import FlightClient
        import pyarrow as pa
        old_send=httpx.Client.send;old_action=FlightClient.do_action;old_get=FlightClient.do_get
        def send(client, request, **kwargs):
            def perform():
                response=old_send(client,request,**kwargs)
                response.read()
                return response
            return self.exchange('http',request.method+' '+str(request.url.copy_with(query=None)),
                perform,self.http_evidence,lambda value:value)
        def action(client, action_type, body=b''):
            def serialize(values):
                evidence=[]
                for value in values:
                    try:evidence.append(self.sanitized(json.loads(value)))
                    except (ValueError,UnicodeError):
                        from stock_analysis.collection.collection import digest
                        evidence.append(dict(bytes=len(value),sha256=digest(value)))
                return encoded(evidence).encode()
            return self.exchange('arrow_flight','do_action:'+action_type,
                lambda:old_action(client,action_type,body),serialize)
        def get(client,ticket):
            def perform():
                reader=old_get(client,ticket)
                chunks=list(reader)
                batches=[chunk.data for chunk in chunks if chunk.data is not None]
                return pa.Table.from_batches(batches,schema=reader.schema),chunks
            def serialize(result):
                table,chunks=result
                stream=pa.BufferOutputStream()
                with pa.ipc.new_stream(stream,table.schema) as writer:writer.write_table(table)
                return stream.getvalue().to_pybytes()
            table,chunks=self.exchange('arrow_flight','do_get',perform,serialize)
            return MaterializedReader(table,chunks)
        httpx.Client.send=send;FlightClient.do_action=action;FlightClient.do_get=get
        try:yield bigquant
        finally:
            httpx.Client.send=old_send;FlightClient.do_action=old_action;FlightClient.do_get=old_get

    def call(self, scope, fn):
        self.scope=scope;self.sources=[]
        self.batch=begin_batch(self.db,self.operation,DATASET,'bigquant',scope=scope,
            frequency='daily',parser_version='sdk-transport-v1',definition_version='bigquant-inputs-v1')
        try:
            # Suppress SDK authentication diagnostics; persist only sanitized errors.
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):value=fn()
            finish_batch(self.db,self.batch,'complete','unknown',result=dict(sources=self.sources))
            return value,list(self.sources)
        except BaseException as exc:
            error=self.sanitized(str(exc))[:1000]
            finish_batch(self.db,self.batch,'failed',error=error,result=dict(sources=self.sources))
            raise RuntimeError(error) from None

    def initialize(self, bigquant):
        self.call(dict(operation='initialize'),lambda:bigquant.init(ak=self.ak,sk=self.sk))

    def query(self, dai, sql, filters, label, folder):
        path=Path(folder)/(label+'.parquet')
        metadata=Path(folder)/(label+'.json')
        scope=dict(operation='query',sql=sql,filters=filters)
        if path.exists() and metadata.exists():
            from stock_analysis.collection.collection import digest
            import pyarrow.parquet as pq
            item=json.loads(metadata.read_text(encoding='utf-8'))
            if item['scope']!=scope or digest(path.read_bytes())!=item['sha256']:raise ValueError('SDK缓存范围或哈希不一致')
            for source in item['sources']:read_source(self.db,source)
            self.progress(f'复用 {label}: {item["rows"]} 行')
            return pq.read_table(path),item
        self.progress(f'BigQuant范围查询 {label}')
        table,sources=self.call(scope,lambda:dai.query(sql,filters=filters).arrow())
        if not sources:raise ValueError('SDK查询未经过已登记的实际传输入口')
        import pyarrow.parquet as pq
        from stock_analysis.collection.collection import digest, atomic_write
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.parquet.tmp');pq.write_table(table,temporary);temporary.replace(path)
        item=dict(scope=scope,sources=sources,rows=len(table),sha256=digest(path.read_bytes()),batch_id=self.batch)
        atomic_write(metadata,encoded(item).encode())
        self.progress(f'已缓存 {label}: {len(table)} 行')
        return table,item
