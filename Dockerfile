FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY .env.example ./
COPY stores.example.yaml ./
COPY ["(V260401版)批量开票-导入开票模板.xlsx", "./(V260401版)批量开票-导入开票模板.xlsx"]

RUN mkdir -p /app/data /app/output /app/backups

CMD ["python", "-m", "app.main", "schedule", "--env-file", ".env"]
