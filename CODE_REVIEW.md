# CODE REVIEW — CryptoTrade 代码评审报告

| 项目 | 内容 |
| :--- | :--- |
| 评审日期 | 2026-09-19 |
| 评审对象 | `F:\projects\CryptoTrade` 全量源码（不含 `tests/`、`*.md`） |
| 评审基线 | `da31c83 feat: 完成 TG 人工弹性指令控制与补仓链路` |
| 测试基线 | `py -m pytest -q` = **136 passed**（本次复跑一致） |
| 评审方法 | 静态通读 + 针对性运行时复现（`PaperAdapter` 桩 + 构造事件/AT 指令） |
| 关联文档 | `codemap.md`、`devPLAN.md`、`README.md` |

> 说明：本报告中标注 **[已验证]** 的条目均在本地用可复现脚本跑通，结论来自实际观测而非纯静态推断；
> 标注 **[静态]** 的条目为代码结构推断，未做运行时复现。

---

## 1. 结论摘要

整体评价：**架构分层清晰、安全边界意识强**（命令层不触碰交易所、远程对象唯一关联校验、
先挂新单后撤旧单、幂等指令编号、审计留痕），这一点在同类交易系统里属于上游水平。
但**后果面（post-trade 生命周期）存在结构性缺口**，导致「已经成交的仓位」在本地失去跟踪，
在真实交易所上会产生**裸仓**与**状态永久卡死**。

| 编号 | 严重度 | 问题 | 位置 |
| :--- | :--- | :--- | :--- |
| P0-1 | 🔴 致命 | ~~部分成交后交易所撤余单，交易被判为 `CANCELLED` 并撤销全部保护单，留下无保护的实盘持仓~~ **✅ 已修复 2026-09-19** | `monitor.py:246-250`、`monitor.py:_handle_entry_terminated` |
| P0-2 | 🔴 致命 | OKX（`entry_protection_attached=True`）**从不写入保护单记录**，导致改止损/改止盈/取消止损/保本离场/自动保本在 OKX 上全部不可用 | `monitor.py:462-463`、`exchanges/okx.py:155` |
| P0-3 | 🔴 致命 | 保护单/平仓单的成交回报**完全没有处理**，止损止盈触发后本地永远停留在 `OPEN`；`CLOSING` 状态**无出边**，永久卡死 | `monitor.py:215-221`、`state_manager.py:18`、`trading_service.py:377` |
| P1-1 | 🟠 高 | 保护单重建使用**确定性 `client_order_id`**，与 `orders.client_order_id UNIQUE` 冲突，重建必然 `IntegrityError` 并把交易锁死 | `monitor.py:531`、`database.py:25` |
| P1-2 | 🟠 高 | `commands.payload_json` 取「最新一条」且**无 `rowid` 兜底**；SQLite `CURRENT_TIMESTAMP` 只有秒级精度，同秒内多条指令会取错，导致保护单用错价格或直接锁死交易 | `monitor.py:392`、`monitor.py:480` |
| P1-3 | 🟠 高 | 重复收到已终态订单推送（WS 重连/重订阅必然发生）会**重跑** `_resize_take_profit`，撤掉已生效止盈后因唯一约束失败 → 仓位失去止盈且交易锁定 | `monitor.py:258-270`、`monitor.py:542-601` |
| P2-1 | 🟡 中 | `RiskManager` 的单交易所累计名义上限参数 `current_notional` **从未被传入**，实际恒定按 0 计算，上限形同虚设 | `risk_manager.py:24-35`、`trading_service.py:109` |
| P2-2 | 🟡 中 | `AMEND_TAKE_PROFIT` 不校验新止盈与持仓方向/均价的关系，可提交立即触发的止盈价 | `validator.py:75-77` |
| P2-3 | 🟡 中 | 来源频道同时被当作**可信写指令通道**（`BTC 止损改 50000` 等直接改单/平仓），与「公告不可信」的威胁模型自相矛盾 | `main.py:149-154`、`telegram_client.py:114-118` |
| P2-4 | 🟡 中 | 每次启动无条件清空 `commands`/`orders`/`trade_instances`，与 P0-2/P0-3 叠加后，**重启即失去全部保护参数** | `startup_reset.py:38-46` |
| P2-5 | 🟡 中 | OKX `get_open_orders` 只查 `orders-pending`，**不含 `orders-algo-pending`**，算法保护单对本地不可见 | `exchanges/okx.py:154-161` |
| P3-* | 🟢 低 | 若干健壮性/一致性改进项（见 §5） | — |

