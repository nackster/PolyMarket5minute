FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Default: run the live trader
CMD ["python", "real_trade.py", "--mode", "live", "--size", "50"]
