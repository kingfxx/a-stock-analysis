import json

from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.infrastructure.storage import Database


def test_live_progress_is_atomically_merged_and_final_aggregation_survives(tmp_path,monkeypatch):
    from stock_analysis.domains.industries import industry_updates

    db=Database(tmp_path/'industry-progress.sqlite3');db.initialize()
    service=IndustryService(db,tmp_path/'industry-sources')
    seen={}

    def perform(_service,action,target,recheck,progress,*,run_id,progress_state):
        progress('下载四个财务接口')
        progress_state({'kind':'financial','phase':'downloading','interfaces':{
            key:{'status':'downloading','page':1,'pages':2,'records':500,'total':501}
            for key in ('income','balance','cashflow','performance')}})
        running=service.status()
        seen['running']=running['result']['collection_progress']
        with db.connection(write=True) as conn:
            row=conn.execute('SELECT result_json FROM collection_operations WHERE id=?',(run_id,)).fetchone()
            result=json.loads(row['result_json'])
            result['aggregation_results']={'saved-period':{'rows':12}}
            conn.execute('UPDATE collection_operations SET result_json=? WHERE id=?',
                         (json.dumps(result),run_id))
        return {'action':action,'target':target,'requested_count':4,'failures':[]}

    monkeypatch.setattr(industry_updates,'perform',perform)
    service._refresh('financial_period','2025-06-30',False)
    assert seen['running']['interfaces']['income']['page']==1
    final=service.status()['result']
    assert final['collection_progress']['phase']=='downloading'
    assert final['aggregation_results']=={'saved-period':{'rows':12}}


def test_old_run_without_progress_remains_unknown(tmp_path):
    db=Database(tmp_path/'industry-old-progress.sqlite3');db.initialize()
    service=IndustryService(db,tmp_path/'industry-sources')
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_operations(action,target,recheck,started_at,finished_at,status,result_json) "
                     "VALUES('financial_period','2025-06-30',0,'2026-10-01','2026-10-01','complete','{}')")
    status=service.status()
    assert status['result']=={}
