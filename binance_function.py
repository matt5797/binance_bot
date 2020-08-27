import datetime
import talib
import pandas as pd
import numpy as np
import ccxt
import time
import json
import sys
import telepot
import math

import logging
import logging.handlers
import traceback
import warnings

warnings.filterwarnings(action='ignore') 

import gspread
from oauth2client.service_account import ServiceAccountCredentials

#from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler

class Trading():
    def __init__(self, setting_file):
        self.setting_file = setting_file    #"data/setting.json"
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        apiKey = setting['apiKey']
        secret = setting['secret']
        self.future = ccxt.binance({
            'apiKey': apiKey,
            'secret': secret,
            'options': { 'defaultMarket': 'futures' },
            'urls': {'api': {'public': 'https://fapi.binance.com/fapi/v1',
                             'private': 'https://fapi.binance.com/fapi/v1',},}
        })
        self.spot = ccxt.binance({
            'apiKey': apiKey,
            'secret': secret,
        })
        
        self.name = setting['name']
        self.order_file = setting['order_file']
        
        
        ## log module init
        self.log = logging.getLogger(self.name)
        self.log.setLevel(logging.DEBUG)
        
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] ('+self.name+':%(filename)s:%(lineno)d) > %(message)s')
        
        log_max_size = 10 * 1024 * 1024    #10MB
        log_file_count = 10
        fileHandler = logging.handlers.RotatingFileHandler(filename='log/%s.log' % self.name, maxBytes=log_max_size, backupCount=log_file_count)
        fileHandler.setFormatter(formatter)
        self.log.addHandler(fileHandler)
        
        ## telegram chat_id
        self.telegram_token = setting['telegram_token']    #텔레그램봇토큰
        self.chat_id = setting['chat_id']                #알림방
        self.chat_id_main = setting['chat_id_main']
        self.chat_id_error = setting['chat_id_error']    #에러방
        
        ## strategy setting init
        self.except_market = setting['except_market'] #['BNB/USDT','ONT/USDT','IOTA/USDT']
        self.market_list = setting['market_list']
        self.rebalance_market = setting['rebalance']
        #self.market_list = list(self.future.fetch_tickers().keys())    #현재가능마켓
        for market in self.except_market:
            if market in self.market_list:
                self.market_list.remove(market)
        
        with open(self.order_file, "r") as f:
            self.orders = json.load(f)
        
        self.obv_length = 13
        self.obv_sma1_length = 14
        self.obv_sma2_length = 21
        
        self.ema1_length = 15
        self.ema2_length = 50
        
        self.HMAPeriods_1 = 5
        self.HMAPeriods_2 = 35
        
        self.base_minute = setting['base_minute']
        
        self.gap = 1    # 1시간봉 ATR에 곱해서 타겟에 증감
        self.leverage = setting['leverage']
        self.leverage_max = setting['leverage_max']
        self.take_profit = 0.2    #0.01이 1%
        self.base = setting['base']
        self.max_base = setting['max_base']
        self.balance = setting['balance']
        self.invest = setting['invest']
        self.max_market = len(self.market_list)

    
    def save_setting(self):
        with open(self.order_file, 'w') as f:
            json.dump(self.orders, f)
    
    def close_position(self, market, side, profit=False):
        price = self.get_price(market, side, profit)
        pre_pos = self.get_position_size(market)
        res = self.future.create_order(
            symbol=market,
            type='limit',
            side=side,
            price=price,
            amount=abs(pre_pos)*2,
            params={"reduceOnly":"true"})
        pnl = round(self.get_pnl(market,side,price),3)
        self.log.info("[포지션 종료] " + market + str(res))
        self.telegram_send("포지션 종료\n" +
                      "방향 : " + side +
                      " 종목 : " + market +
                      " 가격 : " + res['info']['price'] +
                      " 수량 : " + res['info']['origQty'] +
                      " 수익 : " + str(pnl) + " %")
        
    def take_profit_order(self, market, side, price, amount):
        if side == "BUY":
            side = "SELL"
            target_price = round(price * (1+self.take_profit),5)
        elif side == "SELL":
            side = "BUY"
            target_price = round(price * (1-self.take_profit),5)
        res = self.future.create_order(
            symbol=market,
            type='TAKE_PROFIT_MARKET',
            side=side,
            amount=abs(amount)*2,
            params={"reduceOnly":"true","stopPrice":target_price})
        self.log.info("[익절 주문] " + market + str(res))
        
    def close_position_market(self, market, side):
        pre_pos = self.get_position_size(market)
        res = self.future.create_order(
            symbol=market,
            type='market',
            side=side,
            amount=abs(pre_pos)*2,
            params={"reduceOnly":"true"})
        time.sleep(1)
        pnl = round(self.get_realized_pnl(market),3)
        self.log.info("[포지션 종료] " + market + str(res))
        self.telegram_send("포지션 종료\n" +
                      "방향 : " + side +
                      " 종목 : " + market +
                      " 수량 : " + res['info']['origQty'] +
                      " 수익 : " + str(pnl) + " 달러")
        
    def order_first(self):
        for market in self.market_list:
            try:
                leverage = self.leverage
                pre_leverage = self.get_leverage(market)
                orders = None
                pos = self.check_position(market)
                pre_pos = self.get_position_size(market)
                if pos == None:
                    continue
                self.log.debug("[시그널] " + market + ' ' + str(pos))
                self.cancel_order_all(market)
                if abs(pos) == 1:
                    if pre_pos > 0:
                        self.close_position_market(market, "SELL")
                    if pre_pos < 0:
                        self.close_position_market(market, "BUY")
                    self.orders[market] = {'type':int(pos),'state':0}
                    continue
                if abs(pos) == 3:
                    leverage = self.leverage_max
                if pos > 0 and pre_pos <= 0:
                    if pre_pos != 0:
                        self.close_position_market(market, "BUY")
                        self.orders[market] = {'type':int(pos),'state':0}
                    else:
                        self.create_order(market, "BUY", leverage=leverage, take_profit=False)
                        self.orders[market] = {'type':int(pos),'state':1}
                elif pos < 0 and pre_pos >= 0:
                    if pre_pos != 0:
                        self.close_position_market(market, "SELL")
                        self.orders[market] = {'type':int(pos),'state':0}
                    else:
                        self.create_order(market, "SELL", leverage=leverage, take_profit=False)
                        self.orders[market] = {'type':int(pos),'state':1}
            except Exception as ex:
                ex = traceback.format_exc().strip().split('\n')
                self.log.critical(market+" order error!\n"+str(ex))
                self.telegram_error(market+" order error!\n"+str(ex[-4:]))
        self.save_setting()
        self.log.debug("[지갑] " + str(self.future.fapiPrivateGetBalance()))
        
    def check_orders(self):    #type은 pos 즉 시그널, state 0은 포지션종료주문 1은 포지션신규주문 -1은 익절대기중
        change = 0
        for market in self.orders:
            try:
                if self.orders[market]['type'] == 0:# and self.orders[market]['state'] != -1:
                    continue
                res = self.future.fapiPrivate_get_openorders({'symbol':market.replace('/','')})
            except Exception as ex:
                ex = traceback.format_exc().strip().split('\n')
                self.log.critical(market+" order check error!\n"+str(ex))
                continue
            try:
                if len(res) == 0:
                    change = 1
                    if abs(self.orders[market]['type']) == 1 or self.orders[market]['state'] == 1:
                        self.orders[market] = {'type':0,'state':0}
                    elif abs(self.orders[market]['type']) in [2,3] and self.orders[market]['state'] == 0:
                        if abs(self.orders[market]['type']) == 2:
                            leverage = self.leverage
                        elif abs(self.orders[market]['type']) == 3:
                            leverage = self.leverage_max
                        if self.orders[market]['type'] > 0:
                            self.create_order(market, "BUY", leverage=leverage, take_profit=False)
                        elif self.orders[market]['type'] < 0:
                            self.create_order(market, "SELL", leverage=leverage, take_profit=False)
                        self.orders[market] = {'type':0,'state':-1}
            except Exception as ex:
                ex = traceback.format_exc().strip().split('\n')
                self.log.critical(market+" order check error!\n"+str(ex))
                self.telegram_error(market+" order check error!\n"+str(ex[-4:]))
            finally:
                if change == 1:
                    self.save_setting()
                    change = 0
            
    def check_contract(self, runtype):    #체결 확인
        return
            
    def order_second(self):
        orders = self.future.fapiPrivateGetOpenOrders()
        txt = "미청산 주문 : "
        for order in orders:
            try:
                market = order['symbol'][:-4] + "/USDT"
                if order['type']=="LIMIT" and order['reduceOnly'] == True:
                    if abs(self.orders[market]['type']) == 0 or self.orders[market]['state'] == 0:
                        continue
                    self.cancel_order(order)
                    for market in self.market_list:
                        if order['symbol'] == market.replace('/',''):
                            self.close_position(market, order['side'])
                    txt = txt + order['symbol'] + ', '
            except Exception as ex:
                ex = traceback.format_exc().strip().split('\n')
                self.log.critical(market+" order error!\n"+str(ex))
                self.telegram_error(market+" order error!\n"+str(ex[-4:]))
        if txt == "미청산 주문 : ":
            return
        self.log.debug("[미청산 주문] " + str(orders))
        self.telegram_send(txt)
        self.save_setting()
        
    def order_third(self):
        orders = self.future.fapiPrivateGetOpenOrders()
        txt = "미청산 주문 : "
        for order in orders:
            try:
                market = order['symbol'][:-4] + "/USDT"
                if order['type']=="LIMIT" and order['reduceOnly'] == True:
                    if abs(self.orders[market]['type']) == 0 or self.orders[market]['state'] == 0:
                        continue
                    self.cancel_order(order)
                    for market in self.market_list:
                        if order['symbol'] == market.replace('/',''):
                            self.close_position_market(market, order['side'])
                    txt = txt + order['symbol'] + ', '
            except Exception as ex:
                ex = traceback.format_exc().strip().split('\n')
                self.log.critical(market+" order error!"+str(ex))
                self.telegram_error(market+" order error!\n"+str(ex[-4:]))
        if txt == "미청산 주문 : ":
            return
        self.log.debug("[미청산 주문] " + str(orders))
        self.telegram_send(txt)
        
    def order_last(self):
        orders = self.future.fapiPrivateGetOpenOrders()
        txt = "미청산 주문 : "
        for order in orders:
            try:
                if order['reduceOnly'] != True:
                    self.cancel_order(order)
                    amount = float(order['origQty']) - float(order['executedQty'])
                    for market in self.market_list:
                        if order['symbol'] == market.replace('/',''):
                            self.create_market_order(market, order['side'], amount=amount)
                    txt = txt + order['symbol'] + ', '
            except Exception as ex:
                ex = traceback.format_exc().strip().split('\n')
                self.log.critical(market+" order error!\n"+str(ex))
                self.telegram_error(market+" order error!\n"+str(ex[-4:]))
        if txt == "미청산 주문 : ":
            return
        self.log.debug("[최종 미청산 주문] " + str(orders))
        self.telegram_send(txt)
    
    def create_order(self, market, side, profit=False, amount=False, leverage=None, take_profit=False):
        price = self.get_price(market, side, profit)
        if not(amount):
            amount = self.get_amount(market, side, price, leverage)
        if leverage == None:
            leverage = self.get_leverage(market)
        else:
            self.future.fapiPrivatePostLeverage({"symbol":market.replace('/',''),"leverage":leverage})
        res = self.future.create_order(
            symbol=market,
            type='limit',
            side=side,
            price=price,
            amount=amount,)
        pnl = round(self.get_pnl(market,side,price),3)
        self.log.info("[포지션 주문] " + market + str(res))
        self.telegram_send("포지션 주문\n" + 
                      "방향 : " + side +
                      " 종목 : " + market +
                      " 가격 : " + res['info']['price'] +
                      " 수량 : " + res['info']['origQty'] +
                      " 수익 : " + str(pnl) + " %" +
                      " 레버리지 : " + str(leverage))
        if take_profit:
            self.take_profit_order(market,side,price,amount)
        
    def create_market_order(self, market, side, amount=False, leverage=None):
        if not(amount):
            amount = self.get_amount(market, side, price, leverage)
        if leverage == None:
            leverage = self.get_leverage(market)
        else:
            self.future.fapiPrivatePostLeverage({"symbol":market.replace('/',''),"leverage":leverage})
        res = self.future.create_order(
            symbol=market,
            type='market',
            side=side,
            amount=amount,)
        self.log.info("[포지션 주문] " + market + str(res))
        self.telegram_send("포지션 주문\n" + 
                      "방향 : " + side +
                      " 종목 : " + market +
                      " 수량 : " + res['info']['origQty'] +
                      " 레버리지 : " + str(leverage))

    def get_balance(self, asset):
        balance = self.future.fapiPrivateGetBalance()
        for row in balance:
            if row['asset'] == asset:
                return float(row['balance'])
    
    def cancel_order_all(self,market):
        market = market.replace('/','')
        orders = self.future.fapiPrivateGetOpenOrders({'symbol':market})
        for order in orders:
            self.cancel_order(order)
        
        
    def cancel_order(self, order):
        res = self.future.fapiPrivate_delete_order({'symbol':order['symbol'], 'orderId':order['orderId']})
        self.log.info("[주문 취소] " + str(res))
        self.telegram_send("주문 취소 : " + res['symbol'])
        
    def get_leverage(self,market):
        market = market.replace('/','')
        position = self.future.fapiPrivateGetPositionRisk()
        for i in position:
            if i['symbol']==market:
                return int(i['leverage'])
        
    def get_entry_price(self,market):
        market = market.replace('/','')
        position = self.future.fapiPrivateGetPositionRisk()
        for i in position:
            if i['symbol']==market:
                return float(i['entryPrice'])
        return 0
        
    def get_position_size(self,market):
        market = market.replace('/','')
        position = self.future.fapiPrivateGetPositionRisk()
        pos = 0
        for i in position:
            if i['symbol']==market:
                if float(i['entryPrice']) == 0:
                    return pos
                pos = (float(i['isolatedMargin'])-float(i['unRealizedProfit'])) * int(i['leverage']) / float(i['entryPrice'])
                if (float(i['entryPrice'])-float(i['liquidationPrice'])) > 0:
                    return pos
                elif (float(i['entryPrice'])-float(i['liquidationPrice'])) < 0:
                    return pos * -1
        return 0
    
    def get_pnl(self, market, side, price):
        market = market.replace('/','')
        position = self.future.fapiPrivateGetPositionRisk()
        for i in position:
            if i['symbol']==market and float(i['isolatedMargin']) != 0:
                entryPrice = float(i['entryPrice'])
                leverage = int(i['leverage'])
                if side == "SELL":
                    return ((price / entryPrice) - 1) * 100 * leverage
                elif side == "BUY":
                    return ((entryPrice / price) - 1) * 100 * leverage
        return 0
    
    def get_realized_pnl(self, market):
        market = market.replace('/','')
        position = self.future.fapiPrivate_get_income({'symbol':market,'limit':30})
        if len(position)==0:
            return 0
        position.reverse()
        cnt = 0
        acc = 0
        for i in position:
            if i['info'] == "REALIZED_PNL":
                cnt += 1
            if cnt == 1:
                acc += float(i['income'])
            if cnt == 2:
                break
        return acc
    
    def get_amount(self, market, side, price, leverage=None):
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        if leverage == None:
            leverage = self.leverage
        self.rebalance_market = setting['rebalance']
        if market in self.rebalance_market:
            setting['rebalance'].remove(market)
            with open(self.setting_file, 'w') as f:
                json.dump(setting, f)
            pos = self.get_position_size(market)
            if pos == 0:
                amount = (self.invest[market]/price*leverage)
                return amount
            position = self.future.fapiPrivateGetPositionRisk()
            for i in position:
                if i['symbol']==market.replace('/',''):
                    entryPrice = float(i['entryPrice'])
                    if pos > 0:
                        return pos + (self.invest[market]/price*leverage)
                    else:
                        return abs(pos) + (self.invest[market]/price*leverage)
            return 0
        else:
            pos = self.get_position_size(market)
            if pos == 0:
                amount = (self.invest[market]/price*leverage)
                return amount
            market = market.replace('/','')
            position = self.future.fapiPrivateGetPositionRisk()
            for i in position:
                if i['symbol']==market:
                    entryPrice = float(i['entryPrice'])
                    pre_leverage = int(i['leverage'])    #무용지물 수정필
                    if pos > 0:
                        return pos + ((((((price / entryPrice)-1)*pre_leverage) + 1) * pos) * leverage / pre_leverage)
                    else:
                        return abs(pos) + ((((((entryPrice / price)-1)*pre_leverage) + 1) * abs(pos)) * leverage / pre_leverage)
            return 0
    
    def get_price(self, market, side, profit=False):
        book = self.future.fetch_order_book(market)
        if side == "BUY":
            side = 'bids'
        elif side == "SELL":
            side = 'asks'
        if profit:
            price = float(book[side][profit][0])
        else:
            price = float(book[side][self.gap][0])
        return price
    
    def get_price_old(self, market, side, profit=False):
        book = self.future.fetch_order_book(market)
        price = (book['bids'][0][0] + book['asks'][0][0]) / 2
        if profit:
            kline = self.future.fetch_ohlcv(market, '1h', limit=3)[-2]
            gap = (kline[2] - kline[3]) * profit
            #gap = profit
        elif self.future.has['fetchOHLCV']:
            kline = self.future.fetch_ohlcv(market, '1h', limit=3)[-2]
            gap = (kline[2] - kline[3]) * self.gap
        else:
            gap = price * 0.001
        if side == "BUY":
            price = price - gap
        elif side == "SELL":
            price = price + gap
        return price
    
    def custom_setting(self, market):
        pass
    
    def check_position(self, market):
        self.custom_setting(market)
        klines = self.spot.fetch_ohlcv(market, '15m', limit=1000)
        klines = pd.DataFrame(klines, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        
        klines['datetime'] = pd.to_datetime((klines['datetime']*1000000)+(32400000000000))
        klines = klines.set_index('datetime')
        
        conversion = {'open' : 'first', 'high' : 'max', 'low' : 'min', 'close' : 'last', 'volume' : 'sum'}
        klines = klines.resample('60Min', how=conversion, base=self.base_minute)
        klines = klines[:-1].dropna(axis=0)
        
        klines['ohlc4'] = (klines['open'] + klines['high'] + klines['low'] + klines['close']) / 4
    
        klines['x'] = klines['volume'] * np.sign(klines['ohlc4'] - klines['ohlc4'].shift(1))
        klines['sum_x'] = klines.x.rolling(window=self.obv_length).sum()
        klines['sum_y'] = klines['volume'].rolling(window=self.obv_length).sum()
        klines['obv'] = klines['sum_x'] / klines['sum_y']
        
        klines['sma1'] = talib.SMA(klines['obv'].values, timeperiod=self.obv_sma1_length)
        klines['sma2'] = talib.SMA(klines['obv'].values, timeperiod=self.obv_sma2_length)
        
        klines['ema1'] = talib.EMA(klines['ohlc4'].values, timeperiod=self.ema1_length)
        klines['ema2'] = talib.EMA(klines['ohlc4'].values, timeperiod=self.ema2_length)
        
        wmaA = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_1/2) * 2
        wmaB = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_1)
        wmaDiffs = wmaA - wmaB 
        klines['hma1'] = talib.WMA(wmaDiffs, timeperiod=math.sqrt(self.HMAPeriods_1))
        
        wmaA = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_2/2) * 2
        wmaB = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_2)
        wmaDiffs = wmaA - wmaB 
        klines['hma2'] = talib.WMA(wmaDiffs, timeperiod=math.sqrt(self.HMAPeriods_2))
        
        klines['signal'] = np.where((klines['sma1'].shift(1) < klines['sma2'].shift(1)) & (klines['sma1'] > klines['sma2']), 1, 0)
        klines['signal'] = np.where((klines['sma1'].shift(1) > klines['sma2'].shift(1)) & (klines['sma1'] < klines['sma2']), -1, klines['signal'])
        
        klines['signal'] = np.where((klines['signal']==1) & (klines['ema1'] > klines['ema2']) & (klines['hma1'] > klines['hma2']), 3, klines['signal'])
        klines['signal'] = np.where((klines['signal']==-1) & (klines['ema1'] < klines['ema2']) & (klines['hma1'] < klines['hma2']), -3, klines['signal'])
        
        klines['signal'] = np.where((klines['signal']==1) & (klines['hma1'] > klines['hma2']), 2, klines['signal'])
        klines['signal'] = np.where((klines['signal']==-1) & (klines['hma1'] < klines['hma2']), -2, klines['signal'])
        
        signal = klines.tail(1)['signal'].values[0]
        if signal == 0:
            return None
        return signal
        
    def rebalance(self):
        self.telegram_send("리벨런싱 시작!", on_main=True)
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        market_list = list(self.future.fetch_tickers().keys())
        except_market = []
        for market in market_list:
            if market not in setting['market_list'] or market in setting['except_market']:
                except_market.append(market)
        setting['except_market'] = except_market
        setting['rebalance'] = market_list
        setting['market_list'] = market_list
        setting['base'] = self.get_balance('USDT')
        if setting['base'] > self.max_base:
            setting['base'] = self.max_base
        invest = {}
        for i in market_list:
            invest[i] = round(setting['base'] * self.balance / (len(market_list)-len(except_market)),3)
        setting["invest"] = invest
        with open(self.setting_file,'w') as f:
            json.dump(setting, f)
        for market in market_list:
            market = market.replace('/','')
            try:
                #self.future.fapiPrivatePostLeverage({"symbol":market,"leverage":setting['leverage']})
                self.future.fapiPrivatePostMarginType({"symbol":market,"marginType":'ISOLATED'})
            except Exception as ex:
                print(market+str(ex))
        self.telegram_send("리벨런싱 완료!\n총마켓수 : "+str(len(market_list))+"종목\n지정마켓수 : "+str((len(market_list)-len(except_market)))+"종목\n총투자금 : "+str(round(setting['base']))+"달러\n종목별투자금 : "+str(round(setting['base']*self.balance/(len(market_list)-len(except_market))))+"달러\n현금  비율 : "+str(round((1-setting['balance'])*100))+"%\n기본 레버리지 : "+str(setting['leverage'])+"배\n최대 레버리지 : "+str(setting['leverage_max'])+"배", on_main=True)
        self.log.info("[리벨런싱 완료] " + str(setting))
        self.__init__(self.setting_file)
        
    def rebalance_mini(self):
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        setting['base'] = self.get_balance('USDT')
        if setting['base'] > self.max_base:
            setting['base'] = self.max_base
        for i in self.market_list:
            self.invest[i] = round(setting['base'] * self.balance / self.max_market ,3)
        setting["invest"] = self.invest
        with open(self.setting_file,'w') as f:
            json.dump(setting, f)
        self.log.info("[투자금 리벨런싱 완료] " + str(setting))
        self.__init__(self.setting_file)
        
    def report(self):
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        today = datetime.datetime.today().strftime('%y/%m/%d %H:%M')
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_name('data/googleSpread.json', scope)
        gc = gspread.authorize(credentials)
        report = gc.open('Binance_report').worksheet(setting["worksheet"])
        res = report.append_row([today,self.get_balance('USDT')])
        self.log.info("[보고서 갱신] " + str(res))
        
    def report_balance(self):
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        today = datetime.datetime.today().strftime('%y/%m/%d %H:%M')
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_name('data/googleSpread.json', scope)
        gc = gspread.authorize(credentials)
        report = gc.open('Binance_report').worksheet(setting["worksheet"]+"_b")
        
        position = self.future.fapiPrivateGetPositionRisk()
        res_list = []
        for market in list(self.future.fetch_tickers().keys()):
            for i in position:
                if i['symbol'] == market.replace('/',""):
                    res_list += [market, int(i['leverage']), i['marginType'], float(i['liquidationPrice']), float(i['entryPrice']), float(i['markPrice']), float(i['positionAmt']), float(i['isolatedMargin']), float(i['unRealizedProfit'])]
                    
        cell_list = report.range('A2:I'+str(len(position)+1))
        for index, cell in enumerate(cell_list):
            cell.value = res_list[index]
        report.update_cells(cell_list)
        
        res = self.future.fapiPrivateGetBalance()
        for i in res:
            if i['asset'] == "USDT":
                report.update_acell('P2', float(i['balance']))
                report.update_acell('Q2', float(i['withdrawAvailable']))
                report.update_acell('M2', today)

    def report_balance_clear(self):
        with open(self.setting_file, "r") as f:
            setting = json.load(f)
        today = datetime.datetime.today().strftime('%y/%m/%d %H:%M')
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_name('data/googleSpread.json', scope)
        gc = gspread.authorize(credentials)
        report = gc.open('Binance_report').worksheet(setting["worksheet"]+"_b")

        position = self.future.fapiPrivateGetPositionRisk()
        res_list = []
        for market in list(self.future.fetch_tickers().keys()):
            for i in position:
                res_list += ['', '', '', '', '', '', '', '', '']

        cell_list = report.range('A2:I'+str(len(position)+2))
        for index, cell in enumerate(cell_list):
            cell.value = res_list[index]
        report.update_cells(cell_list)

    def check_position_history(self, market):
        self.custom_setting(market)
        klines = self.spot.fetch_ohlcv(market, '15m', limit=1000)
        klines = pd.DataFrame(klines, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        
        klines['datetime'] = pd.to_datetime((klines['datetime']*1000000)+(32400000000000))
        klines = klines.set_index('datetime')
        
        conversion = {'open' : 'first', 'high' : 'max', 'low' : 'min', 'close' : 'last', 'volume' : 'sum'}
        klines = klines.resample('60Min', how=conversion, base=self.base_minute)
        klines = klines[:-1].dropna(axis=0)
        
        klines['ohlc4'] = (klines['open'] + klines['high'] + klines['low'] + klines['close']) / 4
    
        klines['x'] = klines['volume'] * np.sign(klines['ohlc4'] - klines['ohlc4'].shift(1))
        klines['sum_x'] = klines.x.rolling(window=self.obv_length).sum()
        klines['sum_y'] = klines['volume'].rolling(window=self.obv_length).sum()
        klines['obv'] = klines['sum_x'] / klines['sum_y']
        
        klines['sma1'] = talib.SMA(klines['obv'].values, timeperiod=self.obv_sma1_length)
        klines['sma2'] = talib.SMA(klines['obv'].values, timeperiod=self.obv_sma2_length)
        
        klines['ema1'] = talib.EMA(klines['ohlc4'].values, timeperiod=self.ema1_length)
        klines['ema2'] = talib.EMA(klines['ohlc4'].values, timeperiod=self.ema2_length)
        
        wmaA = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_1/2) * 2
        wmaB = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_1)
        wmaDiffs = wmaA - wmaB 
        klines['hma1'] = talib.WMA(wmaDiffs, timeperiod=math.sqrt(self.HMAPeriods_1))
        
        wmaA = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_2/2) * 2
        wmaB = talib.WMA(klines['ohlc4'].values, timeperiod=self.HMAPeriods_2)
        wmaDiffs = wmaA - wmaB 
        klines['hma2'] = talib.WMA(wmaDiffs, timeperiod=math.sqrt(self.HMAPeriods_2))
        
        klines['signal'] = np.where((klines['sma1'].shift(1) < klines['sma2'].shift(1)) & (klines['sma1'] > klines['sma2']), 1, 0)
        klines['signal'] = np.where((klines['sma1'].shift(1) > klines['sma2'].shift(1)) & (klines['sma1'] < klines['sma2']), -1, klines['signal'])
        
        klines['signal'] = np.where((klines['signal']==1) & (klines['ema1'] > klines['ema2']) & (klines['hma1'] > klines['hma2']), 3, klines['signal'])
        klines['signal'] = np.where((klines['signal']==-1) & (klines['ema1'] < klines['ema2']) & (klines['hma1'] < klines['hma2']), -3, klines['signal'])
        
        klines['signal'] = np.where((klines['signal']==1) & (klines['hma1'] > klines['hma2']), 2, klines['signal'])
        klines['signal'] = np.where((klines['signal']==-1) & (klines['hma1'] < klines['hma2']), -2, klines['signal'])
        
        klines = klines.query("signal!=0")
        
        klines['profit'] = np.where(klines['signal']<0, klines['open'] / klines['open'].shift(1), klines['open'].shift(1)/klines['open'])
        #klines['profit'] = np.where(abs(klines['signal'].shift(1))>1, klines['profit'], 1)
        klines['acc_profit'] = klines['profit'].cumprod()
        
        print(market)
        print(klines.tail(3))
        
    def telegram_send(self, message, on_main=False):
        my_token = self.telegram_token    #save token
        chat_id = self.chat_id   #chennel
        try:
            bot = telepot.Bot(token=my_token)  #creat bot
            bot.sendMessage(chat_id=chat_id, text=message)   #send message to chennel
            if (on_main):
                bot.sendMessage(chat_id=self.chat_id_main, text=self.name+'\n'+message)
        except Exception as ex:
            ex = traceback.format_exc().strip().split('\n')
            self.log.critical("[텔레그램 에러 발생] "+str(ex))
    
    def telegram_error(self, message): #send telegram message
        my_token = self.telegram_token
        chat_id = self.chat_id_error   #chennel
        try:
            bot = telepot.Bot(token=my_token)  #creat bot
            bot.sendMessage(chat_id=chat_id, text=self.name+'\n'+message)   #send message to chennel
        except Exception as ex:
            ex = traceback.format_exc().strip().split('\n')
            self.log.critical("[텔레그램 에러 발생] "+str(ex))
