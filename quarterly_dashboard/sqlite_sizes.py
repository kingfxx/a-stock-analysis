"""Read-only physical page accounting when SQLite's dbstat is unavailable.

Format: https://www.sqlite.org/fileformat.html#b_tree_pages
The caller must hold a SQLite read transaction; direct reading is rollback-mode only.
"""
from __future__ import annotations

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
    sizes={}
    with path.open('rb') as source:
        header=source.read(100)
        if len(header)!=100 or header[:16]!=b'SQLite format 3\x00':
            raise ValueError('SQLite 文件头不匹配')
        page_size=int.from_bytes(header[16:18],'big')
        if page_size==1:page_size=65536
        file_size=source.seek(0,2)
        if page_size<512 or page_size>65536 or page_size&(page_size-1) or file_size%page_size:
            raise ValueError('SQLite 页大小或文件长度异常')
        usable=page_size-header[20];page_count=file_size//page_size
        # One bit per page, plus one page buffer; never map the whole database.
        seen=bytearray((page_count+7)//8)
        def page(number):
            if not 1<=number<=page_count:
                raise ValueError('SQLite 页引用异常，停止大小统计')
            index,bit=divmod(number-1,8)
            if seen[index]&(1<<bit):
                raise ValueError('SQLite 页引用异常，停止大小统计')
            seen[index]|=1<<bit
            start=(number-1)*page_size
            source.seek(start)
            block=source.read(page_size)
            if len(block)!=page_size:
                raise ValueError('SQLite 页读取不完整')
            return block
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