---

## 2. 致命问题（P0）

### P0-1 部分成交后撤余单 → 交易被判 `CANCELLED` 并清空保护单 **[已验证] [已修复 2026-09-19]**

**位置**：`monitor.py:246-257`

```python
if status not in {"FILLED", "FINISHED"}:
    if status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"} and not is_add_entry and current in {
        TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value,
    }:
        try:
            await self._cancel_pending_protection(adapter, trade_id)
            await self._set_state(trade_id, TradeState.CANCELLED)
```

**问题**：`PARTIAL_FILL` 意味着**已经有真实成交仓位**。当交易所把余量撤掉（IOC/GTD 到期、
风控撤单、用户手动撤单、`EXPIRED_IN_MARKET`），代码会：

1. 撤销该交易**已建好的止盈/止损保护单**（`_cancel_pending_protection`）；
2. 把交易状态写成 `CANCELLED`。

结果：**交易所上仍有一个真实仓位，但本地无任何保护单、且状态已终结**。由于
`_process_breakeven` 只查询 `state='OPEN'`，`_recover_positions` 只接受
`PENDING_ENTRY/PARTIAL_FILL/OPEN`，`trade_cleanup.PHANTOM_STATES` 也不含 `OPEN`，
该仓位在重启后只会被记为「存在未关联远程持仓」，**永久裸奔**。

**复现结果**（Binance 桩，部分成交后收到 `CANCELED`）：

```
 after partial : PARTIAL_FILL [('ENTRY','PARTIALLY_FILLED'), ('TAKE_PROFIT','NEW'), ('STOP_LOSS','NEW')]
 after cancel  : CANCELLED    [('ENTRY','CANCELED'), ('TAKE_PROFIT','CANCELED'), ('STOP_LOSS','CANCELED')]
 remote position STILL OPEN: [('LONG', '8.048')]
```

**建议**：撤余单只在**零成交**时才允许收敛为 `CANCELLED`。`PARTIAL_FILL` 且远程持仓 > 0 时应当：
① 保留/补建保护单；② 把交易转入 `OPEN`（或专门的 `PARTIALLY_FILLED_CLOSED` 态）并纳入保本监控；
③ 若无法唯一关联则 `ERROR_LOCKED` 并告警，**绝不撤销保护单**。

---


**修复说明（2026-09-19）**：已在 `monitor.py` 新增 `_handle_entry_terminated()` 与 `_remote_position()`，把“订单终态”与“是否已有真实成交”解耦，改为按「本地成交量 + 远程持仓」双重判定：

- **零成交**：保持原行为，清理预挂保护单并收敛为 `CANCELLED`；
- **有成交 + 远程持仓可确认**：保留/补建保护单并转入 `OPEN`，纳入自动保本监控；
- **有成交 + 持仓无法确认**：`ERROR_LOCKED` 锁定人工处理，**绝不撤销保护单、绝不猜测**。

支持交易所侧的 `CANCELED/CANCELLED/EXPIRED/REJECTED` 四种终态；补仓单（`ADD_ENTRY`）同样走该路径，零成交时保持 `WAITING_ADD` 不影响既有持仓。重查次数按「是否已有本地成交」分流（有成交 3 次、否则 1 次），既覆盖持仓回报乱序，也避免零成交撤单路径被无谓拖慢。

