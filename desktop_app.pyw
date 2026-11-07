"""Double-click/shortcut entry for the optional Windows tray application."""
from pathlib import Path

if __name__ == '__main__':
    try:
        from stock_analysis.desktop.logging_setup import configure_logs
        configure_logs(Path(__file__).resolve().parent / 'data' / 'verification' / 'logs')
        import argparse
        parser = argparse.ArgumentParser(description='A 股分析 Windows 托盘')
        parser.add_argument('--port', type=int, default=8765)
        args = parser.parse_args()
        from stock_analysis.desktop.tray import run
        run(args.port)
    except Exception as exc:
        import logging
        logging.getLogger('stock_analysis.desktop').exception('桌面入口启动失败')
        from stock_analysis.desktop.windows_instance import show_error
        show_error(f'启动失败：{exc}\n请检查 desktop.log，以及 requirements-desktop.txt 中的依赖。')
