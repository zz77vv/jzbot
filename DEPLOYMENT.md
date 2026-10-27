# V2 安全上线流程

本次任务书顺序优先：审查 → 独立本地实现 → 完整测试/对账 → GitHub → 生产切换。
旧源码、旧日志、账本、WAL/SHM 和锁文件不得被新源码覆盖。GitHub 不是生产配置来源。

广播增量更新不需要改 ledger.db schema，也不创建旧源码/数据库备份。
生产新运行数据 `/root/jzbot-v2/broadcast.db` 及其 WAL/SHM 与账本一样保护；
只由程序初始化独立广播队列，不上传本地运行数据库、不覆盖已有广播记录。
重启自动暂停广播待发任务，不自动继续向客户发送。

## 前置门槛

1. 核对旧版本地、私有 GitHub、实际生产 PID/命令/cwd、源码 SHA256 及数据库路径。
2. 本地运行 `python scripts/check.py`，实际导入必须来自 V2。完整测试不得访问生产 DB。
3. 在合成、与生产结构一致的临时数据库演练迁移，逐客户和逐记录对账。
4. 使用独立发布镜像审计全部文件、暂存区、差异及历史，核实新私有仓库归属/main。
5. 发布后核验远端 SHA/全部 blob；按该版本生成本地白名单部署清单及 SHA256。

## 上传与服务器验证

只上传必要源码/测试/文档和依赖描述到独立 `/root/jzbot-v2`，逐文件校验清单。
不上传开发账本、日志、凭证、Session、缓存、Git 镜像。不使用 rsync --delete。
核验 `/root/bots-venv/bin/python` 已安装匹配的运行依赖，不擅自升级影响其他机器人。

在 `/root/jzbot-v2` 执行：

```sh
/root/bots-venv/bin/python -m compileall .
/root/bots-venv/bin/python -c "import jzbot,jzbot.app; assert not hasattr(jzbot.app,'db'); print('IMPORT_OK')"
/root/bots-venv/bin/python -m pytest -q
```

Ruff 为本地发布门槛，不为它修改共享生产虚拟环境；服务器的完整 pytest 仍须实际通过。
重启前任何失败立即停止，旧版继续运行。检查代码/配置路径权限与可用空间。

## 受控切换与回滚点

仅在前置检查全部通过后：

1. 再次核验目标旧 PID、其他机器人 PID 和 OS 锁持有者。对准确旧 PID 发 SIGTERM。
2. 确认旧进程确已退出，锁已释放；不删除锁文件。
3. 本次允许在 `/root/.local/share/jzbot-v2-recovery/<受控发布标识>`（0700）建立
   一次性 `ledger.rollback.db`（0600）。使用 SQLite backup API、quick_check、外键检查及
   全行/逐客户摘要核验，不复制活动 WAL 文件，不上传该数据库。
4. 在同一受保护位置建立临时演练副本，运行 `scripts/verify_migration.py`，必要时用
   旧版只读查询接口核对每个客户详情、完整流水、最近流水和统计；演练副本最终清理。
5. 外部凭证位于 `/root/.config/jzbot-v2/config.json`（0600），不打印原 Token。
   配置 DB_PATH 指向原账本，LOCK_PATH 指向原锁，LOG_PATH 指向 V2 的 bot.log，
   RELEASE 写实际发布 SHA。禁止让新目录创建空生产账本。
6. 在新目录仅启动一个 `python -m jzbot`，使用 nohup 追加 `run.log`。
7. 比较原账本全部业务行、逐客户摘要、原表/索引/约束；唯一允许的数据迁移差异为版本标记。
8. 验收新 PID/cwd/cmdline、唯一 polling、OS 锁、发布清单、Telegram Business、Outbox
   worker、启动时间之后的日志。其他机器人 PID 必须保持。

## 失败处理

如果 V2 启动或账务核对失败，先准确停止 V2。确认旧版仍能兼容当前数据库，数据无
未知变化后，使用原目录、原 Python、原锁恢复旧版，日志只追加。**不自动覆盖实时账本**。
数据库回滚副本只作为受保护恢复点；若切换后已有新账目，不可盲目恢复旧副本造成丢账。
遇到无法解释的差异应保留现场并报告，不删 DB/WAL/SHM，不绕过锁，不重启其他机器人。

实际发布 SHA、运行 PID、清单摘要及验收结果保存在独立运行记录；不伪造提前通过。
真实客户禁止测试入金、出金、清账和定位点号。管理员验证只发送经归属核验的私聊页面。
