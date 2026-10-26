from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

import httpx

from quiz_app.oidc import DCEOIDCClient, OIDCError


class DCEOIDCClientTests(unittest.TestCase):
    def build_client(self, handler) -> DCEOIDCClient:
        return DCEOIDCClient(
            base_url="https://dce.example.test",
            client_id="dce-quiz-test",
            client_secret="client-secret-value",
            redirect_uri="http://127.0.0.1:8000/auth/callback",
            transport=httpx.MockTransport(handler),
        )

    def test_authorization_url_uses_discovered_external_endpoint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                "/apis/ghippo.io/v1alpha1/.well-known/openid-configuration",
                request.url.path,
            )
            return httpx.Response(
                200,
                json={
                    "issuer": "https://dce.example.test/auth/realms/ghippo",
                    "authorizationEndpoint": "https://dce.example.test/auth/realms/ghippo/protocol/openid-connect/auth",
                    "tokenEndpoint": "http://cluster-internal/token",
                },
            )

        target = self.build_client(handler).authorization_url(state="state-1")
        parsed = urlparse(target)
        query = parse_qs(parsed.query)
        self.assertEqual("https", parsed.scheme)
        self.assertEqual("dce.example.test", parsed.netloc)
        self.assertEqual(["state-1"], query["state"])
        self.assertEqual(["openid profile email"], query["scope"])
        self.assertNotIn("prompt", query)

    def test_end_session_url_uses_dce_origin_and_logout_hint(self) -> None:
        client = self.build_client(lambda _: httpx.Response(500))
        client._discovery = {
            "endSessionEndpoint": "http://keycloak-internal/auth/realms/ghippo/protocol/openid-connect/logout",
        }
        target = client.end_session_url(
            id_token_hint="id-token-value",
            post_logout_redirect_uri="http://127.0.0.1:8000/auth/callback",
        )
        parsed = urlparse(target)
        query = parse_qs(parsed.query)
        self.assertEqual("https", parsed.scheme)
        self.assertEqual("dce.example.test", parsed.netloc)
        self.assertEqual(
            "/auth/realms/ghippo/protocol/openid-connect/logout",
            parsed.path,
        )
        self.assertEqual(["dce-quiz-test"], query["client_id"])
        self.assertEqual(["id-token-value"], query["id_token_hint"])
        self.assertEqual(
            ["http://127.0.0.1:8000/auth/callback"],
            query["post_logout_redirect_uri"],
        )

    def test_rejects_end_session_endpoint_outside_ghippo(self) -> None:
        client = self.build_client(lambda _: httpx.Response(500))
        client._discovery = {"endSessionEndpoint": "https://dce.example.test/logout"}
        with self.assertRaises(OIDCError):
            client.end_session_endpoint()

    def test_switch_account_authorization_forces_login_prompt(self) -> None:
        client = self.build_client(lambda _: httpx.Response(500))
        client._discovery = {
            "authorizationEndpoint": "https://dce.example.test/auth/realms/ghippo/protocol/openid-connect/auth"
        }
        target = client.authorization_url(state="state-2", prompt="login")
        self.assertEqual(["login"], parse_qs(urlparse(target).query)["prompt"])

    def test_rejects_discovered_authorization_endpoint_on_another_origin(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"authorizationEndpoint": "https://attacker.example/authorize"},
            )

        with self.assertRaises(OIDCError):
            self.build_client(handler).authorization_endpoint()

    def test_code_exchange_and_userinfo_externalize_discovered_keycloak_paths(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/openid-connect/token"):
                payload = parse_qs(request.content.decode())
                self.assertEqual(["authorization-code"], payload["code"])
                self.assertEqual(["client-secret-value"], payload["client_secret"])
                return httpx.Response(200, json={"access_token": "access-token"})
            if request.url.path.endswith("/openid-connect/userinfo"):
                self.assertEqual("Bearer access-token", request.headers["Authorization"])
                return httpx.Response(
                    200,
                    json={"sub": "user-id", "preferredUsername": "zhangsan"},
                )
            return httpx.Response(404)

        client = self.build_client(handler)
        client._discovery = {
            "tokenEndpoint": "http://keycloak-internal/auth/realms/ghippo/protocol/openid-connect/token",
            "userinfoEndpoint": "http://keycloak-internal/auth/realms/ghippo/protocol/openid-connect/userinfo",
        }
        token = client.exchange_code("authorization-code")
        identity = client.userinfo(token["accessToken"])
        self.assertEqual("user-id", identity["sub"])
        self.assertEqual("zhangsan", identity["preferredUsername"])
        self.assertEqual(
            [
                "/auth/realms/ghippo/protocol/openid-connect/token",
                "/auth/realms/ghippo/protocol/openid-connect/userinfo",
            ],
            [request.url.path for request in seen],
        )


if __name__ == "__main__":
    unittest.main()
