# Tele Bot 项目导航地图（Map.md）

> 配合 `Project.md` 使用。本文档描述源码 `Bot 计算机.txt` 的内部结构，用于快速定位代码、排错和二次开发。
> 行号基于当前文件版本（共 3357 行），小幅改动不会使分区失效，大改动请同步更新此表。

---

## 一、总体架构

**单文件 Python 应用**，无数据库、无模块拆分：

```
Bot 计算机.txt
 ├── 常量与 JSON 读写工具（load_json / save_json + 各 *_FILE 路径）
 ├── 按功能域划分的处理函数（命令 / 回调 / 正则消息）
 └── 底部集中注册 Handler → app.run_polling()
```

数据流（文本消息）：

```
消息进入
 ├─ group=1: track_known_group()      # 全局：把 Bot 所在群记入 known_groups.json
 ├─ NEW_CHAT_MEMBERS: welcome_new_member()
 └─ TEXT & ~COMMAND: handle_message()  # 主分发链（见下方链路）
       ├─ 主菜单按钮（群发广播/使用说明/联系客服…）
       ├─ try_handle_ledger_settings()  # 记账设置类 + 账单/结束/导出/清空
       ├─ try_handle_ledger_revoke()    # 撤销 / 撤销恢复
       ├─ try_handle_ledger_entry()     # +金额 / -金额 记账
       ├─ try_handle_ledger_disburse()  # 下发
       ├─ try_handle_welcome_settings() # 欢迎语设置
       └─ 计算器（eval 兜底）
```

⚠️ 注意：当前代码在「联系客服」分支 `return`，上述分发链后半段（个人中心之后到计算器）实际不可达 —— 详见 Project.md 已知问题 #2。修复时应让菜单分支各自 return、记账链保持在可达路径上。

按钮点击（CallbackQuery）按 `callback_data` 前缀路由，见第三节路由表。

---

## 二、源码分区导航表

| 行号区间 | 内容 | 关键入口 |
|---|---|---|
| 1–31 | import、Token、常量（PAGE_SIZE、ADMIN_USERNAMES、文件路径）、ConversationHandler 状态码 | `TOKEN`(17)、`ADMIN_USERNAMES`(21) |
| 46–81 | JSON 读写工具、授权函数 | `load_json`、`is_admin`、`is_authorized` |
| 83–165 | 租户数据工具：周期标签、加月/加年、下期到期日计算、行解析 | `compute_next_due`、`parse_duedate_line` |
| 166–308 | `/duedate` 租户批量登记向导（选群→录行→选周期） | `duedate_start`、`DD_GROUP/DD_ITEMS/DD_CYCLE_DAYS` |
| 314–330 | 通用：`cancel_conversation`、`_reply`、`total_pages` | |
| 336–480 | 授权用户管理：分页列表、添加会话 | `listusers_cmd`、`adduser_conv` |
| 487–544 | 群发目标/文案/计划/已知群的存取 | `load_targets`、`track_known_group` |
| 546–781 | 目标列表页、分类管理（含新建分类会话） | `build_targets_page`、`build_category_list`、`_category_matrix`、`newcat_conv` |
| 784–1067 | `/addtarget` 登记向导（选群→多选分组→备注） | `addtarget_conv` |
| 1074–1080 | `/whereami` | |
| 1083–1232 | 文案库管理 | `listdrafts_cmd`、`adddraft_conv` |
| 1235–1572 | `/broadcast` 四步群发向导 + 定时 Schedule 管理 + `do_broadcast` 实际发送 | `broadcast_conv`、`do_broadcast` |
| 1574–1933 | 记账系统（上）：设置存取、账期/结转/快照工具、记账正则常量 | `RE_LEDGER_ENTRY`(1928) 等所有 `RE_*` |
| 1797–1913 | 下发记账、撤销/恢复处理 | `try_handle_ledger_disburse`（注意 1851 起是死代码）、`try_handle_ledger_revoke` |
| 1936–2058 | 清空账单 / 结束账单 / 撤销结算 | `close_ledger_day`、`undo_close_ledger_day` |
| 2061–2254 | 记账设置类指令处理（含导出 Excel） | `try_handle_ledger_settings` |
| 2256–2318 | +/− 金额记账落账 | `try_handle_ledger_entry` |
| 2320–2486 | 账单渲染（紧凑/展开视图）、费率上标、数字格式化 | `build_ledger_summary`、`build_ledger_expand_page` |
| 2488–2564 | 全局欢迎语设置与新成员处理 | `try_handle_welcome_settings`、`welcome_new_member` |
| 2567–2579 | 全角/中文符号归一化 | `normalize`、`CHAR_MAP` |
| 2582–2600 | 主菜单键盘与 `/start` | `MAIN_MENU`、`start` |
| 2603–2744 | **主消息分发** `handle_message`（菜单分支 + 记账链 + 计算器） | ⚠️ 2683 行 return 造成后续不可达 |
| 2747–2782 | `post_init`：命令表注册、自动结算恢复、每日 12:00/15:00 租户扫描排程 | |
| 2784–2814 | Handler 注册区（前半）：start、四个会话、用户/目标回调 | |
| 2815–3142 | 租户项目 GUI：群列表、勾选矩阵、退租、编辑会话 | `build_duedate_group_list`、`_duedate_matrix`、`ddm_edit_conv` |
| 3144–3303 | 到期扫描与提醒/升级、续租退租回调、手动测试命令 | `duedate_scan_1200`、`duedate_scan_1500`、`dda_renew_cb`、`dda_cancel_cb` |
| 3306–3355 | 「租户列表」关键词、文案库/记账/杂项回调注册、最终兜底 MessageHandler、启动 | |

---

## 三、回调前缀路由表

