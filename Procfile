web: gunicorn web:app --bind 0.0.0.0:$PORT --workers 2
momentum: python crypto_momentum_bot.py --daemon
scalper: python scalper_bot.py --daemon --capital 25000 --leverage 5
