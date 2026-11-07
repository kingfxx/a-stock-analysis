"""Access logging is filtered by response status, without suppressing errors."""
from http import HTTPStatus

import pytest

from stock_analysis.http.server import Handler


def make_handler(path='/'):
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.client_address = ('127.0.0.1', 12345)
    handler.requestline = f'GET {path} HTTP/1.1'
    return handler


@pytest.mark.parametrize('status', [100,200,204,301,302,304,399,'200',HTTPStatus.OK])
def test_successful_and_redirected_requests_do_not_write_access_logs(status, capsys):
    make_handler().log_request(status)
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('status', [400,404,500,503,'404',HTTPStatus.SERVICE_UNAVAILABLE,'-'])
def test_failed_or_unknown_status_is_still_logged(status, capsys):
    make_handler('/api/test').log_request(status)
    assert 'GET /api/test HTTP/1.1' in capsys.readouterr().err


def test_protocol_errors_are_not_suppressed(capsys):
    make_handler().log_error('bad request: %s', 'invalid syntax')
    assert 'bad request: invalid syntax' in capsys.readouterr().err


def test_failed_auth_callback_still_does_not_leak_query_parameters(capsys):
    handler = make_handler('/auth/callback?code=secret-code&state=secret-state')
    handler.log_request(500)
    handler.log_error('callback failed')
    assert capsys.readouterr().err == ''
