# Arena Hero Tactic

这是一个通过官方 `arena-hero` Python SDK 运行的同步 Arena Hero tactic。它只根据当前权威 `Turn` 生成当前 Tick 的完整 Agent 计划；不会复用旧 Turn 的 controller，也不会把记忆中的敌人或资源当作当前事实。

详细设计见 [`docs/arena-hero-tactic-design.md`](docs/arena-hero-tactic-design.md)，本次行为树/调度迁移的实际状态见 [`docs/arena-hero-implementation-progress.md`](docs/arena-hero-implementation-progress.md)。

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
