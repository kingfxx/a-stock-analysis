"""Start the local quarterly A-share analysis page."""

import argparse

from quarterly_dashboard.server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地 A 股季度分析页面")
    parser.add_argument("--port", type=int, default=8765)
    serve(parser.parse_args().port)
