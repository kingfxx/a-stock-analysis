"""Read-only physical page accounting when SQLite's dbstat is unavailable.

Format: https://www.sqlite.org/fileformat.html#b_tree_pages
The caller must hold a SQLite read transaction; direct reading is rollback-mode only.
"""
from __future__ import annotations

import mmap
import sqlite3


def _varint(data, offset):
    value=0
    for i in range(9):
        byte=data[offset+i]
        if i==8:
            return (value<<8)|byte,offset+i+1
        value=(value<<7)|(byte&127)
        if not byte&128:
            return value,offset+i+1
    raise ValueError('无效 SQLite varint')


def physical_sizes(conn, path):
    try:
        sizes={r[0]:r[1] for r in conn.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name')}
        return sizes,'dbstat'
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
    if conn.execute('PRAGMA journal_mode').fetchone()[0]=='wal':
        return {},'unavailable'
    roots=[(r[0],r[1]) for r in conn.execute("SELECT name,rootpage FROM sqlite_master WHERE rootpage>0")]
    roots.append(('sqlite_schema',1))
    sizes={};seen=set()
    with path.open('rb') as source, mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ) as data:
        if data[:16]!=b'SQLite format 3\x00':
            raise ValueError('SQLite 文件头不匹配')
        page_size=int.from_bytes(data[16:18],'big')
        if page_size==1:page_size=65536
        usable=page_size-data[20];page_count=len(data)//page_size
        def page(number):
            if not 1<=number<=page_count or number in seen:
                raise ValueError('SQLite 页引用异常，停止大小统计')
            seen.add(number)
            start=(number-1)*page_size
            return data[start:start+page_size]
        for name,root in roots:
            pending=[root];count=0
            while pending:
                number=pending.pop();block=page(number);count+=1
                header=100 if number==1 else 0
                kind=block[header]
                if kind not in (2,5,10,13):
                    raise ValueError('SQLite B-tree 页类型不匹配')
                interior=kind in (2,5)
                cells=int.from_bytes(block[header+3:header+5],'big')
                pointers=header+(12 if interior else 8)
                if interior:
                    pending.append(int.from_bytes(block[header+8:header+12],'big'))
                for i in range(cells):
                    offset=int.from_bytes(block[pointers+2*i:pointers+2*i+2],'big')
                    if interior:
                        pending.append(int.from_bytes(block[offset:offset+4],'big'));offset+=4
                    if kind==5:continue
                    payload,offset=_varint(block,offset)
                    if kind==13:
                        _,offset=_varint(block,offset)
                    maximum=usable-35 if kind==13 else (usable-12)*64//255-23
                    if payload<=maximum:continue
                    minimum=(usable-12)*32//255-23
                    local=minimum+(payload-minimum)%(usable-4)
                    if local>maximum:local=minimum
                    overflow=int.from_bytes(block[offset+local:offset+local+4],'big')
                    remaining=payload-local
                    while remaining>0:
                        extra=page(overflow);count+=1
                        overflow=int.from_bytes(extra[:4],'big');remaining-=usable-4
                    if overflow:
                        raise ValueError('SQLite overflow 长度不匹配')
            sizes[name]=count*page_size
    return sizes,'sqlite_pages'
