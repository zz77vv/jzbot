# 架构与生命周期

依赖方向：app → runtime → handlers/services → repositories → Database；纯 UI 使用
domain 和 Rich 适配，不导入处理器或数据库。不存在对 V1 的运行时导入或循环模块依赖。

Database 只负责连接、版本与兼容性。LedgerStore 组合七类仓储，并保证连接/写锁共享。
AccountingRepository 明确包住余额、流水、统计、processed_messages、receipt_outbox
的一个事务。StatsRepository 和 OutboxRepository 的事务内部辅助写不独立提交。
ManagementRepository 保留独立审计与管理员 clear 原子性；HistoryRepository SQL 合并分页。

AccountingService 负责已启用连接归属校验；ManagementService 在事务复核之外再做当前
owner 授权。ConnectionsService 保留最新连接刷新、权限诊断和最多八个候选回退。
OutboxService 是唯一正式回执发送者，启动恢复持久 pending，新事务提交后 Event 唤醒。
投递锁防止同 runtime 重复 worker 并行发送；共享 OS 锁阻止第二个生产进程。

运行入口才读取配置、获取 OS 锁、配置日志、构造 Application。post_init 打开并校验 DB，
验证 getMe Business 能力，设置命令后启动 worker。post_shutdown 停止/取消 worker，
收集等待任务，finally 关闭/回滚数据库。默认 concurrent_updates(1)。

富消息传输复用 PTB 的 HTTP 客户端，无第二套 polling。仅 _post 适配点依赖 PTB 私有接口，
版本固定 22.8，真实 PTB 序列化及 callback 解析通过 Mock HTTP 测试覆盖。
官方依据：https://core.telegram.org/bots/api#sendrichmessage
与 https://docs.python-telegram-bot.org/en/v22.8/telegram.bot.html 。

安全改进：外置凭证；导入来源断言；SQL 兼容性失败关闭；取消时回滚；投递等待提交；
RetryAfter 不短于服务端冷却；所有管理员 callback 必须来自自己的机器人私聊。
