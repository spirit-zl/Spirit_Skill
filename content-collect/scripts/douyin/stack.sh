#!/usr/bin/env bash
# dtk 栈管理：把「数据库 + 缓存 + API + worker」当成一个整体来开关。
#
#   ./stack.sh up        启动全栈（容器 -> 迁移 -> API -> worker）
#   ./stack.sh down      停掉 API、worker 和容器
#   ./stack.sh status     看状态
#   ./stack.sh logs [api|worker]
#   ./stack.sh image      （仅首次/换机器）构建本机版 timescaledb 镜像
#   ./stack.sh apilist    把实例的 API 清单导出到 $DTK_HOME/api-list.md
#
# 依赖：docker（含 compose v2）、uv、python3。
set -euo pipefail

DTK_HOME="${DTK_HOME:-$HOME/.workbuddy/data/douyin-data}"

# 仓库位置按「环境变量 → 凭据文件 → 常见路径」找，不写死。
# 换机器时目录名不一样是常态，硬编码一个绝对路径等于把脚本锁死在一台机器上。
if [ -z "${DTK_REPO:-}" ]; then
  if [ -f "$DTK_HOME/credentials.env" ]; then
    # 注意 pipefail：grep 没命中会返回 1，直接接管道会把脚本带走（踩过）。
    DTK_REPO="$(grep -E '^DTK_REPO=' "$DTK_HOME/credentials.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  fi
fi
if [ -z "${DTK_REPO:-}" ]; then
  for cand in "$DTK_HOME/repo" "$HOME/douyin_api" "$HOME/src/douyin_api" \
              "$HOME/WorkBuddy/spirit_WorkBuddy/douyin_api"; do
    if [ -d "$cand/src/dtk" ]; then DTK_REPO="$cand"; break; fi
  done
fi
DTK_REPO="${DTK_REPO:-$HOME/douyin_api}"
# 用自己的 compose，而不是仓库自带的 docker/compose.test.yml。
# 后者是测试 fixture：把 Postgres 数据放在 tmpfs 上，容器一停数据就全没了 ——
# 身份池、归档、API Key、管理员账号都会消失。真实使用要的是命名卷。
DTK_COMPOSE="$DTK_HOME/compose.yml"
COMPOSE=(docker compose -p dtk-data -f "$DTK_COMPOSE")
PG_IMAGE="timescale/timescaledb-ha:pg17"
PID_API="$DTK_HOME/run/api.pid"
PID_WORKER="$DTK_HOME/run/worker.pid"
LOG_API="$DTK_HOME/run/api.log"
LOG_WORKER="$DTK_HOME/run/worker.log"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

need_repo() {
  [ -d "$DTK_REPO/src/dtk" ] || die "在 $DTK_REPO 找不到 dtk 源码；用 DTK_REPO=... 指定仓库路径"
}

# ---------------------------------------------------------------- 容器
#: 运行时依赖的 compose。写成函数里的 heredoc，而不是仓库里的一份文件：
#: 技能要能自己长出来，仓库被重新克隆或测试 fixture 被改都不会影响它。
write_compose() {
  mkdir -p "$DTK_HOME"
  cat >"$DTK_COMPOSE" <<'YAML'
# dtk 运行时依赖：Postgres 17 + TimescaleDB 与 Redis。
#
# 与仓库自带的 docker/compose.test.yml 的唯一区别，也是这个文件存在的理由：
# 那边的 Postgres 把数据放在 tmpfs 上，容器一停数据全部消失（身份池、归档、
# API Key、管理员账号一起没）。这里换成命名卷，数据留在磁盘上。
name: dtk-data

services:
  postgres:
    image: timescale/timescaledb-ha:pg17
    environment:
      POSTGRES_DB: dtk_test
      POSTGRES_USER: dtk
      POSTGRES_PASSWORD: dtk_test_password
      POSTGRES_HOST_AUTH_METHOD: scram-sha-256
    ports:
      - "127.0.0.1:55432:5432"
    volumes:
      - dtk-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dtk -d dtk_test"]
      interval: 2s
      timeout: 3s
      retries: 30
    restart: unless-stopped
    labels:
      dtk.role: runtime-db

  redis:
    image: redis:8-alpine
    # 队列与缓存。开 AOF 是为了让重启后任务状态还在，不再凭空消失。
    command: redis-server --appendonly yes --save ""
    ports:
      - "127.0.0.1:56379:6379"
    volumes:
      - dtk-redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 30
    restart: unless-stopped
    labels:
      dtk.role: runtime-cache

volumes:
  dtk-pgdata:
  dtk-redisdata:
YAML
}

ensure_compose() {
  if [ ! -f "$DTK_COMPOSE" ]; then
    say "生成持久化 compose：$DTK_COMPOSE"
    write_compose
  fi
}

