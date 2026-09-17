# CryptoTrade

这是 `CryptoTrade.md` 的安全第一版实现：Telegram Bot 长轮询接收公告，默认由 DeepSeek API
输出结构化指令，确定性校验与风险模块审批后，路由到 OKX、Binance 或 Gate 的独立适配器。

当前默认连接 OKX 官方模拟盘、Binance 官方测试网和 Gate 官方测试网。`.env` 可同时保存模拟盘和实盘
凭据：程序严格根据 `config.yaml` 的模式读取对应环境变量，例如 `OKX_DEMO_API_KEY` 或
`OKX_LIVE_API_KEY`，不会读取无环境后缀的旧变量。三个适配器封装了各自
的签名、合约代码、价格精度、数量单位、账户/订单 REST 接口和私有 WebSocket 事件流。

## 启动

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
python main.py
```

公告解析默认调用 DeepSeek 的 `deepseek-chat` 模型；请在 `.env` 中设置 `DEEPSEEK_API_KEY`。
解析输出必须符合 `schemas/trade_command.schema.json`，并始终经过确定性校验后才可能下单。
`codex_parser.py` 仍作为停用的备用实现保留，但当前入口不会调用 Codex CLI。

默认 `telegram.enabled: true`；未配置有效 Bot Token 时无法接收公告。接入 Telegram 时，在 `.env` 填入 Bot
Token，并在 `.env` 配置来源 Chat ID。首次启动会丢弃历史积压，
只处理启动后消息。

系统会将启动对账、指令执行结果、自动保本和保护单故障写入 JSONL 与 SQLite 审计日志，并发送到
`TELEGRAM_REPORT_CHAT_ID`。`config.yaml` 的 `notifications` 可分别关闭普通信息、告警或严重告警通知；
通知不包含 API Key、Secret 或原始请求签名。

私密来源频道必须把 Bot 加为管理员，并在 `.env` 配置 `TELEGRAM_SOURCE_CHAT_ID`。获取 ID：

```powershell
python tools/get_telegram_chat_id.py
```

运行前先在频道发布一条新的测试消息。复制脚本输出的 `-100...` 数字；`t.me/+...` 邀请链接
不能直接作为 Bot API 的 Chat ID。

## 文本命令

除交易公告外，Bot 支持三个只读命令：`/start`、`/status`、`/help`。命令不会触发任何下单或平仓动作，
回复直接发回发起命令的聊天。

- `/start`、`/help`：显示可用命令。
- `/status`：显示运行时长、已启用交易所、活动交易、本地活动挂单数、各交易所账户权益和最近指令。

出于安全考虑，命令只对以下来源生效：来源频道，以及 `.env` 中 `TELEGRAM_COMMAND_CHAT_IDS`
显式列出的聊天（逗号分隔，通常填管理员私聊 ID）。未列入的私聊消息不会被接收，也不会被当作公告解析。
私聊中只有 `/` 开头的文本会进入命令层，其余文本一律忽略。

仓位默认按账户权益的 2% 作为保证金、100 倍杠杆、全仓模式计算。模拟盘账户权益由各交易所的
仓位基于交易所 REST 接口返回的真实账户权益计算；仅 `LOCAL` 离线开发模式使用
`paper_equity_usdt`。

三家模式配置：

```yaml
OKX: {mode: DEMO}          # 或 LIVE / LOCAL
BINANCE: {mode: TESTNET}   # 或 LIVE / LOCAL
GATE: {mode: TESTNET}      # 或 LIVE / LOCAL
```

测试网/模拟盘必须使用对应环境创建的 API Key。切换 `LIVE` 前还必须在 `.env` 明确设置
`ALLOW_LIVE_TRADING=true`；API Key 应关闭提现权限并绑定固定 IP。

填写密钥后，先执行只读检查（不会下单）：

```powershell
python tools/check_exchanges.py
```


只有相应交易所显示连接成功后，才启动 `main.py`。本地数据库中旧的 `paper-` 模拟订单不会
出现在官方环境，对账时会产生状态不一致告警；请不要把旧本地模拟数据当成官方订单。

OKX 错误 `50101` 表示 Key 环境不匹配：`mode: DEMO` 必须使用在“模拟交易 API”中创建的
Key，`mode: LIVE` 必须使用实盘 API Key。启动对账或鉴权失败的交易所会在本次运行中禁用，
不会继续进行 WebSocket 无限重连。

系统不额外设置止损亏损比例上限，止损价格按照 Telegram 公告执行。

运行测试：

```powershell
pytest
```

```
py .\main.py --dev --signal "ETH2395-85附近多 目标2405 2409 浮盈一半做保本動作 止损2390"
```

## 安全边界

- API 密钥只从环境变量读取，`.env` 已被 Git 忽略；日志不记录密钥。
- 所有价格和数量使用 `Decimal`；自然语言输出必须再次通过确定性校验。
- Telegram 消息、指令和客户端订单号三层幂等。
- Telegram 人工指令必须包含完整 `trade_id`，且会先核对交易所、合约、方向和远程订单。已支持改未成交挂单、取消进场挂单、取消/恢复止损及明确的市价平仓；无法唯一关联时安全拒绝。
- 退出不会撤单或平仓，并会明确提示本地动态监控已停止。
