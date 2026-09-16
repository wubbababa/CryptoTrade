"""OKX V5 官方 REST 与私有 WebSocket 适配器。"""
from __future__ import annotations
import asyncio, base64, hashlib, hmac, json, logging, os, time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator
from urllib.parse import urlencode
import aiohttp
from exchanges.base import ExchangeAdapter, ExchangeError, round_step
from exchanges.credentials import credentials
from models import Exchange, Instrument, OrderRequest, OrderResult, PositionSide, PositionSnapshot

logger = logging.getLogger(__name__)

def _number(value: Decimal) -> str:
    return format(value, "f")


def _error_message(result: dict) -> str:
    """保留交易所逐笔拒单原因，不输出请求头或鉴权凭据。"""
    parts = [f"OKX API 错误 {result.get('code')}: {result.get('msg')}"]
    for row in result.get("data") or []:
        if isinstance(row, dict) and str(row.get("sCode", "0")) != "0":
            parts.append(f"sCode={row.get('sCode')}, sMsg={row.get('sMsg', '')}")
    return "；".join(parts)

class OKXAdapter(ExchangeAdapter):
    def __init__(self, config: dict) -> None:
        self.config = config
        self.mode = str(config.get("mode", "DEMO")).upper()
        self.demo = self.mode == "DEMO"
        try:
            self.api_key, self.secret, self.passphrase = credentials(
                "OKX", self.mode, "API_KEY", "API_SECRET", "API_PASSPHRASE",
            )
        except ValueError as exc:
            raise ExchangeError(str(exc)) from exc
        self.rest_base = str(config.get("rest_base", "https://www.okx.com")).rstrip("/")
        ws_host = "wss://wspap.okx.com:8443" if self.demo else "wss://ws.okx.com:8443"
        self.private_ws = str(config.get("private_ws", ws_host + "/ws/v5/private"))
        self.public_ws = str(config.get("public_ws", ws_host + "/ws/v5/public"))
        self.session: aiohttp.ClientSession | None = None
        self.instruments: dict[str, Instrument] = {}
        self.order_symbols: dict[str, str] = {}
        self._closed, self._leverage_set = False, set()

    @property
    def entry_protection_attached(self) -> bool:
        """OKX 的 attachAlgoOrds 与开仓订单由同一请求提交。"""
        return True

    async def resolve_leverage(self, instrument: Instrument, requested: Decimal,
                               margin_mode: str) -> Decimal:
        """查询合约档位上限，避免 set-leverage 因 59102 拒绝下单。"""
        # OKX 的 SWAP 档位接口要求 instFamily/uly，不能直接传 instId。
        symbol = instrument.exchange_symbol
        instrument_family = symbol[:-5] if symbol.endswith("-SWAP") else symbol
        rows = await self._request("GET", "/api/v5/public/position-tiers", {
            "instType": "SWAP", "tdMode": margin_mode.lower(),
            "instFamily": instrument_family,
        }, private=False)
        limits = [Decimal(str(row["maxLever"])) for row in rows if row.get("maxLever")]
        if not limits:
            raise ExchangeError(f"OKX 未返回 {instrument.exchange_symbol} 的杠杆上限")
        allowed = max(limits)
        actual = min(requested, allowed)
        if actual < requested:
            logger.warning("OKX %s 最大杠杆为 %s，已将配置杠杆从 %s 自动下调",
                           instrument.exchange_symbol, actual, requested)
        return actual

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        signature = base64.b64encode(hmac.new(self.secret.encode(),
            (timestamp + method.upper() + request_path + body).encode(), hashlib.sha256).digest()).decode()
        headers = {"Content-Type":"application/json", "OK-ACCESS-KEY":self.api_key,
            "OK-ACCESS-SIGN":signature, "OK-ACCESS-TIMESTAMP":timestamp,
            "OK-ACCESS-PASSPHRASE":self.passphrase}
        if self.demo: headers["x-simulated-trading"] = "1"
        return headers

    async def _request(self, method: str, path: str, params: dict | None = None,
                       payload: dict | None = None, private: bool = True) -> list[dict]:
        query, body = urlencode(params or {}), json.dumps(payload, separators=(",", ":")) if payload else ""
        request_path = path + ("?" + query if query else "")
        headers = self._headers(method, request_path, body) if private else ({"x-simulated-trading":"1"} if self.demo else {})
        session = await self._get_session()
        async with session.request(method, self.rest_base + request_path, data=body or None, headers=headers) as response:
            try: result = await response.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                raise ExchangeError(f"OKX HTTP {response.status} 返回非 JSON") from exc
        if response.status >= 400 or str(result.get("code", "0")) != "0":
            if str(result.get("code")) == "50101":
                expected = "官方模拟盘 DEMO" if self.demo else "实盘 LIVE"
                raise ExchangeError(
                    f"OKX API Key 与当前 {expected} 环境不匹配；"
                    "请使用该环境创建的 Key，或核对 config.yaml 的 mode"
                )
            raise ExchangeError(_error_message(result))
        return result.get("data", [])

    async def load_instruments(self) -> dict[str, Instrument]:
        rows = await self._request("GET", "/api/v5/public/instruments", {"instType":"SWAP"}, private=False)
        result = {}
        for row in rows:
            symbol = row.get("instId", "")
            if not symbol.endswith("-USDT-SWAP") or row.get("state") != "live": continue
            asset, multiplier = symbol.split("-")[0], Decimal(row.get("ctVal") or "1")
            result[asset] = Instrument(f"{asset}/USDT:PERP", symbol, Decimal(row["tickSz"]),
                Decimal(row["lotSz"])*multiplier, Decimal(row["minSz"])*multiplier,
                Decimal("0"), multiplier)
        self.instruments = result
        return result

    async def get_equity(self) -> Decimal:
        rows = await self._request("GET", "/api/v5/account/balance")
        return Decimal(rows[0].get("totalEq") or "0") if rows else Decimal("0")

    async def validate_account(self) -> None:
        """只读核对账户及持仓模式，避免用现货账户提交永续订单。"""
        rows = await self._request("GET", "/api/v5/account/config")
        if not rows:
            raise ExchangeError("OKX 账户配置为空")
        account = rows[0]
        level = str(account.get("acctLv", ""))
        if level == "1":
            raise ExchangeError(
                f"OKX [{self.mode}] 当前为现货模式 acctLv=1，不能交易 USDT 永续；"
                "请在对应账户的 OKX 网页/App 交易设置中启用合约模式，再重启程序"
            )
        if level not in {"2", "3", "4"}:
            raise ExchangeError(f"OKX 无法识别账户模式 acctLv={level}")
        expected = "net_mode" if self.config.get("position_mode", "ONE_WAY") == "ONE_WAY" else "long_short_mode"
        if account.get("posMode") != expected:
            raise ExchangeError(
                f"OKX 持仓模式不匹配：账户 posMode={account.get('posMode')}，配置要求 {expected}"
            )

    async def get_open_orders(self) -> list[OrderResult]:
        rows = await self._request("GET", "/api/v5/trade/orders-pending", {"instType":"SWAP"})
        results = []
        for row in rows:
            self.order_symbols[row["ordId"]] = row["instId"]
            results.append(OrderResult(row["ordId"], row.get("clOrdId", ""), row.get("state", ""), row))
        return results

    async def get_positions(self) -> list[PositionSnapshot]:
        if not self.instruments: await self.load_instruments()
        by_symbol = {v.exchange_symbol:v for v in self.instruments.values()}
        rows = await self._request("GET", "/api/v5/account/positions", {"instType":"SWAP"})
        results = []
        for row in rows:
            contracts, instrument = Decimal(row.get("pos") or "0"), by_symbol.get(row.get("instId"))
            if not instrument or contracts == 0: continue
            side = PositionSide.LONG if row.get("posSide") == "long" or contracts > 0 else PositionSide.SHORT
            results.append(PositionSnapshot(Exchange.OKX, instrument.instrument_key, side,
                abs(contracts)*instrument.contract_multiplier, Decimal(row.get("avgPx") or "0")))
        return results

    def _normalize(self, request: OrderRequest) -> tuple[OrderRequest, str]:
        i = request.instrument
        quantity = round_step(request.quantity, i.quantity_step)
        price = round_step(request.price, i.tick_size) if request.price is not None else None
        tp = round_step(request.take_profit_price, i.tick_size) if request.take_profit_price is not None else None
        sl = round_step(request.stop_loss_price, i.tick_size) if request.stop_loss_price is not None else None
        if quantity < i.minimum_quantity: raise ExchangeError("OKX 下单数量低于最小数量")
        normalized = replace(request, quantity=quantity, price=price, take_profit_price=tp, stop_loss_price=sl)
        return normalized, _number(quantity / i.contract_multiplier)

    async def _ensure_leverage(self, request: OrderRequest) -> None:
        symbol = request.instrument.exchange_symbol
        if symbol in self._leverage_set: return
        await self._request("POST", "/api/v5/account/set-leverage", payload={"instId":symbol,
            "lever":_number(request.leverage), "mgnMode":request.margin_mode.lower()})
        self._leverage_set.add(symbol)

    async def _validate_attached_prices(self, request: OrderRequest) -> None:
        """按标记价估算可成交价格，提前拦截 OKX 51051 等保护单错误。"""
        if request.price is None or (
            request.take_profit_price is None and request.stop_loss_price is None
        ):
            return
        rows = await self._request("GET", "/api/v5/public/mark-price", {
            "instType": "SWAP", "instId": request.instrument.exchange_symbol,
        }, private=False)
        if not rows or not rows[0].get("markPx"):
            raise ExchangeError(f"OKX 未返回 {request.instrument.exchange_symbol} 的标记价")
        mark_price = Decimal(str(rows[0]["markPx"]))
        # 可立即成交的限价单以当前标记价作为保守估算，否则以挂单价估算。
        primary_price = min(request.price, mark_price) if request.order_side == "BUY" \
            else max(request.price, mark_price)
        if request.order_side == "BUY":
            valid_sl = request.stop_loss_price is None or request.stop_loss_price < primary_price
            valid_tp = request.take_profit_price is None or request.take_profit_price > primary_price
        else:
            valid_sl = request.stop_loss_price is None or request.stop_loss_price > primary_price
            valid_tp = request.take_profit_price is None or request.take_profit_price < primary_price
        if not valid_sl or not valid_tp:
            raise ExchangeError(
                f"OKX 保护价与可能成交价不匹配：{request.instrument.exchange_symbol} "
                f"标记价={mark_price}，挂单价={request.price}，止损={request.stop_loss_price}，"
                f"止盈={request.take_profit_price}；请检查币种与价格是否对应"
            )

    def _result(self, rows: list[dict], client_id: str, symbol: str) -> OrderResult:
        if not rows: raise ExchangeError("OKX 订单响应缺少数据")
        row = rows[0]
        if row.get("sCode", "0") != "0": raise ExchangeError(f"OKX 订单失败 {row.get('sCode')}: {row.get('sMsg')}")
        order_id = row.get("ordId") or row.get("algoId", "")
        self.order_symbols[order_id] = symbol
        return OrderResult(order_id, row.get("clOrdId") or client_id, "NEW", row)

    async def _regular(self, request: OrderRequest) -> OrderResult:
        await self.validate_account()
        # 在设置杠杆或调用订单接口前检查客户端编号。
        import re
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", request.client_order_id):
            raise ExchangeError("OKX clOrdId 必须为 1–32 位英文字母或数字")
        request, size = self._normalize(request)
        await self._validate_attached_prices(request)
        await self._ensure_leverage(request)
        payload = {"instId":request.instrument.exchange_symbol, "tdMode":request.margin_mode.lower(),
            "side":request.order_side.lower(), "ordType":request.order_type.lower(), "sz":size,
            "clOrdId":request.client_order_id}
        if request.price is not None: payload["px"] = _number(request.price)
        if request.reduce_only: payload["reduceOnly"] = "true"
        # 注入开仓附带止盈止损参数（OKX V5 最新规范使用 attachAlgoOrds 数组，Mark 标记价触发，市价平仓）
        attach_algo: dict[str, str] = {}
        if request.take_profit_price is not None:
            attach_algo["tpTriggerPx"] = _number(request.take_profit_price)
            attach_algo["tpOrdPx"] = "-1"
            attach_algo["tpTriggerPxType"] = "mark"
        if request.stop_loss_price is not None:
            attach_algo["slTriggerPx"] = _number(request.stop_loss_price)
            attach_algo["slOrdPx"] = "-1"
            attach_algo["slTriggerPxType"] = "mark"
        if attach_algo:
            payload["attachAlgoOrds"] = [attach_algo]
        return self._result(await self._request("POST", "/api/v5/trade/order", payload=payload),
                            request.client_order_id, request.instrument.exchange_symbol)

    async def place_entry_order(self, request: OrderRequest) -> OrderResult: return await self._regular(request)

    async def amend_entry_order(self, order_id: str, request: OrderRequest) -> OrderResult:
        request, size = self._normalize(request)
        payload = {"instId":request.instrument.exchange_symbol,"ordId":order_id,"newSz":size}
        if request.price is not None: payload["newPx"] = _number(request.price)
        return self._result(await self._request("POST","/api/v5/trade/amend-order",payload=payload),
                            request.client_order_id, request.instrument.exchange_symbol)

    async def cancel_order(self, order_id: str) -> OrderResult:
        if order_id not in self.order_symbols: await self.get_open_orders()
        symbol = self.order_symbols.get(order_id)
        if not symbol: raise ExchangeError("无法确定 OKX 订单所属合约")
        result = self._result(await self._request("POST","/api/v5/trade/cancel-order",
            payload={"instId":symbol,"ordId":order_id}), "", symbol)
        return replace(result, status="CANCELED")

    async def _conditional(self, request: OrderRequest, prefix: str) -> OrderResult:
        request, size = self._normalize(request)
        if not request.reduce_only or request.price is None: raise ExchangeError("OKX 保护单必须只减仓并提供触发价")
        payload = {"instId":request.instrument.exchange_symbol,"tdMode":request.margin_mode.lower(),
            "side":request.order_side.lower(),"ordType":"conditional","sz":size,"reduceOnly":"true",
            f"{prefix}TriggerPx":_number(request.price),f"{prefix}OrdPx":"-1",f"{prefix}TriggerPxType":"mark"}
        return self._result(await self._request("POST","/api/v5/trade/order-algo",payload=payload),
                            request.client_order_id, request.instrument.exchange_symbol)

    async def place_take_profit(self, request: OrderRequest) -> OrderResult: return await self._conditional(request,"tp")
    async def place_stop_loss(self, request: OrderRequest) -> OrderResult: return await self._conditional(request,"sl")
    async def close_position(self, request: OrderRequest) -> OrderResult:
        return await self._regular(replace(request,order_type="market",price=None,reduce_only=True))

    def _login(self) -> dict:
        timestamp = str(int(time.time()))
        signature = base64.b64encode(hmac.new(self.secret.encode(),
            f"{timestamp}GET/users/self/verify".encode(),hashlib.sha256).digest()).decode()
        return {"op":"login","args":[{"apiKey":self.api_key,"passphrase":self.passphrase,
            "timestamp":timestamp,"sign":signature}]}

    async def _private_stream(self, queue: asyncio.Queue) -> None:
        delay = 1
        while not self._closed:
            try:
                ws = await (await self._get_session()).ws_connect(self.private_ws, heartbeat=20)
                async with ws:
                    await ws.send_json(self._login()); login = await ws.receive_json(timeout=10)
                    if login.get("event") != "login" or login.get("code") != "0":
                        if login.get("code") == "50101":
                            expected = "官方模拟盘 DEMO" if self.demo else "实盘 LIVE"
                            raise ExchangeError(f"OKX API Key 与当前 {expected} 环境不匹配")
                        raise ExchangeError(f"OKX WS 登录失败：{login}")
                    await ws.send_json({"op":"subscribe","args":[{"channel":"orders","instType":"SWAP"},
                        {"channel":"positions","instType":"SWAP"},{"channel":"account"}]})
                    delay = 1
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT: await queue.put(json.loads(message.data))
                        elif message.type in {aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR}: break
            except asyncio.CancelledError: raise
            except Exception as exc:
                logger.warning("OKX WebSocket 断开，%s 秒后重连：%s",delay,exc); await asyncio.sleep(delay); delay=min(delay*2,30)

    async def _public_stream(self, queue: asyncio.Queue) -> None:
        delay = 1
        while not self._closed:
            try:
                if not self.instruments: await self.load_instruments()
                ws = await (await self._get_session()).ws_connect(self.public_ws, heartbeat=20)
                async with ws:
                    assets = {str(v).upper() for v in self.config.get("market_stream_assets", ["BTC","ETH","SOL"])}
                    await ws.send_json({"op":"subscribe","args":[
                        {"channel":"tickers","instId":item.exchange_symbol}
                        for asset,item in self.instruments.items() if asset in assets
                    ]})
                    delay = 1
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT: await queue.put(json.loads(message.data))
                        elif message.type in {aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR}: break
            except asyncio.CancelledError: raise
            except Exception as exc:
                logger.warning("OKX 行情 WebSocket 断开，%s 秒后重连：%s",delay,exc);await asyncio.sleep(delay);delay=min(delay*2,30)

    async def stream_market_and_account_events(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        tasks = [asyncio.create_task(self._private_stream(queue)), asyncio.create_task(self._public_stream(queue))]
        try:
            while not self._closed:
                yield await queue.get()
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        if self.session: await self.session.close()
