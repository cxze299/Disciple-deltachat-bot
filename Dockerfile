FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Menxun Delta Chat Bot" \
      org.opencontainers.image.description="多网站门训打卡、提醒与管理员服务" \
      org.opencontainers.image.source="https://github.com/cxze299/menxun-deltachat-bot"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    MENXUN_DATA_DIR=/data \
    MENXUN_SITES_FILE=/config/sites.json \
    MENXUN_ADMIN_KEY_FILE=/data/admin-key.json

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY menxun_bot.py admin_key.py set_admin_key.py healthcheck.py test_menxun_bot.py docker-entrypoint.sh sites.example.json ./
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /config /data/account

VOLUME ["/config", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["serve"]