| 前缀 | 功能 | 处理函数（注册行） |
|---|---|---|
| `lu:*` | 授权用户列表（翻页/移除/关闭） | `listusers_*_cb`（2793–2798） |
| `lt:page/rm/refresh/close/noop` | 目标列表页 | `listtargets_*_cb`（2802–2808） |
| `lt:cat*` / `lt:tg:` | 分类矩阵编辑 | `category_*_cb`（2809–2814） |
| `at:*` | 添加目标向导内部按钮（选群/手输/勾分组/跳过备注） | `addtarget_*_cb`（在 `addtarget_conv` 内） |
| `ld:*` | 文案库列表 | `listdrafts_*_cb`（3337–3342） |
| `bc_*` / `sched_*` | 群发向导各步 | `bc_*_cb`（在 `broadcast_conv` 内） |
| `clearledger:*` | 清空账单确认 | `clearledger_confirm_cb`（3349） |
| `ledger:expand/collapse/noop` | 账单展开/收起 | `ledger_*_cb`（3345–3347） |
| `dd:*` | 租户登记向导 | `duedate_*_cb`（在 `duedate_conv` 内） |
| `ddm:*` | 租户项目管理（群列表/翻页/勾选/退租/编辑） | `ddm_*_cb`（3035–3042、3139–3142） |
| `dda:renew / dda:cancel` | 提醒卡片上的续租/退租按钮 | `dda_renew_cb`、`dda_cancel_cb`（3300–3301） |

---

## 四、指令速查表

**斜杠命令**（`post_init` 命令表，2748–2762）：
`/start` `/broadcast` `/adduser` `/removeuser`(=listusers 别名) `/listusers` `/addtarget` `/removetarget`(别名) `/listtargets` `/whereami` `/adddraft` `/listdrafts` `/duedate` `/listduedate` `/ledger` `/cancel` `/skip`
另有未入命令表的调试命令：`/ddtest1200`、`/ddtest1500`。

**中文关键词指令**（全部经 `handle_message` 正则匹配，正则定义在 1915–1934、2497–2500）：

- 记账：`+金额`、`-金额`、`下发 金额 [手续X] [备注]`、`撤销`、`撤销恢复`、`账单`、`结束账单`、`撤销结束账单`、`清空账单`、`撤销清空账单`、`导出账单`、`设定日期 YYYY-MM-DD`
- 设置：`设置入账费率 X`、`设置出账费率 X`、`设置[币种]汇率 X`、`设置币种 XXX`、`设置时区 X`、`设置手续费 X`、`设置自动结算时间 HH:MM`、`取消自动结算`、`记账设置`
- 欢迎语：`设置欢迎语 内容`、`开启欢迎`、`关闭欢迎`、`查看欢迎语`
- 其他：`租户列表`、菜单按钮文字（使用说明/联系客服/…）、纯算式

---

## 五、数据文件 ↔ 代码对照

| JSON 文件 | 载入/保存函数 | 主要写入场景 |
|---|---|---|
| authorized_users.json | `load_auth/save_auth` | adduser / 移除确认 |
| known_groups.json | `load_known_groups/save_known_groups` | 任何群消息（`track_known_group`） |
| broadcast_targets.json | `load_targets/save_targets` | addtarget / 分类保存 / 发送后迁移 ID |
| broadcast_drafts.json | `load_drafts/save_drafts` | adddraft / 删除文案 |
| broadcast_schedules.json | `load_schedules/save_schedules` | 群发向导第 1 步 |
| tenants.json | `load_tenants/save_tenants` | duedate 登记 / 续退租 / 编辑 / 扫描推进 |
| ledger_settings.json | `load_ledger_settings/save_ledger_settings`（+`set_group_ledger_setting`） | 各设置指令、账期推进 |
| ledger_entries.json | `load_ledger_entries/save_ledger_entries`、`append_ledger_entry` | 记账/下发/撤销/清空/结算 |
| ledger_carryover.json | `load/save_ledger_carryover`、`add_group_carryover` | 结束账单 |
| ledger_close_snapshot.json | `load/save_close_snapshots` | 结束账单前快照 |
| ledger_clear_snapshot.json | `load/save_clear_snapshots` | 清空账单前快照 |
| ledger_global.json | `load/save_ledger_global` | 自动结算时间 |
| welcome_settings.json | `load/save_welcome_settings` | 欢迎语指令 |

---

## 六、二次开发修改指引

想加一个新的「中文关键词指令」：
1. 在正则常量区（约 1915–1934 行）加 `RE_XXX`。
2. 写 `try_handle_xxx(update, context, text) -> bool`（匹配→处理→return True）。
3. 在 `handle_message` 分发链中、**计算器之前**调用它（注意先修复已知问题 #2，否则新代码放在「联系客服」之后不会生效）。

想加一个新的斜杠命令：
1. 写处理函数（需要多步交互就仿照 `adduser_conv` 建 ConversationHandler，状态码按区段新增）。
2. 在底部注册区 `app.add_handler(...)`。
3. 在 `post_init` 的 `set_my_commands` 列表补一条。
4. 更新「使用说明」文案（约 2623–2679 行）。

想加一种新的续费周期：改 `CYCLE_LABELS`（85 行）+ `compute_next_due`（118 行）。

改记账展示格式：`format_ledger_line` / `format_disburse_line` / `build_ledger_summary`（2339–2423）。

⚠️ 通用注意：
- 所有 JSON 保存是全量覆盖写，注意并发（见 Project.md 已知问题 #6）。
- 新增回调按钮时 callback_data 前缀不要与第三节已有前缀冲突，并确认注册了对应 `CallbackQueryHandler`。
- 改动后建议用 `/ddtest1200`、手动发 `+100` / `账单` 等做冒烟验证。
