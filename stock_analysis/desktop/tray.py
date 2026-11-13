"""Windows tray presentation; runtime remains the sole backend lifecycle owner."""
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
import webbrowser

from stock_analysis.desktop.windows_instance import DesktopInstance, show_error
from stock_analysis.runtime_control import RuntimeControl

LABELS = dict(starting='启动中', running='运行中', restoring='恢复中',
              stopping='退出中', stopped='已停止', failed='运行失败')


def run(port=8765, *, open_browser=True):
    # Resolve identity before importing the backend's large dependency graph.
    from stock_analysis.infrastructure.app_settings import AppSettings
    with DesktopInstance(AppSettings().DATABASE_PATH) as instance:
        if not instance.primary:
            instance.activate()
            return
        import pystray
        from PIL import Image
        from stock_analysis.http.server import serve

        logger = logging.getLogger('stock_analysis.desktop')
        control = RuntimeControl()
        done = Event()
        requested_open = Event()
        restart_requested = Event()
        action_lock = Lock()
        if open_browser:
            requested_open.set()
        logs = AppSettings().ROOT / 'data' / 'verification' / 'logs'
        url = f'http://127.0.0.1:{port}/'

        def background():
            try:
                serve(port, control=control)
            except BaseException as exc:
                logger.exception('后台运行失败')
                control.finish(str(exc))
            else:
                control.finish()

        worker = Thread(target=background, name='dashboard-http')

        def open_page(icon, item):
            requested_open.set()

        def status_label():
            phase, _ = control.snapshot()
            return '重启中' if phase == 'stopping' and restart_requested.is_set() else LABELS[phase]

        def stop(icon, item, *, restart=False):
            def request():
                with action_lock:
                    operation = '重启' if restart else '退出'
                    try:
                        phase, _ = control.snapshot()
                        if phase in ('stopping', 'stopped'):
                            return  # First accepted exit/restart wins.
                        if phase == 'failed' and not restart:
                            if control.cleanup_error:
                                raise ValueError('后台资源清理失败，暂不退出以保留现场，请查看日志。')
                            done.set()
                            icon.stop()
                            return
                        message = control.request_stop()
                        if message:
                            logger.info('%s请求未执行：%s', operation, message)
                            icon.notify(message, f'暂时无法{operation}')
                        else:
                            if restart:
                                restart_requested.set()
                            logger.info('已接受%s请求', operation)
                    except Exception as exc:
                        logger.exception('%s请求未执行', operation)
                        icon.notify(str(exc), f'暂时无法{operation}')
            Thread(target=request, name='desktop-exit-request').start()

        def setup(icon):
            icon.visible = True
            worker.start()
            last_phase = None
            while not done.is_set():
                if instance.wait():
                    requested_open.set()
                if instance.restart_pending():
                    stop(icon, None, restart=True)
                phase, error = control.snapshot()
                if phase != last_phase:
                    logger.info('生命周期：%s', phase)
                    icon.title = f'A 股分析 · {status_label()} · {port}'
                    icon.update_menu()
                    last_phase = phase
                    if phase == 'failed':
                        show_error(f'后台运行失败：{error}\n\n日志：{logs / "desktop.log"}\n'
                                   '若已有命令行后台，请先安全停止原后台再启动托盘。')
                if phase == 'running' and requested_open.is_set():
                    requested_open.clear()
                    try:
                        if not webbrowser.open(url):
                            icon.notify(f'请手动打开 {url}', '浏览器未打开')
                    except OSError:
                        logger.exception('浏览器打开失败')
                        icon.notify(f'请手动打开 {url}', '浏览器未打开')
                if phase == 'stopped':
                    done.set()
                    icon.stop()

        image = Image.open(Path(__file__).with_name('assets') / 'dashboard.ico')
        icon = pystray.Icon('a-stock-analysis', image, 'A 股分析 · 启动中', menu=pystray.Menu(
            pystray.MenuItem('打开页面', open_page, default=True,
                            enabled=lambda item: control.snapshot()[0] == 'running'),
            pystray.MenuItem(lambda item: f'后台：{status_label()} · {port}', None, enabled=False),
            pystray.MenuItem('打开日志目录', lambda icon, item: os.startfile(logs)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('重启后台', lambda icon, item: stop(icon, item, restart=True),
                            enabled=lambda item: control.snapshot()[0] == 'running'),
            pystray.MenuItem('退出程序', stop,
                            enabled=lambda item: control.snapshot()[0] in ('running', 'failed')),
        ))
        try:
            icon.run(setup=setup)
        finally:
            done.set()
            image.close()
            if worker.ident is not None:
                worker.join()
        with action_lock:
            return restart_requested.is_set() and control.snapshot()[0] == 'stopped'
