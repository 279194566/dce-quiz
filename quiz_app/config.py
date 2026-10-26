from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load an optional local environment file without overriding injected secrets."""

    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    secret_key: str = ""
    session_ttl_seconds: int = 8 * 60 * 60
    webhook_secret: str = ""
    worker_poll_seconds: float = 2.0
    dce_base_url: str = ""
    dce_access_token: str = ""
    dce_ca_bundle: str = ""
    dce_target_group_id: str = ""
    dce_target_group_name: str = ""
    dce_target_workspace_id: int = 0
    dce_target_workspace_alias: str = ""
    dce_target_workspace_role: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"

    @classmethod
    def from_env(cls) -> "Settings":
        load_env()
        settings = cls(
            database_url=os.environ.get("DATABASE_URL", "").strip(),
            secret_key=os.environ.get("SECRET_KEY", ""),
            session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", str(8 * 60 * 60))),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            worker_poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "2")),
            dce_base_url=os.environ.get("DCE_BASE_URL", "").strip().rstrip("/"),
            dce_access_token=os.environ.get("DCE_ACCESS_TOKEN", "").strip(),
            dce_ca_bundle=os.environ.get("DCE_CA_BUNDLE", "").strip(),
            dce_target_group_id=os.environ.get("DCE_TARGET_GROUP_ID", "").strip(),
            dce_target_group_name=os.environ.get("DCE_TARGET_GROUP_NAME", "").strip(),
            dce_target_workspace_id=int(os.environ.get("DCE_TARGET_WORKSPACE_ID", "0")),
            dce_target_workspace_alias=os.environ.get(
                "DCE_TARGET_WORKSPACE_ALIAS", ""
            ).strip(),
            dce_target_workspace_role=os.environ.get(
                "DCE_TARGET_WORKSPACE_ROLE", ""
            ).strip(),
            oidc_client_id=os.environ.get("OIDC_CLIENT_ID", "").strip(),
            oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
            oidc_redirect_uri=os.environ.get("OIDC_REDIRECT_URI", "").strip(),
            oidc_scopes=os.environ.get("OIDC_SCOPES", "openid profile email").strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("必须配置 PostgreSQL DATABASE_URL")
        if len(self.secret_key) < 32:
            raise RuntimeError("必须配置至少 32 字符的随机 SECRET_KEY")
        if len(self.webhook_secret) < 24:
            raise RuntimeError("必须配置至少 24 字符的随机 WEBHOOK_SECRET")
        if self.session_ttl_seconds <= 0:
            raise RuntimeError("SESSION_TTL_SECONDS 必须大于 0")
        if self.worker_poll_seconds <= 0:
            raise RuntimeError("WORKER_POLL_SECONDS 必须大于 0")
        if not self.dce_base_url.startswith("https://"):
            raise RuntimeError("必须配置 HTTPS DCE_BASE_URL")
        if not self.dce_access_token:
            raise RuntimeError("必须通过 Secret 注入 DCE_ACCESS_TOKEN")
        if self.dce_ca_bundle and not Path(self.dce_ca_bundle).is_file():
            raise RuntimeError("DCE_CA_BUNDLE 指向的证书文件不存在")
        if not self.dce_target_group_id or not self.dce_target_group_name:
            raise RuntimeError("必须配置目标 DCE 用户组 ID 和名称")
        if self.dce_target_workspace_id <= 0 or not self.dce_target_workspace_alias:
            raise RuntimeError("必须配置目标工作空间 ID 和别名")
        if self.dce_target_workspace_role not in {
            "Workspace Viewer",
            "Workspace Editor",
            "Workspace Admin",
        }:
            raise RuntimeError("DCE_TARGET_WORKSPACE_ROLE 不是受支持的工作空间角色")
        if not self.oidc_client_id or not self.oidc_client_secret:
            raise RuntimeError("必须配置 OIDC_CLIENT_ID 和 OIDC_CLIENT_SECRET")
        if not self.oidc_redirect_uri.startswith("https://"):
            raise RuntimeError("OIDC_REDIRECT_URI 必须使用 HTTPS")
        if "openid" not in set(self.oidc_scopes.split()):
            raise RuntimeError("OIDC_SCOPES 必须包含 openid")

