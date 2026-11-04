#!/bin/sh
# Настройка отзывов: пароль страницы /admin/ и почта, на которую приходит каждый отзыв.
# Запускать на сервере от root, после deploy/setup.sh:  sh deploy/feedback-setup.sh
# Пароли вводятся здесь и хранятся только на сервере — никому их не пересылай.
set -e

ENV_FILE=/etc/lovushki/feedback.env
HTPASSWD=/etc/nginx/lovushki-admin.htpasswd

if [ "$(id -u)" != "0" ]; then
    echo "Нужен root. Запусти: sudo sh deploy/feedback-setup.sh"
    exit 1
fi
if [ ! -f /opt/lovushki/feedback.py ]; then
    echo "Сначала поставь сайт: sh deploy/setup.sh"
    exit 1
fi

ask_secret() {
    # $1 — подсказка; ответ в переменной SECRET, символы не видны на экране
    printf "%s" "$1"
    stty -echo 2>/dev/null || true
    read -r SECRET
    stty echo 2>/dev/null || true
    echo
}

# Для файла настроек: значение в двойных кавычках, спецсимволы экранированы
quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"$`]/\\&/g')"
}

echo
echo "=== 1. Пароль админки «Админ данные» (/admin/) ==="
printf "Логин [admin]: "
read -r LOGIN
[ -n "$LOGIN" ] || LOGIN=admin
while :; do
    ask_secret "Пароль: "
    P1=$SECRET
    ask_secret "Ещё раз: "
    if [ -z "$P1" ]; then echo "Пароль не может быть пустым."; continue; fi
    if [ "$P1" != "$SECRET" ]; then echo "Не совпало, попробуй ещё раз."; continue; fi
    break
done
if ! command -v openssl >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq openssl
fi
HASH=$(printf '%s' "$P1" | openssl passwd -apr1 -stdin)
printf '%s:%s\n' "$LOGIN" "$HASH" > "$HTPASSWD"
chown root:www-data "$HTPASSWD"
chmod 640 "$HTPASSWD"
echo "  готово: логин $LOGIN"

echo
echo "=== 2. Почта для отзывов (можно пропустить — тогда отзывы только на странице /admin/) ==="
echo "Письма уходят через ящик vosem-lovushek@mail.ru (или другой на Mail.ru/Яндексе) — сам себе."
echo "Нужен не обычный пароль, а «пароль для внешних приложений»:"
echo "  Mail.ru: id.mail.ru → Безопасность → Пароли для внешних приложений → Добавить."
printf "С какой почты отправлять [vosem-lovushek@mail.ru] (минус — пропустить): "
read -r SMTP_USER
[ -n "$SMTP_USER" ] || SMTP_USER=vosem-lovushek@mail.ru
[ "$SMTP_USER" != "-" ] || SMTP_USER=""
if [ -n "$SMTP_USER" ]; then
    case "$SMTP_USER" in
        *@yandex.ru|*@ya.ru|*@yandex.com) SMTP_HOST=smtp.yandex.ru ;;
        *@mail.ru|*@bk.ru|*@inbox.ru|*@list.ru|*@internet.ru) SMTP_HOST=smtp.mail.ru ;;
        *@gmail.com) SMTP_HOST=smtp.gmail.com ;;
        *) printf "SMTP-сервер этой почты (например smtp.yandex.ru): "; read -r SMTP_HOST ;;
    esac
    ask_secret "Пароль приложения для $SMTP_USER: "
    SMTP_PASSWORD=$SECRET
    printf "Куда присылать отзывы [%s]: " "$SMTP_USER"
    read -r MAIL_TO
    [ -n "$MAIL_TO" ] || MAIL_TO=$SMTP_USER
fi
printf "Адрес сайта для ссылки в письме [https://vosem-lovushek.duckdns.org]: "
read -r SITE_URL
[ -n "$SITE_URL" ] || SITE_URL=https://vosem-lovushek.duckdns.org

mkdir -p /etc/lovushki
umask 077
{
    echo "# Настройки отзывов. Создано deploy/feedback-setup.sh — не пересылай этот файл никому."
    echo "SITE_URL=$(quote "$SITE_URL")"
    if [ -n "$SMTP_USER" ]; then
        echo "SMTP_HOST=$(quote "$SMTP_HOST")"
        echo "SMTP_PORT=465"
        echo "SMTP_USER=$(quote "$SMTP_USER")"
        echo "SMTP_PASSWORD=$(quote "$SMTP_PASSWORD")"
        echo "MAIL_TO=$(quote "$MAIL_TO")"
    fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

systemctl restart lovushki-feedback
nginx -t >/dev/null 2>&1 && (systemctl reload nginx 2>/dev/null || nginx -s reload)

if [ -n "$SMTP_USER" ]; then
    echo
    echo "→ Отправляю проверочное письмо на $MAIL_TO"
    if (set -a; . "$ENV_FILE"; set +a; python3 /opt/lovushki/feedback.py --test-mail); then
        echo "  Проверь почту (и папку «Спам»)."
    else
        echo "  Не отправилось. Проверь пароль приложения и что в настройках почты"
        echo "  разрешён доступ почтовым программам, потом запусти этот скрипт снова."
    fi
fi

echo
echo "Готово. Админ данные (все отзывы): $SITE_URL/admin/  (логин $LOGIN)"
echo "Там же кнопка «Скачать все отзывы (Excel)»."