回归用例（`tests/test_core.py`，均已验证修复前失败、修复后通过）：

| 用例 | 断言 |
| :--- | :--- |
| `test_partial_fill_then_cancel_keeps_position_protected` | 部分成交后撤余单 → 状态 `OPEN`、保护单仍在且数量等于真实成交量 |
| `test_zero_fill_entry_cancel_still_converges_to_cancelled` | 零成交撤单 → `CANCELLED`（原行为不退化） |
| `test_filled_then_cancel_locks_instead_of_dropping_protection` | 有成交但持仓未确认 → `ERROR_LOCKED`，不撤销保护单 |

修复后实测（部分成交 → 撤余单，远程仓位仍在）：

```
 after partial : PARTIAL_FILL [('ENTRY','PARTIALLY_FILLED'), ('TAKE_PROFIT','NEW'), ('STOP_LOSS','NEW')]
 after cancel  : OPEN         [('ENTRY','CANCELED'), ('TAKE_PROFIT','NEW'), ('STOP_LOSS','NEW')]   <-- 修复前为 CANCELLED
 仍开放的保护单: [('TAKE_PROFIT','2'), ('STOP_LOSS','2')]
 breakeven_triggered = 1   # 自动保本已正常接手
```

---

### P0-2 OKX 保护单从不落库，OKX 上的人工保护动作全部不可用 **[已验证]**

**位置**：`monitor.py:460-463`

```python
async def _ensure_protection(self, exchange, adapter, trade_id: str) -> None:
    if adapter.entry_protection_attached:
        return          # OKX 在此直接返回，从不写 orders 记录
```

`OKXAdapter.entry_protection_attached = True`（`exchanges/okx.py`），且 OKX 的
`place_entry_order` 通过 `attachAlgoOrds` 原子附带止盈止损。代码因此假定「保护单已存在，
无需本地跟踪」——但**本地 `orders` 表里没有任何 `TAKE_PROFIT`/`STOP_LOSS` 行**，
而下游所有保护相关逻辑都依赖这些行：

| 功能 | 依赖 | OKX 实际结果 |
| :--- | :--- | :--- |
| `MOVE_STOP` / 改止损 / 恢复止损 | `_remote_order(..., "STOP_LOSS")` | ❌ 未找到活动 STOP_LOSS 本地订单 |
| `CANCEL_ORDER`（取消止损）/ `cancel_stop` | 同上 | ❌ 同上 |
| `AMEND_TAKE_PROFIT` / 保本离场 | `_remote_order(..., "TAKE_PROFIT")` | ❌ 未找到活动 TAKE_PROFIT 本地订单 |
| `_process_breakeven` 自动保本 | `stop_rows` 非空 | ❌ 直接 `return`，**永不触发** |
| `_trigger_breakeven` | 同上 | ❌ |

**复现结果**（OKX，正常成交后）：

```
state after OKX fill: OPEN
all local orders: [('ENTRY','FILLED')]          # 无任何保护单记录
move_stop         FAILED: 未找到活动 STOP_LOSS 本地订单
amend_take_profit FAILED: 未找到活动 TAKE_PROFIT 本地订单
cancel_stop       FAILED: 未找到活动 STOP_LOSS 本地订单
breakeven_triggered: 0 ; BREAKEVEN audits: []
```

按 `codemap.md` §4.2，**OKX DEMO 是当前唯一已配置凭据的交易所**，因此这不是边缘路径，
而是主路径。`telegram_menu.STATE_ACTIONS["OPEN"]` 中除「市价平仓」外的按钮在 OKX 上全部必然被拒。

**建议**：OKX 成交时同步把 `attachAlgoOrds` 的 `algoId` 落库（`order-algo` / 订单详情返回），
或退化为「成交后显式查询并登记保护单」。**不要用 `entry_protection_attached` 绕过本地跟踪**——
该标志只应决定「是否需要新建」，不应决定「是否需要记录」。

