# CryptoTrade 开发计划与未完成能力 (DevPlan)

本文件承接原 `codemap.md` 第 6 章「当前未完成能力与开发顺序」，作为**风险与进度跟踪的唯一台账**。架构与模块索引仍以 [`codemap.md`](codemap.md) 为准。

维护约定：

- 按**风险优先级**排序，P0 在最前；
- 「已完成」条目保留原因与证据，避免重复踩坑；
- 条目描述的是**代码与验证状态**，不以「已实现」等同「已在真实交易所验证」。

---

## 1. 当前状态速览（2026-09-16）

| 项目 | 状态 |
| :--- | :--- |
| 单元测试基线 | `py -m pytest` = **70 项通过** |
| 已接通并实跑过的交易所 | OKX DEMO（真实链路）、Binance TESTNET（下单成功，保护单路径刚重做） |
| 配置启用但从未验证 | GATE TESTNET（缺凭据） |
| 已知 P0 缺陷 | 无（原 Binance `-4509` 已修复，见 §2.1） |
| 当前锁定交易 | 无（原 `BINANCE-...-002` 已清理，见 §2.2） |

---

## 2. 已完成（含原因与证据）

### 2.1 【已修复】Binance 预挂保护单被 `-4509` 拒单

**原现象**（2026-09-16 12:11，`python main.py --dev` 广播执行）：

```
- OKX: 已提交 OKX-ETH-USDT-PERP-LONG-20260916-010 进场挂单至 OKX，订单号 3927823847786409985
- BINANCE: 失败：Binance API 错误 -4509: Time in Force (TIF) GTE can only be used with open positions.
           Please ensure that positions are available.
```

**根因**：旧流程在**进场单刚受理、尚未成交**时就调用 `POST /fapi/v1/algoOrder` 预挂止盈止损，并使用 `closePosition=true` 全平语义。订单 API 明确要求 `closePosition` 与 `quantity`/`reduceOnly` 互斥，且服务端在账户无持仓时拒绝该组合，返回 `-4509`。异常回滚分支随即撤掉刚受理的进场单并把交易锁为 `ERROR_LOCKED`。

> 值得注意的是：同一进程更早（09:56）的同款调用却成功，说明该接口行为随账户/环境状态漂移，属于**不可依赖的路径**，而非稳定的接口契约。

**修复方案（已实施）**：所有交易所统一改为**「成交后再补建保护单」**，彻底取消进场受理阶段的预挂。

- `trading_service._open()`：删除对 `_place_binance_pending_protection()` 的调用与该方法本身；
- `models.OrderRequest`：移除 `close_position` 字段，从类型层面杜绝重新引入全平语义；
- `exchanges/binance.py::_conditional()`：保护单一律使用「`quantity` + `reduceOnly=true`」，不再发送 `closePosition`；
- 保护单统一由 `Monitor._ensure_protection()` 在收到成交事件后**按真实持仓数量**创建（与 Gate 相同路径），部分成交时按累计成交量创建并在量变化时替换。

**验证**：

- `tests/test_okx_errors.py::test_binance_protection_uses_quantity_reduce_only`：断言发出 `quantity` + `reduceOnly`，且**请求体中不存在 `closePosition`**；
- `tests/test_okx_errors.py::test_binance_protection_rejects_non_reduce_only`：参数缺 `reduceOnly` 时本地即拒绝，不发网络请求；
- `tests/test_core.py::test_entry_acceptance_does_not_preplace_protection`：进场受理后**只有进场单落库、无预挂保护单、无 `PLACE_PENDING_PROTECTION` 审计**；
- `tests/test_cleanup.py`：清理模块的安全边界（有序交易不误清、部分成交不清理、无法核对远程不解锁、dry-run 不写库、桩适配器确保不调用任何撤单/平仓）；
- 全量 `70 项通过`；
- **真实环境复验**（2026-09-16 12:50，`py main.py --dev`）：`BINANCE: 已提交 BINANCE-ETH-USDT-PERP-LONG-20260916-003 进场挂单至 BINANCE，订单号 16796128473`，不再出现 `-4509`；库内该交易只有 1 条 `ENTRY` 订单（`NEW`）、状态 `PENDING_ENTRY`、且无 `PLACE_PENDING_PROTECTION` 审计，证明预挂路径确实已移除。