containers_up() {
  need_repo
  ensure_compose
  say "启动 Postgres 17 + TimescaleDB 与 Redis（compose 项目 dtk-data，数据落命名卷）"
  "${COMPOSE[@]}" up -d --wait
}

containers_down() {
  need_repo
  ensure_compose
  say "停止容器（命名卷保留，数据不丢；要彻底清空用 docker compose -p dtk-data down -v）"
  "${COMPOSE[@]}" down
}

# ---------------------------------------------------------------- 镜像
build_image() {
  need_repo
  if docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
    say "镜像 $PG_IMAGE 已存在，跳过构建"
    return 0
  fi
  local ctx="$DTK_REPO/.build-pg-timescale"
  [ -f "$ctx/Dockerfile" ] || die "缺少构建上下文 ${ctx}（Dockerfile + 两个 timescaledb deb）"
  say "构建本机版 ${PG_IMAGE}（官方 timescale 镜像在国内镜像站不可达，改用 postgres:17 + 官方 deb）"
  docker build --platform linux/arm64 -t "$PG_IMAGE" "$ctx"
}

# ---------------------------------------------------------------- 进程
api_running() { [ -f "$PID_API" ] && kill -0 "$(cat "$PID_API")" 2>/dev/null; }
worker_running() { [ -f "$PID_WORKER" ] && kill -0 "$(cat "$PID_WORKER")" 2>/dev/null; }

start_api() {
  need_repo
  if api_running; then say "API 已在运行（pid $(cat "$PID_API")）"; return 0; fi
  if pgrep -f "dtk serve" >/dev/null 2>&1; then
    say "API 已在运行（外部启动，pid 不在本脚本管辖）；不重复启动"
    return 0
  fi
  say "启动 API（日志：${LOG_API}）"
  ( cd "$DTK_REPO" && nohup uv run dtk serve >>"$LOG_API" 2>&1 & echo $! >"$PID_API" )
  for _ in $(seq 1 40); do
    if curl -fsS -m 2 --noproxy '127.0.0.1,localhost' http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
      say "API 就绪：http://127.0.0.1:8000"
      return 0
    fi
    sleep 1
  done
  die "API 未在 40 秒内就绪，检查 $LOG_API"
}

start_worker() {
  need_repo
  if worker_running; then say "worker 已在运行（pid $(cat "$PID_WORKER")）"; return 0; fi
  if pgrep -f "dtk worker" >/dev/null 2>&1; then
    say "worker 已在运行（外部启动，pid 不在本脚本管辖）；不重复启动"
    return 0
  fi
  say "启动 worker（日志：${LOG_WORKER}）"
  ( cd "$DTK_REPO" && nohup uv run dtk worker >>"$LOG_WORKER" 2>&1 & echo $! >"$PID_WORKER" )
  sleep 2
}

stop_proc() {
  local name="$1" pidfile="$2"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    say "停止 ${name}（pid $(cat "$pidfile")）"
    kill "$(cat "$pidfile")" 2>/dev/null || true
    for _ in $(seq 1 15); do kill -0 "$(cat "$pidfile")" 2>/dev/null || break; sleep 1; done
  fi
  rm -f "$pidfile"
}

migrate() {
  need_repo
  say "应用数据库迁移"
  ( cd "$DTK_REPO" && uv run dtk migrate )
}

