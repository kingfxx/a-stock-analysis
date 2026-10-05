"""Run with python -m unittest discover -s scripts -p test_research.py."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import shutil
import subprocess
import sys
import os
import unittest
from types import SimpleNamespace
import research as r
import valuation as v

def fact(identifier,value,unit='元'):
    return dict(id=identifier,metric=identifier.rsplit('.',1)[-1],period='2025-12-31',value=value,unit=unit,scope='合并',status='direct',source_id='source')
def pack(facts):
    return dict(schema_version=r.SCHEMA,as_of='2026-10-05',company={'name':'测试','code':'600066'},facts=facts,sources=[{'id':'source','published_on':'2026-03-31'}])

class ResearchTests(unittest.TestCase):
    def test_compressed_json_preserves_raw_values_and_is_reproducible(self):
        raw=[{'source_id':'source','payload':{'data':[{'item_value':None},{'item_value':0},{'item_value':'0.000000'},{'item_title':'营业收入'}]}}]*50
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);plain=out/'raw.json';compressed=out/'raw.json.gz'
            r.save(plain,raw);r.save(compressed,raw)
            self.assertEqual(r.load(compressed),r.load(plain))
            self.assertLess(compressed.stat().st_size,plain.stat().st_size)
            before=compressed.read_bytes();r.save(compressed,raw)
            self.assertEqual(compressed.read_bytes(),before)
    def test_chapter_sources_follow_calculation_inputs_and_deduplicate(self):
        raw=fact('raw',100)
        calc=fact('calc.test',200);calc.update(source_id=None,status='calculated',inputs=['raw'],formula='raw * 2')
        p=pack([raw,calc]);p['sources'][0].update(title='年度报告',url='https://example.com/report.pdf',pdf_page=10)
        text='## 一、商业模式分析\n值 {{fact:calc.test}}，原值 {{fact:raw}}。[S:source]\n### 小节\n{{fact:raw}}\n## 二、企业文化分析\n[S:source]'
        expanded,used=r.expand_report(text,p)
        first,second=expanded.split('## 二、企业文化分析')
        self.assertEqual(first.count('https://example.com/report.pdf'),1)
        self.assertEqual(second.count('https://example.com/report.pdf'),1)
        self.assertLess(first.index('### 小节'),first.index('**数据来源：**'))
        self.assertIn('200.00元，原值 100.00元。',first)
        self.assertNotIn('[↗]',expanded);self.assertNotIn('[S:',expanded)
        self.assertEqual(used,['calc.test','raw'])
    def test_circular_or_missing_calculation_input_rejected_during_render(self):
        f=fact('calc.test',1);f.update(source_id=None,inputs=['calc.test'])
        with self.assertRaises(ValueError):r.expand_report('{{fact:calc.test}}',pack([f]))
        f['inputs']=['missing']
        with self.assertRaises(ValueError):r.expand_report('{{fact:calc.test}}',pack([f]))
    def test_render_delivers_plain_numbers_and_only_chapter_sources(self):
        p=pack([fact('raw',0)])
        p['sources'][0].update(title='年度报告',url='https://example.com/report.pdf')
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);r.save(out/'facts.json',p)
            (out/'report.md').write_text('## 一、商业模式分析\n| 指标 | 值 |\n| --- | --- |\n| 零值 | {{fact:raw}} |\n[S:source]',encoding='utf-8')
            r.render(SimpleNamespace(out=d));r.validate(SimpleNamespace(out=d))
            rendered=(out/'report.html').read_text(encoding='utf-8')
            readable=(out/'report-readable.md').read_text(encoding='utf-8')
            self.assertIn('<td>0.00元</td>',rendered)
            self.assertEqual(rendered.count('href="https://example.com/report.pdf"'),1)
            self.assertEqual(readable.count('**数据来源：**'),1)
            self.assertNotIn('[↗]',readable)
    def test_number_zero_and_absence(self):
        self.assertEqual(r.number('0'),0)
        for x in (None,'',True,'NaN','inf'):self.assertIsNone(r.number(x))
    def test_unit_conversion(self):
        self.assertEqual(r.format_fact(fact('x',36228764300),'亿元'),'362.29亿元')
        with self.assertRaises(ValueError):r.format_fact(fact('x',25.12,'%'),'万元')
    def test_manual_conversion_rejects_wrong_scale(self):
        f=fact('x',251200);f.update(raw_value=3622876.43,raw_unit='万元')
        self.assertTrue(any('conversion mismatch' in e for e in r.validate_pack(pack([f]))))
    def test_reconciliation_rejects_percentage_as_revenue(self):
        p=pack([fact('rev',251200),fact('cost',27126545500),fact('margin',25.12,'%')])
        p['reconciliations']=[{'id':'bus','kind':'gross_margin','inputs':['rev','cost','margin']}]
        self.assertTrue(any('reconciliation failed' in e for e in r.validate_pack(p)))
    def test_reconciliation_correct_table(self):
        p=pack([fact('rev',36228764300),fact('cost',27126545500),fact('margin',25.12,'%')])
        p['reconciliations']=[{'id':'bus','kind':'gross_margin','inputs':['rev','cost','margin']}]
        self.assertEqual(r.validate_pack(p),[])
    def test_future_source_and_unknown_source(self):
        p=pack([fact('x',1)]);p['sources'][0]['published_on']='2026-10-06';p['facts'][0]['source_id']='unknown'
        self.assertEqual(len(r.validate_pack(p)),2)
    def test_missing_is_not_zero(self):
        f=fact('x',None);f.update(status='missing',source_id=None)
        self.assertEqual(r.validate_pack(pack([f])),[])
        self.assertEqual(r.format_fact(f),'待核对')
    def test_unknown_report_fact_and_source_rejected(self):
        for text in ('{{fact:missing}}','[S:unknown]'):
            with self.assertRaises(ValueError):r.expand_report(text,pack([]))
    def test_malformed_fact_placeholders_rejected_before_table_render(self):
        for token in ('{fact:raw}', '{fact:raw|亿元}', '{{fact:raw}', '{{fact:raw|亿元}'):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError,'Malformed fact placeholder'):
                    r.expand_report('| 基数 | '+token+' | 说明 |',pack([fact('raw',9603977191.01)]))
        expanded,_=r.expand_report('| 基数 | {{fact:raw|亿元}} | 说明 |',pack([fact('raw',9603977191.01)]))
        self.assertIn('| 基数 | 96.04亿元 | 说明 |',expanded)

    def test_validation_rejects_stale_rendered_placeholders(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);r.save(out/'facts.json',pack([fact('raw',27.3)]))
            (out/'report.md').write_text('{{fact:raw}}',encoding='utf-8')
            (out/'report.html').write_text('<td>{fact:raw}</td>',encoding='utf-8')
            with self.assertRaises(SystemExit):r.validate(SimpleNamespace(out=d))
            self.assertIn('Unresolved fact placeholder in report.html',r.load(out/'validation.json')['errors'])

    def test_report_navigation_has_unique_targets_and_safe_labels(self):
        body,toc,entries=r.report_navigation('<h1>报告</h1><h2>估值 &amp; 风险</h2><h3>DCF</h3><h3>DCF</h3>')
        self.assertEqual(len(entries),4)
        self.assertEqual(len({x['id'] for x in entries}),4)
        self.assertIn('估值 &amp; 风险',toc)
        for entry in entries:
            self.assertIn('href="#'+entry['id']+'"',toc)
            self.assertIn('id="'+entry['id']+'"',body)
        self.assertIn('toc-level-3',toc)

    def test_path_escape_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):r.inside(d,'../private.txt')
    def test_database_readonly(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.sqlite';c=sqlite3.connect(p);c.execute('create table public(value)');c.commit();c.close()
            before=r.sha(p);c=r.readonly(p)
            try:
                with self.assertRaises(sqlite3.DatabaseError):c.execute('insert into public values (1)')
                self.assertEqual(c.execute('select count(*) from public').fetchone()[0],0)
            finally:c.close()
            self.assertEqual(r.sha(p),before)
    def test_calculation_scope_and_missing_cost(self):
        p=pack([fact(r.fact_id('2025-12-31','revenue'),100),fact(r.fact_id('2025-12-31','net_profit'),12),fact(r.fact_id('2025-12-31','parent_profit'),10),fact(r.fact_id('2025-12-31','cfo'),32)])
        with tempfile.TemporaryDirectory() as d:
            r.save(Path(d)/'facts.json',p);r.calculate(SimpleNamespace(out=d));result=r.load(Path(d)/'facts.json');fs={f['metric']:f for f in result['facts']}
            self.assertEqual(fs['net_margin']['value'],12)
            self.assertEqual(fs['cash_conversion']['value'],320)
            self.assertNotIn('gross_margin',fs);self.assertNotIn('fcf_proxy',fs)
            r.calculate(SimpleNamespace(out=d));self.assertEqual(len(result['facts']),len(r.load(Path(d)/'facts.json')['facts']))
    def test_source_priority_zero_cost_and_date_cutoff(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.sqlite';c=sqlite3.connect(p);c.row_factory=sqlite3.Row
            c.execute('create table instruments(id,code,name,exchange)');c.execute("insert into instruments values(1,'600066','测试','sh')")
            c.execute('create table financial_reports(instrument_id,source,report_type,period,publish_date,obtained_at,content_hash,raw_json)')
            def insert(typ,items,pub='2026-03-31',scope='合并期末'):
                j={'rCurrency':'CNY','rType':scope,'data':[{'item_field':k,'item_value':value,'item_title':k} for k,value in items.items()]}
                c.execute('insert into financial_reports values(?,?,?,?,?,?,?,?)',(1,'sina:'+typ,typ,'2025-12-31',pub,'2026-04-01','hash'+typ+pub,json.dumps(j)))
            insert('gjzb',{'BIZTOTINCO':0,'BIZTOTCOST':70,'PARENETP':None,'FCFFPS':0,'FCFEPS':3.2});insert('lrb',{'BIZTOTINCO':100,'BIZCOST':60,'PARENETP':10});insert('llb',{'MANANETR':1000},'2026-11-01')
            c.commit();out=Path(d)/'out';result=r._extract(c,p,Path(d),'600066','2026-10-05',10,out);c.close()
            self.assertFalse((out/'sources/financial-raw.json').exists())
            archived=r.load(out/'sources/financial-raw.json.gz')
            self.assertEqual(len(archived),2)
            self.assertEqual({x['source_id'] for x in archived},{s['id'] for s in result['sources']})
            fs={f['metric']:f for f in result['facts']}
            self.assertEqual(fs['revenue']['value'],0);self.assertEqual(fs['operating_cost']['value'],60)
            self.assertEqual(fs['parent_profit']['value'],10);self.assertIsNone(fs['cfo']['value'])
            self.assertEqual(fs['source_fcff_per_share']['value'],0)
            self.assertEqual(fs['source_fcfe_per_share']['unit'],'元/股')
            self.assertFalse(fs['source_fcfe_per_share']['valuation_ready'])

class PortabilityTests(unittest.TestCase):
    def test_relocated_checkout_collects_from_unrelated_cwd_and_does_not_write_database(self):
        with tempfile.TemporaryDirectory(prefix='research migration ') as d:
            base=Path(d);project=base/'another checkout';skill=project/'.agents/skills/a-share-value-research'
            shutil.copytree(Path(r.__file__).resolve().parent.parent,skill,ignore=shutil.ignore_patterns('__pycache__'))
            data=project/'data';data.mkdir();(data/'company_reports').mkdir()
            db=data/'stock_analysis.sqlite3';conn=sqlite3.connect(db)
            conn.execute('create table instruments(id,code,name,exchange)')
            conn.execute("insert into instruments values(1,'601919','迁移测试','sh')")
            conn.execute('create table financial_reports(instrument_id,source,report_type,period,publish_date,obtained_at,content_hash,raw_json)')
            raw={'rCurrency':'CNY','rType':'合并期末','data':[{'item_field':'BIZTOTINCO','item_value':100,'item_title':'营收'}]}
            conn.execute('insert into financial_reports values(?,?,?,?,?,?,?,?)',(1,'sina:gjzb','gjzb','2025-12-31','2026-03-31','2026-04-01','hash',json.dumps(raw)))
            conn.commit();conn.close();before=r.sha(db)
            script=skill/'scripts/research.py';out=base/'report output'
            def run(*args):
                result=subprocess.run([sys.executable,str(script),*map(str,args)],cwd=base,capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                return result.stdout
            health=json.loads(run('doctor'));self.assertEqual(Path(health['project_root']),project)
            run('collect','--code','601919','--as-of','2026-10-05','--out',out)
            pack=r.load(out/'facts.json');revenue=next(f for f in pack['facts'] if f['metric']=='revenue')
            self.assertEqual(revenue['value'],100)
            (out/'report.md').write_text('## 一、商业模式分析\n{{fact:'+revenue['id']+'}}',encoding='utf-8')
            run('render','--out',out);run('validate','--out',out)
            self.assertIn('100.00元',(out/'report-readable.md').read_text(encoding='utf-8'))
            self.assertEqual(r.sha(db),before)
            db.unlink()
            failed=subprocess.run([sys.executable,str(script),'doctor'],cwd=base,capture_output=True)
            self.assertNotEqual(failed.returncode,0)

    def test_explicit_root_and_absolute_profile_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=root/'external.sqlite3'
            _,paths=r.profile_paths(dict(database=str(db),reports_root='pdfs',output_root='reports'),str(root))
            self.assertEqual(paths['database'],db)
            self.assertEqual(paths['reports_root'],root/'pdfs')

    def test_reference_styles_follow_headers_and_unknown_tables_remain_valid(self):
        body=r.markdown('| 任意 | 表格 |\n| --- | --- |\n| a | b |\n\n| 状态 | 建议 |\n| --- | --- |\n| 空仓者 | ★★★☆☆ |')
        styled=r.reference_style(body)
        self.assertEqual(styled.count('<colgroup>'),1)
        self.assertIn('decision-table',styled)
        self.assertEqual(styled.count('<span class="stars">'),1)
        self.assertIn('<th>任意</th>',styled)

class ValuationTests(unittest.TestCase):
    def test_pe_earnings_with_no_cash_double_count(self):
        c={'mode':'pe_scenarios','shares':10,'market_price':12,'assumptions':['own estimate'],'scenarios':[{'parent_profit':20,'pe':6}]}
        result=v.evaluate(c)['scenarios'][0]
        self.assertEqual(result['value_per_share'],12);self.assertEqual(result['implied_pe_at_market'],6)
    def config(self):return dict(mode='fcff',discount_rate_basis='WACC',base_cash_flow=100,discount_rate=.1,shares=10,years=10,terminal_growth=0,equity_adjustment=0,scenarios=[0],cash_flow_basis='test',assumptions=['test'],market_price=100)
    def test_perpetuity_and_reverse(self):
        result=v.evaluate(self.config());self.assertAlmostEqual(result['scenarios'][0]['value_per_share'],100);self.assertAlmostEqual(result['reverse_growth'],0)
    def test_discount_basis_and_double_count_rejected(self):
        c=self.config();c['mode']='fcfe'
        with self.assertRaises(ValueError):v.evaluate(c)
        c['discount_rate_basis']='cost_of_equity';c['equity_adjustment']=1
        with self.assertRaises(ValueError):v.evaluate(c)
    def test_terminal_growth_and_missing_basis_rejected(self):
        c=self.config();c['terminal_growth']=.1
        with self.assertRaises(ValueError):v.evaluate(c)
        c=self.config();del c['cash_flow_basis']
        with self.assertRaises(ValueError):v.evaluate(c)

if __name__=='__main__':unittest.main()
