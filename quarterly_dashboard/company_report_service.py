"""Explicit report preparation; local reads never fetch or invoke models."""
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import hashlib
import json
import re
import tempfile
from urllib.parse import urlparse
import requests
from .storage import utc_now
from .sources import normalize_code

PARSER_VERSION = "pdfplumber_pages_v1"
EXTRACTION_VERSION = "checklist_topics_v3"
TOPICS = {
 "company": "公司全称|公司名称|中文名称|公司简介",
 "strategy": "愿景|战略定位|使命|经营战略|发展战略|战略规划",
 "products": "经营模式|主要业务|经营范围",
 "market": "分地区|经营地区分类|地区信息|地域信息|中国地区|海外业务|市场布局",
 "control": "控股股东情况|实际控制人情况|控制人变更|公司无控股股东|公司无实际控制人",
 "cycle": "行业情况|所处行业|行业发展|供需|运价|原奶|生鲜乳",
 "competition": "行业地位|竞争格局|核心竞争力",
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
        return path

    def import_report(self, code, period, source, *, url="", published_on=None):
        code = normalize_code(code)
        if period[5:] not in {"12-31", "06-30", "03-31", "09-30"}:
            raise ValueError("财报期次无效")
        datetime.strptime(period, "%Y-%m-%d")
        data = Path(source).read_bytes()
        if not data.startswith(b"%PDF") or len(data) > 100 * 1024 * 1024:
            raise ValueError("财报文件无效或超过100MiB")
        identity = self.db.ensure_instrument(code)
        with self.db.connection() as conn:
            exchange = conn.execute("SELECT exchange FROM instruments WHERE id=?", (identity,)).fetchone()[0]
        sha = hashlib.sha256(data).hexdigest()
        target = self.root / (exchange + code) / period / sha / "report.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temp = target.with_suffix(".part")
            temp.write_bytes(data)
            temp.replace(target)
        kind = {"12-31":"annual", "06-30":"interim"}.get(period[5:], "quarterly")
        with self.db.connection(write=True) as conn:
            previous = conn.execute("SELECT id FROM company_report_documents WHERE instrument_id=? AND report_period=? AND report_type=? ORDER BY id DESC LIMIT 1", (identity, period, kind)).fetchone()
            conn.execute("INSERT INTO company_report_documents(instrument_id,report_period,report_type,published_on,source_url,content_hash,relative_path,supersedes_id,obtained_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                         (identity,period,kind,published_on,url,sha,self._relative(target),previous[0] if previous else None,utc_now()))
            doc = dict(conn.execute("SELECT * FROM company_report_documents WHERE instrument_id=? AND report_period=? AND report_type=? AND content_hash=?",(identity,period,kind,sha)).fetchone())
        target.with_name("manifest.json").write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding="utf-8")
        return {**doc, "code":code}

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
        facts = {}
        for topic, pattern in TOPICS.items():
            candidates = []
            for page in pages:
                matches = list(re.finditer(pattern,page["text"]))
                if not matches: continue
                # Keep neighboring context, full headings and nearby table text.
                pos=matches[0].start()
                text=page["text"] if len(page["text"])<=6000 else page["text"][max(0,pos-300):min(len(page["text"]),pos+4500)]
                candidates.append({"pdf_page":page["pdf_page"],"text":text})
            # Exclude contents pages; keep substantive sections, full nearby tables.
            def rank(candidate):
                text=candidate['text']
                substantive=(topic=='market' and bool(re.search('分地区|经营地区分类|地区信息|地域信息|中国地区',text))) or (topic=='strategy' and bool(re.search('使命|愿景|发展战略|经营战略',text)))
                return ("目录" in text or text.count("....")>2,not substantive,candidate['pdf_page'])
            chosen=sorted(candidates,key=rank)[:4]
            if topic in ('market','control'):
                neighbors=[]
                for candidate in chosen:
                    next_page=next((page for page in pages if page['pdf_page']==candidate['pdf_page']+1),None)
                    if next_page and next_page['pdf_page'] not in {x['pdf_page'] for x in chosen+neighbors}:
                        neighbors.append({'pdf_page':next_page['pdf_page'],'text':next_page['text'][:6000]})
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
                year=re.search(r"(20\d{2})年",title)
                if item.get("secCode")!=code or not year or "摘要" in title or not title.endswith(("报告","报告（修订版）","报告(修订版)")): continue
                if kind=="annual" and "半年度" in title: continue
                ts=item.get("announcementTime")
                published=datetime.fromtimestamp(ts/1000,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat() if isinstance(ts,(int,float)) else None
                if not published or published>datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat():continue
                records.append({"period":year[1]+("-12-31" if kind=="annual" else "-06-30"),"kind":kind,"published":published,"url":"https://static.cninfo.com.cn/"+item["adjunctUrl"]})
        return [max([r for r in records if r['kind']==kind],key=lambda r:(r['period'],r['published'])) for kind in ('annual','interim') if any(r['kind']==kind for r in records)]

    def prepare(self, code, *, refresh=False):
        code=normalize_code(code)
        if not _PREPARE_LOCK.acquire(blocking=False):raise ValueError("已有财报资料正在准备，请稍候")
        try:
            with self.db.connection() as conn:
                saved=[dict(r) for r in conn.execute("SELECT d.*,i.code FROM company_report_documents d JOIN instruments i ON i.id=d.instrument_id WHERE i.code=? ORDER BY report_period DESC,id DESC",(code,))]
            if saved and not refresh:
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
    parser.add_argument('--code',required=True)
    parser.add_argument('--period')
    parser.add_argument('--file',type=Path)
    parser.add_argument('--url',default='')
    parser.add_argument('--published-on')
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args();db=Database(args.database);db.check();service=CompanyReportService(db)
    if args.file:
        if not args.period:parser.error('--file 需要 --period')
        doc=service.import_report(args.code,args.period,args.file,url=args.url,published_on=args.published_on)
        result={**doc,'parse':service.parse(doc)}
    else:result=service.prepare(args.code,refresh=args.refresh)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
