"""Versioned, lossless financial_reports payloads; business readers receive text."""
from __future__ import annotations

import hashlib
import zlib

MAGIC = b'ASTKFR\x01'
MAX_RAW_BYTES = 64 * 1024 * 1024
HEADER_BYTES = len(MAGIC) + 8 + 32


def compress_json(text: str) -> bytes:
    raw = text.encode('utf-8')
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError('Financial JSON exceeds 64 MiB')
    return MAGIC + len(raw).to_bytes(8, 'big') + hashlib.sha256(raw).digest() + zlib.compress(raw, 6)


def decompress_json(value) -> str:
    """Legacy TEXT passes through; reject unknown, damaged or oversized BLOBs."""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes) or not value.startswith(MAGIC) or len(value) <= HEADER_BYTES:
        raise ValueError('Unknown financial JSON encoding')
    size = int.from_bytes(value[len(MAGIC):len(MAGIC) + 8], 'big')
    if size > MAX_RAW_BYTES:
        raise ValueError('Financial JSON exceeds 64 MiB')
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(value[HEADER_BYTES:], size + 1)
    except zlib.error as exc:
        raise ValueError('Damaged compressed financial JSON') from exc
    digest = value[len(MAGIC) + 8:HEADER_BYTES]
    if (len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or hashlib.sha256(raw).digest() != digest):
        raise ValueError('Financial JSON length/hash/stream mismatch')
    return raw.decode('utf-8')


def financial_row(row) -> dict:
    result = dict(row)
    result['raw_json'] = decompress_json(result['raw_json'])
    return result


def migrate_financial_json(conn):
    """Within the schema transaction; update only raw_json and verify every row."""
    for row in conn.execute('SELECT rowid,raw_json,content_hash FROM financial_reports'):
        text = decompress_json(row['raw_json'])
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != row['content_hash']:
            raise ValueError(f"Financial report hash mismatch at rowid={row['rowid']}")
        payload = compress_json(text)
        if decompress_json(payload) != text:
            raise ValueError('Financial compression roundtrip mismatch')
        conn.execute('UPDATE financial_reports SET raw_json=? WHERE rowid=?', (payload, row['rowid']))


def check_financial_json(conn):
    for row in conn.execute("SELECT raw_json,content_hash FROM financial_reports WHERE typeof(raw_json)='blob'"):
        text = decompress_json(row['raw_json'])
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != row['content_hash']:
            raise ValueError('Financial report content_hash mismatch')
