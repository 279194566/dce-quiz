from __future__ import annotations

import uuid
import base64
import binascii
import json
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from .db import DB_INTEGRITY_ERRORS, Database, utcnow


@dataclass
class DCEError(Exception):
    safe_message: str
    retryable: bool = False
    status_code: int | None = None

    def __str__(self) -> str:
        return self.safe_message


class DCEClient(Protocol):
    def create_user(self, username: str, password: str, temporary: bool) -> dict: ...
    def update_user_enabled(self, user_id: str, enabled: bool) -> None: ...
    def get_user_by_username(self, username: str) -> dict | None: ...
    def get_user(self, user_id: str) -> dict | None: ...
    def list_users(self) -> list[dict]: ...
    def get_group(self, group_id: str) -> dict | None: ...
    def list_groups(self) -> list[dict]: ...
    def create_group(self, name: str, summary: str) -> dict: ...
    def list_user_groups(self, user_id: str) -> list[dict]: ...
    def is_user_in_group(self, group_id: str, user_id: str) -> bool: ...
    def add_user_to_group(self, group_id: str, user_id: str) -> None: ...
    def remove_user_from_group(self, group_id: str, user_id: str) -> None: ...
    def delete_user(self, user_id: str) -> None: ...


class RealDCEClient:
    """Ghippo v1alpha1 客户端。

    访问令牌仅保存在进程内存中。调用方应优先传入 CA 文件；跳过 TLS 校验只允许
    在隔离的联调环境显式开启。
    """

    api_prefix = "/apis/ghippo.io/v1alpha1"

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        ca_bundle: str = "",
        insecure_skip_tls_verify: bool = False,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.verify: bool | str = False if insecure_skip_tls_verify else (ca_bundle or True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        not_found_is_none: bool = False,
    ) -> dict | None:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                verify=self.verify,
                timeout=self.timeout_seconds,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = client.request(method, path, json=json_body)
        except httpx.TimeoutException as exc:
            raise DCEError("DCE 请求超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise DCEError("无法连接 DCE API", retryable=True) from exc
        if response.status_code >= 400:
            message = ""
            try:
                payload = response.json()
                message = str(payload.get("message") or payload.get("error") or "")
            except Exception:
                pass
            normalized_message = message.lower()
            if not_found_is_none and (
                response.status_code == 404
                or (
                    response.status_code == 400
                    and ("not found" in normalized_message or "does not exist" in normalized_message)
                )
            ):
                return None
            safe = f"DCE API 返回 HTTP {response.status_code}"
            if message:
                safe += f"：{message[:240]}"
            raise DCEError(
                safe,
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise DCEError("DCE API 返回了非 JSON 响应", retryable=False) from exc

    @staticmethod
    def _user(payload: dict) -> dict:
        source = payload.get("user") if isinstance(payload.get("user"), dict) else payload
        return {
            **source,
            "username": source.get("username") or source.get("name"),
            "enabled": bool(source.get("enabled", True)),
            "source": source.get("source") or "dce",
        }

    def create_user(self, username: str, password: str, temporary: bool) -> dict:
        payload = self._request(
            "POST",
            f"{self.api_prefix}/users",
            json_body={
                "name": username,
                "password": password,
                "description": "Created by DCE Quiz",
                "temporary": temporary,
            },
        )
        user_id = str((payload or {}).get("id") or "")
        if not user_id:
            raise DCEError("DCE 创建用户成功但响应缺少用户 ID", retryable=False)
        user = self.get_user(user_id)
        return user or {
            "id": user_id,
            "username": username,
            "enabled": True,
            "source": "dce",
        }

    def delete_user(self, user_id: str) -> None:
        self._request(
            "DELETE",
            f"{self.api_prefix}/users/{quote(user_id, safe='')}",
            not_found_is_none=True,
        )

    def update_user_enabled(self, user_id: str, enabled: bool) -> None:
        user = self.get_user(user_id)
        if user is None:
            raise DCEError("DCE 用户不存在", retryable=False, status_code=404)
        self._request(
            "PUT",
            f"{self.api_prefix}/users/{quote(user_id, safe='')}",
            json_body={
                "email": str(user.get("email") or ""),
                "description": str(user.get("description") or ""),
                "firstname": str(user.get("firstname") or ""),
                "lastname": str(user.get("lastname") or ""),
                "enabled": bool(enabled),
            },
        )

    def get_current_user(self) -> dict:
        identity = self.get_current_identity()
        user = self.get_user_by_username(identity["username"])
        if not user:
            raise DCEError("DCE 当前身份没有对应用户记录", status_code=401)
        return user

    def get_current_identity(self) -> dict:
        payload = self._request("GET", f"{self.api_prefix}/current-user") or {}
        username = str(payload.get("username") or payload.get("preferred_username") or "")
        if not username:
            raise DCEError("DCE 当前身份响应缺少用户名", status_code=401)
        subject = ""
        try:
            encoded = self.access_token.split(".")[1]
            encoded += "=" * (-len(encoded) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded))
            subject = str(claims.get("sub") or "")
        except (ValueError, IndexError, KeyError, json.JSONDecodeError, binascii.Error):
            pass
        return {"username": username, "subject": subject}

    def list_user_roles(self, user_id: str) -> list[dict]:
        payload = self._request(
            "GET",
            f"{self.api_prefix}/users/{quote(user_id, safe='')}/roles?page=1&pageSize=1000",
        ) or {}
        return list(payload.get("items", []))

    def get_user_by_username(self, username: str) -> dict | None:
        payload = self._request(
            "GET",
            f"{self.api_prefix}/users/username/{quote(username, safe='')}",
            not_found_is_none=True,
        )
        return self._user(payload) if payload else None

    def get_user(self, user_id: str) -> dict | None:
        payload = self._request(
            "GET", f"{self.api_prefix}/users/{quote(user_id, safe='')}", not_found_is_none=True
        )
        return self._user(payload) if payload else None

    def list_users(self) -> list[dict]:
        payload = self._request("GET", f"{self.api_prefix}/users?page=1&pageSize=1000") or {}
        return [self._user(item) for item in payload.get("items", [])]

    def get_group(self, group_id: str) -> dict | None:
        payload = self._request(
            "GET", f"{self.api_prefix}/groups/{quote(group_id, safe='')}", not_found_is_none=True
        )
        if not payload:
            return None
        return payload.get("group") or payload

    def list_groups(self) -> list[dict]:
        payload = self._request("GET", f"{self.api_prefix}/groups?page=1&pageSize=1000") or {}
        return list(payload.get("items", []))

    def create_group(self, name: str, summary: str) -> dict:
        payload = self._request(
            "POST",
            f"{self.api_prefix}/groups",
            json_body={"name": name, "description": summary},
        ) or {}
        group_id = str(payload.get("id") or "")
        if not group_id:
            raise DCEError("DCE 创建用户组成功但响应缺少用户组 ID")
        return self.get_group(group_id) or {"id": group_id, "name": name, "description": summary}

    def list_user_groups(self, user_id: str) -> list[dict]:
        payload = self._request(
            "GET",
            f"{self.api_prefix}/users/{quote(user_id, safe='')}/groups?page=1&pageSize=1000",
        ) or {}
        return list(payload.get("items", []))

    def is_user_in_group(self, group_id: str, user_id: str) -> bool:
        return any(str(group.get("id")) == group_id for group in self.list_user_groups(user_id))

    def add_user_to_group(self, group_id: str, user_id: str) -> None:
        if self.is_user_in_group(group_id, user_id):
            return
        self._request(
            "POST",
            f"{self.api_prefix}/groups/{quote(group_id, safe='')}/members/{quote(user_id, safe='')}",
        )

    def remove_user_from_group(self, group_id: str, user_id: str) -> None:
        if not self.is_user_in_group(group_id, user_id):
            return
        self._request(
            "DELETE",
            f"{self.api_prefix}/groups/{quote(group_id, safe='')}/members/{quote(user_id, safe='')}",
        )


def build_dce_client(settings, db: Database) -> DCEClient:
    if settings.dce_mode == "mock":
        return MockDCEClient(db)
    client = RealDCEClient(
        base_url=settings.dce_base_url,
        access_token=settings.dce_access_token,
        ca_bundle=settings.dce_ca_bundle,
        insecure_skip_tls_verify=settings.dce_insecure_skip_tls_verify,
    )
    group = client.get_group(settings.dce_target_group_id)
    if not group:
        raise RuntimeError("配置的 DCE 目标用户组不存在")
    if group.get("name") != settings.dce_target_group_name:
        raise RuntimeError("配置的 DCE 目标用户组 ID 与名称不匹配")
    db.upsert_real_permission_package(
        group_id=settings.dce_target_group_id,
        group_name=settings.dce_target_group_name,
        workspace_id=settings.dce_target_workspace_id,
        workspace_alias=settings.dce_target_workspace_alias,
        workspace_role=settings.dce_target_workspace_role,
    )
    return client


class MockDCEClient:
    """开发环境 DCE 替身。

    Mock 数据与 Quiz 业务表分开，便于测试“远端已成功、本地状态待确认”以及
    加组幂等逻辑。它不代表已经通过真实 DCE 契约验证。
    """

    def __init__(self, db: Database):
        self.db = db

    def create_user(self, username: str, password: str, temporary: bool) -> dict:
        del temporary
        if len(password) < 8:
            raise DCEError("初始密码至少 8 位", retryable=False, status_code=400)
        user_id = "mock-user-" + uuid.uuid4().hex[:12]
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO mock_dce_users (id, username, enabled, created_at) VALUES (?, ?, 1, ?)",
                    (user_id, username, utcnow()),
                )
        except DB_INTEGRITY_ERRORS as exc:
            raise DCEError("DCE 用户名已经存在", retryable=False, status_code=409) from exc
        return {"id": user_id, "username": username, "enabled": True, "source": "mock"}

    def get_user_by_username(self, username: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mock_dce_users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def update_user_enabled(self, user_id: str, enabled: bool) -> None:
        with self.db.connect() as conn:
            updated = conn.execute(
                "UPDATE mock_dce_users SET enabled = ? WHERE id = ?",
                (int(bool(enabled)), user_id),
            ).rowcount
        if not updated:
            raise DCEError("目标 DCE 用户不存在", retryable=False, status_code=404)

    def get_user(self, user_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mock_dce_users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM mock_dce_users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def get_group(self, group_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mock_dce_groups WHERE id = ?", (group_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_groups(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM mock_dce_groups ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def create_group(self, name: str, summary: str) -> dict:
        group_id = "mock-group-" + uuid.uuid4().hex[:12]
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO mock_dce_groups (id, name, summary) VALUES (?, ?, ?)",
                    (group_id, name, summary),
                )
        except DB_INTEGRITY_ERRORS as exc:
            raise DCEError("Mock DCE 用户组名称已经存在", status_code=409) from exc
        return {"id": group_id, "name": name, "summary": summary}

    def list_user_groups(self, user_id: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.* FROM mock_dce_groups g
                JOIN mock_dce_memberships m ON m.group_id = g.id
                WHERE m.user_id = ? ORDER BY g.name
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_user_in_group(self, group_id: str, user_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM mock_dce_memberships WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
        return row is not None

    def add_user_to_group(self, group_id: str, user_id: str) -> None:
        if not self.get_group(group_id):
            raise DCEError("目标 DCE 用户组不存在", retryable=False, status_code=404)
        if not self.get_user(user_id):
            raise DCEError("目标 DCE 用户不存在", retryable=False, status_code=404)
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO mock_dce_memberships (group_id, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (group_id, user_id, utcnow()),
            )

    def remove_user_from_group(self, group_id: str, user_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM mock_dce_memberships WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )

    def delete_user(self, user_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM mock_dce_memberships WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM mock_dce_users WHERE id = ?", (user_id,))
