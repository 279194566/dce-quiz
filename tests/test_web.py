from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from quiz_app.config import Settings
from quiz_app.services import BusinessError
from quiz_app.web import create_app


TOKEN_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')


class WebFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        settings = Settings(
            app_env="test",
            dce_mode="mock",
            database_path=Path(self.tempdir.name) / "web.db",
            secret_key="test-session-key-with-more-than-thirty-two-characters",
            dev_auto_process_grants=True,
            webhook_secret="test-webhook-secret-with-enough-length",
        )
        self.app = create_app(settings)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def csrf_from(self, path: str) -> str:
        response = self.client.get(path)
        self.assertEqual(200, response.status_code)
        match = TOKEN_PATTERN.search(response.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def login_admin(self) -> str:
        token = self.csrf_from("/dev/login")
        response = self.client.post(
            "/dev/login",
            data={"csrf_token": token, "dce_user_id": "mock-user-admin"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual("/admin", response.headers["location"])
        return self.csrf_from("/admin")

    def test_login_requires_csrf(self) -> None:
        response = self.client.post(
            "/dev/login",
            data={"dce_user_id": "mock-user-admin"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual("/dev/login", response.headers["location"])
        page = self.client.get("/dev/login")
        self.assertIn("请求校验失败", page.text)

    def test_admin_create_user_is_synced_to_dce_with_package(self) -> None:
        token = self.login_admin()
        page = self.client.get("/admin?view=users")
        self.assertIn("创建用户", page.text)
        response = self.client.post(
            "/admin/users",
            data={
                "csrf_token": token,
                "username": "web-user",
                "password": "Temporary-123",
                "temporary": "on",
                "permission_package_id": "package-ai-developer",
            },
            follow_redirects=True,
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("用户已创建并同步到 DCE", response.text)
        self.assertIn("web-user", response.text)
        self.assertIn("AI 开发者", response.text)
        remote = self.client.app.state.dce.get_user_by_username("web-user")
        self.assertIsNotNone(remote)
        self.assertTrue(
            self.client.app.state.dce.is_user_in_group(
                "mock-group-ai-developer", remote["id"]
            )
        )

    def test_admin_assigns_training_with_permission_package(self) -> None:
        token = self.login_admin()
        service = self.client.app.state.service
        user_id = service.create_user(
            "local-user-admin",
            username="training-package-user",
            password="Temporary-123",
            temporary=True,
        )
        page = self.client.get("/admin?view=tasks")
        self.assertIn("通过后开通的权限包", page.text)
        self.assertIn("自动获得相应权限", page.text)

        response = self.client.post(
            "/admin/assignments",
            data={
                "csrf_token": token,
                "user_id": user_id,
                "template_id": "template-platform-admission-v1",
                "permission_package_id": "package-ai-developer",
                "deadline": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("通过后将自动开通所选权限包", response.text)
        assignment = next(
            item
            for item in service.list_assignments()
            if item["user_id"] == user_id and item["purpose"] == "TRAINING_ONLY"
        )
        self.assertEqual("package-ai-developer", assignment["permission_package_id"])
        self.assertEqual("AI 开发者", assignment["package_name"])

    def test_admin_can_update_and_delete_task_then_user(self) -> None:
        token = self.login_admin()
        service = self.client.app.state.service
        user_id, assignment_id = service.create_user_and_assignment(
            actor_id="local-user-admin",
            username="managed-user",
            password="Temporary-123",
            temporary=True,
            quiz_required=True,
            template_id="template-platform-admission-v1",
            permission_package_id="package-ai-developer",
            deadline=None,
        )
        service.update_user(
            "local-user-admin", user_id, permission_package_id="package-ai-developer"
        )

        users_page = self.client.get("/admin?view=users")
        self.assertIn("managed-user", users_page.text)
        self.assertIn("AI 开发者", users_page.text)
        self.assertIn("已绑定权限包", users_page.text)
        self.assertIn("状态", users_page.text)
        self.assertNotIn("是否绑定", users_page.text)
        self.assertIn("编辑", users_page.text)
        self.assertIn("删除", users_page.text)

        updated_user = self.client.post(
            f"/admin/users/{user_id}/update",
            data={"csrf_token": token, "enabled": "on"},
            follow_redirects=True,
        )
        self.assertIn("用户权限包和账户状态已更新并同步到 DCE", updated_user.text)
        self.assertIn("<td>启用</td>", updated_user.text)
        remote = self.client.app.state.dce.get_user_by_username("managed-user")
        self.assertFalse(
            self.client.app.state.dce.is_user_in_group(
                "mock-group-ai-developer", remote["id"]
            )
        )

        token = TOKEN_PATTERN.search(updated_user.text).group(1)
        rebound_user = self.client.post(
            f"/admin/users/{user_id}/update",
            data={
                "csrf_token": token,
                "permission_package_id": "package-k8s-viewer",
                "enabled": "on",
            },
            follow_redirects=True,
        )
        self.assertIn("Kubernetes 只读", rebound_user.text)

        token = TOKEN_PATTERN.search(rebound_user.text).group(1)
        disabled_user = self.client.post(
            f"/admin/users/{user_id}/update",
            data={
                "csrf_token": token,
                "permission_package_id": "package-k8s-viewer",
            },
            follow_redirects=True,
        )
        self.assertIn("<td>禁用</td>", disabled_user.text)
        remote = self.client.app.state.dce.get_user_by_username("managed-user")
        self.assertFalse(remote["enabled"])
        with self.assertRaisesRegex(BusinessError, "已停用"):
            service.login_mock_user(remote["id"])

        token = TOKEN_PATTERN.search(disabled_user.text).group(1)
        enabled_user = self.client.post(
            f"/admin/users/{user_id}/update",
            data={
                "csrf_token": token,
                "permission_package_id": "package-k8s-viewer",
                "enabled": "on",
            },
            follow_redirects=True,
        )
        self.assertIn("<td>启用</td>", enabled_user.text)

        token = self.csrf_from("/admin?view=tasks")
        updated_task = self.client.post(
            f"/admin/assignments/{assignment_id}/update",
            data={
                "csrf_token": token,
                "user_id": user_id,
                "template_id": "template-platform-admission-v1",
                "permission_package_id": "package-k8s-viewer",
                "deadline": "",
            },
            follow_redirects=True,
        )
        self.assertIn("考试任务已更新", updated_task.text)
        self.assertIn("Kubernetes 只读", updated_task.text)

        token = TOKEN_PATTERN.search(updated_task.text).group(1)
        deleted_task = self.client.post(
            f"/admin/assignments/{assignment_id}/delete",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn("考试任务已删除", deleted_task.text)
        self.assertIsNone(service.get_assignment(assignment_id))

        token = self.csrf_from("/admin?view=users")
        deleted_user = self.client.post(
            f"/admin/users/{user_id}/delete",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn("同步删除 DCE 账号", deleted_user.text)
        self.assertNotIn("managed-user", deleted_user.text)
        self.assertIsNone(self.client.app.state.dce.get_user_by_username("managed-user"))

    def test_admin_exam_template_online_edit_download_upload_and_publish(self) -> None:
        token = self.login_admin()
        created = self.client.post(
            "/admin/exam-templates",
            data={
                "csrf_token": token,
                "name": "Web 管理试卷",
                "pass_score": "80",
                "critical_must_all_correct": "on",
                "duration_minutes": "30",
                "max_attempts": "3",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, created.status_code)
        edit_path = created.headers["location"]
        self.assertRegex(edit_path, r"^/admin/exam-templates/.+")
        edit_page = self.client.get(edit_path)
        self.assertIn("试卷草稿已创建", edit_page.text)
        token = TOKEN_PATTERN.search(edit_page.text).group(1)

        added = self.client.post(
            edit_path + "/questions",
            data={
                "csrf_token": token,
                "question_type": "SINGLE",
                "stem": "Web 在线编辑题目",
                "options_text": "选项一\n选项二",
                "correct_options": "2",
                "explanation": "选项二正确",
                "category": "Web 测试",
                "critical": "on",
                "source_url": "https://docs.example.test/web",
                "source_section": "Web",
                "product_version": "DCE 5",
                "reviewer": "tester",
            },
            follow_redirects=True,
        )
        self.assertIn("题目已添加", added.text)
        self.assertIn("Web 在线编辑题目", added.text)

        downloaded = self.client.get(edit_path + "/download")
        self.assertEqual(200, downloaded.status_code)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertEqual(1, downloaded.json()["schema_version"])
        self.assertEqual(1, len(downloaded.json()["questions"]))

        token = TOKEN_PATTERN.search(added.text).group(1)
        published = self.client.post(
            edit_path + "/publish",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn("试卷已发布", published.text)
        self.assertIn("已发布试卷不可直接修改", published.text)

        token = TOKEN_PATTERN.search(published.text).group(1)
        imported = self.client.post(
            "/admin/exam-templates/upload",
            data={"csrf_token": token},
            files={"upload": ("exam.json", downloaded.content, "application/json")},
            follow_redirects=True,
        )
        self.assertEqual(200, imported.status_code)
        self.assertIn("试卷已导入为草稿", imported.text)
        self.assertIn("Web 在线编辑题目", imported.text)

    def test_health_endpoints(self) -> None:
        self.assertEqual({"status": "ok"}, self.client.get("/health/live").json())
        ready = self.client.get("/health/ready")
        self.assertEqual(200, ready.status_code)
        self.assertEqual("mock", ready.json()["dce_mode"])

    def test_user_webhook_rejects_invalid_json(self) -> None:
        unauthorized = self.client.post("/api/hooks/dce/users", content=b"{")
        self.assertEqual(401, unauthorized.status_code)

        invalid = self.client.post(
            "/api/hooks/dce/users",
            content=b"{",
            headers={"X-Quiz-Webhook-Secret": self.app.state.settings.webhook_secret},
        )
        self.assertEqual(400, invalid.status_code)
        self.assertEqual("invalid json", invalid.json()["error"])

    def real_settings(self, database_name: str) -> Settings:
        return Settings(
            app_env="development",
            dce_mode="real",
            database_path=Path(self.tempdir.name) / database_name,
            secret_key="test-session-key-with-more-than-thirty-two-characters",
            webhook_secret="test-webhook-secret-with-enough-length",
            dce_base_url="https://dce.example.test",
            dce_access_token="service-token",
            dce_target_group_id="group-xg",
            dce_target_group_name="quiz-xg-viewer",
            dce_target_workspace_id=4,
            dce_target_workspace_alias="xg",
            dce_target_workspace_role="Workspace Viewer",
            oidc_client_id="dce-quiz-test",
            oidc_client_secret="oidc-client-secret",
            oidc_redirect_uri="http://127.0.0.1:8000/auth/callback",
        )

    @staticmethod
    def configure_oidc_mock(oidc_client: MagicMock, *, username: str, subject: str) -> None:
        oidc_client.authorization_url.side_effect = (
            lambda state, prompt="":
                f"https://dce.example.test/authorize?state={state}"
                + (f"&prompt={prompt}" if prompt else "")
        )
        oidc_client.end_session_url.side_effect = (
            lambda id_token_hint, post_logout_redirect_uri:
                "https://dce.example.test/logout"
                f"?post_logout_redirect_uri={post_logout_redirect_uri}"
                + (f"&id_token_hint={id_token_hint}" if id_token_hint else "")
        )
        oidc_client.exchange_code.return_value = {
            "accessToken": "short-lived-user-access-token",
            "idToken": "id-token",
        }
        oidc_client.userinfo.return_value = {
            "sub": subject,
            "preferredUsername": username,
        }

    def test_real_mode_oidc_login_does_not_put_tokens_in_session(self) -> None:
        real_settings = self.real_settings("real-web.db")
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="admin", subject="real-admin-id")
        service_client.get_user.return_value = {
            "id": "real-admin-id",
            "username": "admin",
            "enabled": True,
            "source": "dce",
        }
        service_client.list_user_roles.return_value = [
            {"name": "Admin", "authorized": True}
        ]
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(real_settings))
            legacy_login = client.get("/login", follow_redirects=False)
            self.assertEqual(303, legacy_login.status_code)
            self.assertEqual("/auth/login?switch=1", legacy_login.headers["location"])
            start = client.get("/auth/login", follow_redirects=False)
            self.assertEqual(302, start.status_code)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            response = client.get(
                f"/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(303, response.status_code)
            self.assertEqual("/admin", response.headers["location"])
            cookies = response.headers.get("set-cookie", "")
            self.assertNotIn("short-lived-user-access-token", cookies)
            self.assertNotIn("id-token", cookies)
            client.close()

    def test_real_user_with_unauthorized_admin_role_only_sees_exams(self) -> None:
        real_settings = self.real_settings("real-user-web.db")
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="zhangsan", subject="real-user-id")
        service_client.get_user.return_value = {
            "id": "real-user-id",
            "username": "zhangsan",
            "enabled": True,
            "source": "dce",
        }
        service_client.list_user_roles.return_value = [
            {"name": "Admin", "authorized": False},
            {"name": "AILab Owner", "authorized": False},
        ]
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(real_settings))
            start = client.get("/auth/login", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            response = client.get(
                f"/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual("/exam", response.headers["location"])
            exam = client.get("/exam")
            self.assertNotIn('href="/admin"', exam.text)
            denied = client.get("/admin", follow_redirects=False)
            self.assertEqual(303, denied.status_code)
            self.assertEqual("/auth/login?switch=1", denied.headers["location"])
            client.close()

    def test_oidc_callback_rejects_wrong_state_before_token_exchange(self) -> None:
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="zhangsan", subject="real-user-id")
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(self.real_settings("bad-state.db")))
            client.get("/auth/login", follow_redirects=False)
            response = client.get("/auth/callback?code=code&state=wrong")
            self.assertEqual(401, response.status_code)
            self.assertIn("state 校验失败", response.text)
            self.assertIn("/auth/login?switch=1", response.text)
            oidc_client.exchange_code.assert_not_called()
            client.close()

    def test_switch_account_login_requests_fresh_dce_authentication(self) -> None:
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="zhangsan", subject="real-user-id")
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(self.real_settings("switch-account.db")))
            home = client.get("/", follow_redirects=False)
            self.assertEqual(303, home.status_code)
            self.assertEqual("/auth/login?switch=1", home.headers["location"])
            response = client.get("/auth/login?switch=1", follow_redirects=False)
            self.assertEqual(302, response.status_code)
            self.assertEqual(
                ["login"],
                parse_qs(urlparse(response.headers["location"]).query)["prompt"],
            )
            oidc_client.authorization_url.assert_called_once()
            self.assertEqual("login", oidc_client.authorization_url.call_args.kwargs["prompt"])
            oidc_client.end_session_url.assert_not_called()
            client.close()

    def test_logged_in_switch_ends_dce_session_before_next_login(self) -> None:
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="zhangsan", subject="real-user-id")
        service_client.get_user.return_value = {
            "id": "real-user-id",
            "username": "zhangsan",
            "enabled": True,
            "source": "dce",
        }
        service_client.list_user_roles.return_value = []
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(self.real_settings("switch-logout.db")))
            start = client.get("/auth/login", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            signed_in = client.get(
                f"/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual("/exam", signed_in.headers["location"])
            self.assertNotIn("id-token", signed_in.headers.get("set-cookie", ""))
            switch = client.get("/auth/login?switch=1", follow_redirects=False)
            self.assertEqual(302, switch.status_code)
            query = parse_qs(urlparse(switch.headers["location"]).query)
            self.assertEqual(["id-token"], query["id_token_hint"])
            self.assertEqual(
                ["http://127.0.0.1:8000/auth/callback"],
                query["post_logout_redirect_uri"],
            )
            returned = client.get("/auth/callback", follow_redirects=False)
            self.assertEqual(303, returned.status_code)
            self.assertEqual("/auth/login", returned.headers["location"])
            client.close()

    def test_real_admin_maps_permission_package_to_existing_dce_group(self) -> None:
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="admin", subject="real-admin-id")
        service_client.get_user.return_value = {
            "id": "real-admin-id",
            "username": "admin",
            "enabled": True,
            "source": "dce",
        }
        service_client.list_user_roles.return_value = [
            {"name": "Admin", "authorized": True}
        ]
        platform_group = {
            "id": "platform-group-id",
            "name": "platform-developers",
            "description": "平台开发者组",
        }
        service_client.list_groups.return_value = [platform_group]
        service_client.get_group.return_value = platform_group
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(self.real_settings("real-package.db")))
            start = client.get("/auth/login", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            signed_in = client.get(
                f"/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual("/admin", signed_in.headers["location"])
            page = client.get("/admin?view=packages")
            self.assertIn("从 DCE 用户组创建权限包", page.text)
            self.assertIn("platform-developers", page.text)
            csrf = TOKEN_PATTERN.search(page.text).group(1)
            created = client.post(
                "/admin/permission-packages",
                data={
                    "csrf_token": csrf,
                    "name": "Platform Developers",
                    "dce_group_id": "platform-group-id",
                    "summary": "开发环境访问权限",
                },
                follow_redirects=True,
            )
            self.assertEqual(200, created.status_code)
            self.assertIn("权限包已映射到所选 DCE 用户组", created.text)
            self.assertIn("Platform Developers", created.text)
            service_client.create_group.assert_not_called()
            client.close()

    def test_real_admin_can_update_and_delete_unreferenced_permission_package(self) -> None:
        service_client = MagicMock()
        oidc_client = MagicMock()
        self.configure_oidc_mock(oidc_client, username="admin", subject="real-admin-id")
        service_client.get_user.return_value = {
            "id": "real-admin-id",
            "username": "admin",
            "enabled": True,
            "source": "dce",
        }
        service_client.list_user_roles.return_value = [
            {"name": "Admin", "authorized": True}
        ]
        first_group = {"id": "crud-first", "name": "crud-first", "description": "第一组"}
        second_group = {"id": "crud-second", "name": "crud-second", "description": "第二组"}
        groups = {item["id"]: item for item in (first_group, second_group)}
        service_client.list_groups.return_value = list(groups.values())
        service_client.get_group.side_effect = lambda group_id: groups.get(group_id)
        with (
            patch("quiz_app.web.build_dce_client", return_value=service_client),
            patch("quiz_app.web.DCEOIDCClient", return_value=oidc_client),
        ):
            client = TestClient(create_app(self.real_settings("real-package-crud.db")))
            start = client.get("/auth/login", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            client.get(f"/auth/callback?code=valid-code&state={state}", follow_redirects=False)
            page = client.get("/admin?view=packages")
            csrf = TOKEN_PATTERN.search(page.text).group(1)
            created = client.post(
                "/admin/permission-packages",
                data={
                    "csrf_token": csrf,
                    "name": "CRUD Web Package",
                    "dce_group_id": first_group["id"],
                    "summary": "创建摘要",
                },
                follow_redirects=True,
            )
            app_service = client.app.state.service
            package_id = next(
                row["id"] for row in app_service.list_packages()
                if row["name"] == "CRUD Web Package"
            )
            csrf = TOKEN_PATTERN.search(created.text).group(1)
            updated = client.post(
                f"/admin/permission-packages/{package_id}/update",
                data={
                    "csrf_token": csrf,
                    "name": "CRUD Web Updated",
                    "dce_group_id": second_group["id"],
                    "summary": "更新摘要",
                },
                follow_redirects=True,
            )
            self.assertIn("权限包已更新", updated.text)
            self.assertIn("CRUD Web Updated", updated.text)
            csrf = TOKEN_PATTERN.search(updated.text).group(1)
            deleted = client.post(
                f"/admin/permission-packages/{package_id}/delete",
                data={"csrf_token": csrf},
                follow_redirects=True,
            )
            self.assertIn("权限包映射已删除", deleted.text)
            self.assertNotIn("CRUD Web Updated", deleted.text)
            self.assertEqual({"crud-first", "crud-second"}, set(groups))
            service_client.create_group.assert_not_called()
            client.close()


if __name__ == "__main__":
    unittest.main()
