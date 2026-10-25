"""Model tests use an isolated database, never the daily database."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import sys
from pathlib import Path

spec=importlib.util.spec_from_file_location('ccfi_build',Path(__file__).with_name('build.py'))
model=importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)
sys.path.insert(0,str(Path(__file__).resolve().parent))
import calibrate
import weekly


class ModelTests(unittest.TestCase):
    def test_shifted_windows_and_missing_month(self):
        self.assertEqual(model.shifted_months(2026,3,1),['2026-06','2026-07','2026-08'])
        self.assertEqual(model.shifted_months(2024,1,1),['2023-12','2024-01','2024-02'])
        self.assertIsNone(model.rate({'2026-06':100,'2026-07':0},2026,3,1))
        self.assertEqual(model.rate({'2026-06':100,'2026-07':0,'2026-08':-10},2026,3,1),30)

    def test_source_priority_quarter_difference_and_cost_definition(self):
        with tempfile.TemporaryDirectory(dir=model.ROOT/'tmp',prefix='ccfi-test-') as tmp:
            path=Path(tmp)/'isolated.sqlite3'
            conn=sqlite3.connect(path)
            conn.executescript('CREATE TABLE instruments(id,exchange,code); INSERT INTO instruments VALUES(7,"sh","601919"); CREATE TABLE financial_reports(instrument_id,source,report_type,period,raw_json,content_hash,obtained_at,publish_date);')
            for period,profit,revenue,cost in [('2026-03-31',30,200,150),('2026-06-30',30,500,400)]:
                for kind in ['gjzb','lrb']:
                    fields={'PARENETP':profit if kind=='gjzb' else 99,'BIZTOTINCO':revenue,'BIZTOTCOST':999,'BIZCOST':cost,'TOTPROFIT':60,'NETPROFIT':50}
                    data=[dict(item_field=k,item_value=str(v*1e8)) for k,v in fields.items()]
                    conn.execute('INSERT INTO financial_reports VALUES(?,?,?,?,?,?,?,?)',(7,'sina',kind,period,json.dumps({'data':data}),'hash','2026-10-24',None))
            # A future observation must never enter the model.
            conn.execute('INSERT INTO financial_reports VALUES(?,?,?,?,?,?,?,?)',(7,'sina','gjzb','2026-09-30','{}','hash','now',None))
            conn.commit();conn.close()
            rows=model.load_financials(path)
            self.assertEqual([r['label'] for r in rows],['2026Q1','2026Q2'])
            self.assertEqual(rows[1]['profit'],0)
            self.assertEqual(rows[1]['revenue'],300)
            self.assertEqual(rows[1]['cost'],250)
            self.assertIsNone(rows[1]['deducted'])
            self.assertEqual(rows[1]['sources']['profit']['current']['report_type'],'gjzb')

    def test_linear_fit(self):
        f=model.fit([{'ccfi':i,'profit':2*i-5} for i in range(10)])
        self.assertAlmostEqual(f['slope'],2)
        self.assertAlmostEqual(f['intercept'],-5)
        self.assertAlmostEqual(f['r2'],1)

    def test_student_tail_and_rank_detection(self):
        self.assertAlmostEqual(calibrate.student_p(0,10),1)
        self.assertAlmostEqual(calibrate.student_p(2.228138852,10),.05,places=8)
        f=calibrate.fit_model([800,900,1000,1100,1200],[4,6,8,10,12],'hinge1500')
        self.assertEqual(f['rank'],2)
        self.assertEqual(f['parameters'],3)

    def test_business_table_parser_and_independent_totals(self):
        header='本集团货运量（标准箱）'
        body='\n'.join(f'{name} 1 2 0 4 8 0' for name in calibrate.ROUTES[:-1])
        pages=[dict(pdf_page=6,text='2023 年第三季度报告\n6 / 21\n'+header+'\n'+body+'\n合计 5 10 0 20 40 0')]
        result=calibrate.extract_table(pages,header,True)
        self.assertEqual(result['total'],20)
        self.assertEqual(result['domestic'],4)
        self.assertEqual(result['direct_q3_total'],5)
        malformed=[dict(pdf_page=6,text=pages[0]['text'].replace('合计 5 10 0 20 40 0','合计 5 10 0 30 40 0'))]
        with self.assertRaisesRegex(ValueError,'reconciliation'):
            calibrate.extract_table(malformed,header,True)

    def weekly_fixture(self):
        return {'weekly':[dict(week=i,values=[100,100,100,100],note=None) for i in range(1,53)]}

    def test_weekly_calendar_alignment_and_holiday_adjustments(self):
        rows=weekly.observations(self.weekly_fixture())
        def record(year,week):return next(r for r in rows if r['year']==year and r['graph_week']==week)
        self.assertEqual(record(2026,1)['inferred_publication_date'],'2026-01-09')
        self.assertFalse(record(2026,7)['scheduled'])
        self.assertEqual(record(2026,17)['inferred_publication_date'],'2026-04-30')
        self.assertEqual(record(2026,24)['inferred_publication_date'],'2026-06-18')
        self.assertEqual(record(2026,38)['inferred_publication_date'],'2026-09-24')
        self.assertEqual(record(2026,39)['inferred_publication_date'],'2026-09-30')
        self.assertEqual(record(2025,18)['inferred_publication_date'],'2025-04-30')
        self.assertFalse(record(2023,4)['scheduled'])

    def test_weekly_window_and_future_price_exclusion(self):
        fixture=self.weekly_fixture()
        fixture['weekly'][36]['values'][3]=1e9  # Sep18, after the shifted quarter's end.
        rows=weekly.observations(fixture)
        result=weekly.exposure(rows,2026,3,2)
        self.assertEqual(result['start'],'2026-06-17')
        self.assertEqual(result['end_exclusive'],'2026-09-17')
        self.assertEqual(result['expected'],13)
        self.assertEqual(result['value'],100)
        self.assertEqual(result['points'][-1]['date'],'2026-09-11')
        self.assertIsNone(weekly.exposure(rows,2023,1,2)['value'])

    def test_weekly_missing_publication_is_not_filled(self):
        fixture=self.weekly_fixture()
        fixture['weekly'][23]['values'][3]=None
        rows=weekly.observations(fixture)
        release=weekly.exposure(rows,2026,3,2)
        daily=weekly.exposure(rows,2026,3,2,'daily_asof')
        self.assertIsNone(release['value'])
        self.assertEqual(release['observed'],12)
        self.assertIsNone(daily['value'])
        self.assertLess(daily['observed'],daily['expected'])
        # A genuine holiday is not an unobserved publication.
        fixture=self.weekly_fixture();fixture['weekly'][6]['values'][1]=None
        self.assertEqual(weekly.exposure(weekly.observations(fixture),2024,1,0)['value'],100)


if __name__=='__main__': unittest.main()