---

### P0-3 保护单/平仓单成交无处理，`CLOSING` 无出边 **[静态 + 已验证]**

**位置**：`monitor.py:215-221`、`state_manager.py:12-19`、`trading_service.py:377`

```python
rows = await self.database.fetch_all(
    "SELECT o.trade_id,o.order_type,t.state FROM orders o JOIN trade_instances t ON t.trade_id=o.trade_id "
    "WHERE o.client_order_id=? AND o.order_type IN ('ENTRY','ADD_ENTRY')",
    (client_order_id,),
)
if not rows:
    return      # 止盈/止损/平仓单的成交推送在此被静默丢弃
```

后果：

1. **止损或止盈在交易所触发后，本地交易仍保持 `OPEN`、`orders.status` 仍为 `NEW`**，
   自动保本会继续把止损往有利方向推（尽管仓位已经不存在）；`/trades`、`/status` 持续误导人工。
2. `CLOSE_POSITION` 成功后 `trading_service.py:377` 转入 `CLOSING`，
   而全仓库**没有任何代码把 `CLOSING` 迁到 `CLOSED`**（`rg` 确认：`CLOSED` 仅出现在
   `ALLOWED_TRANSITIONS` 与只读过滤中）。该交易将永久停留在 `CLOSING`，
   既不在终端态集合 `TERMINAL_STATES`（`telegram_commands.py:51`）中，
   因而会**一直出现在「活动交易」列表**里并继续展示「市价平仓」按钮——而该按钮又要求
   `state == OPEN`，点击必然被拒。

**建议**：`process_event` 需要覆盖 `TAKE_PROFIT` / `STOP_LOSS` / `CLOSE` 三类客户订单号，
按 `reduce_only` 成交收敛交易为 `CLOSED`；`CLOSING` 必须由平仓成交或 `order_sync` 兜底收敛。

---

## 3. 高优先级问题（P1）

### P1-1 保护单确定性 `client_order_id` 与 `UNIQUE` 约束冲突 **[已验证]**

**位置**：`monitor.py:531`

```python
client_order_id=self._protection_client_id(trade_id, f"{order_type}:{trade_quantity}"),
```

`orders.client_order_id` 是 `UNIQUE`（`database.py:25`），而该编号只由
`trade_id + order_type + quantity` 派生。一旦某个保护单已经进入终态（`CANCELED`/`FILLED`），
它**仍然占用**该编号；此时若因外部撤单、重启恢复等场景需要**按相同数量重建**保护单，
`INSERT` 必然抛 `IntegrityError`。

**复现结果**（把止损单远程移除并本地置 `CANCELED` 后触发重建）：

```
rebuild FAILED: IntegrityError UNIQUE constraint failed: orders.client_order_id
```

该异常不是 `ValueError` 子类，在 `_recover_positions`（`monitor.py:177-181`）中会被吞成
`ERROR_LOCKED`，即**重启恢复保护单这一核心场景直接失败并锁死交易**。

**建议**：编号加入序号/时间戳（如 `{order_type}:{quantity}:{attempt}`），或改为
`INSERT` 前检查并复用/更新既有行，而非重复插入。

---

### P1-2 `commands` 取「最新一条」无 `rowid` 兜底，同秒指令取错 **[已验证]**

**位置**：`monitor.py:392`、`monitor.py:480`

```python
"SELECT payload_json FROM commands WHERE trade_id=? ORDER BY created_at DESC LIMIT 1"
```

两个独立缺陷叠加：

1. **精度**：`created_at` 默认值 `CURRENT_TIMESTAMP` 为秒级。同一秒内写入的两条指令
   （开仓 + 紧随其后的人工指令）排序**不确定**。
