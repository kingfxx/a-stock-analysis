"""Explicit report preparation; local reads never fetch or invoke models."""
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import hashlib
import json
import re
import tempfile
import shutil
from urllib.parse import urlparse
import requests
from .storage import utc_now
from .report_business_metrics import business_metrics
from .sources import normalize_code

PARSER_VERSION = "pdfplumber_pages_v1"
EXTRACTION_VERSION = "checklist_topics_v8"
TOPICS = {
 "company": "公司全称|公司名称|中文名称|公司简介",
 "strategy": "愿景|战略定位|使命|经营战略|发展战略|战略规划",
 "products": "经营模式|主要业务|经营范围|分部收益|分部利润|分产品情况|主营业务分行业情况|主营业务分产品情况|报告分部的财务信息|主营收入",
 "market": "分地区|经营地区分类|地区信息|地域信息|中国地区|海外业务|市场布局|对外交易收入|中国大陆以外",
 "control": "控股股东情况|实际控制人情况|控制人变更|公司无控股股东|公司无实际控制人",
 "cycle": "行业情况|所处行业|行业发展|供需|运价|原奶|生鲜乳",
 "competition": "行业地位|竞争格局|核心竞争力|市场占有率|市场份额|全球第一|全球第|世界第|位居|排名",
 "value_chain": "供应链|上下游|采购模式",
 "bargaining": "前五名客户|前五名供应商|议价|定价",
 "financial_notes": "现金流量净额变动原因|商誉减少|短期借款增加|分部间抵销"
}
_PREPARE_LOCK = Lock()

