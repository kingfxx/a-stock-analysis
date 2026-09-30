"""Shared connection settings for public market-data sources."""

import os

import requests


def create_data_session() -> requests.Session:
    """Connect directly unless the user explicitly opts into system proxies.

    Requests also discovers Windows registry proxies. A local HTTP proxy can
    therefore be interpreted as an HTTPS proxy and fail during its TLS handshake.
    Keep TLS certificate verification enabled in either mode.
    """
    session = requests.Session()
    session.trust_env = os.getenv("DASHBOARD_USE_SYSTEM_PROXY", "").strip().lower() in (
        "1", "true", "yes",
    )
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session
