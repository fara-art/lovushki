#!/bin/sh
# Включает HTTP/2 в блоке https, который дописал certbot.
# На медленной мобильной связи это заметно: страница, шрифты и картинки идут
# по одному соединению, а не каждое со своим долгим рукопожатием.
#   sh deploy/http2.sh /etc/nginx/sites-available/lovushki
# Зовут deploy/setup.sh и deploy/https.sh. Если nginx новую строку не примет — всё возвращается как было.
CONF="$1"
[ -f "$CONF" ] || exit 0
grep -Eq 'listen[^;]*443[^;]*ssl' "$CONF" || exit 0
if grep -Eq 'listen[^;]*443[^;]*http2' "$CONF"; then
    echo "  HTTP/2 уже включён"
    exit 0
fi
cp "$CONF" "$CONF.before-http2"
sed -i -E 's/(listen[^;#]*443[^;#]*ssl)([^;#]*;)/\1 http2\2/' "$CONF"
if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx 2>/dev/null || nginx -s reload 2>/dev/null || true
    rm -f "$CONF.before-http2"
    echo "  HTTP/2 включён — на телефоне сайт грузится быстрее"
else
    mv "$CONF.before-http2" "$CONF"
    echo "  HTTP/2 не включился (nginx не принял строку) — оставляю как было"
fi
