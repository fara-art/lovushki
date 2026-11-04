#!/bin/sh
# Сообщает Яндексу и Bing (протокол IndexNow), что страницы сайта обновились, —
# так новая версия попадает в поиск за часы, а не за недели.
#
#   sh deploy/indexnow.sh                       — имя сайта берётся из конфига nginx
#   sh deploy/indexnow.sh vosem-lovushek.duckdns.org
#
# Список страниц — из sitemap.xml, ключ — файл <32 знака>.txt в корне сайта
# (он публичный по задумке IndexNow: так поисковик проверяет, что сайт наш).
# deploy/setup.sh зовёт этот скрипт сам после каждой выкладки, если https включён.

SITE_DIR="${SITE_DIR:-/var/www/lovushki}"
HOST="$1"

if [ -z "$HOST" ]; then
    for conf in /etc/nginx/sites-available/lovushki /etc/nginx/conf.d/lovushki.conf; do
        if [ -f "$conf" ]; then
            HOST=$(awk '$1 == "server_name" { gsub(";", "", $2); print $2; exit }' "$conf")
            break
        fi
    done
fi
if [ -z "$HOST" ] || [ "$HOST" = "_" ]; then
    echo "  IndexNow: у сайта ещё нет имени — сначала sh deploy/https.sh имя.duckdns.org"
    exit 0
fi

KEY_FILE=$(ls "$SITE_DIR" 2>/dev/null | grep -E '^[0-9a-f]{32}\.txt$' | head -1)
if [ -z "$KEY_FILE" ]; then
    echo "  IndexNow: нет файла-ключа в $SITE_DIR — пропускаю"
    exit 0
fi
KEY=${KEY_FILE%.txt}

if [ ! -f "$SITE_DIR/sitemap.xml" ]; then
    echo "  IndexNow: нет sitemap.xml — пропускаю"
    exit 0
fi
# адреса из карты сайта, с именем этого сайта (в карте может быть записан старый домен)
URLS=$(sed -n 's#.*<loc>https\?://[^/]*\(/[^<]*\)</loc>.*#\1#p' "$SITE_DIR/sitemap.xml" |
       awk -v h="$HOST" 'BEGIN { ORS = "" } { if (NR > 1) print ","; print "\"https://" h $0 "\"" }')
if [ -z "$URLS" ]; then
    echo "  IndexNow: в sitemap.xml нет адресов — пропускаю"
    exit 0
fi

BODY="{\"host\":\"$HOST\",\"key\":\"$KEY\",\"keyLocation\":\"https://$HOST/$KEY_FILE\",\"urlList\":[$URLS]}"

if [ -n "$DRY_RUN" ]; then echo "$BODY"; exit 0; fi

# Яндекс и общий адрес IndexNow (его читают Bing, Seznam, Naver и другие)
for API in https://yandex.com/indexnow https://api.indexnow.org/indexnow; do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        -H 'Content-Type: application/json; charset=utf-8' --data "$BODY" "$API" || echo "нет ответа")
    case "$CODE" in
        200|202) WHAT="принято" ;;
        400)     WHAT="ошибка в запросе" ;;
        403)     WHAT="ключ не подошёл — проверь, что открывается https://$HOST/$KEY_FILE" ;;
        422)     WHAT="адреса не с этого сайта" ;;
        429)     WHAT="слишком часто, попробуй позже" ;;
        *)       WHAT="поисковик недоступен, попробуй позже" ;;
    esac
    echo "  IndexNow $(echo "$API" | sed 's#https://\([^/]*\)/.*#\1#'): $CODE — $WHAT"
done
exit 0
