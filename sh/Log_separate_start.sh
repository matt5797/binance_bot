#!/bin/bash
cd /workspace/trading2/binance/log
source /root/miniconda3/bin/activate binance
tail -f OBV_1.log OBV_2.log OBV_3.log OBV_4.log | grep --line-buffered CRITICAL >> CRITICAL.log &
tail -f OBV_1.log OBV_2.log OBV_3.log OBV_4.log | grep --line-buffered INFO >> INFO.log &