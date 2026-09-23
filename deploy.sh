#!/usr/bin/env bash
# 一键更新并重启「记账 Bot + 账单明细网页」（在 VPS 上执行）
#
#   首次：  cd /opt/ledgerbot && curl -fsSL -o deploy.sh \
#             https://raw.githubusercontent.com/kkyylim663-ux/TeleBot/main/deploy.sh && bash deploy.sh
#   以后：  bash /opt/ledgerbot/deploy.sh
#
# 做四件事：① 从 GitHub 拉最新代码 ② 校验语法 ③ 构建镜像 ④ 重建容器并健康检查
# 安全点：构建在删容器之前（构建失败线上不受影响）；只在构建成功后才重建；
#         .env / Dockerfile / data/ 都是目录里的本地文件，git 不会碰它们。
#
# 可调环境变量：APP_DIR / REPO / BRANCH / IMAGE / CONTAINER / PORT / DRY_RUN

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ledgerbot}"
REPO="${REPO:-https://github.com/kkyylim663-ux/TeleBot.git}"
BRANCH="${BRANCH:-main}"
IMAGE="${IMAGE:-ledgerbot_image}"
CONTAINER="${CONTAINER:-ledgerbot_container}"
PORT="${PORT:-8787}"
DRY_RUN="${DRY_RUN:-0}"

cd "$APP_DIR"
echo "▶ 应用目录：$APP_DIR"

# ── 1) 确保目录是 git 工作区（首次自动初始化，不会动 .env / Dockerfile / data）──
if [ ! -d .git ]; then
  echo "▶ 首次部署：初始化 git 工作区"
  git init -q
  git remote add origin "$REPO" 2>/dev/null || git remote set-url origin "$REPO"
else
  git remote set-url origin "$REPO"
fi

STAMP="$(date +%F-%H%M%S)"
for f in bot.py webconsole.py; do
  [ -f "$f" ] && cp "$f" "$f.bak.$STAMP"
done

echo "▶ 拉取 $BRANCH …"
git fetch -q origin "$BRANCH"
git checkout -q -f -B "$BRANCH" "origin/$BRANCH"
echo "  当前版本：$(git log --oneline -1)"

# ── 2) 校验代码完整（防下载/合并截断）──
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "pass" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -n "$PY" ]; then
  "$PY" - <<'PYCODE'
import ast
for f in ("bot.py", "webconsole.py"):
    ast.parse(open(f, encoding="utf-8").read())
print("  语法 OK（bot.py / webconsole.py）")
PYCODE
else
  echo "  ⚠ 没找到可用的 python3/python，跳过语法校验（建议装一个：apt install -y python3）"
fi

# 脚本自身若在本次拉取中更新了，用新版重跑一次（只重跑一次，避免死循环）
SELF="$APP_DIR/deploy.sh"
if [ -f "$SELF" ] && [ "${SELF_UPDATED:-0}" != "1" ] && ! cmp -s "$0" "$SELF"; then
  echo "▶ deploy.sh 有更新，用新版本继续"
  SELF_UPDATED=1 exec bash "$SELF" "$@"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "▶ DRY_RUN=1：跳过构建与容器重建"
  echo "  将要执行：docker build -t $IMAGE . && docker rm -f $CONTAINER && docker run -d … -p $PORT:$PORT -v $APP_DIR/data:/app/data $IMAGE"
  exit 0
fi

# ── 3) 先构建镜像（此时老容器仍在服务）──
echo "▶ 构建镜像 $IMAGE …"
docker build -t "$IMAGE" .

# ── 4) 构建成功后再重建容器 ──
echo "▶ 重建容器 $CONTAINER …"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --restart unless-stopped \
  --env-file "$APP_DIR/.env" \
  -p "${PORT}:${PORT}" \
  -v "$APP_DIR/data:/app/data" \
  "$IMAGE" >/dev/null

# ── 5) 健康检查 ──
sleep 3
docker ps --filter "name=$CONTAINER" --format '  容器：{{.Status}}  端口：{{.Ports}}'
if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null; then
  echo "  ✅ 健康检查通过：http://127.0.0.1:${PORT}/api/health"
else
  echo "  ❗健康检查失败，最近日志："
  docker logs --tail 30 "$CONTAINER" || true
  echo "  回滚：cp bot.py.bak.$STAMP bot.py && cp webconsole.py.bak.$STAMP webconsole.py，然后重跑本脚本"
  exit 1
fi
echo "▶ 完成。本地备份：bot.py.bak.$STAMP / webconsole.py.bak.$STAMP"
