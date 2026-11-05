
FROM python:3.12-slim

WORKDIR /app

COPY payment_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY payment_service/main.py ./main.py
COPY payment_service/payment_store.py ./payment_store.py

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
