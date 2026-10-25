"""Paginated Eastmoney snapshots through the real parser, archive and writer."""
import json
from datetime import datetime, timezone

import requests

from stock_analysis.collection.collection import RequestCollector
from test_market_trade_snapshot import Clock, row


def collector(db, records):
    class Session:
        def __init__(self):self.calls=[]
        def get(self, url, **kwargs):
            page=kwargs['params']['pageNumber'];self.calls.append(page)
            response=requests.Response();response.status_code=200;response.encoding='utf-8'
            response.headers['Content-Type']='application/json'
            response._content=json.dumps(dict(success=True,code=0,result=dict(
                count=len(records),pages=(len(records)+499)//500,data=records[(page-1)*500:page*500]))).encode()
            return response
    clock=Clock();clock.now=datetime.now(timezone.utc).timestamp()
    session=Session()
    return RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a),session


def cap_record(code, day, cap, close=10):
    result=row(code)
    result.update(SECUCODE=code+('.SH' if code.startswith('6') else '.SZ'),
                  TRADE_DATE=day,TOTAL_MARKET_CAP=cap,TOTAL_SHARES=None,CLOSE_PRICE=close)
    return result
