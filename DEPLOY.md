# 记账 Bot · Hostinger VPS Docker 部署记录

> 部署日期：2026-09-10 · 服务器：srv1808383（Hostinger VPS，已装 Docker）
> 结果：新容器 `ledgerbot_container` 成功运行，与旧容器 `newbot_pg1_container` 并行互不影响。

---

## 一、项目文件清单

| 文件 | 位置 | 说明 |
|---|---|---|
| `bot.py` | 本地 `Desktop\Tele Bot\` → VPS `/opt/ledgerbot/bot.py` | 记账 Bot 全部代码（Token 从环境变量读取，源码不含凭据） |
| `Dockerfile` | VPS `/opt/ledgerbot/Dockerfile` | python:3.12-slim 镜像构建定义 |
| `.env` | VPS `/opt/ledgerbot/.env`（权限 600） | 存放 BOT_TOKEN 与 BOT_DATA_DIR |
| `data/` | VPS `/opt/ledgerbot/data/` | 账单/操作员 JSON 数据（挂载进容器 /app/data） |

## 二、VPS 目录结构

```
/opt/ledgerbot/
├── bot.py        # Bot 源码
├── Dockerfile    # 镜像构建文件
├── .env          # 凭据（BOT_TOKEN=... / BOT_DATA_DIR=/app/data）
└── data/         # 持久化数据（账单、操作员名单）
```

## 三、完整部署步骤（成功版本）

### 1. 本地上传 bot.py

因为服务器主机名不能用于外网连接（`scp root@srv1808383` 会报 Could not resolve hostname），需用公网 IP：

```powershell
# 在 VPS 上查公网 IP：curl -s ifconfig.me
# IPv6 地址要加引号+方括号：
scp "C:\Users\senheng\Desktop\Tele Bot\bot.py" "root@[2a02:4780:5e:74bf::1]:/opt/ledgerbot/"
```

备选：Hostinger 面板 → 文件管理器 → 进入 `/opt/ledgerbot` 上传；或 VPS 里 `nano /opt/ledgerbot/bot.py` 整段粘贴（粘贴后必须用 python 校验语法完整性）。

### 2. VPS 创建 .env（凭据只放这里，不进源码/镜像）

```bash
cat > /opt/ledgerbot/.env <<'EOF'
BOT_TOKEN=在等号后填Telegram BotFather给的完整Token
BOT_DATA_DIR=/app/data
EOF
chmod 600 /opt/ledgerbot/.env
```

### 3. 创建 Dockerfile

```bash
cat > /opt/ledgerbot/Dockerfile <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY bot.py .
RUN pip install --no-cache-dir "python-telegram-bot[job-queue]"
CMD ["python", "bot.py"]
EOF
```

### 4. 校验 bot.py 完整性（防粘贴截断）

```bash
python3 -c "import ast; ast.parse(open('/opt/ledgerbot/bot.py', encoding='utf-8').read()); print('OK')"
grep -n "TOKEN = " /opt/ledgerbot/bot.py    # 必须是 TOKEN = os.environ["BOT_TOKEN"]
```

### 5. 构建镜像 + 启动容器（先 cd 进目录！）

```bash
cd /opt/ledgerbot
docker build -t ledgerbot_image .

docker run -d --name ledgerbot_container \
  --restart unless-stopped \
  --env-file /opt/ledgerbot/.env \
  -v /opt/ledgerbot/data:/app/data \
  ledgerbot_image
```

### 6. 验证

```bash
docker ps --filter name=ledgerbot          # STATUS 应为 Up
docker logs -f ledgerbot_container         # 应出现「记账机器人已启动，正在监听消息...」
```
（启动时那条 PTBUserWarning 关于 ConversationHandler per_message=False 是无害警告，可忽略。）

Telegram 实测：`/start` → `+100 lim` → `账单`（图1格式卡片）→ Admin 发 `/addoperator` 批准操作员。

## 四、本次踩过的坑（下次避免）

1. **`docker build` 报 `open Dockerfile: no such file or directory`** → 目录里没上传 Dockerfile，或没 `cd /opt/ledgerbot` 就执行 `docker build .`（`.` 是当前目录）。
2. **nano 粘贴时把 Token 填进源码**：错误写法 `os.environ["<Token字面量>"]`，正确写法 `os.environ["BOT_TOKEN"]`，Token 只放 `.env`。
3. **改了 bot.py 后必须重新 build**：代码是打进镜像的，光重启容器不会生效。流程：改文件 → `docker build` → `docker rm -f` → `docker run`。
4. **命令要在正确目录执行**：`cd /opt/ledgerbot` 后再 build；家目录 `~` 里建的文件不在构建上下文里。
5. **scp 用公网 IP**，机器名（srv1808383）解析不了；IPv6 地址要 `[方括号+引号]` 包起来。

## 五、日常运维速查

```bash
docker logs -f ledgerbot_container                          # 实时日志
docker restart ledgerbot_container                          # 重启
docker stop ledgerbot_container                             # 停止（数据不丢，在 data/ 目录）

# 更新代码
# 1) 上传新 bot.py 到 /opt/ledgerbot/
cd /opt/ledgerbot
docker build -t ledgerbot_image .
docker rm -f ledgerbot_container
docker run -d --name ledgerbot_container \
  --restart unless-stopped \
  --env-file /opt/ledgerbot/.env \
  -v /opt/ledgerbot/data:/app/data \
  ledgerbot_image

# 备份账单数据
cp -r /opt/ledgerbot/data /root/backup_$(date +%F)
```

## 六、安全注意事项

- **Token 绝不写入 bot.py / Dockerfile / 任何源码**，只存 `/opt/ledgerbot/.env`（权限 600）。
- 该 Token 曾在多处明文出现，上线后应到 @BotFather 发 `/revoke` 重生成，然后只改 `.env` 的 `BOT_TOKEN=` 行，`docker rm -f ledgerbot_container` 后重新 `docker run` 即可，代码零改动。
- 换 Token 或改 `.env` 后都需要 `docker rm -f` + `docker run`（env 是容器启动时注入的，restart 不会重新读 .env）。