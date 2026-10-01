"""Official SIWC public-client OAuth and Responses streaming, without API keys."""
from __future__ import annotations

import base64
import ctypes
from datetime import datetime, timedelta, timezone
from ctypes import wintypes
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
from threading import RLock, Timer
import time
from urllib.parse import urlencode
from uuid import uuid4

import requests

AUTH = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
STREAM_ERRORS = {
    "subscription_sharing_usage_limit_exceeded": "ChatGPT 计划共享额度达到限制，请在 ChatGPT 设置的 Usage 中查看；也可能是本应用的额度限制",
    "subscription_sharing_usage_unavailable": "暂时无法核对 ChatGPT 额度，请稍后手动重试",
    "subscription_sharing_user_not_eligible": "当前账号、工作区或策略不允许共享 ChatGPT 计划额度",
    "subscription_sharing_unsupported_capability": "此接入不支持请求中的模型或功能",
    "subscription_sharing_route_not_supported": "此接入不支持请求的接口",
    "subscription_sharing_invalid_user": "无法验证当前订阅账号，请检查连接",
    "chatpass_v2_scope_not_authorized": "当前授权不允许此操作，请检查授权配置",
    "chatpass_v2_invalid_authorization_context": "当前授权上下文无效，请检查授权配置",
    "subscription_sharing_user_unavailable": "账号或工作区信息暂不可用，请稍后手动重试",
}


class ProviderError(ValueError):
    """Only fixed, credential-free messages may cross the application boundary."""

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def redact_diagnostic(value, credentials):
    """Preserve error shape while stripping credential fields and echoed secrets."""
    sensitive = {'authorization', 'access_token', 'refresh_token', 'id_token', 'client_secret',
                 'code_verifier', 'cookie', 'set-cookie'}
    secrets_to_remove = [str(credentials[key]) for key in ('access_token', 'refresh_token', 'id_token')
                         if credentials.get(key)]
    def clean(item):
        if isinstance(item, dict):
            return {key: '[REDACTED]' if key.lower().replace('-', '_') in
                    {name.replace('-', '_') for name in sensitive} else clean(val) for key, val in item.items()}
        if isinstance(item, list):
            return [clean(val) for val in item]
        if isinstance(item, str):
            for secret in secrets_to_remove:
                item = item.replace(secret, '[REDACTED]')
            return re.sub(r'(?i)Bearer\s+[^\s"\x27,}]+', 'Bearer [REDACTED]', item)
        return item
    return clean(value)


