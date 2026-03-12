web: gunicorn web:app --bind 0.0.0.0:$PORT --workers 2
worker: python real_trade.py --mode live --size 50
