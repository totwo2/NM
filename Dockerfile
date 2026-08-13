# N.M — Docker 镜像
FROM python:3.13-slim

WORKDIR /app

# 系统依赖（rg 用于 search_content 工具）
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8787

ENV HARNESS_PORT=8787
ENV HARNESS_DATA_DIR=/app/.harness

CMD ["harness-web"]