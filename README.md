# JZBot V2

独立重写的 Telegram Business 记账机器人。唯一开发目录 `D:\Codex\jzbot-v2`；
私有源码镜像 `zz77vv/jzbot`（仓库 ID `1434510965`），分支 `main`。旧版源码与旧仓库不作为 V2 的运行依赖。

## 功能

Business owner 本人发送 `+10`、`-10`、`+0.5`、`/`、`//`；金额使用整数十分位，
原有纯文本回执、回复引用、置顶和零余额清账语义保持。编辑、客户消息和机器人自身消息不记账。

管理员私聊 `/start`：原生紧凑带边框表格、正文按钮、折叠最近流水、复制摘要；
正余额、欠款、全部客户以及独立的“有入金记录”列表，搜索、详情、完整流水分页，
聊天定位、内部停用标记、二次确认归零、待交付与异常回执查看和安全连接恢复。
所有查询、操作和确认均按 owner 隔离。内部停用不是 Telegram 封禁，不改变记账权限。

客户广播：支持有交易记录、存款（余额 ≥ 0，含零余额）、欠款（余额 < 0）多选去重。
纯文本预览后二次确认，逐人静默发送，支持暂停、继续、取消及分页结果。
广播队列使用日志目录下独立的 `broadcast.db`（生产为 `/root/jzbot-v2/broadcast.db`），
不修改正式账本结构，不混入账务 Outbox。重启后待发任务暂停，发送结果不确定的客户
不会自动重发。Telegram Business 的可回复限制仍适用，不能保证历史客户都能收到。
完整行为、边界及官方依据见 `docs/BROADCAST.md`。

## 结构

- `jzbot/config.py`：明确、外置、权限受限的配置。
- `jzbot/db/`：连接生命周期、版本化兼容校验、旧统计初始化和精确对账。
- `jzbot/repositories/`：账户、账务、统计、客户、历史、管理操作与 Outbox SQL。
- `jzbot/services/`：业务授权、账务入口、定位、独立投递 worker 和确认恢复。
- `jzbot/handlers/`：普通私聊、Business 消息、回调和管理流程，不包含 SQL。
- `jzbot/ui/`：无数据库副作用的统一原生富消息页面。
- `jzbot/infra/`：PTB 兼容适配、日志脱敏、跨平台 OS 单实例锁。
- `jzbot/app.py`：依赖装配、启动、后台任务及有序关闭；普通导入不构造运行实例。
- `scripts/`：完整检查、独立 GitHub 发布、迁移演练、明确入口。
- `tests/`：临时数据库、Mock、移植的业务回归及 V2 专用测试。

## 安装与本地验证

Python 3.12+，`python -m pip install -r requirements-dev.txt`。
运行 `python scripts/check.py` 完成 compile、实际 V2 入口导入、完整 pytest 和 Ruff。
Windows 当前的嵌入式 Python 固定了旧版路径，所以检查脚本显式插入 V2 根目录，
并断言 `jzbot.__file__`，避免“导入旧版成功”冒充 V2 通过。正常 Python 也可执行：

```sh
python -m compileall .
python -c "import jzbot,jzbot.app; assert not hasattr(jzbot.app,'db'); print('IMPORT_OK')"
python -m pytest -q
```

依赖策略：PTB、aiosqlite 与开发测试工具固定版本，不在部署时自动升级共享环境。
Windows 的 tzdata 单独固定；Ubuntu 使用系统时区数据库。依赖变更须重新测试适配层。

## 配置与启动

`JZBOT_TOKEN`、`JZBOT_DB_PATH`（必须是绝对路径、已存在的账本文件）；可选
`JZBOT_LOCK_PATH`、`JZBOT_LOG_PATH`、`JZBOT_RELEASE`。也可通过 `JZBOT_CONFIG`
指向外部 JSON 配置，字段同名；Linux 文件必须归当前用户所有且权限 0600。
源码不包含真实 Token，不提供默认空生产账本。正常 Python 运行 `python -m jzbot`，
嵌入式 Windows 使用 `python scripts/run_bot.py`。生产流程见 `DEPLOYMENT.md`。

## 发布与限制

本次专用顺序：本地完整测试与迁移演练 → 安全审计 → 私有 GitHub 发布 → 生产切换验收。
`python scripts/publish.py` 使用独立 `.github-publish` 镜像，并核对全部远端 blob。
发布目标固定为现有私有仓库及其 ID；`--create-private` 不再创建替代仓库。
不推旧仓库、不强推、不静默解决冲突。
已有发布镜像若仍保存旧仓库名，须核实同一仓库 ID 后将 `origin` 更新为
`https://github.com/zz77vv/jzbot.git`；脚本不会静默修改未知远端。

Telegram 无端到端恰好一次发送保证。网络响应丢失或发送成功后确认落库失败时，
后续重试可能重复回执，但不能重复入账。恢复功能不直接发送、不标记成功、不清理记录，
只在确认后重新核验/修复有效连接，保留原重试时间和失败次数。

原生富消息使用官方 `sendRichMessage` 和 `editMessageText.rich_message`，
PTB 22.8 的低层 `_post` 适配集中在一个模块。只对明确不支持正文按钮做一次 Inline
回退；保留真正的富消息表格，不伪装成普通 HTML。新客户端的具体视觉效果需客户端查看。

更多说明见 `docs/ARCHITECTURE.md`、`docs/BUSINESS_RULES.md`、`docs/RECOVERY.md`。

## 指令与回执恢复

通过身份校验的加减款、清账指令先保存至日志目录下独立的 `commands.db`，
再执行原账务事务。失败后按原消息标识重试，同一客户的后续指令等待前序处理，
重启后继续恢复；延迟或状态未确认时私聊提醒 owner，避免另发同额指令造成重复记账。
队列绑定账本的绝对路径，不改变正式账本结构。`commands.db` 及其 WAL/SHM 是运行数据，
不可上传或用本地文件覆盖。具体故障边界及人工核对要求见 `docs/RECOVERY.md`。

回执保留原文与引用。旧回执补发时，如果同一 owner/客户已有更新的可置顶回执交付，
旧回执不再置顶；正常回执发送、重试及清账语义保持。

