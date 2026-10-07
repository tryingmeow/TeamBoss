# 本地开发与离线演示

部署运行约束见[部署与运维](deployment.md)，环境变量以 [`.env.example`](../.env.example) 为准。以下启动命令从仓库根目录开始，前后端分别使用终端。

## 启动前后端

Python / Node.js 版本参考项目的 [后端镜像](../Dockerfile.backend)、[前端镜像](../Dockerfile.frontend)和 [CI 配置](../.github/workflows/ci.yml)。

```bash
# 后端（Python 3.12）
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env        # 填好 AUTO_TEAM_ADMIN_PASSWORD / AUTO_TEAM_API_KEY
python run_server.py              # 读取仓库根目录 .env，默认监听 127.0.0.1:18087

# 前端（另开终端）
cd frontend
npm ci
npm run dev                       # http://localhost:5173
```

`npm run dev` 起的 Vite 开发服务器会把 `/api` 请求代理到后端：目标地址优先取 `VITE_API_BASE_URL`，否则用 `http://AUTO_TEAM_BACKEND_HOST:AUTO_TEAM_BACKEND_PORT`（默认 `127.0.0.1:18087`），这些变量都从仓库根目录的 `.env` 读取。前端端口可用 `AUTO_TEAM_FRONTEND_PORT` 改。开发服务器默认只绑定 `127.0.0.1`；确需从其他机器访问时设置 `AUTO_TEAM_FRONTEND_HOST`（例如 `0.0.0.0`），注意这会把开发服务器暴露出去。


## 离线演示

无需后端、Owner 或 ChatGPT 账号，也无需 `.env`：

```bash
cd frontend
npm ci
npm run demo                      # 即 vite --mode demo，打开 http://localhost:5173/admin
```

演示模式用浏览器内存中的虚构数据响应所有 `/api/*` 请求，即使设置了 `VITE_API_BASE_URL` 也不会访问真实后端或 ChatGPT。任意密码均可登录；改动刷新即重置。邮箱、卡号和金额都是虚构的，可用于体验界面和截图。演示代码仅在 `demo` 模式加载，不进入 `npm run build` 的正式构建。


## 验证改动

在独立开发副本中运行测试，**将 `AUTO_TEAM_DATA_DIR` 显式指向临时目录，不使用生产数据库或会话**。测试包在该变量未设置时也会自动使用临时目录。以下命令从仓库根目录开始：

```bash
cd backend
python -m pip install -r requirements-test.txt
AUTO_TEAM_DATA_DIR=$(mktemp -d) python -m unittest discover -s tests
```

前端验证另开终端，从仓库根目录执行：

```bash
cd frontend
npm run build
```

提交 PR 前确认后端测试和前端构建均通过，具体 CI 检查见 [工作流](../.github/workflows/ci.yml)。漏洞请按 [SECURITY.md](../SECURITY.md) 私下报告，不要开公开 issue。

## 接口文档

兑换码管理、自助接口和认证说明见 [ADMIN_API.md](../ADMIN_API.md)。
