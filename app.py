"""Start the local quarterly A-share analysis page."""

import argparse

from quarterly_dashboard.server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地 A 股季度分析页面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true", help="启动后在默认浏览器打开页面")
    args = parser.parse_args()
    serve(args.port, open_browser=args.open_browser)