2. **语义**：即使排序正确，「最新一条指令」也**不保证携带保护参数**。
   `MOVE_STOP` / `AMEND_TAKE_PROFIT` / `CANCEL_ORDER` / `ADD_POSITION` 的
   `payload_json` 中 `stop_loss` / `take_profits` 多为 `null` 或空元组。

**复现结果**（同秒插入 `OPEN_POSITION` 与 `AMEND_TAKE_PROFIT`）：

```
open  2026-09-19 14:49:59
amend 2026-09-19 14:49:59
monitor picks (ORDER BY created_at DESC): open   <-- 期望 'amend'
with rowid tiebreak                     : amend
```

由于 `monitor.py:484-487` 对空值直接抛
`ValueError("成交指令缺少止盈或止损，拒绝裸仓")`，取错指令的后果是
**保护单不建、交易被 `ERROR_LOCKED`**（或装上一条指令的陈旧价格）。

**建议**：保护参数应作为**交易维度的不可变快照**存放（例如新增 `trade_protections` 表，
在开仓成功时一次性写入），而不是每次从「最新指令」反推；短期修复至少加
`ORDER BY created_at DESC, rowid DESC`。

---

### P1-3 已终态订单推送重放会重跑补仓止盈重建并锁死 **[已验证]**

**位置**：`monitor.py:258-270`（无「状态已终态则跳过」判据）、`monitor.py:542-601`

交易所 WebSocket 在重连/重订阅后会**重放**最近订单状态，这是正常行为。
`process_event` 对 `FILLED` 没有任何幂等保护，因此同一条 `ADD_ENTRY FILLED` 会被处理多次，
每次都执行 `_resize_take_profit`：先 `place_take_profit`（PaperAdapter 因客户编号重复直接返回旧单），
再 `cancel_order` 撤掉**上一轮刚生效的止盈**，最后 `INSERT` 同名编号 → `UNIQUE` 冲突。

**复现结果**（`ADD_ENTRY` 成交事件重放两次）：

```
 after ADD fill #1: OPEN [('ENTRY','FILLED'), ('TAKE_PROFIT','CANCELED','2549'),
                          ('STOP_LOSS','CANCELED'), ('ADD_ENTRY','FILLED'), ('TAKE_PROFIT','NEW','2549')]
 after ADD fill #2: ERROR_LOCKED [..., ('TAKE_PROFIT','CANCELED','2549')]   # 止盈被撤且未重建
```

净效果：**加仓后的仓位失去止盈保护，同时交易被锁死**。该问题在真实交易所上概率更高
（WS 断线重连频繁）。

**建议**：`process_event` 入口对「订单已是终态且交易已进入目标状态」做短路；
`_resize_take_profit` / `_ensure_protection` 内部对相同 `(trade_id, order_type, quantity)`
做「已存在且远程仍开放则跳过」的判定。

---

## 4. 中优先级问题（P2）

### P2-1 单交易所累计名义上限从未生效 **[静态]**

`RiskManager.check_open` 设计了 `current_notional` 参数用于限制**同一交易所累计**名义敞口，
但唯一调用点 `trading_service.py:109` 是 `self.risk.check_open(command, equity)`，
**从不传第三个参数**，实际恒为 `Decimal("0")`。因此 `max_position_notional_ratio` 只约束
单笔敞口；同交易所多笔（含 `ADD_POSITION` 加仓）可无限叠加。
建议统计该交易所现存持仓名义并传入，或明确删除该参数以免误导。

### P2-2 改止盈不校验与方向/均价的关系 **[静态]**

`validator._validate_amendment` 对 `AMEND_TAKE_PROFIT` 仅校验 `take_profits[0] > 0`。
多单提交一个**低于现价**的止盈价会被交易所立即触发（`TAKE_PROFIT_MARKET`），
等价于一次不受控的市价平仓。建议比对当前持仓均价/标记价，要求高于（多单）均价。

### P2-3 来源频道被当作可信写指令通道 **[静态]**

