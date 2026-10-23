"""Portable Markdown notes. No market requests, model calls, or business tables."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from threading import RLock

from .sources import normalize_code

MAX_DOCUMENT = 2 * 1024 * 1024


def digest(content):
    return hashlib.sha256(content).hexdigest()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.note-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def note_code(code):
    if isinstance(code, str) and code.startswith('index:'):
        if not re.fullmatch(r'index:(?:cn|sw):\d{6}', code):
            raise ValueError('指数笔记须使用 index:cn:六位代码 或 index:sw:六位代码')
        return code
    return normalize_code(code)


def stock_key(code):
    code = note_code(code)
    if code.startswith('index:'):
        return code.replace(':', '_', 1).replace(':', '')
    exchange = 'sh' if code.startswith(('5', '6', '9')) else 'bj' if code.startswith(('4', '8')) else 'sz'
    return exchange + code


class NoteConflict(ValueError):
    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current


class InvestmentNotes:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.config = self.data_dir / 'notes-settings.json'
        self.lock = RLock()
        self.previews = {}

    def root(self):
        if self.config.exists():
            data = json.loads(self.config.read_text(encoding='utf-8'))
            return Path(data['directory']).resolve()
        return self.data_dir / 'investment_notes'

    def directory_version(self):
        return digest(str(self.root()).encode())

    @staticmethod
    def safe(root, relative):
        relative = Path(relative)
        if relative.anchor or relative.drive or '..' in relative.parts or not relative.parts or any(':' in part for part in relative.parts):
            raise ValueError('笔记路径必须位于存储目录内')
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('笔记路径越界')
        current = target
        while current != root:
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise ValueError('笔记目录内不允许符号链接或目录联接')
            current = current.parent
        return target

    @staticmethod
    def identity(path, text):
        index = re.match(r'^index_(cn|sw)(\d{6})_', path.name)
        if index:
            return f'index:{index[1]}:{index[2]}'
        header = re.match(r'\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
        if header:
            field = re.search(r'^stock_code:\s*[\"\']?((?:sh|sz|bj)?\d{6})[\"\']?\s*$', header[1], re.M)
            if field:
                return normalize_code(field[1])
        match = re.match(r'^(?:sh|sz|bj)?(\d{6})(?:_|\.|$)', path.name)
        return match[1] if match else None

    def listing(self, root=None):
        root = root or self.root()
        result = []
        if root.exists():
            for path in sorted(root.glob('*.md')):
                self.safe(root, path.name)
                if path.stat().st_size > MAX_DOCUMENT:
                    result.append({'file': path.name, 'code': None, 'error': '文件超过 2 MiB，请在外部编辑器查看'})
                    continue
                try:
                    text = path.read_text(encoding='utf-8-sig')
                    code = self.identity(path, text)
                    title = next((line[2:].strip() for line in text.splitlines() if line.startswith('# ')), path.stem)
                    result.append({'file': path.name, 'code': code, 'title': title, 'version': digest(path.read_bytes()),
                                   'updated_at': path.stat().st_mtime, 'bytes': path.stat().st_size})
                except UnicodeError:
                    result.append({'file': path.name, 'code': None, 'error': '请将文件转换为 UTF-8'})
        return result

    def _path(self, code, name=''):
        code = note_code(code)
        root = self.root()
        matches = [item for item in self.listing(root) if item.get('code') == code]
        if len(matches) > 1:
            raise ValueError('同一股票有多份笔记，请先在存储目录整理重复文件')
        if matches:
            return self.safe(root, matches[0]['file'])
        label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(name)).strip(' .')[:50] or code.replace(':', '')
        return self.safe(root, f'{stock_key(code)}_{label}.md')

    def read(self, code, name=''):
        with self.lock:
            path = self._path(code, name)
            content = path.read_bytes() if path.exists() else None
            if content is not None and len(content) > MAX_DOCUMENT:
                raise ValueError('笔记超过 2 MiB，请在外部编辑器查看')
            return {'code': note_code(code), 'file': path.name, 'exists': content is not None,
                    'content': content.decode('utf-8-sig') if content is not None else '',
                    'version': digest(content) if content is not None else None,
                    'directory_version': self.directory_version()}

    def save(self, command):
        code = note_code(command.get('code', ''))
        text = command.get('content')
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_DOCUMENT:
            raise ValueError('笔记须为 UTF-8 文本，最大 2 MiB')
        if 'version' not in command or command.get('directory_version') != self.directory_version():
            raise NoteConflict('存储目录已经变化，请重新载入；草稿仍保留')
        with self.lock:
            if command['directory_version'] != self.directory_version():
                raise NoteConflict('存储目录已经变化，请重新载入')
            current = self.read(code, command.get('name', ''))
            if current['version'] != command['version']:
                raise NoteConflict('文件已在其他地方修改，请比较后选择保留内容', current)
            path = self.safe(self.root(), current['file'])
            if self.identity(path, text) != code:
                raise ValueError('正文元信息中的股票代码不能改为其他股票')
            before = path.read_bytes() if path.exists() else None
            if (digest(before) if before is not None else None) != current['version']:
                raise NoteConflict('保存前发现外部修改，请重新比较', self.read(code))
            if before == text.encode('utf-8'):
                return current
            # External editors do not share our lock; recheck before replacement.
            actual = path.read_bytes() if path.exists() else None
            if actual != before:
                raise NoteConflict('保存期间发现外部修改，请重新比较', self.read(code))
            atomic_write(path, text.encode('utf-8'))
            return self.read(code)

    def associate(self, command):
        with self.lock:
            code = note_code(command.get('code', ''))
            name = command.get('file', '')
            if Path(name).name != name or not name.endswith('.md'):
                raise ValueError('请选择目录根部的 Markdown 文件')
            path = self.safe(self.root(), name)
            before = path.read_bytes()
            text = before.decode('utf-8-sig')
            if self.identity(path, text):
                raise ValueError('该文件已有股票身份，请先整理重复或错误关联')
            if any(row.get('code') == code for row in self.listing()):
                raise ValueError('该股票已有笔记，不能覆盖或隐式合并')
            if command.get('version') != digest(before):
                raise NoteConflict('文件已变化，请重新扫描')
            if text.startswith('---\n') or text.startswith('---\r\n'):
                raise ValueError('文件已有元信息，请在外部编辑器添加 stock_code 字段后重新扫描')
            atomic_write(path, f'---\nstock_code: "{code}"\n---\n\n{text}'.encode())
            return self.read(code)

    def resource(self, relative):
        path = self.safe(self.root(), relative)
        if any(part.startswith('.') for part in Path(relative).parts):
            raise ValueError('不能访问隐藏文件')
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if mime not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf', 'text/plain'}:
            raise ValueError('不支持预览该类型资源')
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('资源超过 20 MiB')
        return path.read_bytes(), mime

    def settings(self):
        root = self.root()
        error = ''
        if root.exists() and not root.is_dir():
            error = '路径不是目录'
        return {'directory': str(root), 'directory_version': self.directory_version(),
                'exists': root.is_dir(), 'error': error, 'items': self.listing() if not error else []}

    @staticmethod
    def check_directory(value):
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError('请输入后台宿主机上的绝对目录路径')
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.note-check-', dir=path)
        os.close(fd)
        Path(name).unlink()
        return path

    @staticmethod
    def _manifest(root):
        result = {}
        if root.exists():
            for path in root.rglob('*'):
                InvestmentNotes.safe(root, path.relative_to(root))
                if path.is_file():
                    if path.name.startswith('.note-'):
                        raise ValueError('目录有正在写入的临时文件，请稍后重试')
                    result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
        return result

    def preview_directory(self, command):
        with self.lock:
            target = self.check_directory(command.get('directory', ''))
            source = self.root()
            if target == source or target.is_relative_to(source) or source.is_relative_to(target):
                raise ValueError('新旧目录不能相同，也不能互为子目录')
            mode = command.get('mode')
            if mode not in ('copy', 'use'):
                raise ValueError('请选择复制现有笔记或使用目标目录')
            src = self._manifest(source)
            dst = self._manifest(target)
            conflicts = [name for name, sha in src.items() if name in dst and sha != dst[name]] if mode == 'copy' else []
            items = self.listing(target)
            codes = [r['code'] for r in items if r.get('code')]
            if len(set(codes)) != len(codes):
                conflicts.append('目标目录有同股票重复文件')
            if mode == 'copy':
                combined = {r['file']: r for r in self.listing(source)}
                combined.update({r['file']: r for r in items})
                combined_codes = [r['code'] for r in combined.values() if r.get('code')]
                if len(set(combined_codes)) != len(combined_codes):
                    conflicts.append('合并后同一股票会有多份笔记')
            required = sum((source / n).stat().st_size for n in src if n not in dst) if mode == 'copy' else 0
            if shutil.disk_usage(target).free < required:
                conflicts.append('目标磁盘可用空间不足')
            token = secrets.token_urlsafe(24)
            self.previews = {token: {'source': source, 'target': target, 'mode': mode, 'src': src, 'dst': dst,
                                     'expires': time.time() + 600, 'conflicts': conflicts}}
            return {'token': token, 'directory': str(target), 'mode': mode, 'files': len(src) if mode == 'copy' else len(dst),
                    'bytes_to_copy': required, 'conflicts': conflicts, 'target_notes': items}

    def apply_directory(self, command):
        with self.lock:
            preview = self.previews.pop(command.get('token'), None)
            if not preview or preview['expires'] < time.time():
                raise ValueError('目录预览已过期，请重新检查')
            source, target = preview['source'], preview['target']
            if preview['conflicts']:
                raise ValueError('请先处理目录冲突')
            if source != self.root() or self._manifest(source) != preview['src'] or self._manifest(target) != preview['dst']:
                raise NoteConflict('预览后目录内容已变化，请重新预览')
            if preview['mode'] == 'copy':
                for name, sha in preview['src'].items():
                    path = self.safe(target, name)
                    if name not in preview['dst']:
                        atomic_write(path, self.safe(source, name).read_bytes())
                    if digest(path.read_bytes()) != sha:
                        raise ValueError('复制文件校验失败，原目录仍生效')
                if self._manifest(source) != preview['src']:
                    raise NoteConflict('复制期间原目录发生变化，未切换目录；请重新预览')
                expected = {**preview['dst'], **preview['src']}
                if self._manifest(target) != expected:
                    raise NoteConflict('复制期间目标目录发生变化，未切换目录')
            atomic_write(self.config, json.dumps({'directory': str(target)}, ensure_ascii=False).encode())
            return self.settings()

    @staticmethod
    def choose_directory():
        script = ('import tkinter as t; from tkinter import filedialog; '
                  'r=t.Tk(); r.withdraw(); r.attributes("-topmost",True); '
                  'p=filedialog.askdirectory(title="选择投资笔记存储目录",parent=r); '
                  'print(p); r.destroy()')
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise ValueError('宿主机目录选择窗口不可用，请输入绝对路径后检查')
        return {'directory': result.stdout.strip()}
