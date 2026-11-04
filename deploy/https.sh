#!/bin/sh
# Включает HTTPS (замочек в адресной строке) для сайта «Восемь ловушек».
#
#   sh deploy/https.sh имя.duckdns.org            — спросит почту сам
#   sh deploy/https.sh имя.duckdns.org ты@почта.ру — ничего не спросит
#
# Сертификат выдаётся только на имя, на голый IP-адрес его не получить.
# Значит, сначала нужен домен (подойдёт бесплатный на duckdns.org),
# направленный на этот сервер.
set -e

DOMAIN="$1"
EMAIL="$2"
CONF=""

if [ "$(id -u)" != "0" ]; then
    echo "Нужен root. Запусти: sudo sh deploy/https.sh имя.duckdns.org"
    exit 1
fi

if [ -z "$DOMAIN" ]; then
    echo "Укажи имя сайта: sh deploy/https.sh имя.duckdns.org"
    exit 1
fi

echo "→ Ищу конфиг сайта"
for candidate in /etc/nginx/sites-available/lovushki /etc/nginx/conf.d/lovushki.conf; do
    if [ -f "$candidate" ]; then CONF="$candidate"; break; fi
done
if [ -z "$CONF" ]; then
    echo "  Конфиг не найден. Сначала поставь сайт: sh deploy/setup.sh"
    exit 1
fi
echo "  $CONF"

echo "→ Проверяю, что имя указывает на этот сервер"
NAME_IP=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

if [ -z "$NAME_IP" ]; then
    echo "  Имя $DOMAIN никуда не указывает."
    echo "  Заведи его на duckdns.org и привяжи к серверу:"
    echo "    curl \"https://www.duckdns.org/update?domains=ИМЯ&token=ТОКЕН&ip=\""
    exit 1
fi

echo "  $DOMAIN → $NAME_IP (сервер: ${HOST_IP:-неизвестно})"
if [ -n "$HOST_IP" ] && [ "$NAME_IP" != "$HOST_IP" ]; then
    echo "  Имя указывает на другой адрес. Записи DNS обновляются не сразу —"
    echo "  подожди пару минут и запусти скрипт снова."
    exit 1
fi

echo "→ Прописываю имя в конфиг"
sed -i "s/^\( *\)server_name .*/\1server_name $DOMAIN;/" "$CONF"
nginx -t
systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null || nginx -s reload 2>/dev/null || nginx

echo "→ Ставлю certbot"
if ! command -v certbot >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq certbot python3-certbot-nginx
else
    echo "  уже стоит"
fi

# Порт 443 может быть закрыт файрволом — без него замочек работать не будет
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | head -1 | grep -q active; then
    ufw allow 'Nginx Full' >/dev/null 2>&1 || ufw allow 80,443/tcp >/dev/null 2>&1 || true
fi

echo "→ Получаю сертификат"
if [ -n "$EMAIL" ]; then
    certbot --nginx -d "$DOMAIN" --agree-tos -m "$EMAIL" --redirect --non-interactive
else
    certbot --nginx -d "$DOMAIN" --redirect
fi

# certbot переводит сайт на https, а на 80-м порту оставляет блок, который всем,
# кроме имени, отвечает 404. Сертификата на IP не бывает, поэтому старую ссылку
# вида http://АДРЕС/ держим живой отдельным блоком — обычным http, без переадресации.
# имя запоминаем: setup.sh по нему возвращает https после каждой выкладки
mkdir -p /etc/lovushki && echo "$DOMAIN" > /etc/lovushki/domain

echo "→ Оставляю рабочей старую ссылку по адресу сервера"
IP_CONF="/etc/nginx/conf.d/lovushki-ip.conf"
cat > "$IP_CONF" <<'CONF'
# Ссылка по адресу сервера продолжает работать после включения https.
server {
    listen 80 default_server;
    server_name _;

    root /var/www/lovushki;
    index index.html;
    charset utf-8;

    location / { try_files $uri $uri/ =404; }
}
CONF

if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null || nginx -s reload 2>/dev/null || nginx
    echo "  http://${HOST_IP:-адрес} остаётся рабочей ссылкой"
else
    # На сервере уже есть свой сайт по умолчанию на 80-м порту — не ломаем его
    rm -f "$IP_CONF"
    systemctl reload nginx 2>/dev/null || true
    echo "  пропускаю: на 80-м порту уже есть сайт по умолчанию"
fi

echo "→ Проверяю"
if command -v curl >/dev/null 2>&1; then
    CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/" 2>/dev/null || true)
    case "$CODE" in
        200)     echo "  https://$DOMAIN/ отвечает 200 — замочек на месте" ;;
        ""|000)  echo "  https://$DOMAIN/ не отвечает. Проверь, открыт ли порт 443: ufw status" ;;
        *)       echo "  https://$DOMAIN/ отвечает: $CODE" ;;
    esac
fi

echo
echo "Готово:"
echo "   https://$DOMAIN/"
echo "   https://$DOMAIN/play/  — сразу игра"
echo "   http://${HOST_IP:-адрес}/ — старая ссылка, работает как и работала"
echo
echo "Сертификат продлевается сам, проверить таймер: systemctl list-timers | grep certbot"
