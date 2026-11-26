FROM python:3.12-slim AS api
WORKDIR /app
COPY vbt_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vbt_service/ ./vbt_service/
CMD ["sh","-c","exec uvicorn vbt_service.api:app --host :: --port ${PORT:-8000}"]

FROM api AS worker
# A licensed operator supplies the pinned distribution only in the private build context.
COPY licensed/vectorbtpro.tar.gz /tmp/vectorbtpro.tar.gz
RUN pip install --no-cache-dir /tmp/vectorbtpro.tar.gz && rm /tmp/vectorbtpro.tar.gz
CMD ["python","-m","vbt_service.jobs"]