`main._is_command` 对**所有聊天**（含来源频道）执行 `parse_manual_keyword`，
而 `telegram_client.messages` 对来源频道直接放行。这意味着：

- 任何能在来源频道发帖的人都可以下发 `BTC 市价平仓`/`BTC 止损改 50000` 等**真实写操作**；
- 公告本身是**不可信输入**，但只要公告里出现「止损改」「保本」「补仓」等词，
  `_is_command` 会把它**当作人工指令**而不是交易公告，`parse_manual_keyword` 的
  「币种唯一匹配」在无活动交易时会直接回复拒绝，**公告被静默吞掉**。

建议：写指令仅限 `.env` 白名单私聊（`TELEGRAM_COMMAND_CHAT_IDS`），来源频道仅保留只读命令。

### P2-4 启动无条件清库与上述缺口叠加 **[静态]**

`startup_reset.BUSINESS_TABLES` 每次启动清空 `orders` / `commands` / `trade_instances`，
`commands.payload_json` 是**唯一**保存止盈止损参数的地方（`startup_reset.py` 模块文档已自述）。
在 P0-2/P0-3 未修复前，重启即等于放弃所有持仓的保护参数与状态跟踪，且该行为
**无 dry-run、不可预演**（`order_sync.py` / `trade_cleanup.py` 都有 `--dry-run`，此处却没有）。
建议至少提供 `--no-reset` 开关与预演输出。

### P2-5 OKX 开放订单查询不含算法单 **[静态]**

`exchanges/okx.py:154-161` 仅请求 `/api/v5/trade/orders-pending`，
而 OKX 的条件/算法单在 `/api/v5/trade/orders-algo-pending` 中。因此
`place_take_profit` / `place_stop_loss`（走 `order-algo`）创建的订单**对
`get_open_orders` 不可见**，会导致：
① `_ensure_protection` 的 `missing_remote` 误判为「本地保护单已不在远程」；
② 对账时误报「远程数据和本地数据库挂单状态不一致」。
（Binance 适配器已正确处理 `openAlgoOrders`，可作为实现参考。）

---

## 5. 低优先级与改进建议（P3）

1. **`_move_stop` 的 `trade_quantity` 存在未初始化路径**（`trading_service.py:196-206`）：
   仅在 `state == WAITING_ADD` 时赋值，当前靠 `local is not None` 兜底才安全，建议显式初始化。
2. **`_amend_take_profit` 的 `BREAKEVEN_EXIT` 硬编码 ±1%**（`trading_service.py:242-245`）
   与 `BreakevenSpec.profit_price_ratio` 重复且不一致，建议统一取值来源。
3. **`settings.whitelist` 混装归一化别名与原始大写串**（`settings.py:48-55`），
   实际只靠 `validator` 双重判断才不出错，建议只保留归一化结果。
4. **`monitor._ensure_protection` 允许 `position.quantity > trade_quantity`**
   （`monitor.py:505-508` 只拒绝「小于」），在多个 trade 共用一个汇总仓位时会按单笔
   数量建保护单；虽然后续有 `_allocated_quantity` 一致性校验，但保护单数量会偏小。
5. **`EventNotifier.emit` 把 `level` 直接用于 `getattr(logger, level.lower())`**
   （`notifications.py:22`）：`level` 来自调用方字符串，若拼写异常会静默降级为 `info`，
   建议用白名单映射。
6. **`database.Database` 每次调用新建 SQLite 连接**（`database.py:64-78`）：
   在 `--dev` 与监控高频事件下 IO 放大明显，可考虑连接复用 + `asyncio` 队列。
7. **`README.md` / `codemap.md` 与实现的同步**：`codemap.md` §7 称「仅白名单命令聊天放行
   中文快捷指令」，与实现（来源频道同样放行）不一致，需按 P2-3 的结论二选一并统一。
