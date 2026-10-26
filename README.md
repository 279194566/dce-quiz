# DCE 5 平台准入考试

DCE Quiz 是一个独立运行的 FastAPI 应用，用于把培训考试与 DCE 5 权限开通流程连接起来。系统支持试卷管理、考试任务、权限包映射、DCE 用户同步和异步授权，并提供不依赖真实 DCE 环境的 Mock 演示模式。

> 当前版本定位为开发、演示和方案验收版本。请先阅读“生产边界”，不要直接部署到生产环境。

## 核心能力

- 创建、编辑、启停和删除用户，并在真实模式下同步到 DCE。
- 将权限包映射到已有 DCE 用户组，不在应用内重复编排平台 RBAC。
- 下发准入或培训考试；待考试任务支持编辑和删除。
- 考试通过后生成幂等授权作业，由 Worker 加入对应用户组并回查授权结果。
- 在线维护、发布、复制及导入导出试卷版本。
- 支持考试限时、次数、及格线、红线题、题序与选项乱序。
- 提供 Session、CSRF、管理员鉴权、审计记录及健康检查。
- 支持 Mock DCE 和真实 Ghippo API / DCE OIDC 两种模式。

## 工作流程

```text
管理员下发考试
      ↓
用户答题并通过
      ↓
PASSED_PENDING_GRANT
      ↓
Worker 加入 DCE 用户组并回查
      ↓
ACTIVE（权限已生效）
```

考试通过和权限生效是两个独立状态，避免远端授权失败时错误地把任务标记为已完成。

## 快速开始

### 环境要求

- Python 3.12
- Windows PowerShell、Linux 或 macOS
- Docker（可选，仅用于镜像或本地 PostgreSQL）

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

### Linux / macOS

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

浏览器访问 <http://127.0.0.1:8000>，在 Mock 登录页选择 `dev-admin`。首次启动会创建演示用户、权限包、题目和试卷，运行数据保存在 `data/`，不会提交到 Git。

若设置 `DEV_AUTO_PROCESS_GRANTS=0`，请另开终端运行授权 Worker：

```bash
python worker.py
```

只处理一条授权任务可运行 `python worker.py --once`。

## 容器运行

构建并以 Mock 模式启动：

```bash
docker build -t dce-quiz .
docker run --rm -p 8000:8000 \
  -e APP_ENV=development \
  -e DCE_MODE=mock \
  -e SECRET_KEY=local-development-session-key-change-before-production \
  -v dce-quiz-data:/app/data \
  dce-quiz
```

Windows PowerShell 可将上述命令写成一行。`docker-compose.yml` 目前只提供绑定到本机回环地址的 PostgreSQL 开发实例。

## 测试

```bash
python -m compileall -q app.py worker.py quiz_app tests
python -m unittest discover -s tests -v
```

GitHub Actions 会在每次 push 和 pull request 时执行依赖检查、编译检查和完整单元测试。

## 真实 DCE 联调

1. 复制 `.env.integration.example` 为 `.env`。
2. 将示例域名、工作空间和用户组替换为目标环境的真实值。
3. 通过运行环境或 Secret 管理系统注入 `DCE_ACCESS_TOKEN` 与 `OIDC_CLIENT_SECRET`。
4. 使用受信 CA 配置 `DCE_CA_BUNDLE`；只有隔离的临时联调环境才能跳过 TLS 校验。
5. 启动 Web 与独立 Worker，按[部署手册](./部署手册.md)完成回查。

真实模式不会展示应用自建的用户名输入页。身份来自 DCE OIDC `userinfo`，短期 Access Token 不写入 Cookie、业务数据库或应用日志。

## 主要配置

| 变量 | 用途 |
| --- | --- |
| `APP_ENV` | `development`、`test` 或 `production` |
| `DCE_MODE` | `mock` 或 `real` |
| `DATABASE_PATH` | SQLite 文件路径 |
| `DATABASE_URL` | 可选 PostgreSQL 连接串；设置后优先于 SQLite |
| `SECRET_KEY` | Session 签名密钥，生产环境至少 32 字符 |
| `WEBHOOK_SECRET` | Webhook 校验密钥，生产环境至少 24 字符 |
| `DEV_AUTO_PROCESS_GRANTS` | 是否由 Web 进程在开发模式内联处理授权 |
| `DCE_BASE_URL` | DCE 入口 HTTPS 地址 |
| `DCE_ACCESS_TOKEN` | 调用 Ghippo API 的服务凭据 |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | DCE OIDC 客户端凭据 |
| `DCE_TARGET_*` | 默认工作空间、角色和用户组映射 |

完整说明见[部署手册](./部署手册.md)。

## 项目结构

```text
quiz_app/
  config.py       配置加载与生产环境校验
  db.py           SQLite / PostgreSQL 数据访问与初始化
  dce.py          Mock 与真实 Ghippo API 适配器
  oidc.py         DCE OIDC 授权码登录
  services.py     考试、授权、用户和审计业务规则
  web.py          FastAPI 路由、Session、CSRF 与页面鉴权
templates/        Jinja2 页面
tests/            单元测试与 Web 流程测试
app.py            Web 入口
worker.py         授权作业消费者
```

## 生产边界

- 当前 OIDC 实现包含 `state` 校验和 `userinfo` 身份读取，但 PKCE、nonce 与 ID Token 签名验证尚未补齐。
- PostgreSQL 适配器已经实现，但仍缺少正式迁移工具、持续集成数据库测试和并发压测。
- 尚未提供 Helm Chart、NetworkPolicy、PDB、监控告警和备份恢复自动化。
- 示例题仅用于流程验证，不是正式题库。
- 生产部署前必须完成 DCE API 契约测试、最小权限服务账号配置和安全评审。

目标架构与上线检查项见[评审后实施方案](./评审后实施方案.md)，当前实现细节见[方案与实现说明](./方案与实现说明.md)。

## 安全

`.env`、数据库和日志已被 Git 忽略。提交前仍应检查暂存文件，并立即轮换任何曾出现在终端、日志或 Git 历史中的真实 Token 和密钥。漏洞报告方式见 [SECURITY.md](./SECURITY.md)。

## 贡献与许可证

贡献流程见 [CONTRIBUTING.md](./CONTRIBUTING.md)。当前仓库尚未指定开源许可证；公开发布前应由代码所有者确认许可证。在许可证明确前，默认保留全部权利。

