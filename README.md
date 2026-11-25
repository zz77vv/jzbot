# JZBot (Clean-Slate Rewrite)

从零重新设计的 Telegram Business 私聊记账机器人，支持 0.1 USDT 精确整数运算、统一 SQLite 单库架构、原子回执队列、管理后台与安全广播，并内置经过严格对账校验的 V2 数据迁移工具。

## 架构亮点

* **统一单库设计（`jzbot.db`）**：将旧版分散在 3 个库、12 张表的数据重新设计为清晰的 8 张表（`owners`、`connections`、`customers`、`transactions`、`processed_messages`、`receipt_outbox`、`broadcast_jobs`、`broadcast_recipients`），开启 `WAL` + `synchronous=FULL` + `foreign_keys=ON`。
* **精确整数运算**：内部以 `0.1 USDT = 1` 的 64 位整数存储和计算，无浮点误差，支持 SQL 原生排序、汇总与索引分页。
* **事务级幂等与回执 Outbox**：记账、去重与待发回执在同一 `BEGIN IMMEDIATE` 事务中提交；回执由独立后台 Worker 投递并置顶，失败自动指数退避，绝不重复扣款或入账。
* **完整业务能力**：
  * 私聊记账：`+10` / `-0.5` / `/`（查余额与最近 5 条流水）/ `//`（清零）
  * 管理后台（`/start`）：汇总看板、全部/存款/欠款/历史入金分页列表、客户搜索、流水明细、定位聊天、5 分钟二次确认管理员归零、内部停用标记、待发回执查看
  * 客户广播：多选范围、预览确认、1.2s 限流发送、异常不盲目重发、重启自动暂停

## 本地测试

```bash
python -m pytest -q
```

## 从旧版 V2 迁移数据

迁移工具以**只读模式**打开旧版 `ledger.db`，生成全新的 `jzbot.db`，并逐一核对每个客户的余额、流水条数与外键完整性：

```bash
python tools/migrate_v2.py /path/to/old_ledger.db /path/to/new_jzbot.db
```

## 启动方式

通过环境变量或 `JZBOT_CONFIG`（Linux 下要求 0600 权限的 JSON 文件）配置后运行：

```bash
python -m jzbot
```

| 配置项 | 说明 |
| :--- | :--- |
| `JZBOT_TOKEN` | 已开启 Business Mode 的 Telegram Bot Token |
| `JZBOT_DB_PATH` | 新版统一数据库路径（默认 `jzbot.db`） |
| `JZBOT_LOCK_PATH` | 单实例进程锁路径（默认库同目录 `.jzbot.instance.lock`） |
| `JZBOT_LOG_PATH` | 日志文件路径（默认库同目录 `bot.log`） |
| `JZBOT_TZ_OFFSET` | 显示时区偏移小时数（默认 `8`，即 UTC+8） |
