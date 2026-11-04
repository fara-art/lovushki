#!/bin/sh
# Установка сайта «Восемь ловушек» на чистый Ubuntu или Debian.
# Запускать от root: sh deploy/setup.sh
# Повторный запуск просто обновляет файлы сайта — так же выкладывается новая версия игры.
set -e

SITE_DIR="/var/www/lovushki"
CONF_NAME="lovushki"

# Пути считаем от самого скрипта, а не от текущей папки: так его можно звать откуда угодно
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ Проверяю права"
if [ "$(id -u)" != "0" ]; then
    echo "  Нужен root. Запусти: sudo sh deploy/setup.sh"
    exit 1
fi

if [ ! -f "$ROOT/site/index.html" ]; then
    echo "  Рядом нет папки site — распакуй архив целиком: tar xzf site.tar.gz"
    exit 1
fi

echo "→ Ставлю nginx"
if ! command -v nginx >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq nginx
else
    echo "  уже стоит"
fi

echo "→ Копирую сайт в $SITE_DIR"
# Собираем новую версию рядом и подменяем папку целиком: так с сервера уходят и файлы,
# которые убрали из site/, а сайт пропадает лишь на долю секунды между двумя mv
rm -rf "$SITE_DIR.new" "$SITE_DIR.old"
mkdir -p "$SITE_DIR.new"
cp -r "$ROOT/site/." "$SITE_DIR.new/"
chown -R www-data:www-data "$SITE_DIR.new"
find "$SITE_DIR.new" -type d -exec chmod 755 {} \;
find "$SITE_DIR.new" -type f -exec chmod 644 {} \;
if [ -d "$SITE_DIR" ]; then mv "$SITE_DIR" "$SITE_DIR.old"; fi
mv "$SITE_DIR.new" "$SITE_DIR"
rm -rf "$SITE_DIR.old"

echo "→ Ставлю приём отзывов"
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq python3
fi
mkdir -p /opt/lovushki
cp "$ROOT/deploy/feedback.py" /opt/lovushki/feedback.py
chmod 644 /opt/lovushki/feedback.py
if command -v systemctl >/dev/null 2>&1; then
    # Служба работает от www-data, писать может только в /var/lib/lovushki (StateDirectory)
    cat > /etc/systemd/system/lovushki-feedback.service <<UNIT
[Unit]
Description=Vosem lovushek: anonymous feedback
After=network.target

[Service]
ExecStart=$(command -v python3) /opt/lovushki/feedback.py
User=www-data
StateDirectory=lovushki
# почта для отзывов (если настроена через deploy/feedback-setup.sh)
EnvironmentFile=-/etc/lovushki/feedback.env
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
    # Пароль страницы отзывов: пока его не задали, файл пустой и /admin/ никого не пускает
    if [ ! -f /etc/nginx/lovushki-admin.htpasswd ]; then
        touch /etc/nginx/lovushki-admin.htpasswd
        chown root:www-data /etc/nginx/lovushki-admin.htpasswd
        chmod 640 /etc/nginx/lovushki-admin.htpasswd
    fi
    # отзывы — файлы службы: если их когда-то переписали от root (feedback.py --keep / --clear
    # старой версии), служба не может дописывать новые, и на сайте «Не получилось отправить»
    if [ -d /var/lib/lovushki ] && id www-data >/dev/null 2>&1; then
        chown -R www-data:www-data /var/lib/lovushki
    fi
    systemctl daemon-reload
    systemctl enable lovushki-feedback >/dev/null 2>&1 || true
    systemctl restart lovushki-feedback
    echo "  служба lovushki-feedback запущена"
else
    echo "  systemd нет — отзывы не заработают, остальной сайт работает"
fi

# Если https уже включали, запоминаем имя сайта: новый конфиг затрёт настройки certbot,
# и их нужно будет вернуть, иначе после обновления пропадёт замочек
HTTPS_DOMAIN=""
for old_conf in /etc/nginx/sites-available/lovushki /etc/nginx/conf.d/lovushki.conf; do
    if [ -f "$old_conf" ]; then
        name=$(awk '$1 == "server_name" { gsub(";", "", $2); print $2; exit }' "$old_conf")
        if [ -n "$name" ] && [ "$name" != "_" ] && [ -f "/etc/letsencrypt/live/$name/fullchain.pem" ]; then
            HTTPS_DOMAIN="$name"
        fi
    fi
done
# Если https однажды слетел, в старом конфиге уже «server_name _», и имя оттуда не узнать.
# Тогда берём его из /etc/lovushki/domain (записывает deploy/https.sh) или из единственного
# сертификата certbot — иначе каждая следующая выкладка оставляла бы сайт без замочка
if [ -z "$HTTPS_DOMAIN" ] && [ -f /etc/lovushki/domain ]; then
    name=$(head -1 /etc/lovushki/domain | tr -d ' \r\n')
    [ -n "$name" ] && [ -f "/etc/letsencrypt/live/$name/fullchain.pem" ] && HTTPS_DOMAIN="$name"
fi
if [ -z "$HTTPS_DOMAIN" ] && [ -d /etc/letsencrypt/live ]; then
    certs=$(find /etc/letsencrypt/live -mindepth 2 -maxdepth 2 -name fullchain.pem 2>/dev/null)
    if [ "$(printf '%s\n' "$certs" | grep -c .)" = "1" ]; then
        HTTPS_DOMAIN=$(basename "$(dirname "$certs")")
    fi
fi
if [ -n "$HTTPS_DOMAIN" ]; then
    mkdir -p /etc/lovushki && echo "$HTTPS_DOMAIN" > /etc/lovushki/domain
fi

