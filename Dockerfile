FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py screens.json ./
COPY paper_trading ./paper_trading
COPY vbt_service/__init__.py vbt_service/performance.py ./vbt_service/
CMD ["python", "server.py"]