**行为变化（需知晓）**：进场成交到保护单创建之间存在数秒的**无保护窗口**（原设计试图用预挂消除它，但该预挂在 Binance 上根本不被接受）。此窗口是当前刻意接受的权衡，理由：系统始终知道该交易的确切成交数量，用精确数量保护比全平语义更安全，也不会误平同合约的其它交易。

### 2.2 【已清理】僵尸交易清理能力与历史锁定交易

**背景**：2.1 的回滚分支会撤单并锁定交易，但**没有配套的清理动作**，导致 `BINANCE-ETH-USDT-PERP-LONG-20260916-002` 长期停在 `ERROR_LOCKED`（进场单已撤、无持仓），既不能自动处理也不能人工恢复。

**新增** [`trade_cleanup.py`](trade_cleanup.py)：只写本地数据库，**绝不下单、撤单或平仓**，因此不依赖 `ExchangeRouter`；所有需要触碰交易所的判断都退化为「先只读核对，条件不满足则拒绝」。

两级清理，风险由低到高：

1. `close_phantom_trades()`（默认执行）：`RECEIVED`/`PENDING_ENTRY`/`PARTIAL_FILL` 状态且**本地不存在任何活动订单**的交易 → 收敛为 `CANCELLED`。`PARTIAL_FILL` 因可能已有真实持仓而被排除，只提示人工确认。
2. `unlock_trades()`（需显式 `--unlock`）：`ERROR_LOCKED` 交易需**同时**满足「远程无该合约该方向持仓」+「远程无本交易活动订单」+「本地无活动订单」才解除锁定，否则跳过并给出原因。

命令：

```bash
py trade_cleanup.py --dry-run            # 只打印将要发生的变更
py trade_cleanup.py --unlock             # 同时尝试解除 ERROR_LOCKED
py trade_cleanup.py --no-router          # 不连交易所，仅按本地事实清理
```

**执行记录**：先用 `--dry-run` 确认目标交易远程无持仓、无挂单（其余 13 笔因仍有活动进场挂单被正确跳过），再实际执行 —— `BINANCE-...-002` 由 `ERROR_LOCKED` 收敛为 `CANCELLED`，`audit_logs` 留下 `TRADE_CLEANUP` 前后状态记录。当前库中 `ERROR_LOCKED` 为 0。

### 2.3 已完成的最高优先级闭环：成交状态与保护单

`Monitor` 会从 OKX、Binance、Gate 的订单 WebSocket 事件提取客户订单号和订单状态，并同步本地 `orders` 与交易状态。开仓完整成交后：

- OKX 使用开仓请求中的 `attachAlgoOrds` 原子附带止盈止损；
- Binance、Gate 按实际持仓数量创建只减仓的止盈、止损单，并写入订单与审计日志（Binance 自 2.1 修复后与 Gate 同路径）；
- 保护单创建失败时将交易置为 `ERROR_LOCKED`，阻止后续自动操作并输出高优先级日志，可用 `trade_cleanup.py` 收口。

---

## 3. 待办（按优先级）

### 3.1 【P0】真实交易所冒烟检查纳入 `--dev`

2.1 的缺陷**单测完全无法发现**：测试只断言了 `closePosition` 存在，既没断言 `timeInForce`，也没验证「无持仓时能否下单」。把真实交易所的最小冒烟断言（下单 → 成交 → 保护单补建 → 撤单）纳入 `--dev` 必跑项，才能把这类只在线上暴露的问题前移。

### 3.2 【P1】Gate 交易所端到端未验证

`config.yaml` 中 Gate 保持 `enabled: true`，但 `.env` 未配置 `GATE_TESTNET_API_KEY`/`SECRET`，路由会以「缺少环境变量」将其禁用；`audit_logs` 中没有任何 `GATE` 记录，`exchange_events` 只有 OKX 数据。因此 Gate 适配器（含只减仓保护单与 `futures.tickers` 保本触发路径）**从未跑过真实链路**。待办：补测试网凭据后以 `--dev` 单交易所实跑，覆盖下单、成交、保护单补建、保本移动、撤单、平仓。

