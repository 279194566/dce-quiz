from __future__ import annotations

import hmac
import json
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import ROOT, Settings
from .db import Database
from .dce import DCEError, build_dce_client
from .oidc import DCEOIDCClient, OIDCError
from .services import BusinessError, QuizService


TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    db = Database(settings.database_path, url=settings.database_url or None)
    db.initialize(seed_mock_dce=settings.dce_mode == "mock")
    dce = build_dce_client(settings, db)
    oidc = (
        DCEOIDCClient(
            base_url=settings.dce_base_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
            scopes=settings.oidc_scopes,
            ca_bundle=settings.dce_ca_bundle,
            insecure_skip_tls_verify=settings.dce_insecure_skip_tls_verify,
        )
        if settings.dce_mode == "real"
        else None
    )
    service = QuizService(db, dce)
    login_path = "/dev/login" if settings.dce_mode == "mock" else "/auth/login?switch=1"

    app = FastAPI(title="DCE 5 平台准入考试", version="2.0-dev")
    app.state.settings = settings
    app.state.db = db
    app.state.dce = dce
    app.state.oidc = oidc
    app.state.service = service
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.app_env == "production",
        max_age=settings.session_ttl_seconds,
    )

    def current_user(request: Request):
        user_id = request.session.get("user_id")
        return service.get_user(user_id) if user_id else None

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf_token"] = token
        return token

    async def require_csrf(request: Request) -> None:
        form = await request.form()
        expected = request.session.get("csrf_token") or ""
        actual = str(form.get("csrf_token") or "")
        if not expected or not hmac.compare_digest(expected, actual):
            raise BusinessError("请求校验失败，请刷新页面后重试", 403)

    def require_login(request: Request):
        user = current_user(request)
        if not user:
            raise BusinessError("请先登录", 401)
        if not user["enabled"]:
            raise BusinessError("用户已停用", 403)
        return user

    def require_admin(request: Request):
        user = require_login(request)
        if not user["is_admin"]:
            raise BusinessError("需要 Quiz 管理员权限", 403)
        return user

    def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
        values = dict(context or {})
        values.setdefault("current_user", current_user(request))
        values.setdefault("csrf_token", csrf_token(request))
        values.setdefault("app_env", settings.app_env)
        values.setdefault("dce_mode", settings.dce_mode)
        values.setdefault("dce_base_url", settings.dce_base_url)
        section = ""
        if request.url.path.startswith("/admin"):
            if request.url.path.startswith("/admin/exam-templates"):
                section = "papers"
            else:
                requested = request.query_params.get("view") or "tasks"
                section = requested if requested in {"tasks", "users", "packages", "audits"} else "tasks"
        values["admin_section"] = section
        return TEMPLATES.TemplateResponse(request, name, values, status_code=status_code)

    def redirect_for_error(request: Request, exc: BusinessError, target: str):
        request.session["flash_error"] = exc.message
        return RedirectResponse(target, status_code=303)

    def pop_flash(request: Request) -> tuple[str, str]:
        return (
            str(request.session.pop("flash_error", "")),
            str(request.session.pop("flash_notice", "")),
        )

    def drain_dev_worker(limit: int = 20) -> int:
        processed = 0
        while processed < limit and service.process_one_grant_job("dev-inline-worker"):
            processed += 1
        return processed

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        user = current_user(request)
        if user:
            return RedirectResponse("/admin" if user["is_admin"] else "/exam", status_code=303)
        return RedirectResponse(login_path, status_code=303)

    @app.get("/dev/login", response_class=HTMLResponse)
    def dev_login_page(request: Request):
        if settings.dce_mode != "mock" or settings.app_env == "production":
            return JSONResponse({"detail": "开发身份入口不可用"}, status_code=404)
        error, notice = pop_flash(request)
        return render(
            request,
            "login.html",
            {"users": service.list_login_users(), "error": error, "notice": notice},
        )

    @app.post("/dev/login")
    async def dev_login(request: Request, dce_user_id: str = Form()):
        if settings.dce_mode != "mock" or settings.app_env == "production":
            return JSONResponse({"detail": "开发身份入口不可用"}, status_code=404)
        try:
            await require_csrf(request)
            user = service.login_mock_user(dce_user_id)
        except BusinessError as exc:
            return redirect_for_error(request, exc, "/dev/login")
        request.session.clear()
        request.session["user_id"] = user["id"]
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/admin" if user["is_admin"] else "/exam", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def real_login_page(request: Request):
        if settings.dce_mode != "real":
            return JSONResponse({"detail": "DCE OIDC 登录入口不可用"}, status_code=404)
        return RedirectResponse("/auth/login?switch=1", status_code=303)

    @app.get("/auth/login")
    def oidc_login(request: Request):
        if settings.dce_mode != "real" or oidc is None:
            return JSONResponse({"detail": "DCE OIDC 登录入口不可用"}, status_code=404)
        switch_account = request.query_params.get("switch") == "1"
        if switch_account:
            try:
                target = end_dce_session_target(request)
            except OIDCError as exc:
                request.session.clear()
                return render(
                    request,
                    "auth_error.html",
                    {"error": str(exc)},
                    status_code=exc.status_code,
                )
            if target:
                return RedirectResponse(target, status_code=302)
        state = secrets.token_urlsafe(32)
        request.session.clear()
        request.session["oidc_state"] = state
        try:
            target = oidc.authorization_url(
                state=state,
                prompt="login" if switch_account else "",
            )
        except OIDCError as exc:
            request.session.clear()
            return render(
                request,
                "auth_error.html",
                {"error": str(exc)},
                status_code=exc.status_code,
            )
        return RedirectResponse(target, status_code=302)

    def end_dce_session_target(request: Request) -> str | None:
        hint_key = str(request.session.get("oidc_logout_hint") or "")
        had_user = bool(request.session.get("user_id"))
        id_token = db.get_logout_hint(hint_key) if hint_key else ""
        if not had_user and not id_token:
            return None
        target = oidc.end_session_url(
            id_token_hint=id_token,
            post_logout_redirect_uri=settings.oidc_redirect_uri,
        )
        if hint_key:
            db.delete_logout_hint(hint_key)
        request.session.clear()
        return target

    @app.get("/auth/callback")
    def oidc_callback(request: Request):
        if settings.dce_mode != "real" or oidc is None:
            return JSONResponse({"detail": "DCE OIDC 回调入口不可用"}, status_code=404)
        expected_state = str(request.session.pop("oidc_state", ""))
        returned_state = str(request.query_params.get("state") or "")
        provider_error = str(request.query_params.get("error") or "")
        code = str(request.query_params.get("code") or "")
        if not code and not provider_error and not returned_state and not expected_state:
            return RedirectResponse("/auth/login", status_code=303)
        try:
            if provider_error:
                raise OIDCError("DCE 登录被取消或拒绝", 401)
            if not expected_state or not returned_state or not hmac.compare_digest(
                expected_state, returned_state
            ):
                raise OIDCError("OIDC state 校验失败，请重新登录", 401)
            if not code:
                raise OIDCError("DCE OIDC 回调缺少授权码", 400)
            token_response = oidc.exchange_code(code)
            identity = oidc.userinfo(str(token_response["accessToken"]))
            remote = dce.get_user(identity["sub"])
            if not remote:
                raise DCEError("DCE 当前身份没有对应用户记录", status_code=401)
            if str(remote.get("username")) != identity["preferredUsername"]:
                raise DCEError("DCE OIDC 主体与用户记录不一致", status_code=401)
            if not remote.get("enabled", True):
                raise DCEError("DCE 用户已停用", status_code=403)
            roles = dce.list_user_roles(remote["id"])
            is_admin = any(
                role.get("name") == "Admin" and role.get("authorized") is True
                for role in roles
            )
            user = service.sync_local_user(remote, is_admin=is_admin)
        except (DCEError, OIDCError) as exc:
            request.session.clear()
            return render(
                request,
                "auth_error.html",
                {"error": str(exc)},
                status_code=getattr(exc, "status_code", None) or 502,
            )
        request.session.clear()
        request.session["user_id"] = user["id"]
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        id_token = str(token_response.get("idToken") or "")
        if id_token:
            request.session["oidc_logout_hint"] = db.save_logout_hint(id_token)
        db.audit("auth.oidc.login", "user", user["id"], "success")
        return RedirectResponse("/admin" if user["is_admin"] else "/exam", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        try:
            await require_csrf(request)
        except BusinessError as exc:
            return JSONResponse({"detail": exc.message}, status_code=exc.status_code)
        if settings.dce_mode == "real" and request.session.get("user_id"):
            return RedirectResponse("/auth/login?switch=1", status_code=303)
        request.session.clear()
        return RedirectResponse(login_path, status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        try:
            require_admin(request)
        except BusinessError as exc:
            return redirect_for_error(request, exc, login_path)
        error, notice = pop_flash(request)
        packages = service.list_packages()
        mapped_group_ids = {str(item["dce_group_id"]) for item in packages}
        dce_groups: list[dict] = []
        if settings.dce_mode == "real":
            try:
                dce_groups = service.list_permission_group_candidates()
            except BusinessError as exc:
                error = error or f"读取 DCE 用户组失败：{exc.message}"
        return render(
            request,
            "admin.html",
            {
                "error": error,
                "notice": notice,
                "users": service.list_users(),
                "packages": packages,
                "dce_groups": dce_groups,
                "mapped_group_ids": mapped_group_ids,
                "available_dce_group_count": sum(
                    1 for group in dce_groups if group["id"] not in mapped_group_ids
                ),
                "templates": service.list_templates(),
                "assignments": service.list_assignments(),
                "audits": service.list_audits(30),
            },
        )

    @app.post("/admin/users")
    async def admin_create_user(
        request: Request,
        username: str = Form(),
        password: str = Form(),
        temporary: str = Form(""),
        permission_package_id: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.create_user(
                actor["id"],
                username=username,
                password=password,
                temporary=temporary == "on",
                permission_package_id=permission_package_id,
            )
            request.session["flash_notice"] = "用户已创建并同步到 DCE。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=users", status_code=303)

    @app.post("/admin/users/{user_id}/update")
    async def admin_update_user(
        request: Request,
        user_id: str,
        permission_package_id: str = Form(""),
        enabled: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.update_user(
                actor["id"],
                user_id,
                permission_package_id=permission_package_id,
                enabled=enabled == "on",
            )
            request.session["flash_notice"] = "用户权限包和账户状态已更新并同步到 DCE。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=users", status_code=303)

    @app.post("/admin/users/{user_id}/delete")
    async def admin_delete_user(request: Request, user_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.delete_user(actor["id"], user_id)
            request.session["flash_notice"] = "用户及其考试数据已删除，并已同步删除 DCE 账号。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=users", status_code=303)

    @app.post("/admin/assignments")
    async def admin_assign_training(
        request: Request,
        user_id: str = Form(),
        template_id: str = Form(),
        permission_package_id: str = Form(),
        deadline: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.assign_training(
                actor_id=actor["id"],
                user_id=user_id,
                template_id=template_id,
                permission_package_id=permission_package_id,
                deadline=deadline,
            )
            request.session["flash_notice"] = "权限考试已下发；通过后将自动开通所选权限包。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=tasks", status_code=303)

    @app.post("/admin/assignments/{assignment_id}/update")
    async def admin_update_assignment(
        request: Request,
        assignment_id: str,
        user_id: str = Form(),
        template_id: str = Form(),
        permission_package_id: str = Form(""),
        deadline: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.update_assignment(
                actor_id=actor["id"],
                assignment_id=assignment_id,
                user_id=user_id,
                template_id=template_id,
                permission_package_id=permission_package_id,
                deadline=deadline,
            )
            request.session["flash_notice"] = "考试任务已更新。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=tasks", status_code=303)

    @app.post("/admin/assignments/{assignment_id}/delete")
    async def admin_delete_assignment(request: Request, assignment_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.delete_assignment(actor["id"], assignment_id)
            request.session["flash_notice"] = "考试任务已删除。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=tasks", status_code=303)

    @app.post("/admin/permission-packages")
    async def admin_create_package(
        request: Request,
        name: str = Form(),
        group_name: str = Form(""),
        dce_group_id: str = Form(""),
        summary: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            if settings.dce_mode == "real":
                service.create_permission_package_from_group(
                    actor["id"], name, dce_group_id, summary
                )
                request.session["flash_notice"] = "权限包已映射到所选 DCE 用户组。"
            else:
                service.create_permission_package(actor["id"], name, group_name, summary)
                request.session["flash_notice"] = "Mock 权限包和对应用户组已创建。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=packages", status_code=303)

    @app.post("/admin/permission-packages/{package_id}/update")
    async def admin_update_package(
        request: Request,
        package_id: str,
        name: str = Form(),
        dce_group_id: str = Form(""),
        summary: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.update_permission_package(
                actor["id"], package_id, name, dce_group_id, summary
            )
            request.session["flash_notice"] = "权限包已更新；DCE 用户组本身未被修改。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=packages", status_code=303)

    @app.post("/admin/permission-packages/{package_id}/delete")
    async def admin_delete_package(request: Request, package_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.delete_permission_package(actor["id"], package_id)
            request.session["flash_notice"] = "权限包映射已删除；DCE 用户组保留不变。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=packages", status_code=303)

    @app.get("/admin/exam-templates", response_class=HTMLResponse)
    def admin_exam_templates(request: Request):
        try:
            require_admin(request)
        except BusinessError as exc:
            return redirect_for_error(request, exc, login_path)
        error, notice = pop_flash(request)
        return render(
            request,
            "exam_templates.html",
            {
                "error": error,
                "notice": notice,
                "exam_templates": service.list_admin_templates(),
            },
        )

    @app.post("/admin/exam-templates")
    async def admin_create_exam_template(
        request: Request,
        name: str = Form(),
        pass_score: str = Form(),
        critical_must_all_correct: str = Form(""),
        duration_minutes: str = Form(),
        max_attempts: str = Form(),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            template_id = service.create_exam_template_draft(
                actor["id"],
                name=name,
                pass_score=pass_score,
                critical_must_all_correct=critical_must_all_correct == "on",
                duration_minutes=duration_minutes,
                max_attempts=max_attempts,
            )
            request.session["flash_notice"] = "试卷草稿已创建，可以开始添加题目。"
            return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
            return RedirectResponse("/admin/exam-templates", status_code=303)

    @app.post("/admin/exam-templates/upload")
    async def admin_upload_exam_template(
        request: Request, upload: UploadFile = File()
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            raw = await upload.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise BusinessError("上传文件不能超过 2 MiB")
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BusinessError("上传文件不是有效的 UTF-8 JSON") from exc
            template_id = service.import_exam_template(actor["id"], payload)
            request.session["flash_notice"] = "试卷已导入为草稿，请检查后再发布。"
            return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
            return RedirectResponse("/admin/exam-templates", status_code=303)
        finally:
            await upload.close()

    @app.get("/admin/exam-templates/{template_id}", response_class=HTMLResponse)
    def admin_exam_template_edit(request: Request, template_id: str):
        try:
            require_admin(request)
            template = service.get_exam_template(template_id)
            questions = service.list_template_questions(template_id)
        except BusinessError as exc:
            return redirect_for_error(request, exc, "/admin/exam-templates")
        error, notice = pop_flash(request)
        return render(
            request,
            "exam_template_edit.html",
            {"error": error, "notice": notice, "template": template, "questions": questions},
        )

    @app.get("/admin/exam-templates/{template_id}/download")
    def admin_download_exam_template(request: Request, template_id: str):
        try:
            require_admin(request)
            payload = service.export_exam_template(template_id)
        except BusinessError as exc:
            return JSONResponse({"detail": exc.message}, status_code=exc.status_code)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", payload["template"]["name"]).strip("-")
        safe_name = safe_name or "exam-template"
        filename = f"{safe_name}-v{payload['template']['version']}.json"
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/admin/exam-templates/{template_id}/update")
    async def admin_update_exam_template(
        request: Request,
        template_id: str,
        name: str = Form(),
        pass_score: str = Form(),
        critical_must_all_correct: str = Form(""),
        duration_minutes: str = Form(),
        max_attempts: str = Form(),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.update_exam_template(
                actor["id"], template_id, name=name, pass_score=pass_score,
                critical_must_all_correct=critical_must_all_correct == "on",
                duration_minutes=duration_minutes, max_attempts=max_attempts,
            )
            request.session["flash_notice"] = "试卷基本信息已保存。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)

    @app.post("/admin/exam-templates/{template_id}/clone")
    async def admin_clone_exam_template(request: Request, template_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            new_id = service.clone_exam_template(actor["id"], template_id)
            request.session["flash_notice"] = "已复制为新的草稿版本。"
            return RedirectResponse(f"/admin/exam-templates/{new_id}", status_code=303)
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
            return RedirectResponse("/admin/exam-templates", status_code=303)

    @app.post("/admin/exam-templates/{template_id}/publish")
    async def admin_publish_exam_template(request: Request, template_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.publish_exam_template(actor["id"], template_id)
            request.session["flash_notice"] = "试卷已发布，现在可以下发考试。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)

    @app.post("/admin/exam-templates/{template_id}/delete")
    async def admin_delete_exam_template(request: Request, template_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.delete_exam_template(actor["id"], template_id)
            request.session["flash_notice"] = "试卷已删除。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin/exam-templates", status_code=303)

    def question_form_values(
        question_type: str, stem: str, options_text: str, correct_options: str,
        explanation: str, category: str, critical: str, source_url: str,
        source_section: str, product_version: str, reviewer: str,
    ) -> dict:
        return {
            "question_type": question_type,
            "stem": stem,
            "options_text": options_text,
            "correct_options_text": correct_options,
            "explanation": explanation,
            "category": category,
            "critical": critical == "on",
            "source_url": source_url,
            "source_section": source_section,
            "product_version": product_version,
            "reviewer": reviewer,
        }

    @app.post("/admin/exam-templates/{template_id}/questions")
    async def admin_add_template_question(
        request: Request, template_id: str,
        question_type: str = Form(), stem: str = Form(), options_text: str = Form(),
        correct_options: str = Form(), explanation: str = Form(""),
        category: str = Form(""), critical: str = Form(""),
        source_url: str = Form(""), source_section: str = Form(""),
        product_version: str = Form(""), reviewer: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.add_template_question(
                actor["id"], template_id,
                **question_form_values(
                    question_type, stem, options_text, correct_options, explanation,
                    category, critical, source_url, source_section, product_version, reviewer,
                ),
            )
            request.session["flash_notice"] = "题目已添加。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)

    @app.post("/admin/exam-templates/{template_id}/questions/{question_id}/update")
    async def admin_update_template_question(
        request: Request, template_id: str, question_id: str,
        question_type: str = Form(), stem: str = Form(), options_text: str = Form(),
        correct_options: str = Form(), explanation: str = Form(""),
        category: str = Form(""), critical: str = Form(""),
        source_url: str = Form(""), source_section: str = Form(""),
        product_version: str = Form(""), reviewer: str = Form(""),
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.update_template_question(
                actor["id"], template_id, question_id,
                **question_form_values(
                    question_type, stem, options_text, correct_options, explanation,
                    category, critical, source_url, source_section, product_version, reviewer,
                ),
            )
            request.session["flash_notice"] = "题目已更新。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)

    @app.post("/admin/exam-templates/{template_id}/questions/{question_id}/delete")
    async def admin_delete_template_question(
        request: Request, template_id: str, question_id: str
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.delete_template_question(actor["id"], template_id, question_id)
            request.session["flash_notice"] = "题目已删除。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)

    @app.post("/admin/exam-templates/{template_id}/questions/{question_id}/move")
    async def admin_move_template_question(
        request: Request, template_id: str, question_id: str, direction: str = Form()
    ):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.move_template_question(actor["id"], template_id, question_id, direction)
            request.session["flash_notice"] = "题目顺序已调整。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse(f"/admin/exam-templates/{template_id}", status_code=303)

    @app.post("/admin/assignments/{assignment_id}/retry-grant")
    async def admin_retry_grant(request: Request, assignment_id: str):
        try:
            await require_csrf(request)
            actor = require_admin(request)
            service.retry_grant(assignment_id, actor["id"])
            if settings.dev_auto_process_grants:
                drain_dev_worker()
            request.session["flash_notice"] = "授权任务已重新入队。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=tasks", status_code=303)

    @app.post("/admin/worker/run")
    async def admin_run_worker(request: Request):
        try:
            await require_csrf(request)
            require_admin(request)
            processed = drain_dev_worker()
            request.session["flash_notice"] = f"开发 Worker 本次处理 {processed} 条任务。"
        except BusinessError as exc:
            request.session["flash_error"] = exc.message
        return RedirectResponse("/admin?view=tasks", status_code=303)

    @app.get("/exam", response_class=HTMLResponse)
    def exam_list(request: Request):
        try:
            user = require_login(request)
        except BusinessError as exc:
            return redirect_for_error(request, exc, login_path)
        error, notice = pop_flash(request)
        return render(
            request,
            "exam.html",
            {
                "mode": "list",
                "assignments": service.list_assignments(user_id=user["id"]),
                "error": error,
                "notice": notice,
            },
        )

    @app.post("/exam/{assignment_id}/start")
    async def exam_start(request: Request, assignment_id: str):
        try:
            await require_csrf(request)
            user = require_login(request)
            attempt_id = service.start_attempt(assignment_id, user["id"])
        except BusinessError as exc:
            return redirect_for_error(request, exc, "/exam")
        return RedirectResponse(f"/attempt/{attempt_id}", status_code=303)

    @app.get("/attempt/{attempt_id}", response_class=HTMLResponse)
    def attempt_page(request: Request, attempt_id: str):
        try:
            user = require_login(request)
            attempt, questions = service.load_attempt(attempt_id, user["id"])
        except BusinessError as exc:
            return redirect_for_error(request, exc, "/exam")
        if attempt["status"] == "SUBMITTED":
            return RedirectResponse(f"/exam/result/{attempt_id}", status_code=303)
        if attempt["status"] != "IN_PROGRESS":
            return RedirectResponse("/exam", status_code=303)
        return render(
            request,
            "exam.html",
            {"mode": "attempt", "attempt": attempt, "questions": questions, "error": "", "notice": ""},
        )

    @app.post("/attempt/{attempt_id}/submit")
    async def attempt_submit(request: Request, attempt_id: str):
        try:
            await require_csrf(request)
            user = require_login(request)
            form = await request.form()
            answers: dict[str, list[int]] = {}
            for key in form.keys():
                if not key.startswith("q-"):
                    continue
                question_id = key[2:]
                values = form.getlist(key)
                try:
                    answers[question_id] = [int(value) for value in values]
                except ValueError as exc:
                    raise BusinessError("答案格式无效") from exc
            result = service.submit_attempt(attempt_id, user["id"], answers)
            if result["passed"] and settings.dev_auto_process_grants:
                drain_dev_worker()
        except BusinessError as exc:
            return redirect_for_error(request, exc, f"/attempt/{attempt_id}")
        return RedirectResponse(f"/exam/result/{attempt_id}", status_code=303)

    @app.get("/exam/result/{attempt_id}", response_class=HTMLResponse)
    def exam_result(request: Request, attempt_id: str):
        try:
            user = require_login(request)
            attempt, _ = service.load_attempt(attempt_id, user["id"])
            assignment = service.get_assignment(attempt["assignment_id"])
        except BusinessError as exc:
            return redirect_for_error(request, exc, "/exam")
        if attempt["status"] != "SUBMITTED":
            return RedirectResponse(f"/attempt/{attempt_id}", status_code=303)
        return render(
            request,
            "exam.html",
            {
                "mode": "result",
                "attempt": attempt,
                "assignment": assignment,
                "error": "",
                "notice": "",
            },
        )

    @app.post("/api/hooks/dce/users")
    async def dce_user_hook(request: Request):
        supplied = request.headers.get("X-Quiz-Webhook-Secret", "")
        if not hmac.compare_digest(supplied, settings.webhook_secret):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
        dce_user_id = str(payload.get("id") or "").strip()
        username = str(payload.get("username") or payload.get("name") or "").strip()
        if not dce_user_id or not username:
            return JSONResponse({"ok": False, "error": "missing user identity"}, status_code=400)
        remote = dce.get_user(dce_user_id)
        if not remote:
            return JSONResponse({"ok": False, "error": "DCE user not found"}, status_code=404)
        local = service.sync_local_user(remote, is_admin=False)
        db.audit("webhook.user.discovered", "user", local["id"], "success")
        return {"ok": True, "user_id": local["id"]}

    @app.get("/health/live")
    def health_live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready():
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            # Readiness endpoints are commonly unauthenticated. Do not expose database
            # connection strings, filesystem paths or driver errors in the response.
            return JSONResponse({"status": "not-ready"}, status_code=503)
        return {"status": "ready", "dce_mode": settings.dce_mode}

    return app
