from __future__ import annotations

import json
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB_INTEGRITY_ERRORS, Database, new_id, utcnow
from .dce import DCEClient, DCEError


ACTIVE_ASSIGNMENT_STATES = {"PENDING", "PASSED_PENDING_GRANT"}


class BusinessError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BusinessError("日期格式无效，请使用 YYYY-MM-DD 或 ISO 8601 格式") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_past(value: str | None) -> bool:
    parsed = _parse_datetime(value)
    return parsed is not None and parsed <= datetime.now(timezone.utc)


class QuizService:
    def __init__(self, db: Database, dce: DCEClient):
        self.db = db
        self.dce = dce

    def sync_local_user(self, remote: dict, *, is_admin: bool | None = None) -> sqlite3.Row:
        user_id = str(remote["id"])
        username = str(remote["username"])
        now = utcnow()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE dce_user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                local_id = new_id()
                conn.execute(
                    """
                    INSERT INTO users
                      (id, dce_user_id, username, source, is_admin, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_id,
                        user_id,
                        username,
                        str(remote.get("source") or "mock"),
                        int(bool(is_admin)),
                        int(bool(remote.get("enabled", True))),
                        now,
                        now,
                    ),
                )
            else:
                admin_value = row["is_admin"] if is_admin is None else int(bool(is_admin))
                conn.execute(
                    """
                    UPDATE users SET username = ?, source = ?, is_admin = ?, enabled = ?, updated_at = ?
                    WHERE dce_user_id = ?
                    """,
                    (
                        username,
                        str(remote.get("source") or "mock"),
                        admin_value,
                        int(bool(remote.get("enabled", True))),
                        now,
                        user_id,
                    ),
                )
            return conn.execute(
                "SELECT * FROM users WHERE dce_user_id = ?", (user_id,)
            ).fetchone()

    def login_mock_user(self, dce_user_id: str) -> sqlite3.Row:
        remote = self.dce.get_user(dce_user_id)
        if not remote or not remote.get("enabled"):
            raise BusinessError("Mock DCE 用户不存在或已停用", 404)
        admin = self.dce.is_user_in_group("mock-group-quiz-admin", dce_user_id)
        return self.sync_local_user(remote, is_admin=admin)

    def list_login_users(self) -> list[dict]:
        return self.dce.list_users()

    def get_user(self, local_user_id: str) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (local_user_id,)).fetchone()

    def list_templates(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT * FROM exam_templates WHERE status = 'PUBLISHED' ORDER BY name, version DESC"
            ).fetchall()

    def list_admin_templates(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM exam_template_questions tq
                        WHERE tq.exam_template_id = t.id) AS actual_question_count,
                       (SELECT COUNT(*) FROM assignments a
                        WHERE a.exam_template_id = t.id) AS assignment_count
                FROM exam_templates t
                ORDER BY t.created_at DESC, t.name, t.version DESC
                """
            ).fetchall()

    def get_exam_template(self, template_id: str) -> sqlite3.Row:
        with self.db.connect() as conn:
            template = conn.execute(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM exam_template_questions tq
                        WHERE tq.exam_template_id = t.id) AS actual_question_count,
                       (SELECT COUNT(*) FROM assignments a
                        WHERE a.exam_template_id = t.id) AS assignment_count
                FROM exam_templates t WHERE t.id = ?
                """,
                (template_id,),
            ).fetchone()
        if template is None:
            raise BusinessError("试卷不存在", 404)
        return template

    def list_template_questions(self, template_id: str) -> list[dict]:
        self.get_exam_template(template_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT q.*, tq.position, tq.weight
                FROM exam_template_questions tq
                JOIN questions q ON q.id = tq.question_id
                WHERE tq.exam_template_id = ?
                ORDER BY tq.position, q.created_at, q.id
                """,
                (template_id,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["options"] = json.loads(item["options"])
            item["correct_options"] = json.loads(item["correct_options"])
            item["correct_options_display"] = ",".join(
                str(index + 1) for index in item["correct_options"]
            )
            result.append(item)
        return result

    @staticmethod
    def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise BusinessError(f"{field}格式无效")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise BusinessError(f"{field}格式无效") from exc
        if parsed < minimum or parsed > maximum:
            raise BusinessError(f"{field}必须在 {minimum} 到 {maximum} 之间")
        return parsed

    @classmethod
    def _normalize_question(cls, payload: dict) -> dict:
        question_type = str(payload.get("type") or "").strip().upper()
        if question_type not in {"SINGLE", "MULTIPLE", "TRUE_FALSE"}:
            raise BusinessError("题型必须是 SINGLE、MULTIPLE 或 TRUE_FALSE")
        stem = str(payload.get("stem") or "").strip()
        if len(stem) < 2 or len(stem) > 2000:
            raise BusinessError("题干长度必须在 2 到 2000 个字符之间")
        options_value = payload.get("options")
        if not isinstance(options_value, list) or not all(
            isinstance(option, str) for option in options_value
        ):
            raise BusinessError("选项必须是 JSON 字符串数组")
        options = [option.strip() for option in options_value]
        if len(options) < 2 or len(options) > 10 or any(not option for option in options):
            raise BusinessError("每道题必须有 2 到 10 个非空选项")
        if len(set(options)) != len(options):
            raise BusinessError("同一道题的选项不能重复")
        correct_value = payload.get("correct_options")
        if not isinstance(correct_value, list) or not correct_value:
            raise BusinessError("必须设置至少一个正确答案")
        if not all(isinstance(index, int) and not isinstance(index, bool) for index in correct_value):
            raise BusinessError("正确答案必须使用从 0 开始的整数选项索引")
        correct = sorted(set(correct_value))
        if any(index < 0 or index >= len(options) for index in correct):
            raise BusinessError("正确答案索引超出选项范围")
        if question_type in {"SINGLE", "TRUE_FALSE"} and len(correct) != 1:
            raise BusinessError("单选题和判断题只能有一个正确答案")
        if question_type == "MULTIPLE" and len(correct) < 2:
            raise BusinessError("多选题至少需要两个正确答案")
        if question_type == "TRUE_FALSE" and len(options) != 2:
            raise BusinessError("判断题必须正好有两个选项")
        critical_value = payload.get("critical", False)
        if not isinstance(critical_value, (bool, int)) or critical_value not in (False, True, 0, 1):
            raise BusinessError("critical 必须是布尔值")
        return {
            "type": question_type,
            "stem": stem,
            "options": options,
            "correct_options": correct,
            "explanation": str(payload.get("explanation") or "").strip()[:4000],
            "category": str(payload.get("category") or "未分类").strip()[:120] or "未分类",
            "critical": 1 if bool(critical_value) else 0,
            "source_url": str(payload.get("source_url") or "").strip()[:1000],
            "source_section": str(payload.get("source_section") or "").strip()[:240],
            "product_version": str(payload.get("product_version") or "").strip()[:120],
            "reviewer": str(payload.get("reviewer") or "").strip()[:120],
            "reviewed_at": str(payload.get("reviewed_at") or "").strip() or None,
        }

    @classmethod
    def _normalize_template_fields(cls, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()
        if len(name) < 2 or len(name) > 120:
            raise BusinessError("试卷名称长度必须在 2 到 120 个字符之间")
        critical_rule = payload.get("critical_must_all_correct", True)
        if not isinstance(critical_rule, (bool, int)) or critical_rule not in (False, True, 0, 1):
            raise BusinessError("高危题规则必须是布尔值")
        return {
            "name": name,
            "pass_score": cls._bounded_int(payload.get("pass_score"), "及格分", 1, 100),
            "critical_must_all_correct": 1
            if bool(critical_rule) else 0,
            "duration_minutes": cls._bounded_int(
                payload.get("duration_minutes"), "考试时长", 1, 240
            ),
            "max_attempts": cls._bounded_int(payload.get("max_attempts"), "最大尝试次数", 1, 20),
        }

    def _next_template_version(self, conn: sqlite3.Connection, name: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM exam_templates WHERE name = ?",
            (name,),
        ).fetchone()
        return int(row["version"])

    def create_exam_template_draft(
        self,
        actor_id: str,
        *,
        name: str,
        pass_score: Any,
        critical_must_all_correct: bool,
        duration_minutes: Any,
        max_attempts: Any,
    ) -> str:
        fields = self._normalize_template_fields(
            {
                "name": name,
                "pass_score": pass_score,
                "critical_must_all_correct": critical_must_all_correct,
                "duration_minutes": duration_minutes,
                "max_attempts": max_attempts,
            }
        )
        template_id = new_id()
        now = utcnow()
        with self.db.connect(immediate=True) as conn:
            version = self._next_template_version(conn, fields["name"])
            conn.execute(
                """
                INSERT INTO exam_templates
                  (id, name, version, pass_score, critical_must_all_correct,
                   duration_minutes, max_attempts, question_count, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'DRAFT', ?)
                """,
                (
                    template_id,
                    fields["name"],
                    version,
                    fields["pass_score"],
                    fields["critical_must_all_correct"],
                    fields["duration_minutes"],
                    fields["max_attempts"],
                    now,
                ),
            )
        self.db.audit(
            "exam_template.create",
            "exam_template",
            template_id,
            "success",
            actor_user_id=actor_id,
            detail={"name": fields["name"], "version": version},
        )
        return template_id

    def update_exam_template(
        self,
        actor_id: str,
        template_id: str,
        *,
        name: str,
        pass_score: Any,
        critical_must_all_correct: bool,
        duration_minutes: Any,
        max_attempts: Any,
    ) -> None:
        template = self.get_exam_template(template_id)
        if template["status"] != "DRAFT":
            raise BusinessError("只有草稿试卷可以在线修改")
        fields = self._normalize_template_fields(
            {
                "name": name,
                "pass_score": pass_score,
                "critical_must_all_correct": critical_must_all_correct,
                "duration_minutes": duration_minutes,
                "max_attempts": max_attempts,
            }
        )
        with self.db.connect(immediate=True) as conn:
            version = self._next_template_version(conn, fields["name"])
            if fields["name"] == template["name"]:
                version = template["version"]
            conn.execute(
                """
                UPDATE exam_templates
                SET name = ?, version = ?, pass_score = ?, critical_must_all_correct = ?,
                    duration_minutes = ?, max_attempts = ?
                WHERE id = ? AND status = 'DRAFT'
                """,
                (
                    fields["name"],
                    version,
                    fields["pass_score"],
                    fields["critical_must_all_correct"],
                    fields["duration_minutes"],
                    fields["max_attempts"],
                    template_id,
                ),
            )
        self.db.audit(
            "exam_template.update", "exam_template", template_id, "success",
            actor_user_id=actor_id, detail={"name": fields["name"], "version": version}
        )

    def clone_exam_template(self, actor_id: str, template_id: str) -> str:
        template = self.get_exam_template(template_id)
        questions = self.list_template_questions(template_id)
        new_template_id = new_id()
        now = utcnow()
        with self.db.connect(immediate=True) as conn:
            version = self._next_template_version(conn, template["name"])
            conn.execute(
                """
                INSERT INTO exam_templates
                  (id, name, version, pass_score, critical_must_all_correct,
                   duration_minutes, max_attempts, question_count, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?)
                """,
                (
                    new_template_id, template["name"], version, template["pass_score"],
                    template["critical_must_all_correct"], template["duration_minutes"],
                    template["max_attempts"], len(questions), now,
                ),
            )
            for position, question in enumerate(questions, start=1):
                question_id = new_id()
                conn.execute(
                    """
                    INSERT INTO questions
                      (id, revision, type, stem, options, correct_options, explanation,
                       category, critical, source_url, source_section, product_version,
                       reviewer, reviewed_at, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?)
                    """,
                    (
                        question_id, int(question["revision"]) + 1, question["type"],
                        question["stem"], json.dumps(question["options"], ensure_ascii=False),
                        json.dumps(question["correct_options"]), question["explanation"],
                        question["category"], question["critical"], question["source_url"],
                        question["source_section"], question["product_version"],
                        question["reviewer"], question["reviewed_at"], now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO exam_template_questions
                      (exam_template_id, question_id, weight, position)
                    VALUES (?, ?, ?, ?)
                    """,
                    (new_template_id, question_id, question["weight"], position),
                )
        self.db.audit(
            "exam_template.clone", "exam_template", new_template_id, "success",
            actor_user_id=actor_id,
            detail={"source_template_id": template_id, "version": version},
        )
        return new_template_id

    def _question_from_form(
        self,
        *,
        question_type: str,
        stem: str,
        options_text: str,
        correct_options_text: str,
        explanation: str,
        category: str,
        critical: bool,
        source_url: str,
        source_section: str,
        product_version: str,
        reviewer: str,
    ) -> dict:
        options = [line.strip() for line in options_text.splitlines() if line.strip()]
        try:
            correct = [int(value.strip()) - 1 for value in correct_options_text.split(",") if value.strip()]
        except ValueError as exc:
            raise BusinessError("正确答案请填写选项序号，例如 1 或 1,2") from exc
        return self._normalize_question(
            {
                "type": question_type,
                "stem": stem,
                "options": options,
                "correct_options": correct,
                "explanation": explanation,
                "category": category,
                "critical": critical,
                "source_url": source_url,
                "source_section": source_section,
                "product_version": product_version,
                "reviewer": reviewer,
            }
        )

    def add_template_question(self, actor_id: str, template_id: str, **values: Any) -> str:
        template = self.get_exam_template(template_id)
        if template["status"] != "DRAFT":
            raise BusinessError("只有草稿试卷可以添加题目")
        question = self._question_from_form(**values)
        question_id = new_id()
        now = utcnow()
        with self.db.connect(immediate=True) as conn:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM exam_template_questions WHERE exam_template_id = ?",
                (template_id,),
            ).fetchone()["position"]
            conn.execute(
                """
                INSERT INTO questions
                  (id, revision, type, stem, options, correct_options, explanation,
                   category, critical, source_url, source_section, product_version,
                   reviewer, reviewed_at, status, created_at)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'DRAFT', ?)
                """,
                (
                    question_id, question["type"], question["stem"],
                    json.dumps(question["options"], ensure_ascii=False),
                    json.dumps(question["correct_options"]), question["explanation"],
                    question["category"], question["critical"], question["source_url"],
                    question["source_section"], question["product_version"],
                    question["reviewer"], now,
                ),
            )
            conn.execute(
                "INSERT INTO exam_template_questions (exam_template_id, question_id, weight, position) VALUES (?, ?, 1, ?)",
                (template_id, question_id, position),
            )
            conn.execute(
                "UPDATE exam_templates SET question_count = question_count + 1 WHERE id = ?",
                (template_id,),
            )
        self.db.audit(
            "question.create", "question", question_id, "success",
            actor_user_id=actor_id, detail={"template_id": template_id},
        )
        return question_id

    def update_template_question(
        self, actor_id: str, template_id: str, question_id: str, **values: Any
    ) -> None:
        template = self.get_exam_template(template_id)
        if template["status"] != "DRAFT":
            raise BusinessError("只有草稿试卷可以修改题目")
        question = self._question_from_form(**values)
        with self.db.connect(immediate=True) as conn:
            linked = conn.execute(
                "SELECT 1 FROM exam_template_questions WHERE exam_template_id = ? AND question_id = ?",
                (template_id, question_id),
            ).fetchone()
            if not linked:
                raise BusinessError("题目不属于当前试卷", 404)
            conn.execute(
                """
                UPDATE questions
                SET revision = revision + 1, type = ?, stem = ?, options = ?,
                    correct_options = ?, explanation = ?, category = ?, critical = ?,
                    source_url = ?, source_section = ?, product_version = ?, reviewer = ?
                WHERE id = ? AND status = 'DRAFT'
                """,
                (
                    question["type"], question["stem"],
                    json.dumps(question["options"], ensure_ascii=False),
                    json.dumps(question["correct_options"]), question["explanation"],
                    question["category"], question["critical"], question["source_url"],
                    question["source_section"], question["product_version"],
                    question["reviewer"], question_id,
                ),
            )
        self.db.audit(
            "question.update", "question", question_id, "success",
            actor_user_id=actor_id, detail={"template_id": template_id},
        )

    def delete_template_question(
        self, actor_id: str, template_id: str, question_id: str
    ) -> None:
        template = self.get_exam_template(template_id)
        if template["status"] != "DRAFT":
            raise BusinessError("只有草稿试卷可以删除题目")
        with self.db.connect(immediate=True) as conn:
            link = conn.execute(
                "SELECT position FROM exam_template_questions WHERE exam_template_id = ? AND question_id = ?",
                (template_id, question_id),
            ).fetchone()
            if not link:
                raise BusinessError("题目不属于当前试卷", 404)
            conn.execute(
                "DELETE FROM exam_template_questions WHERE exam_template_id = ? AND question_id = ?",
                (template_id, question_id),
            )
            conn.execute("DELETE FROM questions WHERE id = ? AND status = 'DRAFT'", (question_id,))
            conn.execute(
                "UPDATE exam_template_questions SET position = position - 1 WHERE exam_template_id = ? AND position > ?",
                (template_id, link["position"]),
            )
            conn.execute(
                "UPDATE exam_templates SET question_count = question_count - 1 WHERE id = ?",
                (template_id,),
            )
        self.db.audit(
            "question.delete", "question", question_id, "success",
            actor_user_id=actor_id, detail={"template_id": template_id},
        )

    def move_template_question(
        self, actor_id: str, template_id: str, question_id: str, direction: str
    ) -> None:
        template = self.get_exam_template(template_id)
        if template["status"] != "DRAFT":
            raise BusinessError("只有草稿试卷可以调整题目顺序")
        if direction not in {"up", "down"}:
            raise BusinessError("题目移动方向无效")
        with self.db.connect(immediate=True) as conn:
            current = conn.execute(
                "SELECT position FROM exam_template_questions WHERE exam_template_id = ? AND question_id = ?",
                (template_id, question_id),
            ).fetchone()
            if not current:
                raise BusinessError("题目不属于当前试卷", 404)
            operator = "<" if direction == "up" else ">"
            order = "DESC" if direction == "up" else "ASC"
            neighbor = conn.execute(
                f"""
                SELECT question_id, position FROM exam_template_questions
                WHERE exam_template_id = ? AND position {operator} ?
                ORDER BY position {order} LIMIT 1
                """,
                (template_id, current["position"]),
            ).fetchone()
            if not neighbor:
                return
            conn.execute(
                """
                UPDATE exam_template_questions
                SET position = CASE
                    WHEN question_id = ? THEN ?
                    WHEN question_id = ? THEN ?
                    ELSE position END
                WHERE exam_template_id = ? AND question_id IN (?, ?)
                """,
                (
                    question_id, neighbor["position"], neighbor["question_id"],
                    current["position"], template_id, question_id, neighbor["question_id"],
                ),
            )
        self.db.audit(
            "question.move", "question", question_id, "success",
            actor_user_id=actor_id,
            detail={"template_id": template_id, "direction": direction},
        )

    def publish_exam_template(self, actor_id: str, template_id: str) -> None:
        template = self.get_exam_template(template_id)
        if template["status"] != "DRAFT":
            raise BusinessError("只有草稿试卷可以发布")
        questions = self.list_template_questions(template_id)
        if not questions:
            raise BusinessError("试卷至少需要一道题才能发布")
        for question in questions:
            self._normalize_question(question)
        now = utcnow()
        with self.db.connect(immediate=True) as conn:
            conn.execute(
                "UPDATE questions SET status = 'PUBLISHED', reviewed_at = COALESCE(reviewed_at, ?) WHERE id IN (SELECT question_id FROM exam_template_questions WHERE exam_template_id = ?)",
                (now, template_id),
            )
            conn.execute(
                "UPDATE exam_templates SET status = 'PUBLISHED', question_count = ? WHERE id = ? AND status = 'DRAFT'",
                (len(questions), template_id),
            )
        self.db.audit(
            "exam_template.publish", "exam_template", template_id, "success",
            actor_user_id=actor_id, detail={"question_count": len(questions)},
        )

    def delete_exam_template(self, actor_id: str, template_id: str) -> None:
        template = self.get_exam_template(template_id)
        if template["assignment_count"]:
            raise BusinessError("试卷已被考试任务引用，不能删除")
        with self.db.connect(immediate=True) as conn:
            question_ids = [
                row["question_id"]
                for row in conn.execute(
                    "SELECT question_id FROM exam_template_questions WHERE exam_template_id = ?",
                    (template_id,),
                )
            ]
            conn.execute(
                "DELETE FROM exam_template_questions WHERE exam_template_id = ?",
                (template_id,),
            )
            for question_id in question_ids:
                still_used = conn.execute(
                    """
                    SELECT 1 FROM exam_template_questions WHERE question_id = ?
                    UNION ALL
                    SELECT 1 FROM attempt_questions WHERE question_id = ?
                    UNION ALL
                    SELECT 1 FROM attempt_answers WHERE question_id = ?
                    LIMIT 1
                    """,
                    (question_id, question_id, question_id),
                ).fetchone()
                if still_used is None:
                    conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
            conn.execute("DELETE FROM exam_templates WHERE id = ?", (template_id,))
        self.db.audit(
            "exam_template.delete", "exam_template", template_id, "success",
            actor_user_id=actor_id,
            detail={"name": template["name"], "version": template["version"], "status": template["status"]},
        )

    def export_exam_template(self, template_id: str) -> dict:
        template = self.get_exam_template(template_id)
        questions = self.list_template_questions(template_id)
        return {
            "schema_version": 1,
            "template": {
                "name": template["name"],
                "version": template["version"],
                "pass_score": template["pass_score"],
                "critical_must_all_correct": bool(template["critical_must_all_correct"]),
                "duration_minutes": template["duration_minutes"],
                "max_attempts": template["max_attempts"],
                "status": template["status"],
            },
            "questions": [
                {
                    key: question[key]
                    for key in (
                        "type", "stem", "options", "correct_options", "explanation",
                        "category", "critical", "source_url", "source_section",
                        "product_version", "reviewer", "reviewed_at",
                    )
                }
                for question in questions
            ],
        }

    def import_exam_template(self, actor_id: str, payload: dict) -> str:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise BusinessError("仅支持 schema_version=1 的试卷 JSON")
        template_payload = payload.get("template")
        question_payloads = payload.get("questions")
        if not isinstance(template_payload, dict) or not isinstance(question_payloads, list):
            raise BusinessError("JSON 必须包含 template 对象和 questions 数组")
        if not question_payloads or len(question_payloads) > 200:
            raise BusinessError("导入试卷必须包含 1 到 200 道题")
        fields = self._normalize_template_fields(template_payload)
        questions = [self._normalize_question(item) for item in question_payloads if isinstance(item, dict)]
        if len(questions) != len(question_payloads):
            raise BusinessError("questions 中的每一项都必须是对象")
        template_id = new_id()
        now = utcnow()
        try:
            with self.db.connect(immediate=True) as conn:
                version = self._next_template_version(conn, fields["name"])
                conn.execute(
                    """
                    INSERT INTO exam_templates
                      (id, name, version, pass_score, critical_must_all_correct,
                       duration_minutes, max_attempts, question_count, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?)
                    """,
                    (
                        template_id, fields["name"], version, fields["pass_score"],
                        fields["critical_must_all_correct"], fields["duration_minutes"],
                        fields["max_attempts"], len(questions), now,
                    ),
                )
                for position, question in enumerate(questions, start=1):
                    question_id = new_id()
                    conn.execute(
                        """
                        INSERT INTO questions
                          (id, revision, type, stem, options, correct_options, explanation,
                           category, critical, source_url, source_section, product_version,
                           reviewer, reviewed_at, status, created_at)
                        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?)
                        """,
                        (
                            question_id, question["type"], question["stem"],
                            json.dumps(question["options"], ensure_ascii=False),
                            json.dumps(question["correct_options"]), question["explanation"],
                            question["category"], question["critical"], question["source_url"],
                            question["source_section"], question["product_version"],
                            question["reviewer"], question["reviewed_at"], now,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO exam_template_questions (exam_template_id, question_id, weight, position) VALUES (?, ?, 1, ?)",
                        (template_id, question_id, position),
                    )
        except DB_INTEGRITY_ERRORS as exc:
            raise BusinessError("导入试卷版本冲突，请重试") from exc
        self.db.audit(
            "exam_template.import", "exam_template", template_id, "success",
            actor_user_id=actor_id,
            detail={"name": fields["name"], "version": version, "question_count": len(questions)},
        )
        return template_id

    def list_packages(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM assignments a
                        WHERE a.permission_package_id = p.id) AS assignment_count,
                       (SELECT COUNT(*) FROM users u
                        WHERE u.permission_package_id = p.id) AS user_binding_count,
                       CASE WHEN p.id = 'package-dce-target' THEN 1 ELSE 0 END AS system_managed
                FROM permission_packages p
                WHERE p.enabled = 1
                ORDER BY p.name
                """
            ).fetchall()

    def get_permission_package(self, package_id: str) -> sqlite3.Row:
        with self.db.connect() as conn:
            package = conn.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM assignments a
                        WHERE a.permission_package_id = p.id) AS assignment_count,
                       (SELECT COUNT(*) FROM users u
                        WHERE u.permission_package_id = p.id) AS user_binding_count,
                       CASE WHEN p.id = 'package-dce-target' THEN 1 ELSE 0 END AS system_managed
                FROM permission_packages p
                WHERE p.id = ? AND p.enabled = 1
                """,
                (package_id,),
            ).fetchone()
        if package is None:
            raise BusinessError("权限包不存在", 404)
        return package

    def list_users(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT u.*, p.name AS permission_package_name,
                       p.dce_group_name AS permission_group_name,
                       (SELECT COUNT(*) FROM assignments a
                        WHERE a.user_id = u.id) AS assignment_count
                FROM users u
                LEFT JOIN permission_packages p ON p.id = u.permission_package_id
                ORDER BY u.username
                """
            ).fetchall()

    def create_user(
        self,
        actor_id: str,
        *,
        username: str,
        password: str,
        temporary: bool,
        permission_package_id: str = "",
    ) -> str:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}", username):
            raise BusinessError("用户名使用字母、数字、点、下划线或中划线，长度 2 到 63")
        if len(password) < 8:
            raise BusinessError("初始密码至少 8 位")
        package = None
        if permission_package_id:
            package = self.get_permission_package(permission_package_id)
        try:
            remote = self.dce.create_user(username, password, temporary)
        except DCEError as exc:
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        local_user = self.sync_local_user(remote, is_admin=False)
        try:
            if package is not None:
                self.dce.add_user_to_group(package["dce_group_id"], remote["id"])
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE users SET permission_package_id = ?, updated_at = ? WHERE id = ?",
                        (package["id"], utcnow(), local_user["id"]),
                    )
        except DCEError as exc:
            # 新用户绑定失败时回滚远端账号，避免留下未完成的半成品用户。
            try:
                self.dce.delete_user(remote["id"])
            except DCEError:
                raise BusinessError(
                    "DCE 用户已创建但权限包绑定和自动回滚均失败，请人工对账", 502
                ) from exc
            with self.db.connect() as conn:
                conn.execute("DELETE FROM users WHERE id = ?", (local_user["id"],))
            raise BusinessError(f"权限包绑定失败，已回滚新建用户：{exc}", exc.status_code or 502) from exc
        self.db.audit(
            "user.create",
            "user",
            local_user["id"],
            "success",
            actor_user_id=actor_id,
            detail={
                "username": username,
                "dce_user_id": remote["id"],
                "permission_package_id": package["id"] if package else None,
            },
        )
        return str(local_user["id"])

    def update_user(
        self,
        actor_id: str,
        user_id: str,
        *,
        permission_package_id: str = "",
        enabled: bool = True,
    ) -> None:
        with self.db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise BusinessError("用户不存在", 404)
        if user_id == actor_id and not enabled:
            raise BusinessError("不能禁用当前登录用户")
        target_package = (
            self.get_permission_package(permission_package_id)
            if permission_package_id
            else None
        )
        old_package = (
            self.get_permission_package(str(user["permission_package_id"]))
            if user["permission_package_id"]
            else None
        )
        target_id = str(target_package["id"]) if target_package else None
        old_id = str(old_package["id"]) if old_package else None
        old_enabled = bool(user["enabled"])
        status_changed = old_enabled != bool(enabled)
        package_changed = target_id != old_id
        if not status_changed and not package_changed:
            return
        added_target = False
        removed_old = False
        try:
            if status_changed:
                self.dce.update_user_enabled(str(user["dce_user_id"]), bool(enabled))
            if package_changed and target_package is not None:
                self.dce.add_user_to_group(
                    str(target_package["dce_group_id"]), str(user["dce_user_id"])
                )
                added_target = True
            if package_changed and old_package is not None:
                self.dce.remove_user_from_group(
                    str(old_package["dce_group_id"]), str(user["dce_user_id"])
                )
                removed_old = True
        except DCEError as exc:
            if removed_old and old_package is not None:
                try:
                    self.dce.add_user_to_group(
                        str(old_package["dce_group_id"]), str(user["dce_user_id"])
                    )
                except DCEError:
                    pass
            if added_target and target_package is not None:
                try:
                    self.dce.remove_user_from_group(
                        str(target_package["dce_group_id"]), str(user["dce_user_id"])
                    )
                except DCEError:
                    pass
            if status_changed:
                try:
                    self.dce.update_user_enabled(
                        str(user["dce_user_id"]), old_enabled
                    )
                except DCEError:
                    pass
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET permission_package_id = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_id, int(bool(enabled)), utcnow(), user_id),
            )
        self.db.audit(
            "user.update",
            "user",
            user_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "old_permission_package_id": old_id,
                "permission_package_id": target_id,
                "old_enabled": old_enabled,
                "enabled": bool(enabled),
            },
        )

    def delete_user(self, actor_id: str, user_id: str) -> None:
        with self.db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise BusinessError("用户不存在", 404)
            if user_id == actor_id:
                raise BusinessError("不能删除当前登录用户")
            if user["is_admin"]:
                raise BusinessError("管理员用户不能删除")
            created_count = conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE created_by = ?", (user_id,)
            ).fetchone()["n"]
            if created_count:
                raise BusinessError("用户创建过考试任务，不能删除")
            provisioning_count = conn.execute(
                "SELECT COUNT(*) AS n FROM provisioning_requests WHERE created_by = ?",
                (user_id,),
            ).fetchone()["n"]
            if provisioning_count:
                raise BusinessError("用户有关联的开通记录，不能删除")
        try:
            self.dce.delete_user(str(user["dce_user_id"]))
        except DCEError as exc:
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        with self.db.connect(immediate=True) as conn:
            conn.execute(
                """
                DELETE FROM attempt_answers WHERE attempt_id IN (
                    SELECT at.id FROM attempts at
                    JOIN assignments a ON a.id = at.assignment_id
                    WHERE a.user_id = ?
                )
                """,
                (user_id,),
            )
            conn.execute(
                """
                DELETE FROM attempt_questions WHERE attempt_id IN (
                    SELECT at.id FROM attempts at
                    JOIN assignments a ON a.id = at.assignment_id
                    WHERE a.user_id = ?
                )
                """,
                (user_id,),
            )
            conn.execute(
                "DELETE FROM attempts WHERE assignment_id IN (SELECT id FROM assignments WHERE user_id = ?)",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM grant_jobs WHERE assignment_id IN (SELECT id FROM assignments WHERE user_id = ?)",
                (user_id,),
            )
            conn.execute("DELETE FROM assignments WHERE user_id = ?", (user_id,))
            # 历史审计保留，但删除后不再把该用户作为审计操作者外键引用。
            conn.execute(
                "UPDATE audit_events SET actor_user_id = NULL WHERE actor_user_id = ?",
                (user_id,),
            )
            deleted = conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount
        if not deleted:
            raise BusinessError("用户不存在", 404)
        self.db.audit(
            "user.delete",
            "user",
            user_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "username": user["username"],
                "dce_user_id": user["dce_user_id"],
                "dce_user_deleted": True,
            },
        )

    def create_permission_package(
        self, actor_id: str, name: str, group_name: str, summary: str
    ) -> str:
        name = name.strip()
        group_name = group_name.strip()
        if len(name) < 2 or len(group_name) < 2:
            raise BusinessError("权限包名称和用户组名称至少 2 个字符")
        try:
            remote_group = self.dce.create_group(group_name, summary.strip())
        except DCEError as exc:
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        package_id = new_id()
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO permission_packages
                      (id, name, dce_group_id, dce_group_name, description, target_summary,
                       enabled, last_validated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        package_id,
                        name,
                        remote_group["id"],
                        remote_group["name"],
                        "开发环境创建的 Mock 权限包",
                        summary.strip(),
                        utcnow(),
                        utcnow(),
                    ),
                )
        except DB_INTEGRITY_ERRORS as exc:
            raise BusinessError("权限包名称或用户组已经存在") from exc
        self.db.audit(
            "permission_package.create",
            "permission_package",
            package_id,
            "success",
            actor_user_id=actor_id,
            detail={"name": name, "group_name": group_name},
        )
        return package_id

    def list_permission_group_candidates(self) -> list[dict]:
        try:
            groups = self.dce.list_groups()
        except DCEError as exc:
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        candidates: list[dict] = []
        for group in groups:
            group_id = str(group.get("id") or "").strip()
            group_name = str(group.get("name") or "").strip()
            if not group_id or not group_name:
                continue
            candidates.append(
                {
                    "id": group_id,
                    "name": group_name,
                    "description": str(
                        group.get("description") or group.get("summary") or ""
                    ).strip(),
                }
            )
        return sorted(candidates, key=lambda item: item["name"].casefold())

    def create_permission_package_from_group(
        self,
        actor_id: str,
        name: str,
        dce_group_id: str,
        summary: str,
    ) -> str:
        name = name.strip()
        dce_group_id = dce_group_id.strip()
        if len(name) < 2:
            raise BusinessError("权限包名称至少 2 个字符")
        if not dce_group_id:
            raise BusinessError("请选择 DCE 用户组")
        try:
            remote_group = self.dce.get_group(dce_group_id)
        except DCEError as exc:
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        if not remote_group:
            raise BusinessError("选择的 DCE 用户组不存在或已删除", 404)
        group_name = str(remote_group.get("name") or "").strip()
        if not group_name:
            raise BusinessError("DCE 用户组缺少名称，无法创建权限包")
        target_summary = summary.strip() or str(
            remote_group.get("description") or remote_group.get("summary") or group_name
        ).strip()
        package_id = new_id()
        now = utcnow()
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO permission_packages
                      (id, name, dce_group_id, dce_group_name, description, target_summary,
                       enabled, last_validated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        package_id,
                        name,
                        dce_group_id,
                        group_name,
                        "映射到平台现有 DCE 用户组",
                        target_summary,
                        now,
                        now,
                    ),
                )
        except DB_INTEGRITY_ERRORS as exc:
            raise BusinessError("权限包名称或 DCE 用户组已经被使用") from exc
        self.db.audit(
            "permission_package.create",
            "permission_package",
            package_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "name": name,
                "dce_group_id": dce_group_id,
                "dce_group_name": group_name,
            },
        )
        return package_id

    def update_permission_package(
        self,
        actor_id: str,
        package_id: str,
        name: str,
        dce_group_id: str,
        summary: str,
    ) -> None:
        package = self.get_permission_package(package_id)
        name = name.strip()
        dce_group_id = dce_group_id.strip() or str(package["dce_group_id"])
        if len(name) < 2:
            raise BusinessError("权限包名称至少 2 个字符")
        if package["system_managed"] and dce_group_id != package["dce_group_id"]:
            raise BusinessError("系统配置的默认权限包不能更换 DCE 用户组")
        if package["assignment_count"] and dce_group_id != package["dce_group_id"]:
            raise BusinessError("权限包已被考试任务引用，不能更换 DCE 用户组")
        if package["user_binding_count"] and dce_group_id != package["dce_group_id"]:
            raise BusinessError("权限包已绑定用户，不能更换 DCE 用户组")
        try:
            remote_group = self.dce.get_group(dce_group_id)
        except DCEError as exc:
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        if not remote_group:
            raise BusinessError("选择的 DCE 用户组不存在或已删除", 404)
        group_name = str(remote_group.get("name") or "").strip()
        if not group_name:
            raise BusinessError("DCE 用户组缺少名称，无法保存权限包")
        target_summary = summary.strip() or str(
            remote_group.get("description") or remote_group.get("summary") or group_name
        ).strip()
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE permission_packages
                    SET name = ?, dce_group_id = ?, dce_group_name = ?,
                        target_summary = ?, last_validated_at = ?
                    WHERE id = ? AND enabled = 1
                    """,
                    (name, dce_group_id, group_name, target_summary, utcnow(), package_id),
                )
        except DB_INTEGRITY_ERRORS as exc:
            raise BusinessError("权限包名称或 DCE 用户组已经被使用") from exc
        self.db.audit(
            "permission_package.update",
            "permission_package",
            package_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "name": name,
                "dce_group_id": dce_group_id,
                "dce_group_name": group_name,
            },
        )

    def delete_permission_package(self, actor_id: str, package_id: str) -> None:
        package = self.get_permission_package(package_id)
        if package["system_managed"]:
            raise BusinessError("系统配置的默认权限包不能删除")
        if package["assignment_count"]:
            raise BusinessError(
                f"权限包已被 {package['assignment_count']} 个考试任务引用，不能删除"
            )
        if package["user_binding_count"]:
            raise BusinessError(
                f"权限包已绑定 {package['user_binding_count']} 个用户，不能删除"
            )
        with self.db.connect() as conn:
            deleted = conn.execute(
                "DELETE FROM permission_packages WHERE id = ?", (package_id,)
            ).rowcount
        if not deleted:
            raise BusinessError("权限包不存在", 404)
        self.db.audit(
            "permission_package.delete",
            "permission_package",
            package_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "name": package["name"],
                "dce_group_id": package["dce_group_id"],
                "dce_group_name": package["dce_group_name"],
                "dce_group_preserved": True,
            },
        )

    def create_user_and_assignment(
        self,
        *,
        actor_id: str,
        username: str,
        password: str,
        temporary: bool,
        quiz_required: bool,
        template_id: str,
        permission_package_id: str,
        deadline: str | None,
    ) -> tuple[str, str]:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}", username):
            raise BusinessError("用户名使用字母、数字、点、下划线或中划线，长度 2 到 63")
        if len(password) < 8:
            raise BusinessError("初始密码至少 8 位")
        deadline_dt = _parse_datetime(deadline)
        if deadline_dt and deadline_dt <= datetime.now(timezone.utc):
            raise BusinessError("截止时间必须晚于当前时间")
        with self.db.connect() as conn:
            template = conn.execute(
                "SELECT * FROM exam_templates WHERE id = ? AND status = 'PUBLISHED'",
                (template_id,),
            ).fetchone()
            package = conn.execute(
                "SELECT * FROM permission_packages WHERE id = ? AND enabled = 1",
                (permission_package_id,),
            ).fetchone()
        if not template:
            raise BusinessError("试卷模板不存在或未发布")
        if not package:
            raise BusinessError("权限包不存在或未启用")
        provisioning_id = new_id()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO provisioning_requests
                  (id, username, state, created_by, created_at, updated_at)
                VALUES (?, ?, 'CREATING', ?, ?, ?)
                """,
                (provisioning_id, username, actor_id, utcnow(), utcnow()),
            )
        try:
            remote = self.dce.create_user(username, password, temporary)
        except DCEError as exc:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE provisioning_requests SET state = 'FAILED', error_detail = ?, updated_at = ? WHERE id = ?",
                    (str(exc), utcnow(), provisioning_id),
                )
            self.db.audit(
                "user.provision",
                "provisioning_request",
                provisioning_id,
                "failed",
                actor_user_id=actor_id,
                detail={"username": username, "error": str(exc)},
            )
            raise BusinessError(str(exc), exc.status_code or 502) from exc
        try:
            local_user = self.sync_local_user(remote, is_admin=False)
            assignment_id = new_id()
            purpose = "ONBOARDING_GATE" if quiz_required else "DIRECT_ACTIVATION"
            status = "PENDING" if quiz_required else "PASSED_PENDING_GRANT"
            now = utcnow()
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO assignments
                      (id, user_id, exam_template_id, permission_package_id, purpose, status,
                       pass_score, max_attempts, duration_minutes, deadline, created_by,
                       passed_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment_id,
                        local_user["id"],
                        template["id"],
                        package["id"],
                        purpose,
                        status,
                        template["pass_score"],
                        template["max_attempts"],
                        template["duration_minutes"],
                        deadline_dt.isoformat(timespec="seconds") if deadline_dt else None,
                        actor_id,
                        now if not quiz_required else None,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE provisioning_requests
                    SET state = 'CREATED', dce_user_id = ?, updated_at = ? WHERE id = ?
                    """,
                    (remote["id"], now, provisioning_id),
                )
                if not quiz_required:
                    self._insert_grant_job(conn, assignment_id)
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE provisioning_requests SET state = 'UNKNOWN', error_detail = ?, updated_at = ? WHERE id = ?",
                    ("DCE 用户可能已创建，请人工对账", utcnow(), provisioning_id),
                )
            raise BusinessError("DCE 用户可能已创建，但本地建档失败，请人工对账", 500) from exc
        self.db.audit(
            "user.provision",
            "assignment",
            assignment_id,
            "success",
            actor_user_id=actor_id,
            detail={"username": username, "purpose": purpose, "package": package["name"]},
        )
        return local_user["id"], assignment_id

    def assign_training(
        self,
        *,
        actor_id: str,
        user_id: str,
        template_id: str,
        permission_package_id: str,
        deadline: str | None,
    ) -> str:
        deadline_dt = _parse_datetime(deadline)
        if deadline_dt and deadline_dt <= datetime.now(timezone.utc):
            raise BusinessError("截止时间必须晚于当前时间")
        with self.db.connect() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ? AND enabled = 1", (user_id,)
            ).fetchone()
            template = conn.execute(
                "SELECT * FROM exam_templates WHERE id = ? AND status = 'PUBLISHED'",
                (template_id,),
            ).fetchone()
            package = conn.execute(
                "SELECT * FROM permission_packages WHERE id = ? AND enabled = 1",
                (permission_package_id,),
            ).fetchone()
            if not user:
                raise BusinessError("用户不存在或已停用", 404)
            if not template:
                raise BusinessError("试卷模板不存在或未发布", 404)
            if not package:
                raise BusinessError("权限包不存在或未启用", 404)
            assignment_id = new_id()
            now = utcnow()
            conn.execute(
                """
                INSERT INTO assignments
                  (id, user_id, exam_template_id, permission_package_id, purpose, status,
                   pass_score, max_attempts, duration_minutes, deadline, created_by,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, 'TRAINING_ONLY', 'PENDING', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment_id,
                    user_id,
                    template_id,
                    package["id"],
                    template["pass_score"],
                    template["max_attempts"],
                    template["duration_minutes"],
                    deadline_dt.isoformat(timespec="seconds") if deadline_dt else None,
                    actor_id,
                    now,
                    now,
                ),
            )
        self.db.audit(
            "assignment.training.create",
            "assignment",
            assignment_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "user_id": user_id,
                "permission_package_id": package["id"],
            },
        )
        return assignment_id

    def update_assignment(
        self,
        *,
        actor_id: str,
        assignment_id: str,
        user_id: str,
        template_id: str,
        permission_package_id: str,
        deadline: str | None,
    ) -> None:
        deadline_dt = _parse_datetime(deadline)
        if deadline_dt and deadline_dt <= datetime.now(timezone.utc):
            raise BusinessError("截止时间必须晚于当前时间")
        with self.db.connect(immediate=True) as conn:
            assignment = conn.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM attempts at WHERE at.assignment_id = a.id) AS attempt_count
                FROM assignments a WHERE a.id = ?
                """,
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise BusinessError("考试任务不存在", 404)
            if assignment["status"] != "PENDING":
                raise BusinessError("只有待考试任务可以修改")
            user = conn.execute(
                "SELECT * FROM users WHERE id = ? AND enabled = 1", (user_id,)
            ).fetchone()
            template = conn.execute(
                "SELECT * FROM exam_templates WHERE id = ? AND status = 'PUBLISHED'",
                (template_id,),
            ).fetchone()
            if user is None:
                raise BusinessError("用户不存在或已停用", 404)
            if template is None:
                raise BusinessError("试卷模板不存在或未发布", 404)
            package = conn.execute(
                "SELECT * FROM permission_packages WHERE id = ? AND enabled = 1",
                (permission_package_id,),
            ).fetchone()
            if package is None:
                raise BusinessError("权限包不存在或未启用", 404)
            package_id: str | None = package["id"]
            reset_attempts = bool(
                assignment["attempt_count"]
                and (
                    str(assignment["user_id"]) != user_id
                    or str(assignment["exam_template_id"]) != template_id
                )
            )
            if reset_attempts:
                conn.execute(
                    """
                    DELETE FROM attempt_answers WHERE attempt_id IN (
                        SELECT id FROM attempts WHERE assignment_id = ?
                    )
                    """,
                    (assignment_id,),
                )
                conn.execute(
                    """
                    DELETE FROM attempt_questions WHERE attempt_id IN (
                        SELECT id FROM attempts WHERE assignment_id = ?
                    )
                    """,
                    (assignment_id,),
                )
                conn.execute(
                    "DELETE FROM attempts WHERE assignment_id = ?", (assignment_id,)
                )
            now = utcnow()
            conn.execute(
                """
                UPDATE assignments
                SET user_id = ?, exam_template_id = ?, permission_package_id = ?,
                    pass_score = ?, max_attempts = ?, duration_minutes = ?,
                    deadline = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    user_id,
                    template_id,
                    package_id,
                    template["pass_score"],
                    template["max_attempts"],
                    template["duration_minutes"],
                    deadline_dt.isoformat(timespec="seconds") if deadline_dt else None,
                    now,
                    assignment_id,
                ),
            )
        self.db.audit(
            "assignment.update",
            "assignment",
            assignment_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "user_id": user_id,
                "template_id": template_id,
                "permission_package_id": package_id,
                "deadline": deadline_dt.isoformat(timespec="seconds") if deadline_dt else None,
                "reset_attempt_count": assignment["attempt_count"] if reset_attempts else 0,
            },
        )

    def delete_assignment(self, actor_id: str, assignment_id: str) -> None:
        with self.db.connect(immediate=True) as conn:
            assignment = conn.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM attempts at WHERE at.assignment_id = a.id) AS attempt_count,
                       (SELECT COUNT(*) FROM grant_jobs j WHERE j.assignment_id = a.id) AS grant_job_count
                FROM assignments a WHERE a.id = ?
                """,
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise BusinessError("考试任务不存在", 404)
            if assignment["status"] != "PENDING" or assignment["grant_job_count"]:
                raise BusinessError("只有未进入授权流程的待考试任务可以删除")
            conn.execute(
                """
                DELETE FROM attempt_answers WHERE attempt_id IN (
                    SELECT id FROM attempts WHERE assignment_id = ?
                )
                """,
                (assignment_id,),
            )
            conn.execute(
                """
                DELETE FROM attempt_questions WHERE attempt_id IN (
                    SELECT id FROM attempts WHERE assignment_id = ?
                )
                """,
                (assignment_id,),
            )
            conn.execute("DELETE FROM attempts WHERE assignment_id = ?", (assignment_id,))
            deleted = conn.execute(
                "DELETE FROM assignments WHERE id = ?", (assignment_id,)
            ).rowcount
        if not deleted:
            raise BusinessError("考试任务不存在", 404)
        self.db.audit(
            "assignment.delete",
            "assignment",
            assignment_id,
            "success",
            actor_user_id=actor_id,
            detail={
                "user_id": assignment["user_id"],
                "template_id": assignment["exam_template_id"],
                "permission_package_id": assignment["permission_package_id"],
                "deleted_attempt_count": assignment["attempt_count"],
            },
        )

    def _insert_grant_job(self, conn: sqlite3.Connection, assignment_id: str) -> None:
        now = utcnow()
        conn.execute(
            """
            INSERT OR IGNORE INTO grant_jobs
              (id, assignment_id, state, attempt_count, created_at, updated_at)
            VALUES (?, ?, 'QUEUED', 0, ?, ?)
            """,
            (new_id(), assignment_id, now, now),
        )

    def list_assignments(self, *, user_id: str | None = None) -> list[sqlite3.Row]:
        query = """
            SELECT a.*, u.username, t.name AS template_name, t.version AS template_version,
                   p.name AS package_name, j.state AS grant_job_state, j.last_error_detail,
                   (SELECT COUNT(*) FROM attempts at WHERE at.assignment_id = a.id) AS attempt_count
            FROM assignments a
            JOIN users u ON u.id = a.user_id
            LEFT JOIN exam_templates t ON t.id = a.exam_template_id
            LEFT JOIN permission_packages p ON p.id = a.permission_package_id
            LEFT JOIN grant_jobs j ON j.assignment_id = a.id
        """
        params: tuple[Any, ...] = ()
        if user_id:
            query += " WHERE a.user_id = ?"
            params = (user_id,)
        query += " ORDER BY a.created_at DESC"
        with self.db.connect() as conn:
            return conn.execute(query, params).fetchall()

    def get_assignment(self, assignment_id: str) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT a.*, u.username, t.name AS template_name, p.name AS package_name,
                       p.dce_group_name AS permission_group_name,
                       j.state AS grant_job_state, j.last_error_detail
                FROM assignments a
                JOIN users u ON u.id = a.user_id
                LEFT JOIN exam_templates t ON t.id = a.exam_template_id
                LEFT JOIN permission_packages p ON p.id = a.permission_package_id
                LEFT JOIN grant_jobs j ON j.assignment_id = a.id
                WHERE a.id = ?
                """,
                (assignment_id,),
            ).fetchone()

    def start_attempt(self, assignment_id: str, user_id: str) -> str:
        now_dt = datetime.now(timezone.utc)
        terminal_error: BusinessError | None = None
        attempt_id: str | None = None
        with self.db.connect(immediate=True) as conn:
            assignment = conn.execute(
                "SELECT * FROM assignments WHERE id = ? AND user_id = ?",
                (assignment_id, user_id),
            ).fetchone()
            if not assignment:
                raise BusinessError("考试任务不存在", 404)
            if assignment["status"] != "PENDING":
                raise BusinessError("当前考试状态不能开始答题", 409)
            if _is_past(assignment["deadline"]):
                conn.execute(
                    "UPDATE assignments SET status = 'EXPIRED', updated_at = ? WHERE id = ?",
                    (utcnow(), assignment_id),
                )
                terminal_error = BusinessError("考试已经过截止时间", 409)
            else:
                existing = conn.execute(
                    """
                    SELECT * FROM attempts WHERE assignment_id = ? AND status = 'IN_PROGRESS'
                    ORDER BY attempt_no DESC LIMIT 1
                    """,
                    (assignment_id,),
                ).fetchone()
                if existing:
                    started = datetime.fromisoformat(existing["started_at"])
                    if started + timedelta(minutes=assignment["duration_minutes"]) > now_dt:
                        attempt_id = existing["id"]
                    else:
                        conn.execute(
                            "UPDATE attempts SET status = 'ABANDONED' WHERE id = ?",
                            (existing["id"],),
                        )
                if attempt_id is None:
                    used = conn.execute(
                        "SELECT COUNT(*) AS n FROM attempts WHERE assignment_id = ? AND status IN ('SUBMITTED','ABANDONED')",
                        (assignment_id,),
                    ).fetchone()["n"]
                    if used >= assignment["max_attempts"]:
                        conn.execute(
                            "UPDATE assignments SET status = 'LOCKED', updated_at = ? WHERE id = ?",
                            (utcnow(), assignment_id),
                        )
                        terminal_error = BusinessError("已经达到最大尝试次数", 409)
                    else:
                        question_rows = conn.execute(
                            """
                            SELECT q.id, q.options FROM questions q
                            JOIN exam_template_questions tq ON tq.question_id = q.id
                            WHERE tq.exam_template_id = ? AND q.status = 'PUBLISHED'
                            """,
                            (assignment["exam_template_id"],),
                        ).fetchall()
                        if not question_rows:
                            raise BusinessError("试卷没有可用题目", 500)
                        rows = list(question_rows)
                        random.SystemRandom().shuffle(rows)
                        template = conn.execute(
                            "SELECT question_count FROM exam_templates WHERE id = ?",
                            (assignment["exam_template_id"],),
                        ).fetchone()
                        rows = rows[: template["question_count"]]
                        attempt_id = new_id()
                        conn.execute(
                            """
                            INSERT INTO attempts (id, assignment_id, attempt_no, status, started_at)
                            VALUES (?, ?, ?, 'IN_PROGRESS', ?)
                            """,
                            (
                                attempt_id,
                                assignment_id,
                                used + 1,
                                now_dt.isoformat(timespec="seconds"),
                            ),
                        )
                        for position, question in enumerate(rows, start=1):
                            option_order = list(range(len(json.loads(question["options"]))))
                            random.SystemRandom().shuffle(option_order)
                            conn.execute(
                                """
                                INSERT INTO attempt_questions (attempt_id, question_id, position, option_order)
                                VALUES (?, ?, ?, ?)
                                """,
                                (
                                    attempt_id,
                                    question["id"],
                                    position,
                                    json.dumps(option_order),
                                ),
                            )
        if terminal_error:
            raise terminal_error
        if attempt_id is None:
            raise BusinessError("无法创建答题记录", 500)
        return attempt_id

    def load_attempt(self, attempt_id: str, user_id: str) -> tuple[sqlite3.Row, list[dict]]:
        with self.db.connect() as conn:
            attempt = conn.execute(
                """
                SELECT at.*, a.user_id, a.duration_minutes, a.pass_score, a.status AS assignment_status,
                       t.name AS template_name
                FROM attempts at
                JOIN assignments a ON a.id = at.assignment_id
                JOIN exam_templates t ON t.id = a.exam_template_id
                WHERE at.id = ? AND a.user_id = ?
                """,
                (attempt_id, user_id),
            ).fetchone()
            if not attempt:
                raise BusinessError("答题记录不存在", 404)
            rows = conn.execute(
                """
                SELECT q.*, aq.position, aq.option_order
                FROM attempt_questions aq
                JOIN questions q ON q.id = aq.question_id
                WHERE aq.attempt_id = ? ORDER BY aq.position
                """,
                (attempt_id,),
            ).fetchall()
        questions: list[dict] = []
        for row in rows:
            item = dict(row)
            original = json.loads(row["options"])
            order = json.loads(row["option_order"])
            item["display_options"] = [
                {"original_index": original_index, "text": original[original_index]}
                for original_index in order
            ]
            questions.append(item)
        return attempt, questions

    def submit_attempt(
        self, attempt_id: str, user_id: str, answers: dict[str, list[int]]
    ) -> dict:
        now_dt = datetime.now(timezone.utc)
        timeout_error: BusinessError | None = None
        with self.db.connect(immediate=True) as conn:
            attempt = conn.execute(
                """
                SELECT at.*, a.user_id, a.status AS assignment_status, a.purpose,
                       a.permission_package_id,
                       a.pass_score, a.max_attempts, a.duration_minutes, a.id AS assignment_id,
                       t.critical_must_all_correct
                FROM attempts at
                JOIN assignments a ON a.id = at.assignment_id
                JOIN exam_templates t ON t.id = a.exam_template_id
                WHERE at.id = ? AND a.user_id = ?
                """,
                (attempt_id, user_id),
            ).fetchone()
            if not attempt:
                raise BusinessError("答题记录不存在", 404)
            if attempt["status"] == "SUBMITTED":
                return {
                    "score": attempt["score"],
                    "passed": bool(attempt["passed"]),
                    "critical_failed": bool(attempt["critical_failed"]),
                    "assignment_id": attempt["assignment_id"],
                    "idempotent": True,
                }
            if attempt["status"] != "IN_PROGRESS" or attempt["assignment_status"] != "PENDING":
                raise BusinessError("当前答题记录不能提交", 409)
            started_at = datetime.fromisoformat(attempt["started_at"])
            if started_at + timedelta(minutes=attempt["duration_minutes"]) < now_dt:
                conn.execute("UPDATE attempts SET status = 'ABANDONED' WHERE id = ?", (attempt_id,))
                used = conn.execute(
                    "SELECT COUNT(*) AS n FROM attempts WHERE assignment_id = ? AND status IN ('SUBMITTED','ABANDONED')",
                    (attempt["assignment_id"],),
                ).fetchone()["n"]
                if used >= attempt["max_attempts"]:
                    conn.execute(
                        "UPDATE assignments SET status = 'LOCKED', updated_at = ? WHERE id = ?",
                        (utcnow(), attempt["assignment_id"]),
                    )
                timeout_error = BusinessError("考试已超时，本次尝试已结束", 409)
            else:
                questions = conn.execute(
                    """
                    SELECT q.* FROM attempt_questions aq
                    JOIN questions q ON q.id = aq.question_id
                    WHERE aq.attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchall()
                if not questions:
                    raise BusinessError("答题记录没有题目", 500)
                correct_count = 0
                critical_failed = False
                for question in questions:
                    selected = sorted(set(answers.get(question["id"], [])))
                    correct_options = sorted(json.loads(question["correct_options"]))
                    correct = selected == correct_options
                    correct_count += int(correct)
                    if question["critical"] and not correct:
                        critical_failed = True
                    conn.execute(
                        """
                        INSERT INTO attempt_answers (attempt_id, question_id, selected_options, correct)
                        VALUES (?, ?, ?, ?)
                        """,
                        (attempt_id, question["id"], json.dumps(selected), int(correct)),
                    )
                score = round(correct_count * 100 / len(questions))
                passed = score >= attempt["pass_score"] and not (
                    attempt["critical_must_all_correct"] and critical_failed
                )
                now = now_dt.isoformat(timespec="seconds")
                conn.execute(
                    """
                    UPDATE attempts
                    SET status = 'SUBMITTED', submitted_at = ?, score = ?, passed = ?, critical_failed = ?
                    WHERE id = ?
                    """,
                    (now, score, int(passed), int(critical_failed), attempt_id),
                )
                if passed:
                    if attempt["permission_package_id"]:
                        assignment_status = "PASSED_PENDING_GRANT"
                        self._insert_grant_job(conn, attempt["assignment_id"])
                    else:
                        # 兼容升级前未指定权限包的历史培训任务。
                        assignment_status = "COMPLETED"
                    conn.execute(
                        "UPDATE assignments SET status = ?, passed_at = ?, updated_at = ? WHERE id = ?",
                        (assignment_status, now, now, attempt["assignment_id"]),
                    )
                else:
                    submitted_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM attempts WHERE assignment_id = ? AND status IN ('SUBMITTED','ABANDONED')",
                        (attempt["assignment_id"],),
                    ).fetchone()["n"]
                    assignment_status = (
                        "LOCKED" if submitted_count >= attempt["max_attempts"] else "PENDING"
                    )
                    conn.execute(
                        "UPDATE assignments SET status = ?, updated_at = ? WHERE id = ?",
                        (assignment_status, now, attempt["assignment_id"]),
                    )
        if timeout_error:
            self.db.audit(
                "attempt.timeout",
                "assignment",
                attempt["assignment_id"],
                "abandoned",
                actor_user_id=user_id,
                detail={"attempt_id": attempt_id},
            )
            raise timeout_error
        self.db.audit(
            "attempt.submit",
            "assignment",
            attempt["assignment_id"],
            "passed" if passed else "failed",
            actor_user_id=user_id,
            detail={"attempt_id": attempt_id, "score": score, "critical_failed": critical_failed},
        )
        return {
            "score": score,
            "passed": passed,
            "critical_failed": critical_failed,
            "assignment_id": attempt["assignment_id"],
            "assignment_status": assignment_status,
            "idempotent": False,
        }

    def retry_grant(self, assignment_id: str, actor_id: str) -> None:
        with self.db.connect(immediate=True) as conn:
            assignment = conn.execute(
                "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
            if not assignment:
                raise BusinessError("考试任务不存在", 404)
            if assignment["status"] != "PASSED_PENDING_GRANT" or not assignment["passed_at"]:
                raise BusinessError("只有已经通过考试且授权失败的任务才能重试授权", 409)
            job = conn.execute(
                "SELECT * FROM grant_jobs WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
            if not job or job["state"] not in {"RETRYABLE_FAILED", "PERMANENT_FAILED"}:
                raise BusinessError("当前授权任务不处于失败状态", 409)
            conn.execute(
                """
                UPDATE grant_jobs SET state = 'QUEUED', next_retry_at = NULL,
                       last_error_detail = '', locked_at = NULL, locked_by = NULL, updated_at = ?
                WHERE id = ?
                """,
                (utcnow(), job["id"]),
            )
        self.db.audit(
            "grant.retry",
            "assignment",
            assignment_id,
            "queued",
            actor_user_id=actor_id,
        )

    def process_one_grant_job(self, worker_id: str = "inline-worker") -> bool:
        now = utcnow()
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(
            timespec="seconds"
        )
        with self.db.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE grant_jobs
                SET state = 'RETRYABLE_FAILED', next_retry_at = ?,
                    last_error_detail = 'Worker lease expired; reclaimed automatically',
                    locked_at = NULL, locked_by = NULL, updated_at = ?
                WHERE state = 'RUNNING' AND locked_at < ?
                """,
                (now, now, stale_before),
            )
            job = conn.execute(
                """
                SELECT * FROM grant_jobs
                WHERE state IN ('QUEUED','RETRYABLE_FAILED')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not job:
                return False
            changed = conn.execute(
                """
                UPDATE grant_jobs SET state = 'RUNNING', locked_at = ?, locked_by = ?,
                       attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ? AND state IN ('QUEUED','RETRYABLE_FAILED')
                """,
                (now, worker_id, now, job["id"]),
            ).rowcount
            if changed != 1:
                return False
            job_id = job["id"]
        with self.db.connect() as conn:
            context = conn.execute(
                """
                SELECT j.*, a.status AS assignment_status, a.passed_at, a.id AS assignment_id,
                       u.id AS local_user_id, u.dce_user_id,
                       p.id AS permission_package_id, p.dce_group_id,
                       current_p.dce_group_id AS current_dce_group_id
                FROM grant_jobs j
                JOIN assignments a ON a.id = j.assignment_id
                JOIN users u ON u.id = a.user_id
                JOIN permission_packages p ON p.id = a.permission_package_id
                LEFT JOIN permission_packages current_p ON current_p.id = u.permission_package_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
        if not context:
            self._finish_job_failure(job_id, "授权上下文不完整", retryable=False)
            return True
        if context["assignment_status"] != "PASSED_PENDING_GRANT" or not context["passed_at"]:
            self._finish_job_failure(job_id, "考试尚未通过，拒绝授权", retryable=False)
            return True
        try:
            if not self.dce.is_user_in_group(context["dce_group_id"], context["dce_user_id"]):
                self.dce.add_user_to_group(context["dce_group_id"], context["dce_user_id"])
            if not self.dce.is_user_in_group(context["dce_group_id"], context["dce_user_id"]):
                raise DCEError("加组后未查询到最终成员关系", retryable=True)
            if (
                context["current_dce_group_id"]
                and context["current_dce_group_id"] != context["dce_group_id"]
            ):
                self.dce.remove_user_from_group(
                    context["current_dce_group_id"], context["dce_user_id"]
                )
        except DCEError as exc:
            self._finish_job_failure(job_id, str(exc), retryable=exc.retryable)
            self.db.audit(
                "grant.execute",
                "assignment",
                context["assignment_id"],
                "failed",
                detail={"error": str(exc), "retryable": exc.retryable},
            )
            return True
        with self.db.connect(immediate=True) as conn:
            finished = utcnow()
            conn.execute(
                """
                UPDATE grant_jobs SET state = 'SUCCEEDED', last_error_detail = '',
                       locked_at = NULL, locked_by = NULL, updated_at = ? WHERE id = ?
                """,
                (finished, job_id),
            )
            conn.execute(
                """
                UPDATE assignments SET status = 'ACTIVE', activated_at = ?, updated_at = ?
                WHERE id = ? AND status = 'PASSED_PENDING_GRANT'
                """,
                (finished, finished, context["assignment_id"]),
            )
            conn.execute(
                """
                UPDATE users SET permission_package_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (context["permission_package_id"], finished, context["local_user_id"]),
            )
        self.db.audit(
            "grant.execute",
            "assignment",
            context["assignment_id"],
            "success",
            detail={"group_id": context["dce_group_id"]},
        )
        return True

    def _finish_job_failure(self, job_id: str, message: str, *, retryable: bool) -> None:
        with self.db.connect(immediate=True) as conn:
            job = conn.execute("SELECT * FROM grant_jobs WHERE id = ?", (job_id,)).fetchone()
            attempts = job["attempt_count"] if job else 1
            should_retry = retryable and attempts < 8
            state = "RETRYABLE_FAILED" if should_retry else "PERMANENT_FAILED"
            next_retry = None
            if should_retry:
                delay = min(300, 10 * (2 ** max(0, attempts - 1)))
                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(
                    timespec="seconds"
                )
            conn.execute(
                """
                UPDATE grant_jobs SET state = ?, next_retry_at = ?, last_error_detail = ?,
                       locked_at = NULL, locked_by = NULL, updated_at = ? WHERE id = ?
                """,
                (state, next_retry, message[:500], utcnow(), job_id),
            )

    def list_audits(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT e.*, u.username AS actor_name FROM audit_events e
                LEFT JOIN users u ON u.id = e.actor_user_id
                ORDER BY e.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