### 3.3 【P2】OKX 原子保护单的 Algo ID 关联

旧版 OKX 原子附带保护单未保存可撤销的 Algo ID，因此自动保本不会猜测并修改该类订单，人工操作也会安全拒绝。待补齐 Algo ID 的查询/撤销关联，使这类仓位也能进入自动保本与人工改单流程。

### 3.4 【P2】自动保本策略执行（剩余部分）

已接入 OKX、Binance、Gate 行情事件；对于本地已跟踪的止损单，达到目标进度后会按真实持仓均价创建更优止损、撤销旧止损，并持久化 `breakeven_triggered`。剩余项见 3.3。

### 3.5 【P2】启动后的持仓恢复（剩余部分）

启动对账会刷新仓位快照，并且仅在「交易所 + 合约 + 方向」下本地进场数量之和与远程仓位精确一致、且没有仍开放的进场单时恢复为 `OPEN`、补建非原子保护单及继续保本监控；未关联或数量不一致的远程持仓只告警，绝不猜测归属。**跨设备/清库后的历史订单重建仍未实现。**

### 3.6 【P2】多笔同币种交易的精确关联（剩余部分）

已以本地 `trade_id` 的进场数量作为汇总仓位分配账本；自动保本仅在同一「交易所 + 合约 + 方向」下所有活动交易的数量之和与远程仓位精确一致时逐笔执行，否则记录审计并停止自动操作。跨设备/人工交易导致的数量差异仍需人工处置。

### 3.7 【P3】人工指令的 Telegram 对话入口

已开放 `AMEND_ENTRY`（仅未成交挂单）、`CANCEL_ORDER`（未成交时撤进场、已开仓时取消止损）、`MOVE_STOP`（改止损或恢复止损）与明确的 `CLOSE_POSITION` 市价平仓。每项均要求完整 `trade_id`，并核对交易所、合约、方向和远程订单/仓位；不能唯一关联则拒绝。**修改止盈、补仓及「保本离场」改止盈仍待实现**；面向人工的 Telegram 入口目前只有只读命令（`/status`、`/help`、`/start`），上述人工指令仍无对话入口。

### 3.8 【P4】通知与可观测性（剩余部分）

已将启动对账、命令结果、保本移动、保护单失败和停机事件写入 SQLite 审计、JSONL 日志并按级别发送 Telegram 回报；**尚未提供审计查询 CLI 或周期性运行摘要**。

### 3.9 【P4】成交与事件安全性（剩余部分）

部分成交使用交易所累计成交量创建并随成交扩大而替换保护单；部分成交时拒绝撤余单；行情 ticker 不再逐条写入 SQLite（`exchange_events` 仅保留订单类事件）；关闭时会等待已接收的 Telegram 指令完成。OKX 原子保护单的 Algo ID 关联见 3.3。

---

## 4. 建议开发顺序

1. **P0**：把真实交易所冒烟检查纳入 `--dev` 必跑断言（§3.1）；
2. **P1**：补齐 Gate 测试网凭据并完成端到端实跑（§3.2）；
3. **P2**：OKX 原子保护单 Algo ID 关联（§3.3），连带打通保本与人工改单；
4. **P3**：人工指令的 Telegram 对话入口、改止盈/补仓（§3.7）；
5. **P4**：审计查询 CLI 与周期性运行摘要（§3.8）。

---

## 5. 环境与约定备忘

- 解析器文档已统一：`DeepSeekParser` 是默认运行链路，`codex_parser.py` 仅作为停用的备用实现保留。
- 凭据按「交易所 + 模式」严格隔离；`config.yaml` 中 `enabled: true` 只代表「纳入启动尝试」，实际是否接通取决于对应模式的环境变量是否齐备。
- 测试全部基于本地 `PaperAdapter` 或桩替换，**不覆盖真实交易所网络行为**（`credentials.py` 的凭据读取在导入配置阶段即触发，未配置凭据的交易所无法被真实替换）。