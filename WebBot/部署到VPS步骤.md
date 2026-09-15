# WebBot 部署到 VPS 步骤（把终端 Bot 和网页账单连接起来）

> 目标：WebBot 跑在 VPS 上，**只读共用** bot 的数据文件夹 `/opt/ledgerbot/data/`。
> 效果：Telegram bot 每记一笔，网页刷新即是最新；手机/电脑随时打开看，不再需要本地拉取脚本。
> 部署日期：2026-09-15 · 服务器：srv1808383（Hostinger VPS，已装 Docker，ledgerbot 容器不动）

---

## 〇、原理（一句话）

Telegram bot 往 `/opt/ledgerbot/data/*.json` 写账 → WebBot 容器把这个文件夹**只读挂载**进去 → 网页显示的就是实时账单。两个容器互不影响。

## 一、本机上传（PowerShell，2 条命令）

```powershell
cd "C:\Users\senheng\Desktop\Tele Bot\WebBot"

ssh "root@[2a02:4780:5e:74bf::1]" "mkdir -p /opt/webbot"

scp -r app.py Dockerfile requirements.txt templates static "root@[2a02:4780:5e:74bf::1]:/opt/webbot/"
```

- 会提示输入服务器密码（可能要输两次，ssh 一次 scp 一次）。
- 只上传这 5 样（app.py、Dockerfile、requirements.txt、templates、static）。
  **data/ 样例数据、拉取真实数据.bat、各种 .md 都不上传**，假数据不能上服务器。

## 二、VPS 上构建并启动（3 条命令）

在 Hostinger 网页终端（或 ssh 登录后）执行：

```bash
cd /opt/webbot
docker build -t webbot_image .

docker run -d --name webbot_container \
  --restart unless-stopped \
  -p 8080:8080 \
  -e WEB_KEY=把这里换成你自己的口令 \
  -v /opt/ledgerbot/data:/app/data:ro \
  webbot_image
```

- `WEB_KEY`：访问口令。**一定要设**，否则任何知道你 IP 的人都能看账单。建议字母+数字 8 位以上。
- `:ro` = 只读挂载，网页端永远改不了 bot 的数据。
- 不动 `ledgerbot_container`，它照常运行。

## 三、验证

```bash
docker ps --filter name=webbot        # STATUS 应为 Up
docker logs -f webbot_container       # 应出现 Running on http://0.0.0.0:8080
curl -s ifconfig.me                   # 查 VPS 公网 IP（也应显示 2a02:4780:...）
```

浏览器（手机/电脑都行）打开：

```
http://[2a02:4780:5e:74bf::1]:8080
```

会弹出访问口令框 → 输入 docker run 里设的 WEB_KEY → 显示真实账单（口令会被浏览器记住）。

**打不开就检查 Hostinger 防火墙**：面板 → Firewall → 放行 TCP 8080 端口。

## 四、（推荐）终极连接：Telegram 里点按钮直达网页

bot.py 本来就支持：设置 `LEDGER_DETAIL_BASE_URL` 后，每次的账单卡片下方会出现 **「📋 账单明细」** 按钮，点开直达该群组的网页账单（自动带上群组参数）。

```bash
# 1) 在 bot 的 .env 末尾追加一行（IP 同上）
echo 'LEDGER_DETAIL_BASE_URL=http://[2a02:4780:5e:74bf::1]:8080/' >> /opt/ledgerbot/.env

# 2) env 是容器启动时注入的，必须删除重建（光 restart 不会重新读 .env）
docker rm -f ledgerbot_container
cd /opt/ledgerbot
docker run -d --name ledgerbot_container \
  --restart unless-stopped \
  --env-file /opt/ledgerbot/.env \
  -v /opt/ledgerbot/data:/app/data \
  ledgerbot_image

# 3) 验证 bot 活着
docker logs -f ledgerbot_container    # 出现「记账机器人已启动」即正常
```

之后在 Telegram 群里发「账单」，卡片下面就有按钮。

## 五、本次踩坑提醒（下次避免）

1. **scp 首次/再次上传路径行为不同**：目标 `/opt/webbot` 不存在时 `scp -r ... :/opt/webbot` 会创建它；已存在时会变成 `/opt/webbot/static` 多套一层。再次上传前先 `ssh ... "ls /opt/webbot"` 确认结构对不对，错了就 `rm -rf /opt/webbot` 重传。
2. **忘了设 WEB_KEY**：`docker rm -f webbot_container` 后重新 `docker run`（带上 `-e WEB_KEY=...`）。
3. **改了 .env 必须 rm -f + run**，restart 不会重新读 env（跟 ledgerbot 同一个坑）。
4. **build 前 必须 `cd /opt/webbot`**，否则报 `open Dockerfile: no such file or directory`（跟 ledgerbot 同一个坑）。
5. **手机地址栏用 `http://` 不是 `https://`**——没配证书，https 会直接打不开。
6. 本地那个 `拉取真实数据.bat` 部署后就不需要了，留着当 VPS 临时打不开时的备用查看方式。

## 六、日常运维速查

```bash
docker logs -f webbot_container             # 实时日志
docker restart webbot_container             # 重启
docker stop webbot_container                # 停止（不动 bot 和数据）

# 更新网页代码（本机重新上传改动的文件后）：
cd /opt/webbot
docker build -t webbot_image .
docker rm -f webbot_container
docker run -d --name webbot_container \
  --restart unless-stopped \
  -p 8080:8080 \
  -e WEB_KEY=你的口令 \
  -v /opt/ledgerbot/data:/app/data:ro \
  webbot_image
```

## 七、安全注意事项

- `WEB_KEY` 是网页唯一的门槛，**别用弱口令**；觉得可能泄露就在 VPS 上换一个重跑 docker run。
- 连接是 http 明文（没配 https 证书），账单数据在公网明文传输。局域网/个人使用可接受；以后想上 https 再说（需要域名 + 反向代理）。
- 数据挂载是只读的，网页端被入侵也**改不了、删不了** bot 的账单。