echo "→ Подключаю конфиг"
if [ -d /etc/nginx/sites-available ] && [ -d /etc/nginx/sites-enabled ]; then
    cp "$ROOT/deploy/nginx.conf" "/etc/nginx/sites-available/$CONF_NAME"
    ln -sf "/etc/nginx/sites-available/$CONF_NAME" "/etc/nginx/sites-enabled/$CONF_NAME"
    CONF_PATH="/etc/nginx/sites-available/$CONF_NAME"

    # Стандартная заглушка nginx мешает: она тоже слушает 80 как сайт по умолчанию
    if [ -L /etc/nginx/sites-enabled/default ]; then
        rm -f /etc/nginx/sites-enabled/default
        echo "  заглушка nginx отключена"
    fi
else
    # Сборка nginx без sites-available — кладём конфиг в conf.d
    mkdir -p /etc/nginx/conf.d
    cp "$ROOT/deploy/nginx.conf" "/etc/nginx/conf.d/$CONF_NAME.conf"
    CONF_PATH="/etc/nginx/conf.d/$CONF_NAME.conf"
    rm -f /etc/nginx/conf.d/default.conf
    echo "  конфиг положен в conf.d"
fi

# На части VPS IPv6 отключён, и тогда nginx не стартует из-за listen [::]:80
if [ ! -f /proc/net/if_inet6 ]; then
    sed -i '/listen \[::\]:80;/d' "$CONF_PATH"
    echo "  IPv6 на сервере выключен — строка listen [::]:80 убрана"
fi

if [ -n "$HTTPS_DOMAIN" ]; then
    sed -i "s/^\( *\)server_name .*/\1server_name $HTTPS_DOMAIN;/" "$CONF_PATH"
fi

# Блок для ссылки по IP (ставит deploy/https.sh) — обновляем, чтобы и там работали отзывы
if [ -f /etc/nginx/conf.d/lovushki-ip.conf ]; then
    cp "$ROOT/deploy/nginx-ip.conf" /etc/nginx/conf.d/lovushki-ip.conf
    echo "  блок для ссылки по IP обновлён"
fi

echo "→ Проверяю конфиг"
nginx -t

echo "→ Перезагружаю nginx"
systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null || nginx -s reload 2>/dev/null || nginx

if [ -n "$HTTPS_DOMAIN" ] && command -v certbot >/dev/null 2>&1; then
    echo "→ Возвращаю https для $HTTPS_DOMAIN (сертификат уже есть, новый не запрашиваю)"
    if certbot install --nginx --cert-name "$HTTPS_DOMAIN" --redirect --non-interactive >/dev/null 2>&1; then
        echo "  замочек на месте"
    else
        echo "  НЕ ВЫШЛО — сайт сейчас без https. Включи заново: sh deploy/https.sh $HTTPS_DOMAIN"
    fi
fi

# Если файрвол включён, без этого правила сайт снаружи не откроется
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | head -1 | grep -q active; then
    echo "→ Открываю порты 80 и 443 в ufw"
    ufw allow 'Nginx Full' >/dev/null 2>&1 || ufw allow 80,443/tcp >/dev/null 2>&1 || true
fi

echo "→ Проверяю, что сайт отвечает"
if command -v curl >/dev/null 2>&1; then
    CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/ || echo "нет ответа")
    PLAY=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/play/ || echo "нет ответа")
    sleep 1
    # После https http://127.0.0.1 обслуживает простой блок для ссылки по IP (deploy/https.sh) —
    # отзывов в нём нет и там будет 404. Поэтому спрашиваем саму службу и сайт по имени.
    FB=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/api/feedback/stats || echo "нет ответа")
    if [ -n "$HTTPS_DOMAIN" ]; then
        FB_WEB=$(curl -s -o /dev/null -w '%{http_code}' "https://$HTTPS_DOMAIN/api/feedback/stats" || echo "нет ответа")
        FB="$FB, через сайт: $FB_WEB"
    fi
    echo "  главная: $CODE, игра: $PLAY, отзывы: $FB"
    if [ "$CODE" != "200" ] || [ "$PLAY" != "200" ]; then
        echo "  Ожидалось 200. Смотри журнал: tail -n 30 /var/log/nginx/lovushki.error.log"
    fi
else
    echo "  curl не установлен — пропускаю проверку"
fi

# Сказать Яндексу и Bing, что страницы обновились (нужно имя сайта, поэтому только с https)
if [ -n "$HTTPS_DOMAIN" ] && command -v curl >/dev/null 2>&1; then
    echo "→ Сообщаю поисковикам об обновлении (IndexNow)"
    sh "$ROOT/deploy/indexnow.sh" "$HTTPS_DOMAIN" || true
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Готово. Сайт работает:"
echo "   http://${IP:-адрес-сервера}/"
echo "   http://${IP:-адрес-сервера}/play/  — сразу игра"
echo
echo "Файлы сайта:  $SITE_DIR"
echo "Отзывы:       python3 /opt/lovushki/feedback.py --list"
if [ ! -s /etc/nginx/lovushki-admin.htpasswd ]; then
    echo "              пароль страницы /admin/ и почта для отзывов: sh deploy/feedback-setup.sh"
fi
echo "Конфиг nginx: $CONF_PATH"
echo
echo "Дальше по желанию:"
echo "   • домен: направь A-запись на этот адрес, потом впиши его в server_name"
if [ -n "$HTTPS_DOMAIN" ]; then
    echo "   • https уже включён: https://$HTTPS_DOMAIN/"
else
    echo "   • https (замочек): sh deploy/https.sh имя.duckdns.org"
fi
echo "   • обновить игру: залей новый site.tar.gz, распакуй и запусти этот скрипт снова"
