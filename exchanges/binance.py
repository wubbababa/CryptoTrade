"""Binance USDⓈ-M 官方 REST 与用户数据 WebSocket 适配器。"""
from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, os, time
from dataclasses import replace
from decimal import Decimal
from typing import AsyncIterator
from urllib.parse import urlencode
import aiohttp
from exchanges.base import ExchangeAdapter, ExchangeError, round_step
from exchanges.credentials import credentials
from models import Exchange, Instrument, OrderRequest, OrderResult, PositionSide, PositionSnapshot

logger = logging.getLogger(__name__)
def _number(v: Decimal) -> str: return format(v, "f")

class BinanceAdapter(ExchangeAdapter):
    def __init__(self, config: dict) -> None:
        self.config, self.mode = config, str(config.get("mode", "TESTNET")).upper()
        self.testnet = self.mode == "TESTNET"
        try:
            self.api_key, self.secret = credentials("BINANCE", self.mode, "API_KEY", "API_SECRET")
        except ValueError as exc:
            raise ExchangeError(str(exc)) from exc
        self.rest_base = str(config.get("rest_base", "https://demo-fapi.binance.com" if self.testnet else "https://fapi.binance.com")).rstrip("/")
        self.ws_base = str(config.get("ws_base", "wss://fstream.binancefuture.com" if self.testnet else "wss://fstream.binance.com")).rstrip("/")
        self.session: aiohttp.ClientSession | None = None
        self.instruments: dict[str, Instrument] = {}
        self.order_symbols: dict[str, str] = {}
        self.algo_orders: set[str] = set()
        self._closed, self._leverage_set = False, set()

    async def _get_session(self):
        if self.session is None or self.session.closed: self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    async def _request(self, method: str, path: str, params: dict | None = None,
                       private: bool = False, api_key_only: bool = False):
        values = {k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in (params or {}).items() if v is not None}
        headers = {}
        if private:
            values.setdefault("timestamp", str(int(time.time()*1000))); values.setdefault("recvWindow","5000")
            values["signature"] = hmac.new(self.secret.encode(), urlencode(values).encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        elif api_key_only:
            headers["X-MBX-APIKEY"] = self.api_key
        session = await self._get_session()
        async with session.request(method, self.rest_base+path, params=values, headers=headers) as response:
            try: result = await response.json()
            except (aiohttp.ContentTypeError,json.JSONDecodeError) as exc: raise ExchangeError(f"Binance HTTP {response.status} 返回非 JSON") from exc
        if response.status >= 400 or isinstance(result,dict) and "code" in result and int(result["code"]) < 0:
            raise ExchangeError(f"Binance API 错误 {result.get('code')}: {result.get('msg')}")
        return result

    async def load_instruments(self):
        data = await self._request("GET","/fapi/v1/exchangeInfo"); result={}
        for row in data.get("symbols",[]):
            if row.get("contractType")!="PERPETUAL" or row.get("quoteAsset")!="USDT" or row.get("status")!="TRADING": continue
            filters={f["filterType"]:f for f in row.get("filters",[])}; price=filters.get("PRICE_FILTER",{}); lot=filters.get("LOT_SIZE",{}); notion=filters.get("MIN_NOTIONAL",{})
            asset=row["baseAsset"]; result[asset]=Instrument(f"{asset}/USDT:PERP",row["symbol"],Decimal(price.get("tickSize","1")),Decimal(lot.get("stepSize","1")),Decimal(lot.get("minQty","0")),Decimal(notion.get("notional","0")))
        self.instruments=result; return result

    async def get_equity(self):
        rows=await self._request("GET","/fapi/v3/balance",private=True)
        item=next((r for r in rows if r.get("asset")=="USDT"),None)
        return Decimal((item or {}).get("balance","0"))

    async def get_open_orders(self):
        rows=await self._request("GET","/fapi/v1/openOrders",private=True); results=[]
        for row in rows:
            oid=str(row["orderId"]); self.order_symbols[oid]=row["symbol"]
            results.append(OrderResult(oid,row.get("clientOrderId",""),self._canonical_status(row.get("status","")),row))
        algos=await self._request("GET","/fapi/v1/openAlgoOrders",private=True)
        for row in algos if isinstance(algos,list) else algos.get("orders",[]):
            oid=str(row["algoId"]); self.order_symbols[oid]=row["symbol"]; self.algo_orders.add(oid)
            results.append(OrderResult(oid,row.get("clientAlgoId",""),self._canonical_status(row.get("algoStatus","NEW")),row))
        return results

    @staticmethod
    def _canonical_status(status):
        """统一 Binance 状态：EXPIRED_IN_MATCH 归并到 EXPIRED，其余保持大写标准值。"""
        value=str(status).upper()
        return "EXPIRED" if value=="EXPIRED_IN_MATCH" else value

    async def get_order(self,client_order_id,exchange_order_id=None,instrument_key=""):
        """按订单号或客户订单号查询单笔订单；订单不存在（-2013）时返回 None。"""
        symbol=""
        if instrument_key:
            asset=instrument_key.split("/",1)[0]
            if not self.instruments:
                await self.load_instruments()
            instrument=self.instruments.get(asset)
            symbol=instrument.exchange_symbol if instrument else f"{asset}USDT"
        if not symbol:
            raise ExchangeError("缺少合约标识，无法查询 Binance 订单")
        params={"symbol":symbol}
        if exchange_order_id and str(exchange_order_id).isdigit():
            params["orderId"]=str(exchange_order_id)
        elif client_order_id:
            params["origClientOrderId"]=client_order_id
        else:
            raise ExchangeError("缺少订单编号，无法查询 Binance 订单")
        try:
            row=await self._request("GET","/fapi/v1/order",params,private=True)
        except ExchangeError as exc:
            if "-2013" in str(exc):
                return None
            raise
        if not isinstance(row,dict) or not row.get("orderId"):
            return None
        oid=str(row["orderId"]); self.order_symbols[oid]=row.get("symbol","")
        return OrderResult(oid,str(row.get("clientOrderId") or client_order_id),
                           self._canonical_status(row.get("status","")),row)

    async def get_positions(self):
        rows=await self._request("GET","/fapi/v3/positionRisk",private=True); results=[]
        for row in rows:
            qty=Decimal(row.get("positionAmt","0"))
            if qty==0: continue
            side=PositionSide.LONG if row.get("positionSide")=="LONG" or qty>0 else PositionSide.SHORT
            asset=row["symbol"].removesuffix("USDT")
            results.append(PositionSnapshot(Exchange.BINANCE,f"{asset}/USDT:PERP",side,abs(qty),Decimal(row.get("entryPrice","0"))))
        return results

    def _normalize(self,r):
        i=r.instrument; q=round_step(r.quantity,i.quantity_step); p=round_step(r.price,i.tick_size) if r.price is not None else None
        if q<i.minimum_quantity: raise ExchangeError("Binance 下单数量低于最小数量")
        if p is not None and i.minimum_notional and q*p<i.minimum_notional: raise ExchangeError("Binance 订单名义价值低于最小值")
        return replace(r,quantity=q,price=p)

    async def _ensure_leverage(self,r):
        symbol=r.instrument.exchange_symbol
        if symbol in self._leverage_set:return
        if r.margin_mode.upper() == "CROSS":
            try:
                await self._request("POST","/fapi/v1/marginType",{"symbol":symbol,"marginType":"CROSSED"},True)
            except ExchangeError as exc:
                # Binance 在已经是全仓时返回 -4046，此状态可安全继续。
                if "No need to change margin type" not in str(exc) and "-4046" not in str(exc): raise
        await self._request("POST","/fapi/v1/leverage",{"symbol":symbol,"leverage":int(r.leverage)},True); self._leverage_set.add(symbol)

    def _result(self,row,client=""):
        oid=str(row.get("orderId") or row.get("algoId")); symbol=row.get("symbol",""); self.order_symbols[oid]=symbol
        if row.get("algoId") is not None:self.algo_orders.add(oid)
        return OrderResult(oid,row.get("clientOrderId") or row.get("clientAlgoId") or client,row.get("status") or row.get("algoStatus","NEW"),row)

    async def _regular(self,r):
        r=self._normalize(r); await self._ensure_leverage(r)
        p={"symbol":r.instrument.exchange_symbol,"side":r.order_side,"type":r.order_type.upper(),"quantity":_number(r.quantity),"newClientOrderId":r.client_order_id}
        if r.order_type.upper()=="LIMIT":p.update({"price":_number(r.price),"timeInForce":"GTC"})
        if r.reduce_only:p["reduceOnly"]="true"
        return self._result(await self._request("POST","/fapi/v1/order",p,True),r.client_order_id)

    async def place_entry_order(self,r):return await self._regular(r)
    async def amend_entry_order(self,oid,r):
        r=self._normalize(r); p={"symbol":r.instrument.exchange_symbol,"orderId":oid,"side":r.order_side,"quantity":_number(r.quantity),"price":_number(r.price)}
        return self._result(await self._request("PUT","/fapi/v1/order",p,True),r.client_order_id)
    async def cancel_order(self,oid):
        if oid not in self.order_symbols:await self.get_open_orders()
        symbol=self.order_symbols.get(oid)
        if not symbol:raise ExchangeError("无法确定 Binance 订单所属合约")
        path="/fapi/v1/algoOrder" if oid in self.algo_orders else "/fapi/v1/order"
        key="algoId" if oid in self.algo_orders else "orderId"
        return replace(self._result(await self._request("DELETE",path,{"symbol":symbol,key:oid},True)),status="CANCELED")
    async def _conditional(self,r,kind):
        r=self._normalize(r)
        # 保护单一律使用「数量 + reduceOnly」：Binance 的 closePosition 全平模式在进场尚未成交时
        # 会被服务端以 -4509（Time in Force GTE can only be used with open positions）拒绝，
        # 且系统始终知道该交易的确切持仓数量，无需全平语义。
        if not r.reduce_only or r.price is None:
            raise ExchangeError("Binance 保护单必须只减仓并提供触发价")
        p={"algoType":"CONDITIONAL","symbol":r.instrument.exchange_symbol,"side":r.order_side,"type":kind,
           "triggerPrice":_number(r.price),"workingType":"MARK_PRICE","clientAlgoId":r.client_order_id,
           "quantity":_number(r.quantity),"reduceOnly":"true"}
        return self._result(await self._request("POST","/fapi/v1/algoOrder",p,True),r.client_order_id)
    async def place_take_profit(self,r):return await self._conditional(r,"TAKE_PROFIT_MARKET")
    async def place_stop_loss(self,r):return await self._conditional(r,"STOP_MARKET")
    async def close_position(self,r):return await self._regular(replace(r,order_type="MARKET",price=None,reduce_only=True))

    async def _keepalive(self):
        while not self._closed:
            await asyncio.sleep(1800)
            try: await self._request("PUT","/fapi/v1/listenKey",api_key_only=True)
            except Exception as exc: logger.warning("Binance listenKey 续期失败：%s",exc)
    async def stream_market_and_account_events(self)->AsyncIterator[dict]:
        delay=1
        while not self._closed:
            keepalive=None
            try:
                data=await self._request("POST","/fapi/v1/listenKey",api_key_only=True); key=data["listenKey"]
                if not self.instruments: await self.load_instruments()
                assets = {str(v).upper() for v in self.config.get("market_stream_assets", ["BTC","ETH","SOL"])}
                market_streams = [f"{item.exchange_symbol.lower()}@markPrice" for asset,item in self.instruments.items() if asset in assets]
                stream_names = "/".join([key, *market_streams])
                ws=await (await self._get_session()).ws_connect(f"{self.ws_base}/stream?streams={stream_names}",heartbeat=20); keepalive=asyncio.create_task(self._keepalive())
                async with ws:
                    delay=1
                    async for message in ws:
                        if message.type==aiohttp.WSMsgType.TEXT:yield json.loads(message.data)
                        elif message.type in {aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR}:break
            except asyncio.CancelledError:raise
            except Exception as exc:logger.warning("Binance WebSocket 断开，%s 秒后重连：%s",delay,exc);await asyncio.sleep(delay);delay=min(delay*2,30)
            finally:
                if keepalive:keepalive.cancel()
    async def close(self):
        self._closed=True
        if self.session:await self.session.close()
