#!/bin/sh
set -eu

mkdir -p /config /data/account

if [ ! -f /config/sites.json ]; then
    cp /app/sites.example.json /config/sites.json
    echo "Created /config/sites.json from the example. Edit it before starting the bot."
fi

exec python /app/menxun_bot.py --config-dir /data/account "$@"
