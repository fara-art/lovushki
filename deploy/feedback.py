#!/usr/bin/env python3
"""
Приём отзывов об игре «Восемь ловушек».

Маленький сервер на стандартной библиотеке Python: слушает только 127.0.0.1,
снаружи до него доходит nginx (см. deploy/nginx.conf).

  POST /api/feedback        {"name", "age", "rating", "text", "wish", "website"} — новый отзыв
  GET  /api/feedback/stats  {"count": N, "average": 4.6} — для сайта
  GET  /admin/              «Админ данные»: все отзывы таблицей (nginx пускает только по паролю)
  GET  /admin/otzyvy.xlsx   все отзывы таблицей Excel
  GET  /admin/otzyvy.docx   все отзывы документом Word
  POST /admin/send          все отзывы письмом на почту, Excel и Word во вложении
  POST /admin/delete        {"ids": [...]} или {"all": true} — удалить отзывы (удалённые
                            откладываются в feedback.deleted.jsonl, их можно вернуть руками)

Имя и возраст — по желанию. Адрес отправителя не сохраняется. Поле website — ловушка
для ботов: человек его не видит и не заполняет.

Кулдаун: и с одного компьютера (поле client — случайная метка из браузера), и с одного
адреса — не больше 20 отзывов за 10 минут и 120 за сутки. Метки и адреса держатся
только в памяти, зашифрованными (хэш с солью), и забываются через сутки.

Отзывы лежат в /var/lib/lovushki/feedback.jsonl (по строке JSON на отзыв).
Если настроена почта (/etc/lovushki/feedback.env, см. deploy/feedback-setup.sh),
каждый новый отзыв ещё и приходит письмом.

На сервере:  python3 /opt/lovushki/feedback.py --list   — прочитать все отзывы
             python3 /opt/lovushki/feedback.py --keep 2 — оставить два первых, остальные удалить
             python3 /opt/lovushki/feedback.py --clear  — удалить все отзывы
             python3 /opt/lovushki/feedback.py --test-mail — проверить почту
"""
import hashlib
import html
import io
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("FEEDBACK_PORT", "8787"))
DATA_DIR = os.environ.get("FEEDBACK_DIR", "/var/lib/lovushki")
DATA_FILE = os.path.join(DATA_DIR, "feedback.jsonl")
TRASH_FILE = os.path.join(DATA_DIR, "feedback.deleted.jsonl")
MAX_BODY = 16 * 1024
MAX_TEXT = 1500
MAX_NAME = 60
# время в отзывах — московское, так привычнее читать
TZ = timezone(timedelta(hours=3))

# Почта: всё берётся из окружения (файл /etc/lovushki/feedback.env подключает systemd)
MAIL = {
    "host": os.environ.get("SMTP_HOST", ""),
    "port": int(os.environ.get("SMTP_PORT", "465") or 465),
    "user": os.environ.get("SMTP_USER", ""),
    "password": os.environ.get("SMTP_PASSWORD", ""),
    "to": os.environ.get("MAIL_TO", "") or os.environ.get("SMTP_USER", ""),
    "site": os.environ.get("SITE_URL", ""),
}

lock = threading.Lock()

# ---------- кулдаун: чтобы с одного компьютера не сыпались отзывы ----------
# одинаково для компьютера и для адреса
WINDOW = 10 * 60            # короткое окно, секунд
WINDOW_LIMIT = 20           # отзывов за 10 минут
DAY_LIMIT = 120             # отзывов за сутки
SALT = os.urandom(16)       # новая соль при каждом запуске: хэши нельзя сверить с адресами
recent = {}                 # хэш → времена отправки за последние сутки
recent_lock = threading.Lock()


def key(kind, value):
    return kind + ":" + hashlib.sha256(SALT + str(value).encode("utf-8")).hexdigest()[:24]