class CompanyReportService:
    def __init__(self, db, root=None):
        self.db = db
        self.root = Path(root or db.path.parent / "company_reports").resolve()

    def _relative(self, path):
        return str(Path(path).resolve().relative_to(self.root)).replace("\\", "/")

    def resolve(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("财报路径超出正式目录")
        if not path.exists():
            parts=Path(relative).parts
            if len(parts)>=4 and re.fullmatch(r'[0-9a-f]{64}',parts[2]):
                with self.db.connection() as conn:
                    document=conn.execute('SELECT relative_path FROM company_report_documents WHERE content_hash=? AND report_period=?',(parts[2],parts[1])).fetchone()
                if document:
                    mapped=(self.root/Path(document[0]).parent/Path(*parts[3:])).resolve()
                    if not mapped.is_relative_to(self.root):raise ValueError('财报路径超出正式目录')
                    return mapped
        return path

    def _next_directory(self, parent, conn, identity, period):
        numbers=[int(path.name[1:]) for path in parent.iterdir() if re.fullmatch(r"v[1-9]\d*",path.name)] if parent.exists() else []
        for row in conn.execute("SELECT relative_path FROM company_report_documents WHERE instrument_id=? AND report_period=?",(identity,period)):
            match=re.fullmatch(r"v([1-9]\d*)",Path(row[0]).parent.name)
            if match:numbers.append(int(match[1]))
        return parent / ('v'+str(max(numbers,default=0)+1))

    def import_report(self, code, period, source, *, url="", published_on=None):
        code=normalize_code(code)
        if period[5:] not in {"12-31","06-30","03-31","09-30"}:raise ValueError("财报期次无效")
        datetime.strptime(period,"%Y-%m-%d")
        data=Path(source).read_bytes()
        if not data.startswith(b"%PDF") or len(data)>100*1024*1024:raise ValueError("财报文件无效或超过100MiB")
        identity=self.db.ensure_instrument(code);sha=hashlib.sha256(data).hexdigest()
        kind={"12-31":"annual","06-30":"interim"}.get(period[5:],"quarterly")
        # Allocation and registration share the SQLite writer lock, including CLI imports.
        with self.db.connection(write=True) as conn:
            existing=conn.execute("SELECT * FROM company_report_documents WHERE instrument_id=? AND report_period=? AND report_type=? AND content_hash=?",(identity,period,kind,sha)).fetchone()
            if existing:
                doc=dict(existing);target=self.resolve(doc['relative_path'])
            else:
                exchange=conn.execute("SELECT exchange FROM instruments WHERE id=?",(identity,)).fetchone()[0]
                parent=self.root/(exchange+code)/period
                target=self._next_directory(parent,conn,identity,period)/'report.pdf'
                previous=conn.execute("SELECT id FROM company_report_documents WHERE instrument_id=? AND report_period=? AND report_type=? ORDER BY id DESC LIMIT 1",(identity,period,kind)).fetchone()
                conn.execute("INSERT INTO company_report_documents(instrument_id,report_period,report_type,published_on,source_url,content_hash,relative_path,supersedes_id,obtained_at) VALUES (?,?,?,?,?,?,?,?,?)",(identity,period,kind,published_on,url,sha,self._relative(target),previous[0] if previous else None,utc_now()))
                doc=dict(conn.execute("SELECT * FROM company_report_documents WHERE id=last_insert_rowid()").fetchone())
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=sha:raise ValueError("已存财报哈希不符，请先核对原件")
            if not target.exists():
                temp=target.with_suffix('.part');temp.write_bytes(data);temp.replace(target)
            target.with_name('manifest.json').write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
        return {**doc,'code':code}

    def migrate_directories(self):
        """Copy and verify before updating paths; preserve immutable AI snapshots."""
        moved=[]
        with self.db.connection() as conn:
            documents=[dict(row) for row in conn.execute('SELECT * FROM company_report_documents ORDER BY report_period,id')]
        for document in documents:
            original=self.resolve(document['relative_path']);old_dir=original.parent
            if not re.fullmatch(r'[0-9a-f]{64}',old_dir.name):continue
            if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest()!=document['content_hash']:raise ValueError('迁移前财报原件哈希不符')
            with self.db.connection(write=True) as conn:
                target_dir=self._next_directory(old_dir.parent,conn,document['instrument_id'],document['report_period'])
                if not old_dir.is_relative_to(self.root) or not target_dir.resolve().is_relative_to(self.root):raise ValueError('迁移路径超出正式目录')
                shutil.copytree(old_dir,target_dir)
                for file in old_dir.rglob('*'):
                    if file.is_file() and hashlib.sha256(file.read_bytes()).digest()!=hashlib.sha256((target_dir/file.relative_to(old_dir)).read_bytes()).digest():raise ValueError('迁移文件校验失败，原资料已保留')
                old_prefix=self._relative(old_dir)+'/'
                new_prefix=self._relative(target_dir)+'/'
                conn.execute('UPDATE company_report_documents SET relative_path=? WHERE id=?',(new_prefix+'report.pdf',document['id']))
                for row in conn.execute('SELECT id,relative_path FROM company_report_parses WHERE document_id=?',(document['id'],)).fetchall():
                    if row['relative_path'] and row['relative_path'].startswith(old_prefix):
                        conn.execute('UPDATE company_report_parses SET relative_path=? WHERE id=?',(new_prefix+row['relative_path'][len(old_prefix):],row['id']))
                updated=dict(conn.execute('SELECT * FROM company_report_documents WHERE id=?',(document['id'],)).fetchone())
                (target_dir/'manifest.json').write_text(json.dumps(updated,ensure_ascii=False,indent=2),encoding='utf-8')
            # Only remove the validated old hash directory after the database commit.
            if old_dir.resolve().is_relative_to(self.root) and re.fullmatch(r'[0-9a-f]{64}',old_dir.name):
                shutil.rmtree(old_dir)
            moved.append({'document_id':document['id'],'old_path':document['relative_path'],'new_path':updated['relative_path']})
        return moved

    def parse(self, document):
        try:
            return self._parse(document)
        except Exception:
            with self.db.connection(write=True) as conn:
                conn.execute("INSERT INTO company_report_parses(document_id,parser_version,status,page_count,relative_path,quality_json,created_at) VALUES (?,?,'failed',0,'',?,?) ON CONFLICT(document_id,parser_version) DO UPDATE SET status='failed',quality_json=excluded.quality_json", (document["id"],PARSER_VERSION,json.dumps({"error":"原件或解析校验失败，请核对PDF"}),utc_now()))
            raise

    def _parse(self, document):
        with self.db.connection() as conn:
            cached = conn.execute("SELECT * FROM company_report_parses WHERE document_id=? AND parser_version=? AND status='succeeded'", (document["id"],PARSER_VERSION)).fetchone()
        with self.db.connection() as conn:
            facts_count=conn.execute("SELECT count(*) FROM company_report_facts WHERE parse_id=? AND extraction_version=?",(cached['id'] if cached else -1,EXTRACTION_VERSION)).fetchone()[0]
            code=conn.execute("SELECT i.code FROM instruments i JOIN company_report_documents d ON d.instrument_id=i.id WHERE d.id=?",(document['id'],)).fetchone()[0]
        file=self.resolve(document['relative_path'])
        if hashlib.sha256(file.read_bytes()).hexdigest()!=document['content_hash']:
            raise ValueError("财报原件哈希不符，请核对资料")
        if cached and self.resolve(cached["relative_path"]).exists() and facts_count==len(TOPICS):
            return {**dict(cached), "cache_hit": True}
        import pdfplumber
        file = self.resolve(document["relative_path"])
        if hashlib.sha256(file.read_bytes()).hexdigest() != document["content_hash"]:
            raise ValueError("财报原件哈希不符，请核对资料")
        if cached and self.resolve(cached['relative_path']).exists():
            pages=[json.loads(line) for line in self.resolve(cached['relative_path']).read_text(encoding='utf-8').splitlines() if line]
        else:
            with pdfplumber.open(file) as pdf:
                pages = [{"pdf_page":i+1,"text":page.extract_text() or ""} for i,page in enumerate(pdf.pages)]
        preview = "".join(p["text"] for p in pages[:6])
        year = document["report_period"][:4]
        expected = "年度报告" if document["report_type"]=="annual" else "半年度报告" if document["report_type"]=="interim" else "季度报告"
        compact=re.sub(r"\s+","",preview)
        if code not in compact or not re.search(year+r"年?"+expected,compact) or "年度报告摘要" in compact:
            raise ValueError("财报股票或年度不匹配，请核对下载文件")
        if sum(len(p["text"].strip()) for p in pages) < 500:
            raise ValueError("财报文本不足，扫描文件暂需人工核对")
        directory = file.parent / "parsed" / PARSER_VERSION
        directory.mkdir(parents=True,exist_ok=True)
        target = directory / "pages.jsonl"
        temp = target.with_suffix(".part")
        temp.write_text("\n".join(json.dumps(p,ensure_ascii=False) for p in pages),encoding="utf-8")
        temp.replace(target)
        quality = {"empty_pages":[p["pdf_page"] for p in pages if not p["text"].strip()],"tables_require_original_page_check":True}
        currency_context=None
        for currency_page in pages:
            declaration=re.search(r'本财务报表以人民币列示',re.sub(r'\s+','',currency_page['text']))
            if declaration:
                currency_context={'pdf_page':currency_page['pdf_page'],'text':declaration[0]};break
        for index, page in enumerate(pages):
            metrics = business_metrics(page["text"], document["report_period"], pages[index-1]["text"] if index else "", currency_context=currency_context)
            if metrics:page["business_metrics"] = metrics
        facts = {}
        for topic, pattern in TOPICS.items():
            candidates = []
            for page in pages:
                matches = list(re.finditer(pattern,page["text"]))
                if not matches: continue
                # Keep neighboring context, full headings and nearby table text.
                pos=matches[0].start()
                text=page["text"] if len(page["text"])<=6000 else page["text"][max(0,pos-300):min(len(page["text"]),pos+4500)]
                candidates.append({**page,"text":text})
            # Exclude contents pages; keep substantive sections, full nearby tables.
            def rank(candidate):
                text=candidate['text']
                substantive=(topic=='market' and bool(re.search('分地区|经营地区分类|地区信息|地域信息|中国地区',text))) or (topic=='strategy' and bool(re.search('使命|愿景|发展战略|经营战略',text)))
                numeric = (topic=='products' and any(candidate.get('business_metrics',{}).get(key) for key in ('profit_mix','revenue_mix'))) or (topic=='market' and bool(candidate.get('business_metrics',{}).get('region_mix'))) or (topic=='competition' and bool(re.search('市场占有率|市场份额|全球第|世界第|位居.{0,12}第|排名',text)))
                return ("目录" in text or text.count("....")>2,not numeric,not substantive,candidate['pdf_page'])
            ordered=sorted(candidates,key=rank)
            chosen=ordered[:4]
            # Keep a business description alongside numerical tables.
            if topic in ('products','market') and len(ordered)>4:
                descriptive=next((c for c in ordered if not c.get('business_metrics') and re.search('经营模式|主要业务|海外业务|市场布局',c['text']) and '目录' not in c['text']),None)
                if descriptive and descriptive not in chosen:chosen[-1]=descriptive
            if topic in ('market','control','products','competition'):
                neighbors=[]
                for candidate in chosen:
                    offset=-1 if topic=='products' and candidate.get('business_metrics',{}).get('profit_mix') else 1
                    next_page=next((page for page in pages if page['pdf_page']==candidate['pdf_page']+offset),None)
                    if next_page and next_page['pdf_page'] not in {x['pdf_page'] for x in chosen+neighbors}:
                        neighbors.append({**next_page,'text':next_page['text'][:6000]})
                chosen+=neighbors[:2]
            facts[topic]=chosen
        with self.db.connection(write=True) as conn:
            conn.execute("INSERT INTO company_report_parses(document_id,parser_version,status,page_count,relative_path,quality_json,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(document_id,parser_version) DO UPDATE SET status=excluded.status,page_count=excluded.page_count,relative_path=excluded.relative_path,quality_json=excluded.quality_json",(document["id"],PARSER_VERSION,"succeeded",len(pages),self._relative(target),json.dumps(quality),utc_now()))
            parse_id=conn.execute("SELECT id FROM company_report_parses WHERE document_id=? AND parser_version=?",(document["id"],PARSER_VERSION)).fetchone()[0]
            for topic, items in facts.items():
                conn.execute("INSERT INTO company_report_facts(parse_id,extraction_version,topic,facts_json,created_at) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",(parse_id,EXTRACTION_VERSION,topic,json.dumps(items,ensure_ascii=False),utc_now()))
        (directory/"quality.json").write_text(json.dumps(quality,ensure_ascii=False,indent=2),encoding="utf-8")
        return {"id":parse_id,"page_count":len(pages),"cache_hit":False}

    def discover(self, code):
        headers={"User-Agent":"Mozilla/5.0", "Referer":"https://www.cninfo.com.cn/"}
        response=requests.get("https://www.cninfo.com.cn/new/data/szse_stock.json",headers=headers,timeout=20)
        response.raise_for_status()
        org=next((x["orgId"] for x in response.json().get("stockList",[]) if x["code"]==code),None)
        if not org: raise ValueError("未找到正式公告股票索引")
        records=[]
        for term,kind in [("年度报告","annual"),("半年度报告","interim")]:
            response=requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",data={"stock":f"{code},{org}","tabName":"fulltext","pageSize":30,"pageNum":1,"column":"","category":"","plate":"","seDate":"","searchkey":term,"isHLtitle":"false"},headers=headers,timeout=20)
            response.raise_for_status()
            for item in response.json().get("announcements",[]) or []:
                title=re.sub("<[^>]+>","",item.get("announcementTitle", ""))
                title=re.sub(r"\s+","",title).replace('（','(').replace('）',')')
                year=re.search(r"(20\d{2})年?(半年度|年度)报告(?:全文|\((?:全文|修订版|修订|更正版|更新版)\))?$",title)
                if item.get("secCode")!=code or not year or "摘要" in title:continue
                if (year[2]=='半年度') != (kind=='interim'):continue
                ts=item.get("announcementTime")
                published=datetime.fromtimestamp(ts/1000,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat() if isinstance(ts,(int,float)) else None
                if not published or published>datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat():continue
                records.append({"period":year[1]+("-12-31" if kind=="annual" else "-06-30"),"kind":kind,"published":published,"url":"https://static.cninfo.com.cn/"+item["adjunctUrl"]})
        return [max([r for r in records if r['kind']==kind],key=lambda r:(r['period'],r['published'])) for kind in ('annual','interim') if any(r['kind']==kind for r in records)]

    def cached_documents(self, code):
        code=normalize_code(code)
        today=datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.db.connection() as conn:
            saved=[dict(row) for row in conn.execute("SELECT d.*,p.status AS parse_status,p.page_count,p.relative_path AS text_path,(SELECT count(*) FROM company_report_facts f WHERE f.parse_id=p.id AND f.extraction_version=?) AS topic_count FROM company_report_documents d JOIN instruments i ON i.id=d.instrument_id LEFT JOIN company_report_parses p ON p.document_id=d.id AND p.parser_version=? WHERE i.code=? ORDER BY d.report_period DESC,d.id DESC",(EXTRACTION_VERSION,PARSER_VERSION,code))]
            periods=[row[0] for row in conn.execute("SELECT r.period FROM financial_reports r JOIN instruments i ON i.id=r.instrument_id WHERE i.code=? AND r.period<=? AND (r.publish_date IS NULL OR r.publish_date<=?)",(code,today,today))]
        result=[]
        for kind,end in [('annual','12-31'),('interim','06-30')]:
            doc=next((doc for doc in saved if doc['report_type']==kind),None)
            expected=max((period for period in periods if period.endswith(end)),default=None)
            if not doc:
                result.append({'report_type':kind,'report_period':None,'state':'missing','expected_period':expected})
                continue
            file_exists=self.resolve(doc['relative_path']).is_file()
            parsed=file_exists and doc['parse_status']=='succeeded' and bool(doc['text_path']) and self.resolve(doc['text_path']).is_file() and doc['topic_count']==len(TOPICS)
            result.append({'id':doc['id'],'report_type':kind,'report_period':doc['report_period'],'published_on':doc['published_on'],'version':Path(doc['relative_path']).parent.name,'source_url':doc['source_url'],'state':'ready' if parsed else 'unparsed' if file_exists else 'missing_file','page_count':doc['page_count'],'expected_period':expected,'outdated':bool(expected and doc['report_period']<expected)})
        return result

    def prepare(self, code, *, refresh=False):
        code=normalize_code(code)
        if not _PREPARE_LOCK.acquire(blocking=False):raise ValueError("已有财报资料正在准备，请稍候")
        try:
            with self.db.connection() as conn:
                saved=[dict(r) for r in conn.execute("SELECT d.*,i.code FROM company_report_documents d JOIN instruments i ON i.id=d.instrument_id WHERE i.code=? ORDER BY report_period DESC,id DESC",(code,))]
            cached=self.cached_documents(code)
            if saved and not refresh and all(d.get("report_period") and not d.get("outdated") for d in cached):
                docs=[]
                for kind in ('annual','interim'):
                    doc=next((d for d in saved if d['report_type']==kind),None)
                    if doc:docs.append({**doc,"parse":self.parse(doc)})
                return {"documents":docs,"cached":True}
            docs=[]
            for record in self.discover(code):
                host=urlparse(record['url']).hostname
                if host!='static.cninfo.com.cn':raise ValueError("公告下载地址无效")
                with requests.get(record['url'],timeout=30,stream=True,allow_redirects=False) as response:
                    response.raise_for_status();data=bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data)>100*1024*1024:raise ValueError("财报超过100MiB")
                with tempfile.TemporaryDirectory() as temporary:
                    file=Path(temporary)/'report.pdf';file.write_bytes(data)
                    doc=self.import_report(code,record['period'],file,url=record['url'],published_on=record['published'])
                    doc['code']=code;docs.append({**doc,"parse":self.parse(doc)})
            if not docs:raise ValueError("未找到可用完整年报或中报，可稍后刷新资料")
            return {"documents":docs,"cached":False}
        finally:_PREPARE_LOCK.release()


def main():
    import argparse
    from .storage import Database, DEFAULT_DATABASE
    parser=argparse.ArgumentParser(description="导入、准备可复用公司财报")
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--code')
    parser.add_argument('--migrate-directories',action='store_true')
    parser.add_argument('--period')
    parser.add_argument('--file',type=Path)
    parser.add_argument('--url',default='')
    parser.add_argument('--published-on')
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args();db=Database(args.database);db.check();service=CompanyReportService(db)
    if args.migrate_directories:
        result={'moved':service.migrate_directories()}
    elif not args.code:parser.error('需要 --code 或 --migrate-directories')
    elif args.file:
        if not args.period:parser.error('--file 需要 --period')
        doc=service.import_report(args.code,args.period,args.file,url=args.url,published_on=args.published_on)
        result={**doc,'parse':service.parse(doc)}
    else:result=service.prepare(args.code,refresh=args.refresh)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
