from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quiz_app.config import Settings
from quiz_app.db import Database
from quiz_app.dce import MockDCEClient
from quiz_app.services import BusinessError, QuizService


class QuizServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.db")
        self.db.initialize()
        self.dce = MockDCEClient(self.db)
        self.service = QuizService(self.db, self.dce)
        self.admin = self.service.login_mock_user("mock-user-admin")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_gated_user(self, username: str = "alice") -> tuple[str, str]:
        return self.service.create_user_and_assignment(
            actor_id=self.admin["id"],
            username=username,
            password="Temporary-123",
            temporary=True,
            quiz_required=True,
            template_id="template-platform-admission-v1",
            permission_package_id="package-ai-developer",
            deadline=None,
        )

    def correct_answers(self, attempt_id: str) -> dict[str, list[int]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT q.id, q.correct_options FROM attempt_questions aq
                JOIN questions q ON q.id = aq.question_id
                WHERE aq.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchall()
        return {row["id"]: json.loads(row["correct_options"]) for row in rows}

    def test_pass_then_grant_and_repeat_worker_are_idempotent(self) -> None:
        user_id, assignment_id = self.create_gated_user()
        assignment = self.service.get_assignment(assignment_id)
        self.assertEqual("PENDING", assignment["status"])
        local_user = self.service.get_user(user_id)
        self.assertFalse(
            self.dce.is_user_in_group("mock-group-ai-developer", local_user["dce_user_id"])
        )

        with self.assertRaises(BusinessError):
            self.service.retry_grant(assignment_id, self.admin["id"])

        attempt_id = self.service.start_attempt(assignment_id, user_id)
        result = self.service.submit_attempt(
            attempt_id, user_id, self.correct_answers(attempt_id)
        )
        self.assertTrue(result["passed"])
        self.assertEqual("PASSED_PENDING_GRANT", result["assignment_status"])
        self.assertFalse(
            self.dce.is_user_in_group("mock-group-ai-developer", local_user["dce_user_id"])
        )

        self.assertTrue(self.service.process_one_grant_job("test-worker"))
        self.assertEqual("ACTIVE", self.service.get_assignment(assignment_id)["status"])
        self.assertTrue(
            self.dce.is_user_in_group("mock-group-ai-developer", local_user["dce_user_id"])
        )
        self.assertFalse(self.service.process_one_grant_job("test-worker"))

    def test_training_pass_grants_selected_permission_package(self) -> None:
        user_id, direct_assignment_id = self.service.create_user_and_assignment(
            actor_id=self.admin["id"],
            username="bob",
            password="Temporary-123",
            temporary=True,
            quiz_required=False,
            template_id="template-platform-admission-v1",
            permission_package_id="package-k8s-viewer",
            deadline=None,
        )
        self.assertTrue(self.service.process_one_grant_job("test-worker"))
        self.assertEqual("ACTIVE", self.service.get_assignment(direct_assignment_id)["status"])

        training_id = self.service.assign_training(
            actor_id=self.admin["id"],
            user_id=user_id,
            template_id="template-platform-admission-v1",
            permission_package_id="package-ai-developer",
            deadline=None,
        )
        attempt_id = self.service.start_attempt(training_id, user_id)
        result = self.service.submit_attempt(
            attempt_id, user_id, self.correct_answers(attempt_id)
        )
        self.assertTrue(result["passed"])
        self.assertEqual("PASSED_PENDING_GRANT", result["assignment_status"])
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM grant_jobs WHERE assignment_id = ?", (training_id,)
            ).fetchone()["n"]
        self.assertEqual(1, count)

        self.assertTrue(self.service.process_one_grant_job("training-worker"))
        self.assertEqual("ACTIVE", self.service.get_assignment(training_id)["status"])
        user = self.service.get_user(user_id)
        self.assertEqual("package-ai-developer", user["permission_package_id"])
        self.assertTrue(
            self.dce.is_user_in_group("mock-group-ai-developer", user["dce_user_id"])
        )
        self.assertFalse(
            self.dce.is_user_in_group("mock-group-k8s-viewer", user["dce_user_id"])
        )

    def test_pending_assignment_can_be_updated_and_deleted(self) -> None:
        user_id, assignment_id = self.create_gated_user("editable-user")
        self.service.update_assignment(
            actor_id=self.admin["id"],
            assignment_id=assignment_id,
            user_id=user_id,
            template_id="template-platform-admission-v1",
            permission_package_id="package-k8s-viewer",
            deadline=None,
        )
        assignment = self.service.get_assignment(assignment_id)
        self.assertEqual("package-k8s-viewer", assignment["permission_package_id"])
        self.assertEqual("Kubernetes 只读", assignment["package_name"])
        users = {row["id"]: row for row in self.service.list_users()}
        self.assertIsNone(users[user_id]["permission_package_id"])

        self.service.delete_assignment(self.admin["id"], assignment_id)
        self.assertIsNone(self.service.get_assignment(assignment_id))
        self.service.delete_user(self.admin["id"], user_id)
        self.assertIsNone(self.service.get_user(user_id))
        self.assertIsNone(self.dce.get_user_by_username("editable-user"))

    def test_pending_assignment_with_attempts_can_be_reassigned_and_deleted(self) -> None:
        user_id, assignment_id = self.create_gated_user("protected-user")
        first_attempt_id = self.service.start_attempt(assignment_id, user_id)
        replacement_id = self.service.create_user(
            self.admin["id"],
            username="replacement-user",
            password="Temporary-123",
            temporary=True,
        )
        self.service.update_assignment(
            actor_id=self.admin["id"],
            assignment_id=assignment_id,
            user_id=replacement_id,
            template_id="template-platform-admission-v1",
            permission_package_id="package-k8s-viewer",
            deadline=None,
        )
        with self.db.connect() as conn:
            self.assertIsNone(
                conn.execute("SELECT id FROM attempts WHERE id = ?", (first_attempt_id,)).fetchone()
            )
        second_attempt_id = self.service.start_attempt(assignment_id, replacement_id)
        self.service.delete_assignment(self.admin["id"], assignment_id)
        self.assertIsNone(self.service.get_assignment(assignment_id))
        with self.db.connect() as conn:
            self.assertIsNone(
                conn.execute("SELECT id FROM attempts WHERE id = ?", (second_attempt_id,)).fetchone()
            )
        self.service.delete_user(self.admin["id"], user_id)
        self.assertIsNone(self.dce.get_user_by_username("protected-user"))
        self.service.delete_user(self.admin["id"], replacement_id)

    def test_user_permission_package_binding_is_synced_to_dce(self) -> None:
        user_id = self.service.create_user(
            self.admin["id"],
            username="binding-user",
            password="Temporary-123",
            temporary=True,
            permission_package_id="package-ai-developer",
        )
        user = self.service.get_user(user_id)
        self.assertEqual("package-ai-developer", user["permission_package_id"])
        self.assertTrue(
            self.dce.is_user_in_group("mock-group-ai-developer", user["dce_user_id"])
        )

        self.service.update_user(
            self.admin["id"], user_id, permission_package_id="package-k8s-viewer"
        )
        self.assertFalse(
            self.dce.is_user_in_group("mock-group-ai-developer", user["dce_user_id"])
        )
        self.assertTrue(
            self.dce.is_user_in_group("mock-group-k8s-viewer", user["dce_user_id"])
        )
        self.service.update_user(self.admin["id"], user_id, permission_package_id="")
        self.assertIsNone(self.service.get_user(user_id)["permission_package_id"])
        self.assertFalse(
            self.dce.is_user_in_group("mock-group-k8s-viewer", user["dce_user_id"])
        )
        self.service.update_user(
            self.admin["id"], user_id, permission_package_id="", enabled=False
        )
        self.assertFalse(self.service.get_user(user_id)["enabled"])
        self.assertFalse(self.dce.get_user(user["dce_user_id"])["enabled"])
        with self.assertRaisesRegex(BusinessError, "已停用"):
            self.service.login_mock_user(user["dce_user_id"])

        self.service.update_user(
            self.admin["id"], user_id, permission_package_id="", enabled=True
        )
        self.assertTrue(self.service.get_user(user_id)["enabled"])
        self.assertTrue(self.dce.get_user(user["dce_user_id"])["enabled"])
        with self.assertRaisesRegex(BusinessError, "当前登录用户"):
            self.service.delete_user(self.admin["id"], self.admin["id"])

        with self.assertRaisesRegex(BusinessError, "不能禁用当前登录用户"):
            self.service.update_user(
                self.admin["id"],
                self.admin["id"],
                permission_package_id="",
                enabled=False,
            )

    def test_permission_package_can_map_existing_group_without_creating_group(self) -> None:
        group = self.dce.create_group("existing-platform-group", "平台已有用户组")
        group_count = len(self.dce.list_groups())
        package_id = self.service.create_permission_package_from_group(
            self.admin["id"],
            "Existing Group Package",
            group["id"],
            "已有平台权限",
        )
        self.assertEqual(group_count, len(self.dce.list_groups()))
        package = next(item for item in self.service.list_packages() if item["id"] == package_id)
        self.assertEqual(group["id"], package["dce_group_id"])
        self.assertEqual("existing-platform-group", package["dce_group_name"])
        with self.assertRaises(BusinessError):
            self.service.create_permission_package_from_group(
                self.admin["id"], "Duplicate Group Package", group["id"], ""
            )

    def test_permission_package_can_be_updated_and_deleted_without_mutating_group(self) -> None:
        first_group = self.dce.create_group("package-crud-first", "第一组")
        second_group = self.dce.create_group("package-crud-second", "第二组")
        package_id = self.service.create_permission_package_from_group(
            self.admin["id"], "CRUD Package", first_group["id"], "初始摘要"
        )

        self.service.update_permission_package(
            self.admin["id"], package_id, "Updated Package", second_group["id"], "更新摘要"
        )
        package = self.service.get_permission_package(package_id)
        self.assertEqual("Updated Package", package["name"])
        self.assertEqual(second_group["id"], package["dce_group_id"])
        self.assertEqual("更新摘要", package["target_summary"])

        self.service.delete_permission_package(self.admin["id"], package_id)
        with self.assertRaises(BusinessError):
            self.service.get_permission_package(package_id)
        self.assertIsNotNone(self.dce.get_group(first_group["id"]))
        self.assertIsNotNone(self.dce.get_group(second_group["id"]))

    def test_referenced_permission_package_cannot_change_group_or_be_deleted(self) -> None:
        self.create_gated_user("package-reference-user")
        another_group = self.dce.create_group("package-reference-other", "另一组")
        package = self.service.get_permission_package("package-ai-developer")

        with self.assertRaisesRegex(BusinessError, "不能更换"):
            self.service.update_permission_package(
                self.admin["id"],
                package["id"],
                package["name"],
                another_group["id"],
                package["target_summary"],
            )
        with self.assertRaisesRegex(BusinessError, "不能删除"):
            self.service.delete_permission_package(self.admin["id"], package["id"])

        self.service.update_permission_package(
            self.admin["id"],
            package["id"],
            "引用中的权限包",
            package["dce_group_id"],
            "允许更新展示信息",
        )
        updated = self.service.get_permission_package(package["id"])
        self.assertEqual("引用中的权限包", updated["name"])

    def test_system_managed_permission_package_cannot_be_deleted(self) -> None:
        self.db.upsert_real_permission_package(
            group_id="system-target-group",
            group_name="system-target-group",
            workspace_id=4,
            workspace_alias="xg",
            workspace_role="Workspace Viewer",
        )
        with self.assertRaisesRegex(BusinessError, "系统配置"):
            self.service.delete_permission_package(self.admin["id"], "package-dce-target")

    @staticmethod
    def question_form_values(stem: str = "在线编辑测试题") -> dict:
        return {
            "question_type": "SINGLE",
            "stem": stem,
            "options_text": "错误答案\n正确答案\n另一个错误答案",
            "correct_options_text": "2",
            "explanation": "第二项正确",
            "category": "自动化测试",
            "critical": True,
            "source_url": "https://docs.example.test/quiz",
            "source_section": "测试章节",
            "product_version": "DCE 5",
            "reviewer": "tester",
        }

    def test_exam_template_clone_online_edit_and_publish(self) -> None:
        template_id = self.service.clone_exam_template(
            self.admin["id"], "template-platform-admission-v1"
        )
        draft = self.service.get_exam_template(template_id)
        self.assertEqual("DRAFT", draft["status"])
        self.assertEqual(2, draft["version"])
        self.assertEqual(8, draft["actual_question_count"])

        questions = self.service.list_template_questions(template_id)
        first = questions[0]
        second = questions[1]
        self.service.move_template_question(
            self.admin["id"], template_id, second["id"], "up"
        )
        self.assertEqual(
            second["id"], self.service.list_template_questions(template_id)[0]["id"]
        )
        self.service.update_template_question(
            self.admin["id"], template_id, first["id"],
            **self.question_form_values("修改后的在线题目"),
        )
        new_question_id = self.service.add_template_question(
            self.admin["id"], template_id, **self.question_form_values("新增在线题目")
        )
        self.assertEqual(9, self.service.get_exam_template(template_id)["actual_question_count"])
        self.service.delete_template_question(self.admin["id"], template_id, new_question_id)
        self.assertEqual(8, self.service.get_exam_template(template_id)["actual_question_count"])

        self.service.publish_exam_template(self.admin["id"], template_id)
        self.assertEqual("PUBLISHED", self.service.get_exam_template(template_id)["status"])
        with self.assertRaisesRegex(BusinessError, "只有草稿"):
            self.service.update_template_question(
                self.admin["id"], template_id, first["id"],
                **self.question_form_values("不允许修改"),
            )
        self.service.delete_exam_template(self.admin["id"], template_id)
        with self.assertRaisesRegex(BusinessError, "试卷不存在"):
            self.service.get_exam_template(template_id)

    def test_referenced_exam_template_cannot_be_deleted(self) -> None:
        self.create_gated_user()
        with self.assertRaisesRegex(BusinessError, "已被考试任务引用"):
            self.service.delete_exam_template(self.admin["id"], "template-platform-admission-v1")
        self.assertEqual(
            "PUBLISHED",
            self.service.get_exam_template("template-platform-admission-v1")["status"],
        )

    def test_exam_template_export_import_round_trip_creates_new_draft(self) -> None:
        exported = self.service.export_exam_template("template-platform-admission-v1")
        self.assertEqual(1, exported["schema_version"])
        self.assertEqual(8, len(exported["questions"]))

        imported_id = self.service.import_exam_template(self.admin["id"], exported)
        imported = self.service.get_exam_template(imported_id)
        self.assertEqual("DRAFT", imported["status"])
        self.assertEqual(2, imported["version"])
        self.assertEqual(8, imported["actual_question_count"])
        self.assertEqual(
            exported["questions"][0]["stem"],
            self.service.list_template_questions(imported_id)[0]["stem"],
        )

    def test_exam_template_question_validation_rejects_invalid_answer(self) -> None:
        template_id = self.service.create_exam_template_draft(
            self.admin["id"], name="校验测试卷", pass_score=80,
            critical_must_all_correct=True, duration_minutes=30, max_attempts=3,
        )
        values = self.question_form_values()
        values["correct_options_text"] = "9"
        with self.assertRaisesRegex(BusinessError, "超出"):
            self.service.add_template_question(self.admin["id"], template_id, **values)
        with self.assertRaisesRegex(BusinessError, "至少需要一道题"):
            self.service.publish_exam_template(self.admin["id"], template_id)

    def test_real_target_upsert_preserves_admin_created_packages(self) -> None:
        group = self.dce.create_group("persistent-package-group", "保留映射")
        package_id = self.service.create_permission_package_from_group(
            self.admin["id"], "Persistent Package", group["id"], ""
        )
        self.db.upsert_real_permission_package(
            group_id="another-real-group",
            group_name="quiz-xg-viewer",
            workspace_id=4,
            workspace_alias="xg",
            workspace_role="Workspace Viewer",
        )
        package = next(item for item in self.service.list_packages() if item["id"] == package_id)
        self.assertEqual(1, package["enabled"])

    def test_critical_question_failure_overrides_score(self) -> None:
        user_id, assignment_id = self.create_gated_user("carol")
        attempt_id = self.service.start_attempt(assignment_id, user_id)
        answers = self.correct_answers(attempt_id)
        with self.db.connect() as conn:
            critical = conn.execute(
                """
                SELECT q.id FROM attempt_questions aq JOIN questions q ON q.id = aq.question_id
                WHERE aq.attempt_id = ? AND q.critical = 1 LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
        answers[critical["id"]] = []
        result = self.service.submit_attempt(attempt_id, user_id, answers)
        self.assertFalse(result["passed"])
        self.assertTrue(result["critical_failed"])
        self.assertEqual("PENDING", self.service.get_assignment(assignment_id)["status"])

    def test_production_rejects_mock_mode(self) -> None:
        settings = Settings(
            app_env="production",
            dce_mode="mock",
            database_path=Path(self.tempdir.name) / "unused.db",
            secret_key="a" * 40,
            webhook_secret="b" * 32,
        )
        with self.assertRaises(RuntimeError):
            settings.validate()

    def test_expired_deadline_is_persisted_when_start_is_rejected(self) -> None:
        user_id, assignment_id = self.create_gated_user("deadline-user")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE assignments SET deadline = ? WHERE id = ?", (past, assignment_id)
            )
        with self.assertRaises(BusinessError):
            self.service.start_attempt(assignment_id, user_id)
        self.assertEqual("EXPIRED", self.service.get_assignment(assignment_id)["status"])

    def test_timed_out_attempt_is_persisted_when_submit_is_rejected(self) -> None:
        user_id, assignment_id = self.create_gated_user("timeout-user")
        attempt_id = self.service.start_attempt(assignment_id, user_id)
        old_start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE attempts SET started_at = ? WHERE id = ?", (old_start, attempt_id)
            )
        with self.assertRaises(BusinessError):
            self.service.submit_attempt(attempt_id, user_id, {})
        with self.db.connect() as conn:
            status = conn.execute(
                "SELECT status FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()["status"]
        self.assertEqual("ABANDONED", status)

    def test_stale_running_grant_job_is_reclaimed(self) -> None:
        _, assignment_id = self.service.create_user_and_assignment(
            actor_id=self.admin["id"],
            username="stale-worker-user",
            password="Temporary-123",
            temporary=True,
            quiz_required=False,
            template_id="template-platform-admission-v1",
            permission_package_id="package-k8s-viewer",
            deadline=None,
        )
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE grant_jobs SET state = 'RUNNING', locked_at = ?, locked_by = 'dead-worker'
                WHERE assignment_id = ?
                """,
                (stale, assignment_id),
            )
        self.assertTrue(self.service.process_one_grant_job("replacement-worker"))
        assignment = self.service.get_assignment(assignment_id)
        self.assertEqual("ACTIVE", assignment["status"])
        self.assertEqual("SUCCEEDED", assignment["grant_job_state"])


if __name__ == "__main__":
    unittest.main()