class WindowsCredentials:
    """Windows DPAPI user-bound encryption; no plaintext fallback on any platform."""
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _crypt(data, decrypt=False):
        if os.name != "nt":
            raise ProviderError("此版本凭据保护仅支持 Windows")
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
        buffer = ctypes.create_string_buffer(data)
        incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        outgoing = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise ProviderError("Windows 凭据保护失败，需要重新连接")
        try:
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))

    def load(self):
        if not self.path.exists():
            return None
        return json.loads(self._crypt(self.path.read_bytes(), True))

    def save(self, value):
        payload = self._crypt(json.dumps(value).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


def check_http(response):
    if response.status_code == 401:
        raise ProviderError("账号授权已失效，请重新连接")
    if response.status_code == 403:
        raise ProviderError("此账号尚未获得计划使用权限，请在 ChatGPT 设置中检查授权")
    if response.status_code == 429:
        raise ProviderError("ChatGPT 额度不足或请求受限，请稍后手动重试")
    if not 200 <= response.status_code < 300:
        raise ProviderError("OpenAI 请求失败，请稍后手动重试")


class ChatGPTProvider:
    def __init__(self, directory, *, session=None, credentials=None):
        self.directory = Path(directory)
        self.credentials = credentials or WindowsCredentials(self.directory / "credentials.dpapi")
        self.session = session or requests.Session()  # requests performs no automatic retries.
        self.lock = RLock()
        self.pending = None
        self.error = None
        self.catalog = []
        self.responses = {}
        self.discovery = None
        self.config_path = self.directory / "registration.json"
        self.config = json.loads(self.config_path.read_text()) if self.config_path.exists() else {}
        self.record = None
        try:
            self.record = self.credentials.load()
        except (ValueError, OSError):
            self.error = "本地凭据不可用，请重新连接"

    def _save_config(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config), encoding="utf-8")
        temporary.replace(self.config_path)

    def status(self):
        with self.lock:
            if self.pending and self.pending["expires_at"] < time.monotonic():
                self.pending = None
                self.error = "登录会话已过期，请重新连接"
            connected = bool(self.record)
            enabled = connected and PLAN_SCOPE in self.record.get("scopes", [])
            return {"connected": connected, "plan_authorized": bool(enabled),
                    "account": self.record.get("name") or self.record.get("email") if connected else None,
                    "account_ref": self.record.get("account_ref") if connected else None,
                    "connecting": bool(self.pending), "error": self.error}

    def connect(self, port):
        with self.lock:
            if "ext_agent_host_id" not in self.config:
                self.config["ext_agent_host_id"] = "urn:uuid:" + str(uuid4())
                self._save_config()
            client = self.config.get("client_id", "dynamic_agent_client")
            verifier = secrets.token_urlsafe(64)
            pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                "verifier": verifier, "client_id": client, "expires_at": time.monotonic() + 600,
                "redirect_uri": f"http://127.0.0.1:{port}/auth/callback",
                "subject": self.record.get("subject") if self.record else self.config.get("subject")}
            params = {"client_id": client, "ext_agent_host_id": self.config["ext_agent_host_id"],
                "response_type": "code", "redirect_uri": pending["redirect_uri"],
                "scope": "openid profile email offline_access resource.invoke " + PLAN_SCOPE,
                "resource": RESOURCE, "state": pending["state"], "nonce": pending["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
            if client == "dynamic_agent_client":
                params["agent_name_hint"] = "A股本地综合研判"
            self.pending = pending
            self.error = None
            return {"authorization_url": AUTH + "/api/accounts/authorize?" + urlencode(params)}

    def _discovery(self):
        if not self.discovery:
            response = self.session.get(AUTH + "/.well-known/openid-configuration", timeout=(15, 15))
            check_http(response)
            discovery = response.json()
            if discovery.get("issuer") != AUTH or not discovery.get("jwks_uri", "").startswith(AUTH + "/"):
                raise ProviderError("OpenAI 身份配置校验失败")
            self.discovery = discovery
        return self.discovery

    def _identity(self, token, client, nonce=None):
        import jwt
        response = self.session.get(self._discovery()["jwks_uri"], timeout=(15, 15))
        check_http(response)
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256"}:
            raise ProviderError("身份令牌算法不支持")
        keys = [k for k in response.json()["keys"] if k.get("kid") == header.get("kid")]
        if len(keys) != 1:
            raise ProviderError("身份签名密钥不存在")
        key = jwt.PyJWK.from_dict(keys[0]).key
        claims = jwt.decode(token, key, algorithms=[header["alg"]], audience=client, issuer=AUTH,
                            options={"require": ["sub", "exp", "iat"]}, leeway=5)
        if not isinstance(claims["sub"], str) or not claims["sub"] or nonce is not None and claims.get("nonce") != nonce:
            raise ProviderError("身份令牌校验失败")
        return claims

    def callback(self, query):
        with self.lock:
            pending = self.pending
            if not pending or pending["expires_at"] < time.monotonic() or not secrets.compare_digest(
                    str(query.get("state", "")), pending["state"]):
                raise ProviderError("登录会话已失效或校验失败，请重新连接")
            self.pending = None  # consume once, including denial and token-exchange failure.
            if query.get("error"):
                self.error = "登录或计划授权未完成，请重新连接"
                raise ProviderError(self.error)
            client = query.get("client_id", pending["client_id"])
            if not isinstance(client, str) or not client.startswith("oaiapp_") or (
                    pending["client_id"] != "dynamic_agent_client" and client != pending["client_id"]):
                raise ProviderError("登录返回的客户端身份不匹配")
            if not query.get("code"):
                raise ProviderError("登录缺少授权码")
            try:
                response = self.session.post(AUTH + "/api/accounts/oauth/token", data={
                    "grant_type": "authorization_code", "client_id": client, "code": query["code"],
                    "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"],
                    "resource": RESOURCE}, timeout=(15, 30))
                check_http(response)
                tokens = response.json()
                claims = self._identity(tokens["id_token"], client, pending["nonce"])
                if pending["subject"] and claims["sub"] != pending["subject"]:
                    raise ProviderError("重新登录账号与原账号不匹配，请先断开连接")
                record = {"client_id": client, "subject": claims["sub"], "name": claims.get("name"),
                    "email": claims.get("email"), "account_ref": hashlib.sha256((client + claims["sub"]).encode()).hexdigest(),
                    "scopes": tokens.get("scope", "").split(), "access_token": tokens.get("access_token"),
                    "refresh_token": tokens.get("refresh_token"), "id_token": tokens["id_token"],
                    "expires_at": time.time() + float(tokens.get("expires_in", 0))}
                self.credentials.save(record)
                self.config.update(client_id=client, subject=claims["sub"])
                self._save_config()
                self.record, self.error, self.catalog = record, None, []
            except Exception:
                self.error = "连接校验失败，请检查网络后重新连接"
                raise ProviderError(self.error) from None

    def _access(self):
        with self.lock:
            record = self.record
            if not record or PLAN_SCOPE not in record.get("scopes", []):
                raise ProviderError("尚未连接或未授权使用 ChatGPT 额度，请打开 AI 设置")
            if record["expires_at"] < time.time() + 60:
                try:
                    response = self.session.post(AUTH + "/api/accounts/oauth/token", data={
                        "grant_type": "refresh_token", "client_id": record["client_id"],
                        "refresh_token": record["refresh_token"], "resource": RESOURCE}, timeout=(15, 30))
                    check_http(response)
                    tokens = response.json()
                    if tokens.get("id_token"):
                        if self._identity(tokens["id_token"], record["client_id"])["sub"] != record["subject"]:
                            raise ProviderError("账号身份发生变化")
                    refreshed = {**record, **{k: tokens[k] for k in ("access_token", "refresh_token", "id_token") if k in tokens},
                                 "expires_at": time.time() + float(tokens["expires_in"])}
                    if "scope" in tokens:
                        refreshed["scopes"] = tokens["scope"].split()
                    self.credentials.save(refreshed)
                    self.record = record = refreshed
                except Exception:
                    self.record = None
                    self.error = "授权刷新失败，需要重新连接"
                    self.credentials.clear()
                    raise ProviderError(self.error) from None
            if not record.get("access_token") or PLAN_SCOPE not in record["scopes"]:
                raise ProviderError("尚未授权使用 ChatGPT 额度")
            return record["access_token"]

    def models(self):
        response = self.session.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + self._access()}, timeout=(15, 30))
        check_http(response)
        self.catalog = [{"slug": m["slug"], "display_name": m["display_name"]}
                        for m in response.json().get("models", []) if m.get("visibility") == "list"]
        return self.catalog

    def cancel(self, run_id):
        with self.lock:
            response = self.responses.get(run_id)
        if response is not None:
            response.close()

    def disconnect(self):
        with self.lock:
            confirmed = not self.record
            if self.record and self.record.get("refresh_token"):
                try:
                    endpoint = self._discovery().get("revocation_endpoint")
                    if endpoint and endpoint.startswith(AUTH + "/"):
                        response = self.session.post(endpoint, data={"token": self.record["refresh_token"],
                            "token_type_hint": "refresh_token", "client_id": self.record["client_id"]}, timeout=(15, 15))
                        confirmed = response.status_code == 200
                except Exception:
                    pass
            self.record, self.pending, self.catalog = None, None, []
            self.credentials.clear()
            self.error = None if confirmed else "本地已断开，远程撤销未确认，可在 ChatGPT 设置中断开应用"
            return self.status()

    def infer(self, run_id, model, instructions, input_data, cancelled):
        if cancelled.is_set():
            raise ProviderError("分析已取消")
        token = self._access()
        if cancelled.is_set():
            raise ProviderError("分析已取消")
        response = self.session.post(RESOURCE + "/responses", headers={"Authorization": "Bearer " + token},
            json={"model": model, "instructions": instructions, "input": [{"role": "user", "content": json.dumps(input_data, ensure_ascii=False)}],
                  "store": False, "stream": True}, stream=True, timeout=(15, 60))
        credentials = {**self.record, 'access_token': token}
        response_headers = getattr(response, 'headers', {})
        diagnostic = {
            'url': RESOURCE + '/responses', 'method': 'POST', 'model': model,
            'received_at_beijing': datetime.now(timezone(timedelta(hours=8))).isoformat(),
            'http_status': response.status_code,
            'response_headers': {key: response_headers.get(key) for key in ('x-request-id', 'Retry-After')},
            'account_ref': self.record.get('account_ref'), 'client_id': self.record.get('client_id'),
            'registered_subject_matches': bool(self.record.get('subject')) and self.config.get('subject') == self.record.get('subject'),
            'workspace_independently_verified': False,
            'automatic_retries': 0,
        }
        try:
            check_http(response)
        except ProviderError as exc:
            try:
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > 262144:
                        diagnostic['raw_error_truncated'] = True
                        break
                text = bytes(body[:262144]).decode('utf-8', errors='replace')
                try:
                    diagnostic['raw_error_response'] = json.loads(text)
                except ValueError:
                    diagnostic['raw_error_response'] = text
            except (requests.RequestException, OSError):
                diagnostic['raw_error_unavailable'] = True
            finally:
                response.close()
            exc.diagnostic = redact_diagnostic(diagnostic, credentials)
            raise
        with self.lock:
            self.responses[run_id] = response
        timer = Timer(180, response.close)
        timer.daemon = True
        timer.start()
        started, buffer = time.monotonic(), []
        text_parts, text_size = [], 0
        try:
            for line in response.iter_lines(chunk_size=1, decode_unicode=False):
                if cancelled.is_set():
                    raise ProviderError("分析已取消")
                if time.monotonic() - started > 180:
                    raise ProviderError("分析超时，请手动重试")
                if line.startswith(b"data:"):
                    buffer.append(line[5:].strip().decode("utf-8"))
                    if sum(map(len, buffer)) > 262144:
                        raise ProviderError("模型事件超出大小限制")
                elif not line and buffer:
                    text = "\n".join(buffer)
                    buffer.clear()
                    if text == "[DONE]":
                        continue
                    event = json.loads(text)
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ProviderError("模型文本事件格式无效")
                        text_size += len(delta)
                        if text_size > 65536:
                            raise ProviderError("模型输出超过限制")
                        text_parts.append(delta)
                    if kind in {"response.failed", "response.incomplete", "error", "response.refusal.done"}:
                        error = event.get("response", {}).get("error") or event.get("error") or event
                        diagnostic.update(terminal_event=kind, raw_error_response=error,
                                          response_id=event.get('response', {}).get('id'))
                        code = error.get("code") if isinstance(error, dict) else None
                        if isinstance(code, str) and code in STREAM_ERRORS:
                            raise ProviderError(STREAM_ERRORS[code] + "（" + code + "）")
                        raise ProviderError("模型未成功完成或拒绝分析")
                    if kind == "response.completed":
                        diagnostic['terminal_event'] = kind
                        result = event.get("response", {})
                        if result.get("status") != "completed":
                            raise ProviderError("模型缺少成功终态")
                        content = [part for item in result.get("output", []) for part in item.get("content", [])]
                        if any(p.get("type") == "refusal" for p in content):
                            raise ProviderError("模型拒绝分析")
                        output = "".join(p.get("text", "") for p in content if p.get("type") == "output_text")
                        if not output:
                            output = "".join(text_parts)
                        if not output or len(output) > 65536:
                            raise ProviderError("模型输出为空或超过限制")
                        return {"text": output, "response_id": result.get("id"), "model": result.get("model"),
                                "usage": result.get("usage"), "diagnostic": redact_diagnostic(diagnostic, credentials)}
            raise ProviderError("响应流中断，未收到成功终态")
        except (requests.RequestException, OSError, ValueError) as exc:
            if isinstance(exc, ProviderError):
                exc.diagnostic = redact_diagnostic(diagnostic, credentials)
                raise
            raise ProviderError("模型请求中断或网络不可用，请手动重试",
                                diagnostic=redact_diagnostic(diagnostic, credentials)) from None
        finally:
            timer.cancel()
            response.close()
            with self.lock:
                self.responses.pop(run_id, None)
