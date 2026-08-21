#!/bin/sh
set -eu

cd "$(dirname "$0")"
ACTION="${1:-help}"

need_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo "未找到 Docker，请先安装 Docker Engine 和 Compose 插件。" >&2
        exit 1
    }
    docker compose version >/dev/null
}

show_help() {
    cat <<'EOF'
门训机器人 Docker 管理工具

./manage.sh setup       构建镜像并生成配置
./manage.sh init        初始化 Delta Chat 机器人账号
./manage.sh admin-key   设置管理员密钥
./manage.sh link        显示机器人邀请链接
./manage.sh start       后台启动
./manage.sh logs        查看实时日志
./manage.sh status      查看容器状态
./manage.sh restart     重启服务
./manage.sh stop        停止服务
./manage.sh test        在镜像内运行测试
./manage.sh update      拉取代码、重建并启动
EOF
}

if [ "$ACTION" = "help" ]; then
    show_help
    exit 0
fi

need_docker

case "$ACTION" in
    setup)
        mkdir -p runtime/config runtime/data
        [ -f .env ] || cp .env.example .env
        docker compose build
        docker compose run --rm menxun-bot --help
        echo "初始化完成。下一步编辑 runtime/config/sites.json。"
        ;;
    init)
        printf "机器人邮箱，或 DCACCOUNT URI："
        read -r address
        case "$address" in
            DCACCOUNT:*|dcaccount:*) docker compose run --rm menxun-bot init "$address" ;;
            *)
                printf "邮箱授权码或应用密码："
                stty -echo
                read -r password
                stty echo
                printf '\n'
                docker compose run --rm menxun-bot init "$address" "$password"
                unset password
                ;;
        esac
        ;;
    admin-key) docker compose run --rm --entrypoint python menxun-bot /app/set_admin_key.py --file /data/admin-key.json ;;
    link) docker compose run --rm menxun-bot link ;;
    start) docker compose up -d ;;
    stop) docker compose down ;;
    restart) docker compose restart menxun-bot ;;
    logs) docker compose logs --follow --tail 200 menxun-bot ;;
    status) docker compose ps ;;
    test)
        docker compose build
        docker compose run --rm --entrypoint python menxun-bot -m unittest discover -v -s /app -p test_menxun_bot.py
        ;;
    update)
        git pull --ff-only
        docker compose up -d --build
        ;;
    *) show_help; exit 1 ;;
esac
