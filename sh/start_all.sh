#!/bin/bash
cd /workspace/trading2/binance
source /root/miniconda3/bin/activate binance
nohup python3 OBV1.py &
nohup python3 OBV2.py &
nohup python3 OBV3.py &
nohup python3 OBV4.py &
./sh/Log_separate_start.sh