8. **测试覆盖的结构性空洞**：136 个用例全部基于 `PaperAdapter`，因此
   `entry_protection_attached=True`（OKX 真实路径）、保护单成交回报、WS 重放、
   `CLOSING` 收敛等场景**零覆盖**——本报告 P0-2/P0-3/P1-3 均落入该空洞。
   建议优先补「同一事件重放两次」与「部分成交后撤单」两条回归用例。

---

## 6. 值得肯定的设计

- **分层与边界**：`telegram_commands.py` 明确不触碰交易所适配器，全部经
  `TradingService._load_trade` 做「交易所 + 合约 + 方向」三元组核对，人工指令无法伪造矛盾信息。
- **远程对象唯一关联**：`_remote_order`、`_close_position`、`_move_stop` 均要求
  本地订单/持仓与远程**精确匹配**，不匹配即拒绝而非猜测，符合「宁可拒绝不可猜错」。
- **撤单时序**：`_amend_take_profit`、`_trigger_breakeven`、`_move_stop` 采用
  「先挂新单、后撤旧单」，并在撤旧失败时回滚新单，显著缩短裸仓窗口。
- **幂等设计**：`commands.command_id` 绑定 Telegram 消息编号、
  `orders.client_order_id` 为 SHA-256 摘要、`telegram_messages` 主键去重，三层防重。
- **审计完备**：`audit_logs` + JSONL + Telegram 三通道留痕，关键分支均有前后置记录。
- **失败安全**：`StartupResetter` 在「所有交易所都读不通」时**放弃清库**；
  `trade_cleanup` 在无法核对远程时拒绝解锁；`Monitor._first_row` 对畸形推送做形状归一，
  避免单条坏消息终止整条事件流（`tests/test_monitor_events.py` 已覆盖）。

---

## 7. 建议修复顺序

| 顺序 | 内容 | 理由 |
| :--- | :--- | :--- |
| 1 | ~~**P0-1** 部分成交撤单不得判 `CANCELLED`~~ **✅ 已修复** | 2026-09-19：`_handle_entry_terminated()` 改为按成交事实收敛，附 3 条回归用例 |
| 2 | **P0-2** OKX 保护单落库 | 主交易所的核心功能整体失效 |
| 3 | **P0-3** 保护单/平仓单成交处理 + `CLOSING` 收敛 | 状态机与事实脱节，误导人工决策 |
| 4 | **P1-1 / P1-3** 保护单编号唯一性 + 事件重放幂等 | 重启恢复与 WS 重连下的必然故障 |
| 5 | **P1-2** 保护参数快照化（替代「最新指令」） | 根治保护单价格来源不可靠 |
| 6 | **P2-1 ~ P2-5** | 风控真实性与权限边界 |
| 7 | **P3 各项** | 健壮性与可维护性 |

---

## 8. 复现脚本说明

本报告的 `[已验证]` 结论均由一次性脚本产生，模式统一如下（无需真实凭据）：

```python
# 以 LOCAL 模式重建配置，避免 credentials 读取失败
src = Settings.load("config.yaml").raw
for c in src["exchanges"].values():
    c["mode"] = "LOCAL"

# 用桩适配器精确控制「保护单是否需要成交后补建」
class Deferred(PaperAdapter):
    @property
    def entry_protection_attached(self) -> bool:
        return False            # 模拟 Binance/Gate 的非原子保护单

# 再通过 Monitor.process_event 注入交易所原始推送（Binance/OKX/Gate 三种报文形状）
await monitor.process_event(Exchange.BINANCE, adapter, {
    "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": client_id, "X": "PARTIALLY_FILLED", "z": qty, "ap": price}},
})
```

其中 **P0-1 已于 2026-09-19 修复并固化为 3 条回归用例**；建议继续将 P0-2、P0-3、P1-1、P1-3 固化为 `tests/` 下的回归用例，
它们共同覆盖了当前测试矩阵中缺失的「成交后生命周期」区域。
