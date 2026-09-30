import requests

from quarterly_dashboard.network import create_data_session


def test_default_session_ignores_environment_and_windows_proxies(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USE_SYSTEM_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    # Simulate Windows proxy discovery independently of this test machine.
    monkeypatch.setattr(requests.sessions, "get_environ_proxies",
                        lambda *args, **kwargs: {"https": "https://127.0.0.1:7890"})
    with create_data_session() as session:
        settings = session.merge_environment_settings("https://quotes.sina.cn/", {}, False, None, None)
        assert settings["proxies"] == {}
        assert settings["verify"] is True


def test_opt_in_session_uses_proxy_and_keeps_certificate_verification(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USE_SYSTEM_PROXY", "1")
    monkeypatch.setattr(requests.sessions, "get_environ_proxies",
                        lambda *args, **kwargs: {"https": "http://127.0.0.1:7890"})
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    with create_data_session() as session:
        settings = session.merge_environment_settings("https://quotes.sina.cn/", {}, False, None, None)
        assert settings["proxies"]["https"] == "http://127.0.0.1:7890"
        assert settings["verify"] is True
