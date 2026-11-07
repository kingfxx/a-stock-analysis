"""Double-click/shortcut entry for the optional Windows tray application."""
from pathlib import Path

if __name__ == '__main__':
    try:
        import argparse
        parser = argparse.ArgumentParser(description='A 股分析 Windows 托盘')
        parser.add_argument('--port', type=int, default=8765)
        parser.add_argument('--no-open-browser', action='store_true', help='不自动打开浏览器')
        parser.add_argument('--restart-parent', type=int, help=argparse.SUPPRESS)
        args = parser.parse_args()
        if args.restart_parent is not None:
            from stock_analysis.desktop.restart import wait_for_parent
            wait_for_parent(args.restart_parent)
        from stock_analysis.desktop.logging_setup import configure_logs
        configure_logs(Path(__file__).resolve().parent / 'data' / 'verification' / 'logs')
        from stock_analysis.desktop.tray import run
        if run(args.port, open_browser=not args.no_open_browser):
            from stock_analysis.desktop.restart import launch_replacement
            try:
                launch_replacement(Path(__file__), args.port)
            except Exception as exc:
                import logging
                logging.getLogger('stock_analysis.desktop').exception('重启进程启动失败')
                from stock_analysis.desktop.windows_instance import show_error
                show_error(f'原后台已安全停止，但新进程未能启动：{exc}\n请使用桌面快捷方式重新启动。')
    except Exception as exc:
        import logging
        logging.getLogger('stock_analysis.desktop').exception('桌面入口启动失败')
        from stock_analysis.desktop.windows_instance import show_error
        show_error(f'启动失败：{exc}\n请检查 desktop.log，以及 requirements-desktop.txt 中的依赖。')
