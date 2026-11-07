"""Thread-safe local lifecycle control; no UI or business state ownership."""
from threading import RLock, Thread


class RuntimeControl:
    def __init__(self):
        self.lock = RLock()
        self.phase = 'starting'
        self.error = ''
        self.server = None
        self.activity_check = None
        self.shutdown_started = False
        self.cleanup_error = ''

    def snapshot(self):
        with self.lock:
            return self.phase, self.error

    def bind(self, server, activity_check):
        with self.lock:
            self.server, self.activity_check = server, activity_check
            self.shutdown_started = False
        original = server.service_actions

        def service_actions():
            original()
            with self.lock:
                if self.phase in ('starting', 'restoring'):
                    self.phase = 'running'
                if self.phase == 'stopping' and not self.shutdown_started:
                    # Called inside serve_forever: shutdown must run elsewhere.
                    self.shutdown_started = True
                    Thread(target=server.shutdown, name='desktop-shutdown').start()

        server.service_actions = service_actions

    def request_stop(self):
        with self.lock:
            if self.phase == 'stopping':
                return ''
            if self.phase != 'running' or self.server is None:
                return '后台正在启动、恢复或已停止，请稍候。'
            gate = self.server.request_gate
            with gate.lock:
                # All HTTP launches and scheduler launches enter this gate first.
                if gate.blocked or gate.active:
                    return '页面请求或后台维护正在处理，请稍候再退出。'
                gate.blocked = True
            try:
                if self.activity_check():
                    raise ValueError('采集或分析任务尚未完成，请先在页面完成或停止任务。')
            except Exception:
                with gate.lock:
                    gate.blocked = False
                raise
            self.phase = 'stopping'
            return ''

    def detach(self, restoring=False):
        with self.lock:
            self.server = None
            self.phase = 'restoring' if restoring else 'stopping'

    def finish(self, error=''):
        with self.lock:
            self.server = None
            self.error = error
            self.phase = 'failed' if error else 'stopped'
