# CryptoTrade

这是 `CryptoTrade.md` 的安全第一版实现：Telegram Bot 长轮询接收公告，本机 Codex CLI
输出结构化指令，确定性校验与风险模块审批后，路由到 OKX、Binance 或 Gate 的独立适配器。

当前默认连接 OKX 官方模拟盘、Binance 官方测试网和 Gate 官方测试网。三个适配器封装了各自
的签名、合约代码、价格精度、数量单位、账户/订单 REST 接口和私有 WebSocket 事件流。

## 启动

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
python main.py
```

程序启动时会检查 `codex` 命令是否存在，并兼容 Windows 上由 pnpm/npm 安装的 `codex.CMD`。
可以先用 `codex --version` 验证 Codex CLI 已安装并完成登录。
公告解析使用 `schemas/trade_command.schema.json` 约束最终输出，并通过临时文件读取 Codex 的
最终回答，不会把 CLI 的过程事件误识别成交易指令。

默认 `telegram.enabled: false`，程序只执行启动对账。接入 Telegram 时，在 `.env` 填入 Bot
Token，并在 `.env` 配置来源 Chat ID。当前默认已经启用；首次启动会丢弃历史积压，
只处理启动后消息。

私密来源频道必须把 Bot 加为管理员，并在 `.env` 配置 `TELEGRAM_SOURCE_CHAT_ID`。获取 ID：

```powershell
python tools/get_telegram_chat_id.py
```

运行前先在频道发布一条新的测试消息。复制脚本输出的 `-100...` 数字；`t.me/+...` 邀请链接
不能直接作为 Bot API 的 Chat ID。

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

## 安全边界

- API 密钥只从环境变量读取，`.env` 已被 Git 忽略；日志不记录密钥。
- 所有价格和数量使用 `Decimal`；自然语言输出必须再次通过确定性校验。
- Telegram 消息、指令和客户端订单号三层幂等。
- 修改类指令当前安全拒绝，避免在尚未完成真实账户对账前误改订单。
- 退出不会撤单或平仓，并会明确提示本地动态监控已停止。