# ---------------------------------------------------------------- 依赖探测
#: 从 .env 里读 Postgres / Redis 的地址，做一个 TCP 连通性探测。
#:
#: 为什么要有这个：Docker 在这个项目里**只是 Postgres + Redis 的载体**，
#: 换成机器自带的服务（Homebrew 的 postgresql/redis、公司现成的库、远程实例）
#: 一样能跑。`--no-docker` 就是这条路：不碰容器，只要求 .env 指向的库可达。
probe_deps() {
  python3 - "$DTK_REPO/.env" <<'PY'
import re, socket, sys
from pathlib import Path

env = Path(sys.argv[1])
if not env.exists():
    print("  找不到 .env，无法读连接串"); sys.exit(1)
text = env.read_text(encoding="utf-8", errors="replace")

def value(key):
    m = re.search(rf"^{key}=(.+)$", text, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""

def hostport(url, default_port):
    if not url:
        return None
    m = re.search(r"@([^/:]+):(\d+)", url) or re.search(r"//([^/:]+):(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"@([^/:]+)(?:/|$)", url) or re.search(r"//([^/:]+)(?:/|$)", url)
    return (m.group(1), default_port) if m else None

bad = 0
for key, port, name in (("DTK_DATABASE_URL", 5432, "Postgres"),
                        ("DTK_REDIS_URL", 6379, "Redis")):
    hp = hostport(value(key), port)
    if not hp:
        print(f"  ✗ {name:<9} .env 里没有 {key}"); bad += 1; continue
    host, p = hp
    try:
        with socket.create_connection((host, p), timeout=3):
            print(f"  ✓ {name:<9} {host}:{p} 可达（不依赖 Docker）")
    except OSError as exc:
        print(f"  ✗ {name:<9} {host}:{p} 连不上（{exc.strerror or exc}）")
        bad += 1
sys.exit(1 if bad else 0)
PY
}

# ---------------------------------------------------------------- 命令
cmd_up() {
  mkdir -p "$DTK_HOME/run"
  need_repo
  if [ "${NO_DOCKER:-0}" = "1" ]; then
    say "跳过容器：只启动 API 与 worker（数据库/缓存用 .env 里配的）"
    probe_deps || die "依赖不可达。装好 Postgres/Redis，或把 .env 里的 DTK_DATABASE_URL / DTK_REDIS_URL 指过去"
  else
    docker info >/dev/null 2>&1 || die "Docker 守护进程没在跑，先启动 Docker Desktop（或加 --no-docker 用本机数据库）"
    build_image
    containers_up
  fi
  migrate
  start_api
  start_worker
  say "全栈就绪。API：http://127.0.0.1:8000　控制台：/setup（首次）或 /"
}

cmd_down() {
  stop_proc api "$PID_API"
  stop_proc worker "$PID_WORKER"
  if [ "${NO_DOCKER:-0}" = "1" ]; then
    say "已停止 API 与 worker（容器没动，因为这次是 --no-docker 起的）"
  else
    containers_down
    say "已停止"
  fi
}

cmd_status() {
  say "容器"
  if [ -f "$DTK_COMPOSE" ]; then "${COMPOSE[@]}" ps || true; else echo "  （尚未生成 ${DTK_COMPOSE}，先跑 stack.sh up）"; fi
  say "进程"
  if api_running; then echo "  API    : 运行中 (pid $(cat "$PID_API"))"
  elif pgrep -f "dtk serve" >/dev/null 2>&1; then echo "  API    : 运行中（外部启动）"
  else echo "  API    : 未运行"; fi
  if worker_running; then echo "  worker : 运行中 (pid $(cat "$PID_WORKER"))"
  elif pgrep -f "dtk worker" >/dev/null 2>&1; then echo "  worker : 运行中（外部启动）"
  else echo "  worker : 未运行"; fi
  say "健康"
  curl -fsS -m 3 --noproxy '127.0.0.1,localhost' http://127.0.0.1:8000/readyz && echo || echo "  API 不可达"
}

cmd_logs() {
  case "${1:-api}" in
    api) tail -n 80 "$LOG_API" ;;
    worker) tail -n 80 "$LOG_WORKER" ;;
    *) die "用法：stack.sh logs [api|worker]" ;;
  esac
}

cmd_apilist() {
  mkdir -p "$DTK_HOME"
  python3 "$(dirname "$0")/dtk.py" apilist --out "$DTK_HOME/openapi.json"
}

# `up --no-docker` / `down --no-docker`：不碰容器，只用 .env 里配的数据库与缓存。
# 也认环境变量 DTK_NO_DOCKER=1，方便脚本里一次性带上。
shift_args=()
for arg in "$@"; do
  case "$arg" in
    --no-docker) NO_DOCKER=1 ;;
    *) [ -n "$arg" ] && shift_args+=("$arg") ;;
  esac
done
set -- "${shift_args[@]:-}"

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  migrate) migrate ;;
  image) build_image ;;
  deps) probe_deps ;;
  apilist) cmd_apilist ;;
  *) cat <<EOF
用法：$0 <命令> [--no-docker]

  up [--no-docker]  启动全栈（默认：容器 -> 迁移 -> API -> worker）
                    --no-docker：不碰容器，只要求 .env 里配的 Postgres/Redis 可达，
                    然后跑迁移并起 API + worker —— 换机器不想装 Docker 时用这条
  down [--no-docker] 停止（--no-docker 时只停进程，不动容器）
  status            查看容器、进程与健康状态
  deps              只探测 Postgres / Redis 是否可达（不启动任何东西）
  logs              tail API 或 worker 日志：logs [api|worker]
  migrate           只跑数据库迁移
  image             构建本机版 timescaledb 镜像（首次或换机时）
  apilist           导出 API 清单到 \$DTK_HOME/api-list.md

环境变量：DTK_REPO（源码路径，默认 ${DTK_REPO}）　DTK_HOME（默认 ${DTK_HOME}）
          DTK_NO_DOCKER=1 等价于加 --no-docker
EOF
     exit 1 ;;
esac
