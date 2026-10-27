# V2 上下文与边界

V1 审查基线：本地 `D:\Codex\jzbot`、生产 `/root/jzbot`；旧私有仓库 main
`963b57b38c42ed7e13f5b5b4750480f9f8549674`。审查时旧版 272 测试通过，
旧版源码、GitHub 完整脱敏树、生产主程序及辅助模块核验一致。

V2 不导入 V1，不运行旧 publisher，不将真实凭证复制进新源码。移植回归测试仅在
tests/compat_api.py 中保留旧断言接口，实际调用 V2 的仓储、服务和处理器；生产不引用它。
旧版绑定仓库的 60 项发布工具测试不混入 V2，V2 发布器有独立测试。

LedgerStore 是组合式 unit of work；每个 SQL 仓储使用同一数据库连接和写锁，跨仓储
账务写入仍在一个 BEGIN IMMEDIATE 中。金额/普通清账不调用 Telegram，提交后只唤醒
独立 OutboxService；投递读在写锁之后进行，不能发送事务内未提交的回执。

版本 1 仅记录 app_meta.jzbot_v2_schema=1。旧表、索引、约束和数据兼容；不改普通
ledger 的消息 ID 约束，管理员 clear 仍来自 admin_operations。遇到不一致结构、
未知触发器或未来版本直接停止。已有统计缺迁移标记时不会盲目重算覆盖。

新增的历史入金列表与原正余额列表明确区分。回执连接恢复是受 owner、有效期、
任务状态和失败次数约束的内存确认；与投递共享锁，修改时复核，不提前重试，不改金额。

同一个生产 Token 的 V1/V2 必须使用同一个 OS 锁 `/root/jzbot/.jzbot.instance.lock`。

广播使用独立 BroadcastRepository / BroadcastService / BroadcastHandlers，队列数据库在
日志目录/broadcast.db。LedgerStore 只新增 owner 隔离的只读客户范围查询；正式账本
schema、版本标记及财务 Outbox 不变。范围中 deposit 包含零余额，仅用于广播。
新生命周期同时关闭两个 worker；广播失败不能阻断财务 worker 的关闭。
生产数据库继续是 `/root/jzbot/ledger.db`，新代码目录 `/root/jzbot-v2`，日志写新目录。
任务书本次允许一次性受保护回滚数据库；不恢复周期任务，不批量删除历史备份。
