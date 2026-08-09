# Arena Hero 长期运行服务

这个项目是本地策略 Agent，不是 Arena Hero 游戏服务器。Docker 容器负责保持
策略进程常驻，Python SDK 负责连接官方 WebSocket、接收 Turn、提交计划，并在
连接异常时自动重连。

## 启动（开发模式）
项目目录：`/root/project/arena_hero`

```bash
cd /root/project/arena_hero
docker compose up -d --build
```

当前 Compose 是开发优先的挂载模式：镜像只构建 Python 运行环境和
`arena-hero` 依赖，`tactic.py`、`arena_tactic/` 和 `runtime/` 通过挂载提供。
`.env` 不会挂载进容器，只通过 Compose 的 `env_file` 注入环境变量。
由于宿主机项目目录当前是 root 私有权限，开发容器暂时以 root 运行以读取挂载源码；
这只用于开发 Compose，不代表生产镜像的运行用户策略。

因此修改 `tactic.py`、`arena_tactic/` 或 Dashboard 页面后，不需要重新构建：

```bash
docker compose restart arena-hero
```

如果只是修改了 Compose 配置（端口、挂载、环境变量），使用：

```bash
docker compose up -d --no-build --force-recreate
```

只有修改 `pyproject.toml`、Dockerfile 或运行依赖时，才需要：

```bash
docker compose up -d --build
```

首次启动前确认 `.env` 存在，并且只包含本机使用的密钥：

```dotenv
ARENA_HERO_API_KEY=你的密钥
```

不要把 `.env` 提交到 Git，也不要把 API Key 放进 `docker-compose.yml`。

## 打开 Dashboard

启动后，在浏览器访问：

```text
http://127.0.0.1:8787/
```

根路径是中文 Web Dashboard，展示服务连接状态、运行时间、最近 Tick、提交
统计、重连次数、当前策略、资源容量、人口、最近错误，以及最近回合的动作和
事件。页面无 CDN 和外部前端依赖，每 3 秒从 `/api/dashboard` 自动刷新。

Dashboard 数据来自 `runtime/replay.jsonl` 的有界尾部窗口；文件不存在、末行因
异常退出而截断或暂时没有成功提交时，页面会显示空数据状态，不影响对战 worker。
API 只返回允许展示的聚合字段，不返回凭据、请求头、Cookie 或完整 UUID。

## JSON 状态接口

```bash
curl http://127.0.0.1:8787/livez
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/status
curl http://127.0.0.1:8787/api/dashboard
docker compose ps
docker compose logs -f --tail=100 arena-hero
```

- `/livez`：Python 进程和 HTTP 状态端口仍然存活。
- `/healthz`：最近已经连接并收到 Arena Hero 的 Turn；未连接时返回 HTTP 503。
- `/status`：查看最后 Tick、成功提交数、重连次数和最近错误。
- `/api/dashboard`：Dashboard 使用的脱敏聚合数据；即使回放缺失也返回有效 JSON。

## 停止与更新

```bash
docker compose stop
docker compose restart arena-hero
```

修改项目源代码后，使用 `docker compose restart arena-hero` 即可加载新代码，
不需要重新构建镜像。只有修改 `pyproject.toml`、Dockerfile 或运行依赖时，才
使用：

```bash
docker compose up -d --build
```

`restart: unless-stopped` 会在宿主机 Docker 重启后自动启动容器。项目代码来自
宿主机挂载，策略状态和脱敏回放也保存在宿主机的 `runtime/`，不会随容器删除而
丢失。

## 端口

默认在宿主机的所有网络接口发布 `8787`。可通过
`http://<宿主机地址>:8787/` 从局域网访问。若只允许本机访问，可将 Compose
端口映射改为 `127.0.0.1:8787:8787`。不建议直接暴露到公网，因为 Dashboard
没有登录认证；需要公网访问时，应在前方配置带 TLS 和身份认证的反向代理。

容器内仍只有一个 Python 进程：对战 worker 与标准库 HTTP Dashboard 同进程，
不会启动第二套 Web 服务，也不需要 Node、Flask 或 FastAPI。
