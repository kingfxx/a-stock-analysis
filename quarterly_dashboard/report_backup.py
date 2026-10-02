"""Explicit full-data archive; credentials and unrelated files are excluded."""
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from .storage import Database, DEFAULT_DATABASE
from .company_report_service import CompanyReportService


def backup(db, destination):
    destination=Path(destination).resolve();destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():raise ValueError("备份目标已存在")
    service=CompanyReportService(db)
    with tempfile.TemporaryDirectory() as temporary:
        snapshot=Database(db.backup(Path(temporary)/'stock_analysis.sqlite3'))
        entries={'stock_analysis.sqlite3':snapshot.path}
        with snapshot.connection() as conn:
            docs=[dict(r) for r in conn.execute('SELECT * FROM company_report_documents')]
        for doc in docs:
            original=service.resolve(doc['relative_path'])
            if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest()!=doc['content_hash']:
                raise ValueError("正式财报缺失或哈希不符，备份未完成")
            for file in original.parent.rglob('*'):
                if file.is_file() and file.suffix in ('.pdf','.json','.jsonl'):
                    entries['company_reports/'+service._relative(file)]=file
        hashes={name:hashlib.sha256(file.read_bytes()).hexdigest() for name,file in entries.items()}
        with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,file in entries.items():archive.write(file,name)
            archive.writestr('archive-manifest.json',json.dumps({'version':1,'sha256':hashes},indent=2))
    return {'path':str(destination),'files':len(entries)}


def restore(source,destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError("恢复目录必须不存在；不覆盖日常资料")
    with zipfile.ZipFile(source) as archive:
        manifest=json.loads(archive.read('archive-manifest.json'))
        names=archive.namelist()
        if manifest.get('version')!=1 or len(names)!=len(set(names)) or set(names)!={'archive-manifest.json',*manifest['sha256']}:
            raise ValueError("备份清单无效")
        # Validate all paths and hashes before writing anything.
        for name,sha in manifest['sha256'].items():
            target=(destination/name).resolve()
            if not target.is_relative_to(destination) or '\\' in name or ':' in name or not (name=='stock_analysis.sqlite3' or name.startswith('company_reports/')):
                raise ValueError("备份路径无效")
            if hashlib.sha256(archive.read(name)).hexdigest()!=sha:raise ValueError("备份哈希不符")
        destination.mkdir(parents=True)
        for name in manifest['sha256']:
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(name))
    Database(destination/'stock_analysis.sqlite3').check()
    return {'path':str(destination),'files':len(manifest['sha256'])}


def main():
    parser=argparse.ArgumentParser(description="SQLite 与正式财报完整备份/恢复")
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--restore',type=Path)
    args=parser.parse_args()
    result=restore(args.restore,args.output) if args.restore else backup(Database(args.database),args.output)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
