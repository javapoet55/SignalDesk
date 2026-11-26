"""Background Finviz marks for display; never changes saved fills or realized statistics."""
import datetime as dt
import math
import re
import threading
import time
import finviz_prices as fvp

class OpenTradeMarks:
    def __init__(self, client):
        self.client=client;self.lock=threading.Lock();self.quotes={};self.attempts={};self.busy=False
    def snapshot(self, trades):
        symbols=sorted({t['symbol'] for t in trades if re.fullmatch(r'[A-Z0-9.\-]{1,10}',t['symbol'])})
        with self.lock:
            now=time.time()
            wanted=[s for s in symbols if now-self.attempts.get(s,0)>=60]
            if self.client is not None and wanted and not self.busy:
                self.busy=True
                threading.Thread(target=self._refresh,args=(wanted,),daemon=True).start()
            return {s:dict(self.quotes[s],stale=now-self.quotes[s]['fetched']>120) for s in symbols if s in self.quotes},self.busy
    def _refresh(self,symbols):
        try:
            for symbol in symbols:
                with self.lock:self.attempts[symbol]=time.time()
                try:
                    bars,_=fvp.parse_quote_csv(self.client.quote_history(symbol,'d'))
                    days=sorted(bars);day=days[-1];price=float(bars[day]['c'])
                    previous_close=float(bars[days[-2]]['c']) if len(days)>1 else None
                    today_pct=(price/previous_close-1)*100 if previous_close and previous_close>0 else None
                    if not math.isfinite(price) or price<=0:continue
                    with self.lock:self.quotes[symbol]=dict(price=price,day=day,fetched=time.time(),today_pct=today_pct)
                except Exception:
                    # Keep prior marks with their fetch time; never log credentials or provider responses.
                    continue
        finally:
            with self.lock:self.busy=False

def valuation(trade,quote):
    if trade['status']!='open' or not quote:return None,None
    qty=trade.get('open_qty')
    qty=trade['qty'] if qty is None else qty
    basis=trade.get('mark_basis',trade['entry_price'])
    gross=trade.get('realized_gross',0.0)+(quote['price']-basis)*qty*(1 if trade['side']=='long' else -1)
    net=gross-trade['fees'];cost=trade['entry_price']*trade['qty']
    return net,net/cost*100 if cost else None
