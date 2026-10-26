# 贡献指南

感谢参与 DCE Quiz 的改进。提交变更前，请先确认变更范围清晰且不包含真实环境数据。

## 开发流程

1. 使用 Python 3.12 创建虚拟环境。
2. 安装依赖：`python -m pip install -r requirements.txt`。
3. 复制 `.env.example` 为 `.env`，保持 `DCE_MODE=mock` 完成本地开发。
4. 修改代码时同步补充或更新测试。
5. 提交前运行：

   ```bash
   python -m compileall -q app.py worker.py quiz_app tests
   python -m unittest discover -s tests -v
   ```

## Pull Request 要求

- 说明变更目的、行为差异和验证方式。
- 涉及数据库结构、任务状态或 DCE API 时，说明兼容性和回滚方式。
- 不得提交 `.env`、Token、密钥、内网地址、真实用户信息、数据库和日志。
- UI 变更建议附操作截图，但截图中必须隐藏账号和环境信息。

## 设计边界

权限包只映射已有 DCE 用户组；应用不应在考试流程中自行拼装底层 RBAC。考试通过和授权成功是两个独立状态，新增逻辑不得绕过授权作业的幂等与回查机制。