def wait_minutes(sec):
    return max(1, int(sec // 60) + (1 if sec % 60 else 0))


def check_cooldown(computer, ip, now=None):
    """None — можно отправлять, иначе (секунд до следующей попытки, текст для человека)."""
    now = now or time.time()
    with recent_lock:
        # старые записи забываем, чтобы память не росла
        for k in [k for k, ts in recent.items() if not ts or now - ts[-1] > 86400]:
            del recent[k]
        for k, who in ((computer, "с этого компьютера"), (ip, "из этой сети")):
            day = [t for t in recent.get(k, []) if now - t < 86400]
            short = [t for t in day if now - t < WINDOW]
            if len(short) >= WINDOW_LIMIT:
                left = WINDOW - (now - short[-WINDOW_LIMIT])
                return left, f"Слишком много отзывов {who} подряд. Следующий можно через {wait_minutes(left)} мин."
            if len(day) >= DAY_LIMIT:
                left = 86400 - (now - day[-DAY_LIMIT])
                return left, f"На сегодня отзывов {who} достаточно — спасибо! Можно будет снова завтра."
    return None


def note_sent(computer, ip, now=None):
    now = now or time.time()
    with recent_lock:
        for k in (computer, ip):
            recent[k] = [t for t in recent.get(k, []) if now - t < 86400] + [now]


def clean_text(value, limit=MAX_TEXT):
    """Обычный текст без управляющих символов, не длиннее limit."""
    if not isinstance(value, str):
        return ""
    keep = [ch for ch in value if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127)]
    return "".join(keep).strip()[:limit]


def read_all():
    if not os.path.exists(DATA_FILE):
        return []
    items = []
    with open(DATA_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                pass
    return items


def record_id(r):
    """Метка отзыва для удаления: по содержимому, поэтому не сдвигается, если файл успел измениться."""
    return hashlib.sha1(json.dumps(r, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def same_owner(path):
    """Файл — тому же владельцу, что и папка с отзывами. Команды --keep/--clear запускают
    от root, а служба пишет от www-data: без этого новые отзывы перестают сохраняться."""
    try:
        st = os.stat(DATA_DIR)
        if os.geteuid() == 0:
            os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o640)
    except OSError:
        pass


def delete_records(keep):
    """Оставляет в файле отзывы, для которых keep(номер, отзыв) — True. Удалённые — в корзину."""
    with lock:
        items = read_all()
        stay, gone = [], []
        for i, r in enumerate(items):
            (stay if keep(i, r) else gone).append(r)
        if not gone:
            return 0
        os.makedirs(DATA_DIR, exist_ok=True)
        stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        with open(TRASH_FILE, "a", encoding="utf-8") as f:
            for r in gone:
                f.write(json.dumps(dict(r, deleted=stamp), ensure_ascii=False) + "\n")
        same_owner(TRASH_FILE)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in stay:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        same_owner(tmp)
        os.replace(tmp, DATA_FILE)
        return len(gone)


def valid_rating(value):
    """Оценка от 0,5 до 5 с шагом 0,5 (полузвёзды); иначе None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0.5 <= value <= 5 or abs(value * 2 - round(value * 2)) > 1e-9:
        return None
    return int(value) if value == int(value) else round(value, 1)


def stats():
    ratings = [r.get("rating") for r in read_all() if valid_rating(r.get("rating")) is not None]
    if not ratings:
        return {"count": 0, "average": None}
    return {"count": len(ratings), "average": round(sum(ratings) / len(ratings), 1)}


def num(n):
    """4.5 → «4,5», 4.0 → «4»: как пишут оценки по-русски."""
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return str(n).replace(".", ",")


def stars(n):
    """4.5 → «★★★★½ 4,5»: полные звёзды, половинка, пустые и число."""
    n = valid_rating(n)
    if n is None:
        return "без оценки"
    full = int(n)
    half = n - full >= 0.5
    return "★" * full + ("½" if half else "") + "☆" * (5 - full - (1 if half else 0)) + " " + num(n)


def who(r):
    name = r.get("name") or "Без имени"
    return name + (f", {r['age']} лет" if r.get("age") else "")


# ---------- почта ----------

def mail_ready():
    return bool(MAIL["host"] and MAIL["user"] and MAIL["password"] and MAIL["to"])


def send_mail(subject, body, attachments=()):
    """attachments — список (имя файла, байты, mime-тип)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Восемь ловушек <{MAIL['user']}>"
    msg["To"] = MAIL["to"]
    msg.set_content(body)
    for name, data, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    context = ssl.create_default_context()
    if MAIL["port"] == 465:
        with smtplib.SMTP_SSL(MAIL["host"], MAIL["port"], context=context, timeout=30) as s:
            s.login(MAIL["user"], MAIL["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(MAIL["host"], MAIL["port"], timeout=30) as s:
            if MAIL["port"] == 587:
                s.starttls(context=context)
            if MAIL["password"] != "-":
                s.login(MAIL["user"], MAIL["password"])
            s.send_message(msg)


def mail_error(e):
    """Понятная причина, почему письмо не ушло, — для админки и для консоли."""
    where = f"{MAIL['host']}:{MAIL['port']}"
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return (f"Почта не приняла логин или пароль {MAIL['user']}. Нужен пароль для внешних приложений "
                "(не обычный пароль от ящика) с доступом к SMTP — задай его заново: sh deploy/feedback-setup.sh.")
    if isinstance(e, (TimeoutError, ConnectionError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return (f"Сервер не может подключиться к {where} ({e}). Обычно это хостинг закрыл исходящие "
                "почтовые порты — попроси поддержку VPS открыть порт 465 (или 587).")
    return f"Почта не приняла письмо: {e}"


def mail_feedback(r):
    """Письмо о новом отзыве. Ошибка почты не мешает: отзыв уже сохранён в файле."""
    if not mail_ready():
        return
    body = (
        f"Новый отзыв об игре «Восемь ловушек»\n\n"
        f"Кто: {who(r)}\n"
        f"Оценка: {stars(r.get('rating'))}\n"
        f"Когда: {ru_date(r.get('date'))}\n\n"
        f"Отзыв:\n{r.get('text') or '—'}\n\n"
        f"Что можно добавить:\n{r.get('wish') or '—'}\n"
    )
    if MAIL["site"]:
        body += f"\nВсе отзывы: {MAIL['site'].rstrip('/')}/admin/\n"
    try:
        send_mail(f"Отзыв: {stars(r.get('rating'))} — {who(r)}", body)
    except Exception as e:  # noqa: BLE001 — любую ошибку почты только пишем в журнал службы
        print("Письмо не отправилось:", mail_error(e), file=sys.stderr, flush=True)


def mail_all():
    """Все отзывы одним письмом: файлы Word и Excel во вложении."""
    items = read_all()
    s = stats()
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    body = (
        f"Все отзывы об игре «Восемь ловушек» на {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}.\n"
        f"Всего: {len(items)}" + (f", средняя оценка {num(s['average'])}" if s["average"] else "") + ".\n\n"
        "Во вложении: таблица Excel и документ Word.\n"
    )
    send_mail(f"Все отзывы ({len(items)}) — Восемь ловушек", body, [
        (f"otzyvy-{day}.xlsx", export_xlsx(), XLSX_MIME),
        (f"otzyvy-{day}.docx", export_docx(), DOCX_MIME),
    ])
    return len(items)


# ---------- выгрузка в Excel и Word ----------
# Настоящие .xlsx и .docx, собранные из XML стандартной библиотекой — без сторонних пакетов.

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def ru_date(value):
    """«2026-10-15 20:51» → «15.10.2026 20:51»."""
    try:
        return datetime.strptime(value or "", "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value or ""


def x(text):
    """Текст для XML: экранирование и без символов, которых XML не допускает."""
    text = "".join(ch for ch in str(text) if ch in "\t\n\r" or ord(ch) >= 32)
    return html.escape(text, quote=True)


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def export_xlsx(items=None):
    """Таблица: шапка жирная и закреплена, ширина столбцов по смыслу, длинный текст переносится.
    Дата — текстом «15.10.2026 20:51», иначе Excel превращает её в число и показывает «#####»."""
    head = ["Дата", "Имя", "Возраст", "Оценка", "Отзыв", "Что добавить"]
    widths = [18, 16, 10, 10, 60, 50]

    def cell(ref, value, style):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{x(value)}</t></is></c>'

    rows = ['<row r="1">' + "".join(cell(f"{'ABCDEF'[i]}1", h, 1) for i, h in enumerate(head)) + "</row>"]
    for n, r in enumerate(read_all() if items is None else items, start=2):
        rating = valid_rating(r.get("rating"))
        values = [ru_date(r.get("date")), r.get("name") or "", r.get("age") if r.get("age") else "",
                  rating if rating is not None else "", r.get("text") or "", r.get("wish") or ""]
        rows.append(f'<row r="{n}">' + "".join(cell(f"{'ABCDEF'[i]}{n}", v, 2) for i, v in enumerate(values)) + "</row>")
    last = max(1, len(rows))
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(rows)}</sheetData>"
        f'<autoFilter ref="A1:F{last}"/>'
        # при печати — альбомный лист, все столбцы по ширине страницы
        '<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
        '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9F2E6"/></patternFill></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    return zip_bytes([
        ("[Content_Types].xml",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
         '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
         '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
         "</Types>"),
        ("_rels/.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
         "</Relationships>"),
        ("xl/workbook.xml",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
         '<sheets><sheet name="Отзывы" sheetId="1" r:id="rId1"/></sheets>'
         '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">Отзывы!$A$1:$F$' + str(last) + "</definedName></definedNames>"
         "</workbook>"),
        ("xl/_rels/workbook.xml.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
         '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
         "</Relationships>"),
        ("xl/worksheets/sheet1.xml", sheet),
        ("xl/styles.xml", styles),
    ])


def export_docx(items=None):
    """Документ Word: заголовок, итог и каждый отзыв отдельным блоком, новые сверху.
    items — свой список отзывов (например, один отзыв); по умолчанию все."""
    def para(text, bold=False, size=22, color=None, space_after=80, keep=False):
        props = ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") + f'<w:sz w:val="{size}"/>'
        ppr = f'<w:pPr>{"<w:keepNext/>" if keep else ""}<w:spacing w:after="{space_after}"/></w:pPr>'
        runs = []
        for i, line in enumerate(str(text).split("\n")):
            if i:
                runs.append("<w:r><w:br/></w:r>")
            runs.append(f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{x(line)}</w:t></w:r>')
        return f"<w:p>{ppr}{''.join(runs)}</w:p>"

    single = items is not None and len(items) == 1
    items = list(reversed(read_all() if items is None else items))
    s = stats()
    parts = [para("Отзыв об игре «Восемь ловушек»" if single else "Отзывы об игре «Восемь ловушек»", bold=True, size=36, space_after=120)]
    if not single:
        parts.append(para(f"Выгрузка {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}. Всего отзывов: {len(items)}"
                          + (f", средняя оценка {num(s['average'])}" if s["average"] else "") + ".", color="555555", space_after=240))
    if not items:
        parts.append(para("Отзывов пока нет."))
    for r in items:
        parts.append(para(f"{who(r)} · {stars(r.get('rating'))}", bold=True, size=26, space_after=40, keep=True))
        parts.append(para(ru_date(r.get("date")), color="777777", size=20, space_after=80, keep=True))
        parts.append(para("Отзыв:", bold=True, space_after=20, keep=True))
        parts.append(para(r.get("text") or "—", space_after=80))
        parts.append(para("Что можно добавить:", bold=True, space_after=20, keep=True))
        parts.append(para(r.get("wish") or "—", space_after=280))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(parts) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="709" w:footer="709" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
        '<w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>'
    )
    return zip_bytes([
        ("[Content_Types].xml",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
         '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
         "</Types>"),
        ("_rels/.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
         "</Relationships>"),
        ("word/_rels/document.xml.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
         "</Relationships>"),
        ("word/document.xml", document),
        ("word/styles.xml", styles),
    ])


# ---------- страница «Админ данные» ----------

def admin_page():
    all_items = read_all()
    s = stats()
    now = datetime.now(TZ)
    week = 0
    for r in all_items:
        try:
            if now - datetime.strptime(r.get("date", ""), "%Y-%m-%d %H:%M").replace(tzinfo=TZ) <= timedelta(days=7):
                week += 1
        except ValueError:
            pass
    # сколько отзывов на каждую оценку (полузвёзды — к ближайшей целой вверх: 4,5 → 5)
    dist = {k: 0 for k in range(5, 0, -1)}
    rated = 0
    for r in all_items:
        v = valid_rating(r.get("rating"))
        if v is not None:
            dist[min(5, int(v + 0.5))] += 1
            rated += 1
    bars = "".join(
        f'<div class="dist__row"><span>{k}★</span><i><b style="width:{(dist[k] / rated * 100) if rated else 0:.0f}%"></b></i><em>{dist[k]}</em></div>'
        for k in dist
    )

    def stars_only(v):
        # v — любое число от 0 до 5 (у средней не обязательно шаг 0,5)
        return (f'<span class="stars" style="--v:{v / 5 * 100:.1f}%" aria-label="{num(v)} из 5">'
                f'<span class="stars__fill">★★★★★</span>★★★★★</span>')

    def star_html(v):
        v = valid_rating(v)
        if v is None:
            return '<span class="rate"><span class="stars stars--none">без оценки</span></span>'
        return f'<span class="rate">{stars_only(v)}<b class="score">{num(v)}</b></span>'

    cards = []
    for idx in range(len(all_items) - 1, -1, -1):          # новые сверху; idx — номер строки в файле
        r = all_items[idx]
        name = html.escape(r.get("name") or "Без имени")
        age = f'<span class="age">{r["age"]} лет</span>' if r.get("age") else ""
        rid = record_id(r)
        cards.append(f"""<div class="row" data-id="{rid}"><label class="pick" aria-label="Выбрать отзыв"><input type="checkbox" value="{rid}"></label><details class="card">
<summary><span class="who"><b>{name}</b>{age}</span>{star_html(r.get('rating'))}<span class="date">{html.escape(ru_date(r.get('date')))}</span><span class="chev" aria-hidden="true"></span></summary>
<div class="body">
<div class="field"><span class="label">Отзыв</span><p>{html.escape(r.get('text') or '—')}</p></div>
<div class="field"><span class="label">Что можно добавить</span><p>{html.escape(r.get('wish') or '—')}</p></div>
<div class="mini"><a href="otzyv/{idx}.docx">Скачать Word</a><a href="otzyv/{idx}.xlsx">Скачать Excel</a><button class="del" type="button" data-del="{rid}">Удалить</button></div>
</div></details></div>""")

    if mail_ready():
        mail_btn = (f'<button class="btn btn--ghost" id="sendAll" type="button">Отправить все на почту</button>'
                    f'<span class="hint">{html.escape(MAIL["to"])}</span><span id="sendStatus" role="status"></span>')
    else:
        mail_btn = ('<span class="hint">Почта не настроена — на сервере запусти <code>sh deploy/feedback-setup.sh</code>, '
                    'и здесь появится «Отправить все на почту».</span>')
    avg = num(s["average"]) if s["average"] else "—"
    avg_stars = stars_only(s["average"]) if s["average"] else '<span class="stars stars--none">пока нет оценок</span>'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Админ данные — Восемь ловушек</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/fonts.css">
<style>
:root{{--ink:#070B14;--slab:#111A2B;--fog:#253552;--paper:#EAF0FA;--muted:#8E9BB8;--lamp:#FFC46B;--mint:#3EE6A8;--teal:#22D3C5;--sky:#49C3F5;--alarm:#FF6F61;
--grad:linear-gradient(100deg,#3EE6A8,#22D3C5 50%,#49C3F5);--display:"Unbounded",system-ui,sans-serif;--body:"Golos Text",system-ui,sans-serif}}
*{{box-sizing:border-box}}
html{{min-height:100%;background:var(--ink)}}
body{{margin:0;min-height:100vh;font:16px/1.55 var(--body);color:var(--paper)}}
/* фон — отдельным слоем во весь экран: background-attachment:fixed телефоны не умеют */
body::before{{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background:radial-gradient(1100px 700px at 8% -10%,rgba(14,140,100,.38),transparent 65%),
radial-gradient(900px 700px at 100% 10%,rgba(13,100,150,.34),transparent 65%),
radial-gradient(900px 800px at 50% 110%,rgba(90,55,150,.28),transparent 65%),
linear-gradient(180deg,#081320,#070B14 60%)}}
main{{max-width:1080px;margin:0 auto;padding:32px max(16px,env(safe-area-inset-right)) 70px max(16px,env(safe-area-inset-left))}}
header{{display:flex;align-items:center;gap:14px;margin-bottom:26px}}
header img{{width:46px;height:46px;filter:drop-shadow(0 0 14px rgba(62,230,168,.45))}}
h1{{margin:0;font:800 30px/1.1 var(--display);background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}}
header p{{margin:4px 0 0;color:var(--muted);font-size:15px}}
.stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) minmax(0,1.3fr);gap:14px;margin-bottom:22px}}
.tile{{padding:18px 20px;border-radius:18px;background:linear-gradient(180deg,rgba(22,32,52,.82),rgba(13,20,35,.82));border:1px solid rgba(120,160,210,.16)}}
.tile__k{{display:block;font:700 11px/1 var(--display);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:12px}}
.tile b{{font:800 34px/1 var(--display);font-variant-numeric:tabular-nums}}
.tile .stars{{font-size:18px;margin-top:10px}}
.dist__row{{display:grid;grid-template-columns:34px 1fr 28px;align-items:center;gap:8px;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}}
.dist__row i{{height:8px;border-radius:4px;background:rgba(120,160,210,.14);overflow:hidden}}
.dist__row b{{display:block;height:100%;border-radius:4px;background:var(--lamp)}}
.dist__row em{{font-style:normal;text-align:right}}
.bar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:22px}}
.btn{{display:inline-flex;align-items:center;padding:11px 18px;border-radius:12px;border:0;background:var(--grad);color:var(--ink);font:700 14px var(--display);text-decoration:none;cursor:pointer}}
.btn--ghost{{background:rgba(17,26,43,.9);color:var(--paper);border:1px solid #2E4366}}
.btn:hover{{filter:brightness(1.08)}} .btn:disabled{{opacity:.6;cursor:wait}}
.hint{{color:var(--muted);font-size:14px}} code{{background:var(--slab);padding:2px 6px;border-radius:6px}}
#sendStatus{{font-size:14px}} #sendStatus.ok{{color:var(--mint)}} #sendStatus.err{{color:var(--alarm)}}
.listhead{{display:flex;align-items:center;justify-content:space-between;margin:6px 0 12px;gap:10px}}
.listhead h2{{margin:0;font:800 18px var(--display)}} .listhead h2 .hint{{font:400 14px var(--body);margin-left:6px}}
.link{{background:none;border:0;color:var(--teal);font:600 14px var(--body);cursor:pointer;padding:6px;white-space:nowrap}}
.list{{display:flex;flex-direction:column;gap:10px}}
.card{{border-radius:16px;background:linear-gradient(180deg,rgba(22,32,52,.86),rgba(13,20,35,.86));border:1px solid rgba(120,160,210,.16);transition:border-color .2s}}
.card[open]{{border-color:rgba(62,230,168,.4)}}
summary{{list-style:none;display:grid;grid-template-columns:minmax(0,1fr) auto auto 24px;align-items:center;gap:16px;padding:16px 18px;cursor:pointer}}
summary::-webkit-details-marker{{display:none}}
summary:focus-visible{{outline:2px solid var(--mint);outline-offset:-2px;border-radius:16px}}
.who{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.who b{{font-weight:600}} .age{{color:var(--muted);margin-left:8px}}
.rate{{display:inline-flex;align-items:center;gap:8px;justify-self:start;white-space:nowrap}}
.stars{{position:relative;display:inline-block;width:max-content;color:#3E4F70;letter-spacing:2px;white-space:nowrap;font-size:17px;line-height:1}}
.stars__fill{{position:absolute;left:0;top:0;width:var(--v);overflow:hidden;color:var(--lamp)}}
.stars--none{{color:var(--muted);font-size:13px;letter-spacing:0}}
.score{{color:var(--lamp);font-variant-numeric:tabular-nums;font-weight:700}}
.date{{color:var(--muted);font-size:14px;font-variant-numeric:tabular-nums;white-space:nowrap}}
.chev{{width:24px;height:24px;border-radius:8px;display:grid;place-items:center;background:rgba(120,160,210,.12)}}
.chev::before{{content:"";width:7px;height:7px;border-right:2px solid var(--paper);border-bottom:2px solid var(--paper);transform:translateY(-2px) rotate(45deg);transition:transform .2s}}
.card[open] .chev::before{{transform:translateY(2px) rotate(-135deg)}}
.body{{padding:2px 18px 18px;display:flex;flex-direction:column;gap:14px;border-top:1px solid rgba(120,160,210,.12);margin:0 18px;padding-left:0;padding-right:0;padding-top:14px}}
.field .label{{display:block;font:700 11px/1 var(--display);letter-spacing:.12em;text-transform:uppercase;color:var(--teal);margin-bottom:6px}}
.field p{{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}}
.mini{{display:flex;gap:10px;flex-wrap:wrap}}
.mini a{{font-size:13px;color:var(--paper);text-decoration:none;padding:7px 12px;border-radius:10px;border:1px solid #2E4366;background:rgba(7,11,20,.5)}}
.mini a:hover{{border-color:var(--mint)}}
.row{{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;align-items:start}}
.pick{{display:none;width:44px;height:56px;place-items:center;cursor:pointer}}
.pick input{{width:22px;height:22px;accent-color:var(--alarm);cursor:pointer}}
.selecting .row{{grid-template-columns:44px minmax(0,1fr)}} .selecting .pick{{display:grid}}
.row.chosen .card{{border-color:rgba(255,111,97,.6)}}
.del{{margin-left:auto;font:600 13px var(--body);color:#FFB4AB;padding:7px 12px;border-radius:10px;border:1px solid rgba(255,111,97,.45);background:rgba(255,111,97,.08);cursor:pointer}}
.del:hover,.del.ask{{background:var(--alarm);color:#2A0905;border-color:var(--alarm)}}
.btn--danger{{background:rgba(255,111,97,.12);color:#FFB4AB;border:1px solid rgba(255,111,97,.5)}}
.btn--danger.ask,.btn--danger:hover{{background:var(--alarm);color:#2A0905;filter:none}}
.selbar{{display:none;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 14px;padding:12px 14px;border-radius:14px;border:1px solid rgba(255,111,97,.4);background:rgba(40,16,20,.6)}}
.selecting .selbar{{display:flex}}
#delStatus{{font-size:14px}} #delStatus.err{{color:var(--alarm)}}
.empty{{padding:40px 20px;text-align:center;color:var(--muted);border:1px dashed #2E4366;border-radius:16px}}
@media (max-width:820px){{.stats{{grid-template-columns:1fr 1fr}}}}
@media (max-width:620px){{summary{{grid-template-columns:minmax(0,1fr) 24px;gap:6px 12px}}
summary .who{{grid-column:1}} summary .rate{{grid-column:1;grid-row:2}}
summary .date{{grid-column:1;grid-row:3}} summary .chev{{grid-column:2;grid-row:1/span 3}}
.tile{{padding:16px}} .tile__k{{font-size:10px;letter-spacing:.08em;line-height:1.3}} .tile b{{font-size:28px}} h1{{font-size:24px}}}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style></head><body><main>
<header><img src="/favicon.svg" alt=""><div><h1>Админ данные</h1><p>Отзывы об игре «Восемь ловушек»</p></div></header>

<section class="stats">
<div class="tile"><span class="tile__k">Всего отзывов</span><b>{len(all_items)}</b></div>
<div class="tile"><span class="tile__k">Средняя оценка</span><b>{avg}</b><br>{avg_stars}</div>
<div class="tile"><span class="tile__k">За 7 дней</span><b>{week}</b></div>
<div class="tile"><span class="tile__k">Оценки</span>{bars}</div>
</section>

<div class="bar">
<a class="btn" href="otzyvy.xlsx">Скачать все — Excel</a>
<a class="btn" href="otzyvy.docx">Скачать все — Word</a>
{mail_btn}
</div>

<div class="listhead"><h2>Все отзывы <span class="hint">новые сверху</span></h2>
{'<span><button class="link" id="pickMode" type="button">Выбрать для удаления</button><button class="link" id="toggleAll" type="button">Развернуть все</button></span>' if all_items else ''}</div>
<div id="listWrap">
<div class="selbar"><button class="btn btn--danger" id="delPicked" type="button" disabled>Удалить выбранные (0)</button>
<button class="link" id="pickAll" type="button">Выбрать все</button><button class="link" id="pickCancel" type="button">Отмена</button>
<button class="btn btn--danger" id="delAll" type="button">Удалить все отзывы</button><span id="delStatus" role="status"></span></div>
<div class="list">{''.join(cards) or '<div class="empty">Отзывов пока нет. Они появятся здесь, как только кто-нибудь нажмёт «Отзыв» на сайте.</div>'}</div>
</div>
</main>
<script>
var t = document.getElementById("toggleAll");
if (t) t.addEventListener("click", function () {{
  var all = document.querySelectorAll("details.card"), open = t.textContent === "Развернуть все";
  for (var i = 0; i < all.length; i++) all[i].open = open;
  t.textContent = open ? "Свернуть все" : "Развернуть все";
}});
var b = document.getElementById("sendAll"), st = document.getElementById("sendStatus");
if (b) b.addEventListener("click", function () {{
  b.disabled = true; st.className = ""; st.textContent = "Отправляю…";
  fetch("send", {{ method: "POST", headers: {{ "X-Admin-Action": "send" }} }})
    .then(function (r) {{ return r.json(); }})
    .then(function (d) {{
      st.className = d.ok ? "ok" : "err";
      st.textContent = d.ok ? "Отправлено: " + d.count + " отзывов, Word и Excel во вложении." : (d.error || "Не получилось отправить.");
    }})
    .catch(function () {{ st.className = "err"; st.textContent = "Не получилось отправить. Проверь интернет."; }})
    .then(function () {{ b.disabled = false; }});
}});

/* ---------- удаление: каждая кнопка срабатывает со второго нажатия ---------- */
var wrap = document.getElementById("listWrap"), ds = document.getElementById("delStatus");
function removeIds(body, btn) {{
  btn.disabled = true;
  fetch("delete", {{ method: "POST", headers: {{ "X-Admin-Action": "delete", "Content-Type": "application/json" }}, body: JSON.stringify(body) }})
    .then(function (r) {{ return r.json(); }})
    .then(function (d) {{ if (d.ok) location.reload(); else throw new Error(d.error || ""); }})
    .catch(function (e) {{ btn.disabled = false; if (ds) {{ ds.className = "err"; ds.textContent = e.message || "Не получилось удалить."; }} else alert("Не получилось удалить."); }});
}}
function twoStep(btn, askText, go) {{
  if (!btn.classList.contains("ask")) {{
    btn.dataset.text = btn.textContent; btn.classList.add("ask"); btn.textContent = askText;
    setTimeout(function () {{ if (btn.classList.contains("ask")) {{ btn.classList.remove("ask"); btn.textContent = btn.dataset.text; }} }}, 4000);
    return;
  }}
  go();
}}
document.addEventListener("click", function (e) {{
  var d = e.target.closest("[data-del]");
  if (d) twoStep(d, "Точно удалить?", function () {{ removeIds({{ ids: [d.dataset.del] }}, d); }});
}});
function picked() {{ return Array.prototype.map.call(document.querySelectorAll(".pick input:checked"), function (i) {{ return i.value; }}); }}
function syncPicked() {{
  var n = picked().length, dp = document.getElementById("delPicked");
  dp.textContent = "Удалить выбранные (" + n + ")"; dp.disabled = !n;
  document.querySelectorAll(".row").forEach(function (r) {{ r.classList.toggle("chosen", r.querySelector(".pick input").checked); }});
}}
var pm = document.getElementById("pickMode");
if (pm) {{
  pm.addEventListener("click", function () {{ wrap.classList.toggle("selecting"); }});
  document.getElementById("pickCancel").addEventListener("click", function () {{
    wrap.classList.remove("selecting");
    document.querySelectorAll(".pick input").forEach(function (i) {{ i.checked = false; }}); syncPicked();
  }});
  document.getElementById("pickAll").addEventListener("click", function () {{
    document.querySelectorAll(".pick input").forEach(function (i) {{ i.checked = true; }}); syncPicked();
  }});
  wrap.addEventListener("change", syncPicked);
  var dp = document.getElementById("delPicked");
  dp.addEventListener("click", function () {{ twoStep(dp, "Точно удалить " + picked().length + "?", function () {{ removeIds({{ ids: picked() }}, dp); }}); }});
  var da = document.getElementById("delAll");
  da.addEventListener("click", function () {{ twoStep(da, "Точно удалить ВСЕ?", function () {{ removeIds({{ all: true }}, da); }}); }});
}}
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "lovushki-feedback"

    def log_message(self, fmt, *args):
        # без журнала запросов: в нём оказался бы адрес отправителя
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_bytes(code, body.encode("utf-8"), ctype, extra)

    def send_bytes(self, code, data, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def reply(self, code, payload):
        self.send(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/feedback/stats":
            self.reply(200, stats())
        elif path in ("/admin", "/admin/"):
            self.send(200, admin_page(), "text/html; charset=utf-8")
        elif re.fullmatch(r"/admin/otzyv/\d+\.(docx|xlsx)", path):
            idx, ext = path.rsplit("/", 1)[1].split(".")
            items = read_all()
            if int(idx) >= len(items):
                self.reply(404, {"ok": False})
                return
            r = items[int(idx)]
            data = export_docx([r]) if ext == "docx" else export_xlsx([r])
            day = (r.get("date") or "")[:10] or "bez-daty"
            self.send_bytes(200, data, DOCX_MIME if ext == "docx" else XLSX_MIME,
                            {"Content-Disposition": f'attachment; filename="otzyv-{day}-{idx}.{ext}"'})
        elif path in ("/admin/otzyvy.xlsx", "/admin/otzyvy.docx"):
            ext = path.rsplit(".", 1)[1]
            data = export_xlsx() if ext == "xlsx" else export_docx()
            name = "otzyvy-" + datetime.now(TZ).strftime("%Y-%m-%d") + "." + ext
            self.send_bytes(200, data, XLSX_MIME if ext == "xlsx" else DOCX_MIME,
                            {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.reply(404, {"ok": False})

    def do_POST(self):
        if self.path.split("?")[0] == "/admin/send":
            self.send_all()
            return
        if self.path.split("?")[0] == "/admin/delete":
            self.delete()
            return
        if self.path.split("?")[0] != "/api/feedback":
            self.reply(404, {"ok": False})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.reply(413 if length > MAX_BODY else 400, {"ok": False, "error": "size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.reply(400, {"ok": False, "error": "json"})
            return
        if not isinstance(data, dict):
            self.reply(400, {"ok": False, "error": "json"})
            return

        # ловушка для ботов: делаем вид, что всё хорошо, но ничего не сохраняем
        if clean_text(data.get("website")):
            self.reply(200, {"ok": True})
            return

        text = clean_text(data.get("text"))
        wish = clean_text(data.get("wish"))
        if not text and not wish:
            self.reply(400, {"ok": False, "error": "empty"})
            return

        # адрес приходит от nginx; без метки из браузера компьютер узнаём по адресу
        ip = self.headers.get("X-Real-IP") or self.client_address[0]
        client = re.sub(r"[^A-Za-z0-9-]", "", str(data.get("client") or ""))[:40]
        computer = key("pc", client) if len(client) >= 8 else key("pc-ip", ip)
        net = key("ip", ip)
        busy = check_cooldown(computer, net)
        if busy:
            self.send(429, json.dumps({"ok": False, "error": "cooldown", "wait": int(busy[0]) + 1, "message": busy[1]}, ensure_ascii=False),
                      "application/json; charset=utf-8", {"Retry-After": str(int(busy[0]) + 1)})
            return
        rating = valid_rating(data.get("rating"))
        age = data.get("age")
        try:
            age = int(age) if age not in (None, "") else None
        except (TypeError, ValueError):
            age = None
        if age is not None and not 3 <= age <= 120:
            age = None

        record = {
            "date": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "name": clean_text(data.get("name"), MAX_NAME).replace("\n", " "),
            "age": age,
            "rating": rating,
            "text": text,
            "wish": wish,
        }
        try:
            with lock:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(DATA_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            # чаще всего — чужой владелец файла: chown -R www-data:www-data /var/lib/lovushki
            print("Отзыв не записался:", e, file=sys.stderr, flush=True)
            self.reply(500, {"ok": False, "error": "write"})
            return
        note_sent(computer, net)
        # письмо — в отдельном потоке, чтобы человек на сайте не ждал почту
        threading.Thread(target=mail_feedback, args=(record,), daemon=True).start()
        self.reply(200, {"ok": True})


last_send = [0.0]


def _send_all(handler):
    # кнопку на странице жмёт только сам владелец: свой заголовок чужой сайт подставить не может
    if handler.headers.get("X-Admin-Action") != "send":
        handler.reply(403, {"ok": False, "error": "Запрос не со страницы «Админ данные»."})
        return
    if not mail_ready():
        handler.reply(400, {"ok": False, "error": "Почта не настроена: запусти на сервере sh deploy/feedback-setup.sh."})
        return
    if time.time() - last_send[0] < 20:
        handler.reply(429, {"ok": False, "error": "Только что отправлено — подожди полминуты."})
        return
    last_send[0] = time.time()
    try:
        count = mail_all()
    except Exception as e:  # noqa: BLE001
        print("Выгрузка на почту не отправилась:", e, file=sys.stderr, flush=True)
        handler.reply(502, {"ok": False, "error": mail_error(e)})
        return
    handler.reply(200, {"ok": True, "count": count})


Handler.send_all = _send_all


def _delete(handler):
    # как и рассылка — только со страницы «Админ данные»: чужой сайт свой заголовок не подставит
    if handler.headers.get("X-Admin-Action") != "delete":
        handler.reply(403, {"ok": False, "error": "Запрос не со страницы «Админ данные»."})
        return
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        data = json.loads(handler.rfile.read(min(length, 256 * 1024)).decode("utf-8")) if length > 0 else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("all") is True:
        removed = delete_records(lambda i, r: False)
    else:
        ids = {str(x) for x in (data.get("ids") or []) if isinstance(x, str)}
        if not ids:
            handler.reply(400, {"ok": False, "error": "Не выбрано ни одного отзыва."})
            return
        removed = delete_records(lambda i, r: record_id(r) not in ids)
    handler.reply(200, {"ok": True, "removed": removed, "left": len(read_all())})


Handler.delete = _delete


def print_list():
    items = read_all()
    if not items:
        print("Отзывов пока нет.")
        return
    for i, r in enumerate(items, 1):
        print(f"№{i}  {ru_date(r.get('date'))}  {stars(r.get('rating'))}  {who(r)}")
        if r.get("text"):
            print("   Отзыв: " + r["text"].replace("\n", "\n          "))
        if r.get("wish"):
            print("   Добавить: " + r["wish"].replace("\n", "\n             "))
        print()
    s = stats()
    print(f"Всего: {len(items)}" + (f", средняя оценка {num(s['average'])}" if s["average"] else ""))


if __name__ == "__main__":
    if "--list" in sys.argv:
        print_list()
    elif "--clear" in sys.argv:
        n = delete_records(lambda i, r: False)
        print(f"Удалено отзывов: {n}. Они отложены в {TRASH_FILE}.")
    elif "--keep" in sys.argv:
        try:
            keep_n = int(sys.argv[sys.argv.index("--keep") + 1])
        except (IndexError, ValueError):
            print("Напиши, сколько первых отзывов оставить: --keep 2")
            sys.exit(1)
        n = delete_records(lambda i, r: i < keep_n)
        print(f"Оставлено первых: {min(keep_n, len(read_all()))}, удалено: {n}. Удалённые отложены в {TRASH_FILE}.")
    elif "--mail-all" in sys.argv:
        if not mail_ready():
            print("Почта не настроена: запусти sh deploy/feedback-setup.sh")
            sys.exit(1)
        try:
            print("Отправлено отзывов:", mail_all(), "на", MAIL["to"])
        except Exception as e:  # noqa: BLE001
            print(mail_error(e))
            sys.exit(1)
    elif "--test-mail" in sys.argv:
        if not mail_ready():
            print("Почта не настроена: запусти sh deploy/feedback-setup.sh")
            sys.exit(1)
        try:
            send_mail("Проверка: отзывы «Восьми ловушек»", "Почта настроена — новые отзывы будут приходить сюда.")
        except Exception as e:  # noqa: BLE001
            print(mail_error(e))
            sys.exit(1)
        print("Письмо отправлено на", MAIL["to"])
    else:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
