FROM python:3.10-slim

WORKDIR /app

# Configure Debian mirror and install system tools
RUN (sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
    && apt-get update && apt-get install -y --no-install-recommends iproute2 procps curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY server/ ./server/

EXPOSE 8089

# Build arguments and environment variables for MQTT and platform guide
ARG MQTT_PUBLIC_HOST=""
ARG MQTT_PUBLIC_PORT=1883
ARG MQTT_USER=""
ARG MQTT_PASS=""

ENV HOST=0.0.0.0
ENV PORT=8089
ENV TZ=Asia/Shanghai
ENV DB_PATH=/app/data/fx.db
ENV MQTT_PUBLIC_HOST=$MQTT_PUBLIC_HOST
ENV MQTT_PUBLIC_PORT=$MQTT_PUBLIC_PORT
ENV MQTT_USER=$MQTT_USER
ENV MQTT_PASS=$MQTT_PASS

CMD ["python", "server/app.py"]
