# Arena Hero Tactic

这是一个通过官方 `arena-hero` Python SDK 运行的同步 Arena Hero tactic。它只根据当前权威 `Turn` 生成当前 Tick 的完整 Agent 计划；不会复用旧 Turn 的 controller，也不会把记忆中的敌人或资源当作当前事实。

详细设计见 [`docs/arena-hero-tactic-design.md`](docs/arena-hero-tactic-design.md)，本次行为树/调度迁移的实际状态见 [`docs/arena-hero-implementation-progress.md`](docs/arena-hero-implementation-progress.md)。

## Vue 3 控制台

Dashboard 前端位于 `frontend/`，使用 Vue 3、TypeScript 和 Vite；现有
`/api/dashboard`、回放、地图记忆和 Command API 契约保持不变。PixiJS 战术地图
继续使用仓库内的本地资源，不依赖 CDN。构建产物写入
`arena_tactic/web/static/app/`，由同一个 Python HTTP 服务托管：

```bash
cd frontend
pnpm install
pnpm typecheck
pnpm build
pnpm test:contract
```

开发调试可运行 `pnpm dev`，然后访问 `http://localhost:5173/static/app/`。
Vite 会把 `/api` 和旧版 `/static` 资源代理到本机 `127.0.0.1:8787`，但会保留
`/static/app/` 给 Vue 开发页面；后端服务仍需由操作者手动启动。

构建后再按原方式运行 Python tactic 或 Docker Compose。直接启动 Python 服务时，
如果 `arena_tactic/web/static/app/index.html` 不存在，启动流程会自动检查并执行
Vue 构建；生成目录已加入 Git 忽略，不应提交。未生成且无法构建时，真实服务不会
继续启动；代码中保留的内嵌 Dashboard 仅用于兼容性诊断和升级回退。新入口中的单位、
编组、战术地图、回放、事件日志、人工任务、核心迁移和策略设置均继续复用现有
后端接口与校验边界。

## Mac 本地运行

需要 Python 3.11 或更高版本。以下步骤只使用本地虚拟环境；Docker 不是 Mac 上运行或验证 tactic 的必需步骤。

```bash
cd /path/to/arena_hero
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .
```

运行离线检查：

```bash
source .venv/bin/activate
python3 -m compileall -q tactic.py arena_tactic tests
python3 -m pip check
python3 -m pytest -q
PYTHONPATH=.agents/skills/arena-hero python3 -m pytest -q .agents/skills/arena-hero/tests
git diff --check
```

仅在准备好连接真实 Arena Hero 对局时，才在前台运行：

```bash
source .venv/bin/activate
python3 tactic.py
```

将 API key 放在未跟踪的 `.env` 或环境变量中，使用项目现有变量名 `ARENA_HERO_API_KEY`。不要把 key、Cookie 或 Authorization header 写入代码、文档、日志或 Git；不要提交 `.env` 与 `.venv`。前台运行会连接真实服务并提交当前 Tick 的计划，离线测试不会。
