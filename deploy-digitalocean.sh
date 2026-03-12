#!/bin/bash
# DigitalOcean Droplet Setup Script
# Run this on a fresh Ubuntu 24.04 droplet (London/Singapore/Amsterdam)
#
# Usage:
#   1. Create a $6/mo droplet (1 vCPU, 1GB RAM) in London/Amsterdam/Singapore
#   2. SSH in: ssh root@<droplet-ip>
#   3. Run: bash deploy-digitalocean.sh
#   4. Edit .env with your keys
#   5. Run: docker compose up -d

set -e

echo "=== Installing Docker ==="
apt-get update
apt-get install -y docker.io docker-compose-v2 git

echo "=== Cloning repo ==="
cd /opt
git clone https://github.com/nackster/PolyMarket5minute.git trader
cd trader

echo "=== Creating .env file ==="
cat > .env << 'ENVEOF'
# Fill in your keys:
POLYMARKET_PRIVATE_KEY=
POLYMARKET_API_KEY=

# Heroku Postgres DATABASE_URL (copy from: heroku config:get DATABASE_URL)
DATABASE_URL=

# Binance (global works from EU/Asia servers)
BINANCE_WS_URL=wss://stream.binance.com:9443/ws/btcusdt@trade
ENVEOF

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/trader/.env with your keys:"
echo "     nano /opt/trader/.env"
echo ""
echo "  2. Get your DATABASE_URL from Heroku:"
echo "     heroku config:get DATABASE_URL -a polymarket-5min-trader"
echo ""
echo "  3. Start the trader:"
echo "     cd /opt/trader && docker compose up -d"
echo ""
echo "  4. Check logs:"
echo "     docker compose logs -f trader"
echo ""
echo "  5. To stop:"
echo "     docker compose down"
