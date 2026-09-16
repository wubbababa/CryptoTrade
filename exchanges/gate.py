"""Gate API v4 USDT 永续官方 REST 与私有 WebSocket 适配器。"""
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

logger=logging.getLogger(__name__)
def _number(v:Decimal)->str:return format(v,"f")

class GateAdapter(ExchangeAdapter):
    def __init__(self,config:dict)->None:
        self.config,self.mode=config,str(config.get("mode","TESTNET")).upper();self.testnet=self.mode=="TESTNET"
        try:self.api_key,self.secret=credentials("GATE",self.mode,"API_KEY","API_SECRET")
        except ValueError as exc:raise ExchangeError(str(exc)) from exc
        self.rest_base=str(config.get("rest_base","https://api-testnet.gateapi.io/api/v4" if self.testnet else "https://api.gateio.ws/api/v4")).rstrip("/")
        self.ws_url=str(config.get("private_ws","wss://ws-testnet.gate.com/v4/ws/futures/usdt" if self.testnet else "wss://fx-ws.gateio.ws/v4/ws/usdt"))
        self.session:aiohttp.ClientSession|None=None;self.instruments={};self.order_symbols={};self._closed=False
        self.user_id: str | None = None
        self._leverage_set: set[str] = set()

    async def _get_session(self):
        if self.session is None or self.session.closed:self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.session
    def _headers(self,method,path,query,body):
        timestamp=str(int(time.time()));hashed=hashlib.sha512(body.encode()).hexdigest()
        sign=hmac.new(self.secret.encode(),f"{method}\n/api/v4{path}\n{query}\n{hashed}\n{timestamp}".encode(),hashlib.sha512).hexdigest()
        return {"KEY":self.api_key,"Timestamp":timestamp,"SIGN":sign,"Content-Type":"application/json",
                "X-Gate-Size-Decimal":"1"}
    async def _request(self,method,path,params=None,payload=None,private=False):
        query=urlencode(params or {});body=json.dumps(payload,separators=(",",":")) if payload is not None else ""
        headers=self._headers(method,path,query,body) if private else {}
        url=self.rest_base+path+("?"+query if query else "")
        async with (await self._get_session()).request(method,url,data=body or None,headers=headers) as response:
            try:result=await response.json()
            except (aiohttp.ContentTypeError,json.JSONDecodeError) as exc:raise ExchangeError(f"Gate HTTP {response.status} 返回非 JSON") from exc
        if response.status>=400:
            raise ExchangeError(f"Gate API 错误 {response.status}: {result.get('label') if isinstance(result,dict) else ''} {result.get('message') if isinstance(result,dict) else result}")
        return result
    async def load_instruments(self):
        rows=await self._request("GET","/futures/usdt/contracts");result={}
        for row in rows:
            if row.get("in_delisting") or not row.get("name","").endswith("_USDT"):continue
            asset=row["name"].removesuffix("_USDT");mult=Decimal(row.get("quanto_multiplier") or "1")
            result[asset]=Instrument(f"{asset}/USDT:PERP",row["name"],Decimal(row.get("order_price_round") or "1"),mult,mult,Decimal("0"),mult)
        self.instruments=result;return result
    async def get_equity(self):
        row=await self._request("GET","/futures/usdt/accounts",private=True)
        self.user_id = str(row.get("user")) if row.get("user") is not None else None
        return Decimal(row.get("total") or "0")
    async def get_open_orders(self):
        rows=await self._request("GET","/futures/usdt/orders",{"status":"open"},private=True);result=[]
        for row in rows:
            oid=str(row["id"]);self.order_symbols[oid]=row["contract"]
            result.append(OrderResult(oid,row.get("text","").removeprefix("t-"),row.get("status",""),row))
        return result
    async def get_positions(self):
        if not self.instruments:await self.load_instruments()
        by_symbol={v.exchange_symbol:v for v in self.instruments.values()};rows=await self._request("GET","/futures/usdt/positions",private=True);result=[]
        for row in rows:
            size=Decimal(str(row.get("size",0)));instrument=by_symbol.get(row.get("contract"))
            if size==0 or not instrument:continue
            result.append(PositionSnapshot(Exchange.GATE,instrument.instrument_key,PositionSide.LONG if size>0 else PositionSide.SHORT,abs(size)*instrument.contract_multiplier,Decimal(str(row.get("entry_price",0)))))
        return result
    def _normalize(self,r):
        i=r.instrument;q=round_step(r.quantity,i.quantity_step);p=round_step(r.price,i.tick_size) if r.price is not None else None
        if q<i.minimum_quantity:raise ExchangeError("Gate 下单数量低于最小数量")
        return replace(r,quantity=q,price=p),q/i.contract_multiplier
    def _result(self,row,client=""):
        oid=str(row.get("id") or row.get("order_id", ""));symbol=row.get("contract","")
        if oid:self.order_symbols[oid]=symbol
        return OrderResult(oid,row.get("text","").removeprefix("t-") or client,row.get("status","open"),row)
    async def _regular(self,r):
        r,contracts=self._normalize(r);await self._ensure_leverage(r);size=contracts if r.order_side=="BUY" else -contracts
        payload={"contract":r.instrument.exchange_symbol,"size":_number(size),"price":_number(r.price) if r.price is not None else "0","tif":"gtc" if r.price is not None else "ioc","text":"t-"+r.client_order_id[:28],"reduce_only":r.reduce_only}
        return self._result(await self._request("POST","/futures/usdt/orders",payload=payload,private=True),r.client_order_id)
    async def _ensure_leverage(self,r):
        symbol=r.instrument.exchange_symbol
        if symbol in self._leverage_set:return
        # Gate 以 leverage=0 表示全仓，cross_leverage_limit 设置全仓杠杆上限。
        await self._request("POST",f"/futures/usdt/positions/{symbol}/leverage",
            {"leverage":"0","cross_leverage_limit":_number(r.leverage)},private=True)
        self._leverage_set.add(symbol)
    async def place_entry_order(self,r):return await self._regular(r)
    async def amend_entry_order(self,oid,r):
        r,contracts=self._normalize(r);payload={"size":_number(contracts if r.order_side=="BUY" else -contracts),"price":_number(r.price)}
        return self._result(await self._request("PUT",f"/futures/usdt/orders/{oid}",payload=payload,private=True),r.client_order_id)
    async def cancel_order(self,oid):
        row=await self._request("DELETE",f"/futures/usdt/orders/{oid}",private=True)
        return replace(self._result(row),status="finished")
    async def _conditional(self,r,rule):
        r,contracts=self._normalize(r)
        if not r.reduce_only or r.price is None:raise ExchangeError("Gate 保护单必须只减仓并提供触发价")
        size=contracts if r.order_side=="BUY" else -contracts
        payload={"initial":{"contract":r.instrument.exchange_symbol,"size":_number(size),"price":"0","tif":"ioc","text":"t-"+r.client_order_id[:28],"reduce_only":True},"trigger":{"strategy_type":0,"price_type":1,"price":_number(r.price),"rule":rule,"expiration":86400}}
        return self._result(await self._request("POST","/futures/usdt/price_orders",payload=payload,private=True),r.client_order_id)
    async def place_take_profit(self,r):
        rule=1 if r.position_side==PositionSide.LONG else 2
        return await self._conditional(r,rule)
    async def place_stop_loss(self,r):
        rule=2 if r.position_side==PositionSide.LONG else 1
        return await self._conditional(r,rule)
    async def close_position(self,r):return await self._regular(replace(r,order_type="MARKET",price=None,reduce_only=True))
    def _auth(self,channel,event,timestamp):
        sign=hmac.new(self.secret.encode(),f"channel={channel}&event={event}&time={timestamp}".encode(),hashlib.sha512).hexdigest()
        return {"method":"api_key","KEY":self.api_key,"SIGN":sign}
    async def stream_market_and_account_events(self)->AsyncIterator[dict]:
        delay=1
        while not self._closed:
            try:
                ws=await (await self._get_session()).ws_connect(self.ws_url,heartbeat=20)
                async with ws:
                    if self.user_id is None:
                        await self.get_equity()
                    if self.user_id is None:
                        raise ExchangeError("Gate 账户响应缺少用户 ID，无法订阅私有 WebSocket")
                    now=int(time.time())
                    for channel in ("futures.orders","futures.positions","futures.balances"):
                        payload = [self.user_id] if channel == "futures.balances" else [self.user_id, "!all"]
                        await ws.send_json({"time":now,"channel":channel,"event":"subscribe","payload":payload,"auth":self._auth(channel,"subscribe",now)})
                    # 公共 ticker 推送用于实时价格与自动保本判断。
                    await ws.send_json({"time":now,"channel":"futures.tickers","event":"subscribe","payload":["!all"]})
                    delay=1
                    async for message in ws:
                        if message.type==aiohttp.WSMsgType.TEXT:yield json.loads(message.data)
                        elif message.type in {aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR}:break
            except asyncio.CancelledError:raise
            except Exception as exc:logger.warning("Gate WebSocket 断开，%s 秒后重连：%s",delay,exc);await asyncio.sleep(delay);delay=min(delay*2,30)
    async def close(self):
        self._closed=True
        if self.session:await self.session.close()
