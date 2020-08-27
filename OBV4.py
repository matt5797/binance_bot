from binance_function import *

class Trading(Trading):
    def __init__(self, setting_file):
        super().__init__(setting_file)
        
        self.obv_length = 15
        self.obv_sma1_length = 10
        self.obv_sma2_length = 20
        
        self.ema1_length = 5
        self.ema2_length = 50
        
        self.HMAPeriods_1 = 5
        self.HMAPeriods_2 = 50

bot = Trading("data/OBV4.json")
        
sched = BackgroundScheduler()
sched.add_job(bot.check_orders, 'cron', minute='*')
sched.add_job(bot.order_first, 'cron', hour='*', minute='01')    #46
sched.add_job(bot.order_second, 'cron', hour='*', minute='25')    #10
sched.add_job(bot.order_third, 'cron', hour='*', minute='45')    #30
#sched.add_job(bot.order_last, 'cron', hour='*', minute='40')

#sched.add_job(bot.check_contract, 'cron', hour='*', minute='47', args=[0])
#sched.add_job(bot.check_contract, 'cron', hour='*', minute='08', args=[1])
#sched.add_job(bot.check_contract, 'cron', hour='*', minute='28', args=[2])
#sched.add_job(bot.check_contract, 'cron', hour='*', minute='38', args=[3])

sched.add_job(bot.report_balance, 'cron', hour='*', minute='59')    #42
sched.add_job(bot.report, 'cron', hour='00', minute='08')    #08
sched.add_job(bot.rebalance_mini, 'cron', hour='00', minute='10')    #10
sched.add_job(bot.rebalance, 'cron', day_of_week='mon', hour='00', minute='30')
sched.start()

while True:
    time.sleep(30)

