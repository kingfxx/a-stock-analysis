"""Bounded UTF-8 desktop logs, including existing print-based diagnostics."""
import io
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading


class DesktopLogHandler(RotatingFileHandler):
    def handleError(self, record):
        # The default handler writes to stderr, which we redirect back here.
        if not getattr(self, 'reported_error', False):
            self.reported_error = True
            from stock_analysis.desktop.windows_instance import show_error
            show_error('桌面日志写入失败，请检查磁盘空间和日志目录权限。')


class LogStream(io.TextIOBase):
    def __init__(self, logger, level):
        self.logger, self.level = logger, level
        self.local = threading.local()

    @property
    def encoding(self):
        return 'utf-8'

    def write(self, text):
        pending = getattr(self.local, 'pending', '') + text
        lines = pending.split('\n')
        self.local.pending = lines.pop()
        for line in lines:
            if line.strip():
                self.logger.log(self.level, line.rstrip('\r'))
        # Bound partial writes, too.
        if len(self.local.pending) > 8192:
            self.flush()
        return len(text)

    def flush(self):
        pending = getattr(self.local, 'pending', '')
        if pending:
            self.local.pending = ''
            self.logger.log(self.level, pending)


def configure_logs(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handler = DesktopLogHandler(directory / 'desktop.log', maxBytes=2*1024*1024,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
    logger = logging.getLogger('stock_analysis.desktop')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    sys.stdout = LogStream(logger, logging.INFO)
    sys.stderr = LogStream(logger, logging.ERROR)
    sys.excepthook = lambda kind, value, tb: logger.critical('未处理异常', exc_info=(kind, value, tb))
    threading.excepthook = lambda args: logger.critical(
        '线程未处理异常：%s', args.thread.name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    return logger
