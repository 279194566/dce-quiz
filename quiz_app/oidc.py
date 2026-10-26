from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, urlunparse

import httpx


@dataclass
class OIDCError(Exception):
    safe_message: str
    status_code: int = 502

    def __str__(self) -> str:
        return self.safe_message


class DCEOIDCClient:
    """DCE/Ghippo OIDC authorization-code client.

    Ghippo's discovery document advertises cluster-internal token and userinfo
    origins. Their Keycloak paths are valid, so back-channel endpoints preserve
    the discovered path while replacing only the origin with DCE_BASE_URL.
    """

    api_prefix = "/apis/ghippo.io/v1alpha1"

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str = "openid profile email",
        ca_bundle: str = "",
        insecure_skip_tls_verify: bool = False,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.verify: bool | str = False if insecure_skip_tls_verify else (ca_bundle or True)
        self._discovery: dict | None = None

    def _client(self, *, access_token: str = "") -> httpx.Client:
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            verify=self.verify,
            timeout=self.timeout_seconds,
            transport=self.transport,
            trust_env=False,
        )

    @staticmethod
    def _safe_json(response: httpx.Response, failure_message: str) -> dict:
        if response.status_code >= 400:
            raise OIDCError(failure_message, 401 if response.status_code in {400, 401, 403} else 502)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OIDCError(failure_message) from exc
        if not isinstance(payload, dict):
            raise OIDCError(failure_message)
        return payload

    def discovery(self) -> dict:
        if self._discovery is not None:
            return self._discovery
        try:
            with self._client() as client:
                response = client.get(f"{self.api_prefix}/.well-known/openid-configuration")
        except httpx.HTTPError as exc:
            raise OIDCError("无法读取 DCE OIDC 发现信息") from exc
        self._discovery = self._safe_json(response, "DCE OIDC 发现信息无效")
        return self._discovery

    def authorization_endpoint(self) -> str:
        payload = self.discovery()
        endpoint = str(
            payload.get("authorizationEndpoint") or payload.get("authorization_endpoint") or ""
        )
        parsed_base = urlparse(self.base_url)
        parsed_endpoint = urlparse(endpoint)
        if (
            parsed_endpoint.scheme != "https"
            or parsed_endpoint.netloc != parsed_base.netloc
            or not parsed_endpoint.path
        ):
            raise OIDCError("DCE OIDC 授权地址不可信")
        return endpoint

    def end_session_endpoint(self) -> str:
        raw = str(
            self.discovery().get("endSessionEndpoint")
            or self.discovery().get("end_session_endpoint")
            or ""
        )
        parsed = urlparse(raw)
        base = urlparse(self.base_url)
        if (
            base.scheme != "https"
            or not base.netloc
            or not parsed.path.startswith("/auth/realms/ghippo/protocol/openid-connect/")
        ):
            raise OIDCError("DCE OIDC 退出地址不可信")
        return urlunparse((base.scheme, base.netloc, parsed.path, "", parsed.query, ""))

    def end_session_url(self, *, id_token_hint: str, post_logout_redirect_uri: str) -> str:
        parameters = {
            "client_id": self.client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri,
        }
        if id_token_hint:
            parameters["id_token_hint"] = id_token_hint
        return f"{self.end_session_endpoint()}?{urlencode(parameters)}"

    def _backchannel_endpoint(self, camel_name: str, snake_name: str) -> str:
        raw = str(self.discovery().get(camel_name) or self.discovery().get(snake_name) or "")
        parsed = urlparse(raw)
        base = urlparse(self.base_url)
        if not parsed.path.startswith("/auth/realms/ghippo/protocol/openid-connect/"):
            raise OIDCError("DCE OIDC 后端地址不可信")
        return urlunparse((base.scheme, base.netloc, parsed.path, "", parsed.query, ""))

    def authorization_url(self, *, state: str, prompt: str = "") -> str:
        if prompt not in {"", "login"}:
            raise OIDCError("不支持的 OIDC 登录提示参数", 400)
        parameters = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
        }
        if prompt:
            parameters["prompt"] = prompt
        query = urlencode(parameters)
        return f"{self.authorization_endpoint()}?{query}"

    def exchange_code(self, code: str) -> dict:
        try:
            with httpx.Client(
                verify=self.verify,
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={"Accept": "application/json"},
                trust_env=False,
            ) as client:
                response = client.post(
                    self._backchannel_endpoint("tokenEndpoint", "token_endpoint"),
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                        "scope": self.scopes,
                    },
                )
        except httpx.HTTPError as exc:
            raise OIDCError("无法连接 DCE OIDC 令牌端点") from exc
        payload = self._safe_json(response, "DCE 登录凭据交换失败")
        access_token = str(payload.get("accessToken") or payload.get("access_token") or "")
        if not access_token:
            raise OIDCError("DCE OIDC 响应缺少访问令牌")
        return {
            **payload,
            "accessToken": access_token,
            "idToken": str(payload.get("idToken") or payload.get("id_token") or ""),
        }

    def userinfo(self, access_token: str) -> dict:
        try:
            with httpx.Client(
                verify=self.verify,
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
                trust_env=False,
            ) as client:
                response = client.get(
                    self._backchannel_endpoint("userinfoEndpoint", "userinfo_endpoint")
                )
        except httpx.HTTPError as exc:
            raise OIDCError("无法连接 DCE OIDC 用户信息端点") from exc
        payload = self._safe_json(response, "DCE OIDC 用户信息校验失败")
        subject = str(payload.get("sub") or "")
        username = str(payload.get("preferredUsername") or payload.get("preferred_username") or "")
        if not subject or not username:
            raise OIDCError("DCE OIDC 用户信息缺少主体或用户名", 401)
        return {**payload, "sub": subject, "preferredUsername": username}
