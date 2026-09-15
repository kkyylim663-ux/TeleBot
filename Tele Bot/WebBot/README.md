# WebBot · 网页版账单查看器（只读）

Telegram 记账 Bot 的网页查看版：**只看不记**，记账仍在 Telegram bot 里操作。
本网页读取 bot 的数据文件（与 VPS `/opt/ledgerbot/data/` 完全同格式），每笔记录网页刷新即是最新。

功能：
- 当前账单明细：入账（+/- 合并流水，与 Telegram 账单「已入账」同口径）/ 下发 / 群组（分组代号统计）三张表
- Date Range 日期范围选择：点第一下选开始日期，第二下自动作为结束日期；另有「全部时间」、操作人筛选、备注关键字筛选
- Deposit / Withdraw / Grand Total / 下发合计
- 账单历史：日切归档的每日汇总（明细 bot 日切时会清空，所以历史只有汇总，与 bot 行为一致）
- 导出 xlsx（入账/下发/群组/汇总 分 sheet）
- 多群组切换，支持 `?chat_id=` 参数直达

## 本地运行

```powershell
cd "C:\Users\senheng\Desktop\Tele Bot\WebBot"
pip install -r requirements.txt
python app.py
```

浏览器打开 http://127.0.0.1:8080

- `data\` 里现在是**测试样例数据**（格式与 VPS 上完全一致），可随意改着玩。
- 导出 xlsx 优先用本地 `static/vendor/xlsx.full.min.js`，没有则自动走 CDN（需要联网）。

## 绑定真实账单数据（不用 GitHub，不用部署）

双击 **`拉取真实数据.bat`** 即可：

1. 自动关闭旧的 WebBot 服务（避免端口占用、新旧数据混淆）
2. 用 `scp` 从 VPS 把 `/opt/ledgerbot/data/` 的最新账单拉到本地 `vps_data\` 文件夹（**会提示输入一次服务器密码**）
3. 自动启动服务并打开浏览器 → 显示的就是真实账单

以后**想看最新账单就再双击一次**（输一次密码）。密码输错或断网时，页面仍能打开，只是显示上一次拉取的旧数据，窗口里会有失败提示。

> 手动等价命令（PowerShell，脚本坏了时备用）：
>
> ```powershell
> scp "root@[2a02:4780:5e:74bf::1]:/opt/ledgerbot/data/*" "C:\Users\senheng\Desktop\Tele Bot\WebBot\vps_data\"
> $env:WEB_DATA_DIR = "C:\Users\senheng\Desktop\Tele Bot\WebBot\vps_data"
> python app.py
> ```

## 部署到 VPS（真正实时绑定，手机随时看）

**完整步骤看 [部署到VPS步骤.md](部署到VPS步骤.md)**，概要：上传 5 个文件到 `/opt/webbot` → `docker build` → `docker run` 时把 `/opt/ledgerbot/data` **只读挂载**进容器。bot 记一笔，网页刷新即是最新。

- 用 `-e WEB_KEY=你的口令` 设置**访问口令**（公网必设，否则任何人都能看账单）；网页首次打开会弹框要口令，浏览器会记住。
- 设置 bot 的 `LEDGER_DETAIL_BASE_URL` 后，Telegram 账单卡片会出现「📋 账单明细」按钮，点开直达该群网页（步骤文档第四节有完整命令）。

## 数据文件说明（与 bot.py 格式一致）

| 文件 | 内容 |
|---|---|
| `ledger_entries.json` | 每群组的入账/出账/下发明细（`type`: in/out/disburse，`voided`: true 为已撤销） |
| `ledger_settings.json` | 每群组设置（币种、账期等） |
| `global_bill_archive.json` | 日切后的每日汇总（账单历史） |
| `operators.json` | 操作员名单 |

注意：**历史账单只有每日汇总**——bot 在「结束账单/日切」时会把当期明细清空、只留汇总进 archive，这是 bot 本身的设计，不是网页缺功能。

## 安全提醒

- 设置环境变量 `WEB_KEY` 后，数据接口必须带口令才能读（网页会弹框要口令并记住）；**部署到公网时务必设置**。本地自己看可以不设。
- WebBot 不涉及 bot Token，与凭据完全无关。
