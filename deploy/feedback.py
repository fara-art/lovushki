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

  POST /api/players         снимок аккаунта из игры: кто играет и как далеко прошёл
  GET  /admin/igroki        вкладка «Кто играл»: поиск по фамилии, фильтр по классу и игре
  GET  /admin/igroki.xlsx   тот же список в Excel (?klass=7Б&game=udar — только выбранные)
  POST /admin/igroki/delete {"ids": [...]} — убрать игроков из списка

Ученик (по желанию, и в отзыве, и в игре): имя, фамилия и класс — чтобы учитель нашёл
его в списке. Без отметки «Я ученик» фамилия и класс не спрашиваются и не хранятся.

Имя и возраст — по желанию. Адрес отправителя не сохраняется. Поле website — ловушка
для ботов: человек его не видит и не заполняет.

Кулдаун: с одного компьютера (поле client — случайная метка из браузера) — не больше 20
отзывов за 10 минут и 120 за сутки; с одного адреса — больше, потому что в школе весь класс
выходит в интернет через один адрес. Метки и адреса держатся
только в памяти, зашифрованными (хэш с солью), и забываются через сутки.

Отзывы лежат в /var/lib/lovushki/feedback.jsonl (по строке JSON на отзыв).
Если настроена почта (/etc/lovushki/feedback.env, см. deploy/feedback-setup.sh),
каждый новый отзыв ещё и приходит письмом.

На сервере:  python3 /opt/lovushki/feedback.py --list   — прочитать все отзывы
             python3 /opt/lovushki/feedback.py --players — кто играл
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
import secrets
import smtplib
import ssl
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote

HOST = "127.0.0.1"
PORT = int(os.environ.get("FEEDBACK_PORT", "8787"))
DATA_DIR = os.environ.get("FEEDBACK_DIR", "/var/lib/lovushki")
DATA_FILE = os.path.join(DATA_DIR, "feedback.jsonl")
TRASH_FILE = os.path.join(DATA_DIR, "feedback.deleted.jsonl")
PLAYERS_FILE = os.path.join(DATA_DIR, "players.json")
PLAYERS_TRASH = os.path.join(DATA_DIR, "players.deleted.jsonl")
MAX_PLAYERS = 50000
GAMES = {"lovushki": "Восемь ловушек", "udar": "Двойной удар"}
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

# Файлы с отзывами и фамилиями учеников читает только сама служба (и root):
# всё, что она создаёт, — без доступа для остальных пользователей сервера
os.umask(0o077)

lock = threading.Lock()

# ---------- кулдаун: чтобы с одного компьютера не сыпались отзывы ----------
WINDOW = 10 * 60            # короткое окно, секунд
WINDOW_LIMIT = 20           # отзывов за 10 минут с одного компьютера
DAY_LIMIT = 120             # отзывов за сутки с одного компьютера
# Целый класс сидит за одним адресом школы: 30 детей пишут отзыв в конце урока
NET_WINDOW_LIMIT = 200
NET_DAY_LIMIT = 1000
# Новых аккаунтов в списке «Кто играл» с одного адреса за сутки: школе хватит с запасом
# (30 учеников × 2 игры × несколько классов), а завалить список мусором не выйдет
NEW_PLAYERS_DAY = 2000
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
        for k, who, w_limit, d_limit in ((computer, "с этого компьютера", WINDOW_LIMIT, DAY_LIMIT),
                                         (ip, "из этой сети", NET_WINDOW_LIMIT, NET_DAY_LIMIT)):
            day = [t for t in recent.get(k, []) if now - t < 86400]
            short = [t for t in day if now - t < WINDOW]
            if len(short) >= w_limit:
                left = WINDOW - (now - short[-w_limit])
                return left, f"Слишком много отзывов {who} подряд. Следующий можно через {wait_minutes(left)} мин."
            if len(day) >= d_limit:
                left = 86400 - (now - day[-d_limit])
                return left, f"На сегодня отзывов {who} достаточно — спасибо! Можно будет снова завтра."
    return None


def note_sent(computer, ip, now=None):
    now = now or time.time()
    with recent_lock:
        for k in (computer, ip):
            recent[k] = [t for t in recent.get(k, []) if now - t < 86400] + [now]


def new_player_allowed(net, now=None):
    """Можно ли с этого адреса завести ещё один аккаунт сегодня; если да — засчитывает его."""
    if not net:
        return True
    now = now or time.time()
    with recent_lock:
        day = [t for t in recent.get(net, []) if now - t < 86400]
        if len(day) >= NEW_PLAYERS_DAY:
            recent[net] = day
            return False
        recent[net] = day + [now]
    return True


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
        os.chmod(path, 0o600)
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


stats_cache = [None, None]      # (время изменения и размер файла, готовый ответ)


def stats():
    """Число оценок и средняя. Адрес открыт всем, поэтому файл перечитываем, только когда
    он изменился: иначе частыми запросами можно было бы занять сервер чтением диска."""
    try:
        st = os.stat(DATA_FILE)
        mark = (st.st_mtime_ns, st.st_size)
    except OSError:
        mark = None
    if stats_cache[1] is not None and stats_cache[0] == mark:
        return dict(stats_cache[1])
    ratings = [r.get("rating") for r in read_all() if valid_rating(r.get("rating")) is not None]
    if not ratings:
        result = {"count": 0, "average": None}
    else:
        result = {"count": len(ratings), "average": round(sum(ratings) / len(ratings), 1)}
    stats_cache[0], stats_cache[1] = mark, result
    return dict(result)


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


def clean_grade(value):
    """«7 б», «7-Б», «11а» → «7Б», «11А». Класс — число от 1 до 11 и буква по желанию."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[-–]?\s*([А-ЯЁа-яёA-Za-z]?)\s*(класс|кл\.?)?\s*", str(value or ""), re.I)
    if not m or not 1 <= int(m.group(1)) <= 11:
        return ""
    return m.group(1) + m.group(2).upper()


def clean_person(value, limit=40):
    """Имя или фамилия: буквы, пробел, дефис, апостроф; каждое слово с большой буквы."""
    text = clean_text(value, limit).replace("\n", " ")
    text = re.sub(r"[^A-Za-zА-Яа-яЁё' -]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -'")
    return "-".join(p[:1].upper() + p[1:] for p in text.split("-")) if text else ""


def clean_student(value):
    """{"first", "last", "grade"} — только если заполнено всё; иначе None."""
    if not isinstance(value, dict):
        return None
    st = {"first": clean_person(value.get("first")), "last": clean_person(value.get("last")),
          "grade": clean_grade(value.get("grade"))}
    if len(st["first"]) < 2 or len(st["last"]) < 2 or not st["grade"]:
        return None
    return st


def full_name(st):
    return f"{st['last']} {st['first']}" if st else ""


def who(r):
    st = r.get("student")
    if st:
        return f"{full_name(st)}, {st['grade']} класс"
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


def col_name(i):
    """0 → A, 25 → Z, 26 → AA."""
    name = ""
    i += 1
    while i:
        i, rest = divmod(i - 1, 26)
        name = chr(65 + rest) + name
    return name


def make_xlsx(sheet_name, head, widths, table):
    """Таблица: шапка жирная и закреплена, ширина столбцов по смыслу, длинный текст переносится.
    table — список строк, каждая — список значений (числа остаются числами)."""
    def cell(ref, value, style):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{x(value)}</t></is></c>'

    rows = ['<row r="1">' + "".join(cell(f"{col_name(i)}1", h, 1) for i, h in enumerate(head)) + "</row>"]
    for n, values in enumerate(table, start=2):
        rows.append(f'<row r="{n}">' + "".join(cell(f"{col_name(i)}{n}", v, 2) for i, v in enumerate(values)) + "</row>")
    last = max(1, len(rows))
    end = col_name(len(head) - 1)
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(rows)}</sheetData>"
        f'<autoFilter ref="A1:{end}{last}"/>'
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
         f'<sheets><sheet name="{x(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
         f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">\'{x(sheet_name)}\'!$A$1:${end}${last}</definedName></definedNames>'
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

def export_xlsx(items=None):
    """Все отзывы (или свой список). Дата — текстом «15.10.2026 20:51», иначе Excel превращает
    её в число и показывает «#####»."""
    head = ["Дата", "Имя", "Фамилия", "Класс", "Возраст", "Оценка", "Отзыв", "Что добавить"]
    widths = [18, 16, 18, 8, 10, 10, 60, 50]
    table = []
    for r in read_all() if items is None else items:
        rating = valid_rating(r.get("rating"))
        st = r.get("student") or {}
        table.append([ru_date(r.get("date")), st.get("first") or r.get("name") or "", st.get("last", ""),
                      st.get("grade", ""), r.get("age") if r.get("age") else "",
                      rating if rating is not None else "", r.get("text") or "", r.get("wish") or ""])
    return make_xlsx("Отзывы", head, widths, table)


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

ADMIN_CSS = """:root{--ink:#070B14;--slab:#111A2B;--fog:#253552;--paper:#EAF0FA;--muted:#8E9BB8;--lamp:#FFC46B;--mint:#3EE6A8;--teal:#22D3C5;--sky:#49C3F5;--alarm:#FF6F61;
--grad:linear-gradient(100deg,#3EE6A8,#22D3C5 50%,#49C3F5);--display:"Unbounded",system-ui,sans-serif;--body:"Golos Text",system-ui,sans-serif}
*{box-sizing:border-box}
html{min-height:100%;background:var(--ink)}
body{margin:0;min-height:100vh;font:16px/1.55 var(--body);color:var(--paper)}
/* фон — отдельным слоем во весь экран: background-attachment:fixed телефоны не умеют */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background:radial-gradient(1100px 700px at 8% -10%,rgba(14,140,100,.38),transparent 65%),
radial-gradient(900px 700px at 100% 10%,rgba(13,100,150,.34),transparent 65%),
radial-gradient(900px 800px at 50% 110%,rgba(90,55,150,.28),transparent 65%),
linear-gradient(180deg,#081320,#070B14 60%)}
main{max-width:1080px;margin:0 auto;padding:32px max(16px,env(safe-area-inset-right)) 70px max(16px,env(safe-area-inset-left))}
header{display:flex;align-items:center;gap:14px;margin-bottom:26px}
header img{width:46px;height:46px;filter:drop-shadow(0 0 14px rgba(62,230,168,.45))}
h1{margin:0;font:800 30px/1.1 var(--display);background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
header p{margin:4px 0 0;color:var(--muted);font-size:15px}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) minmax(0,1.3fr);gap:14px;margin-bottom:22px}
.tile{padding:18px 20px;border-radius:18px;background:linear-gradient(180deg,rgba(22,32,52,.82),rgba(13,20,35,.82));border:1px solid rgba(120,160,210,.16)}
.tile__k{display:block;font:700 11px/1 var(--display);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:12px}
.tile b{font:800 34px/1 var(--display);font-variant-numeric:tabular-nums}
.tile .stars{font-size:18px;margin-top:10px}
.dist__row{display:grid;grid-template-columns:34px 1fr 28px;align-items:center;gap:8px;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.dist__row i{height:8px;border-radius:4px;background:rgba(120,160,210,.14);overflow:hidden}
.dist__row b{display:block;height:100%;border-radius:4px;background:var(--lamp)}
.dist__row em{font-style:normal;text-align:right}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:22px}
.btn{display:inline-flex;align-items:center;padding:11px 18px;border-radius:12px;border:0;background:var(--grad);color:var(--ink);font:700 14px var(--display);text-decoration:none;cursor:pointer}
.btn--ghost{background:rgba(17,26,43,.9);color:var(--paper);border:1px solid #2E4366}
.btn:hover{filter:brightness(1.08)} .btn:disabled{opacity:.6;cursor:wait}
.hint{color:var(--muted);font-size:14px} code{background:var(--slab);padding:2px 6px;border-radius:6px}
#sendStatus{font-size:14px} #sendStatus.ok{color:var(--mint)} #sendStatus.err{color:var(--alarm)}
.listhead{display:flex;align-items:center;justify-content:space-between;margin:6px 0 12px;gap:10px}
.listhead h2{margin:0;font:800 18px var(--display)} .listhead h2 .hint{font:400 14px var(--body);margin-left:6px}
.link{background:none;border:0;color:var(--teal);font:600 14px var(--body);cursor:pointer;padding:6px;white-space:nowrap}
.list{display:flex;flex-direction:column;gap:10px}
.card{border-radius:16px;background:linear-gradient(180deg,rgba(22,32,52,.86),rgba(13,20,35,.86));border:1px solid rgba(120,160,210,.16);transition:border-color .2s}
.card[open]{border-color:rgba(62,230,168,.4)}
summary{list-style:none;display:grid;grid-template-columns:minmax(0,1fr) auto auto 24px;align-items:center;gap:16px;padding:16px 18px;cursor:pointer}
summary::-webkit-details-marker{display:none}
summary:focus-visible{outline:2px solid var(--mint);outline-offset:-2px;border-radius:16px}
.who{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.who b{font-weight:600} .age{color:var(--muted);margin-left:8px}
.rate{display:inline-flex;align-items:center;gap:8px;justify-self:start;white-space:nowrap}
.stars{position:relative;display:inline-block;width:max-content;color:#3E4F70;letter-spacing:2px;white-space:nowrap;font-size:17px;line-height:1}
.stars__fill{position:absolute;left:0;top:0;width:var(--v);overflow:hidden;color:var(--lamp)}
.stars--none{color:var(--muted);font-size:13px;letter-spacing:0}
.score{color:var(--lamp);font-variant-numeric:tabular-nums;font-weight:700}
.date{color:var(--muted);font-size:14px;font-variant-numeric:tabular-nums;white-space:nowrap}
.chev{width:24px;height:24px;border-radius:8px;display:grid;place-items:center;background:rgba(120,160,210,.12)}
.chev::before{content:"";width:7px;height:7px;border-right:2px solid var(--paper);border-bottom:2px solid var(--paper);transform:translateY(-2px) rotate(45deg);transition:transform .2s}
.card[open] .chev::before{transform:translateY(2px) rotate(-135deg)}
.body{padding:2px 18px 18px;display:flex;flex-direction:column;gap:14px;border-top:1px solid rgba(120,160,210,.12);margin:0 18px;padding-left:0;padding-right:0;padding-top:14px}
.field .label{display:block;font:700 11px/1 var(--display);letter-spacing:.12em;text-transform:uppercase;color:var(--teal);margin-bottom:6px}
.field p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.mini{display:flex;gap:10px;flex-wrap:wrap}
.mini a{font-size:13px;color:var(--paper);text-decoration:none;padding:7px 12px;border-radius:10px;border:1px solid #2E4366;background:rgba(7,11,20,.5)}
.mini a:hover{border-color:var(--mint)}
.row{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;align-items:start}
.pick{display:none;width:44px;height:56px;place-items:center;cursor:pointer}
.pick input{width:22px;height:22px;accent-color:var(--alarm);cursor:pointer}
.selecting .row{grid-template-columns:44px minmax(0,1fr)} .selecting .pick{display:grid}
.row.chosen .card{border-color:rgba(255,111,97,.6)}
.del{margin-left:auto;font:600 13px var(--body);color:#FFB4AB;padding:7px 12px;border-radius:10px;border:1px solid rgba(255,111,97,.45);background:rgba(255,111,97,.08);cursor:pointer}
.del:hover,.del.ask{background:var(--alarm);color:#2A0905;border-color:var(--alarm)}
.btn--danger{background:rgba(255,111,97,.12);color:#FFB4AB;border:1px solid rgba(255,111,97,.5)}
.btn--danger.ask,.btn--danger:hover{background:var(--alarm);color:#2A0905;filter:none}
.selbar{display:none;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 14px;padding:12px 14px;border-radius:14px;border:1px solid rgba(255,111,97,.4);background:rgba(40,16,20,.6)}
.selecting .selbar{display:flex}
#delStatus{font-size:14px} #delStatus.err{color:var(--alarm)}
.empty{padding:40px 20px;text-align:center;color:var(--muted);border:1px dashed #2E4366;border-radius:16px}
@media (max-width:820px){.stats{grid-template-columns:1fr 1fr}}
@media (max-width:620px){summary{grid-template-columns:minmax(0,1fr) 24px;gap:6px 12px}
summary .who{grid-column:1} summary .rate{grid-column:1;grid-row:2}
summary .date{grid-column:1;grid-row:3} summary .chev{grid-column:2;grid-row:1/span 3}
.tile{padding:16px} .tile__k{font-size:10px;letter-spacing:.08em;line-height:1.3} .tile b{font-size:28px} h1{font-size:24px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
/* вкладки «Отзывы» / «Кто играл» */
.tabs{display:flex;gap:6px;margin:-8px 0 24px;padding:5px;border-radius:14px;background:rgba(17,26,43,.7);border:1px solid rgba(120,160,210,.16);width:max-content;max-width:100%}
.tabs a{padding:9px 16px;border-radius:10px;color:var(--muted);font:700 14px var(--display);text-decoration:none;white-space:nowrap}
.tabs a.on{background:var(--grad);color:var(--ink)}
.tabs a:not(.on):hover{color:var(--paper)}
.pupil{display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;color:var(--ink);background:var(--mint)}
/* список игроков */
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.filters input,.filters select{padding:10px 12px;border-radius:12px;border:1px solid #2E4366;background:#0E1526;color:var(--paper);font:15px var(--body)}
.filters input[type=search]{flex:1 1 220px;min-width:0}
.filters label{display:inline-flex;gap:8px;align-items:center;color:var(--muted);font-size:14px;cursor:pointer}
.filters input[type=checkbox]{width:18px;height:18px;accent-color:var(--mint)}
.tablewrap{overflow-x:auto;border-radius:16px;border:1px solid rgba(120,160,210,.16);background:linear-gradient(180deg,rgba(22,32,52,.86),rgba(13,20,35,.86))}
table{width:100%;border-collapse:collapse;font-size:15px}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid rgba(120,160,210,.12);vertical-align:top}
th{font:700 11px/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);white-space:nowrap;cursor:pointer;user-select:none}
th[data-sort]:hover{color:var(--paper)} th.asc::after{content:" ▲"} th.desc::after{content:" ▼"}
tr:last-child td{border-bottom:0}
td small{display:block;color:var(--muted);font-size:13px}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.ok{color:var(--mint);font-weight:700;white-space:nowrap} .no{color:var(--muted)}
.game{display:inline-block;padding:2px 8px;border-radius:8px;font-size:12px;font-weight:700;white-space:nowrap;border:1px solid #2E4366}
.game--udar{color:var(--sky)} .game--lovushki{color:var(--mint)}
td.pick2{width:40px;display:none} .selecting td.pick2,.selecting th.pick2{display:table-cell} th.pick2{display:none;width:40px}
td.pick2 input{width:20px;height:20px;accent-color:var(--alarm)}
.count{color:var(--muted);font-size:14px;margin:0 0 10px}
@media (max-width:620px){th,td{padding:9px 10px;font-size:14px}}
"""


def tabs(active):
    """Переключатель вкладок вверху обеих страниц админки."""
    items = [("feedback", "./", "Отзывы"), ("players", "igroki", "Кто играл")]
    return '<nav class="tabs">' + "".join(
        f'<a href="{href}"{' class="on"' if key == active else ""}>{label}</a>' for key, href, label in items) + "</nav>"


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
        st = r.get("student")
        name = html.escape(full_name(st) if st else (r.get("name") or "Без имени"))
        age = (f'<span class="pupil">{html.escape(st["grade"])} класс</span>' if st
               else f'<span class="age">{r["age"]} лет</span>' if r.get("age") else "")
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
{ADMIN_CSS}</style></head><body><main>
<header><img src="/favicon.svg" alt=""><div><h1>Админ данные</h1><p>Отзывы об игре «Восемь ловушек»</p></div></header>
{tabs("feedback")}

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


# ---------- «Кто играл»: аккаунты из обеих игр ----------
# Игра присылает снимок аккаунта целиком (ник, ученик ли, сколько прошёл, есть ли сертификат),
# поэтому повторная отправка ничего не портит: запись просто обновляется.
# Ключ — игра + случайная метка компьютера + номер аккаунта в этом браузере.

players_lock = threading.Lock()


def read_players():
    try:
        with open(PLAYERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_players(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PLAYERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    same_owner(tmp)
    os.replace(tmp, PLAYERS_FILE)


def small_int(value, lo=0, hi=1000):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if lo <= value <= hi else None


def clean_progress(game, value):
    """Только известные поля и в разумных пределах — что-то другое в базу не попадёт."""
    value = value if isinstance(value, dict) else {}
    if game == "lovushki":
        out = {"finished": small_int(value.get("finished")) or 0}
        best = value.get("best") if isinstance(value.get("best"), dict) else {}
        safe, total = small_int(best.get("safe"), 0, 100), small_int(best.get("total"), 1, 100)
        if safe is not None and total and safe <= total:
            out["best"] = {"safe": safe, "total": total, "level": clean_text(best.get("level"), 20).replace("\n", " ")}
        return out
    out = {"levels": small_int(value.get("levels"), 0, 50) or 0,
           "of": small_int(value.get("of"), 1, 50) or 1,
           "stars": small_int(value.get("stars"), 0, 150) or 0}
    out["levels"] = min(out["levels"], out["of"])
    return out


def progress_text(p):
    pr = p.get("progress") or {}
    if p.get("game") == "lovushki":
        best = pr.get("best")
        if not pr.get("finished"):
            return "ещё не прошёл район"
        times = pr["finished"]
        text = f"прошёл {times} раз" + ("а" if times % 10 in (2, 3, 4) and times % 100 not in (12, 13, 14) else "")
        if best:
            text += f" · лучший {best['safe']} из {best['total']}" + (f" ({best['level']})" if best.get("level") else "")
        return text
    return f"уровней {pr.get('levels', 0)} из {pr.get('of', 1)} · ★ {pr.get('stars', 0)}"


def save_player(data, now=None, net=None):
    """Обновляет запись игрока. "ok" — записано, "bad" — запрос не похож на игру,
    "busy" — с этого адреса (net) сегодня слишком много новых аккаунтов или список полон."""
    game = data.get("game")
    if game not in GAMES:
        return "bad"
    client = re.sub(r"[^A-Za-z0-9-]", "", str(data.get("client") or ""))[:40]
    pid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("pid") or ""))[:40]
    nick = clean_text(data.get("nick"), 30).replace("\n", " ")
    if len(client) < 8 or not pid or not nick:
        return "bad"
    key_ = hashlib.sha1(f"{game}:{client}:{pid}".encode("utf-8")).hexdigest()[:16]
    stamp = (now or datetime.now(TZ)).strftime("%Y-%m-%d %H:%M")
    with players_lock:
        players = read_players()
        old = players.get(key_)
        if old is None and (len(players) >= MAX_PLAYERS or not new_player_allowed(net)):
            return "busy"
        rec = dict(old or {"first_seen": stamp})
        rec.update({"game": game, "nick": nick, "last_seen": stamp,
                    "student": clean_student(data.get("student")),
                    "progress": clean_progress(game, data.get("progress"))})
        # сертификат: запоминаем первый раз и больше не снимаем
        if data.get("cert") is True and not rec.get("cert"):
            rec["cert"] = stamp
        players[key_] = rec
        write_players(players)
    return "ok"


def player_rows(game=None, grade=None):
    """Список для страницы и Excel: свежие сверху."""
    items = []
    for k, p in read_players().items():
        if not isinstance(p, dict) or p.get("game") not in GAMES:
            continue
        st = p.get("student") or {}
        if game and p["game"] != game:
            continue
        if grade and st.get("grade", "").lower() != grade.lower():
            continue
        items.append(dict(p, id=k))
    items.sort(key=lambda p: p.get("last_seen", ""), reverse=True)
    return items


def export_players_xlsx(game=None, grade=None):
    head = ["Фамилия", "Имя", "Класс", "Ник в игре", "Игра", "Результат", "Сертификат", "Первый раз", "Последний раз"]
    widths = [18, 16, 8, 16, 18, 40, 18, 18, 18]
    table = []
    for p in player_rows(game, grade):
        st = p.get("student") or {}
        table.append([st.get("last", ""), st.get("first", ""), st.get("grade", ""), p.get("nick", ""),
                      GAMES[p["game"]], progress_text(p), ru_date(p["cert"]) if p.get("cert") else "нет",
                      ru_date(p.get("first_seen")), ru_date(p.get("last_seen"))])
    return make_xlsx("Кто играл", head, widths, table)


def delete_players(ids):
    with players_lock:
        players = read_players()
        gone = [(k, players.pop(k)) for k in list(players) if k in ids]
        if not gone:
            return 0
        os.makedirs(DATA_DIR, exist_ok=True)
        stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        with open(PLAYERS_TRASH, "a", encoding="utf-8") as f:
            for k, p in gone:
                f.write(json.dumps(dict(p, id=k, deleted=stamp), ensure_ascii=False) + "\n")
        same_owner(PLAYERS_TRASH)
        write_players(players)
        return len(gone)


def grade_key(g):
    m = re.match(r"(\d+)(.*)", g or "")
    return (int(m.group(1)), m.group(2)) if m else (99, g or "")


def players_page():
    items = player_rows()
    now = datetime.now(TZ)
    pupils = [p for p in items if p.get("student")]
    certs = [p for p in items if p.get("cert")]
    week = 0
    for p in items:
        try:
            if now - datetime.strptime(p.get("last_seen", ""), "%Y-%m-%d %H:%M").replace(tzinfo=TZ) <= timedelta(days=7):
                week += 1
        except ValueError:
            pass
    grades = sorted({p["student"]["grade"] for p in pupils}, key=grade_key)
    rows = []
    for p in items:
        st = p.get("student") or {}
        name = (f'<b>{html.escape(full_name(st))}</b>' if st else '<span class="no">не ученик</span>')
        cert = (f'<span class="ok">✓ есть</span><small>{html.escape(ru_date(p["cert"]))}</small>' if p.get("cert")
                else '<span class="no">нет</span>')
        search = " ".join([st.get("last", ""), st.get("first", ""), p.get("nick", ""), st.get("grade", "")]).lower()
        rows.append(
            f'<tr data-id="{p["id"]}" data-game="{p["game"]}" data-grade="{html.escape(st.get("grade", ""))}" '
            f'data-pupil="{1 if st else 0}" data-q="{html.escape(search)}" data-name="{html.escape(full_name(st).lower() or "я" + p.get("nick", "").lower())}" '
            f'data-seen="{html.escape(p.get("last_seen", ""))}" data-cert="{1 if p.get("cert") else 0}">'
            f'<td class="pick2"><input type="checkbox" value="{p["id"]}" aria-label="Выбрать"></td>'
            f'<td>{name}</td><td class="num">{html.escape(st.get("grade", "")) or "—"}</td>'
            f'<td>{html.escape(p.get("nick", ""))}</td>'
            f'<td><span class="game game--{p["game"]}">{GAMES[p["game"]]}</span></td>'
            f'<td>{html.escape(progress_text(p))}</td><td>{cert}</td>'
            f'<td class="num">{html.escape(ru_date(p.get("last_seen")))}<small>с {html.escape(ru_date(p.get("first_seen")))}</small></td></tr>')
    grade_opts = "".join(f'<option value="{html.escape(g)}">{html.escape(g)}</option>' for g in grades)
    table = (f'''<div class="tablewrap"><table><thead><tr><th class="pick2"></th><th data-sort="name">Ученик</th><th data-sort="grade">Класс</th>
<th>Ник</th><th>Игра</th><th>Результат</th><th data-sort="cert">Сертификат</th><th data-sort="seen" class="desc">Когда играл</th></tr></thead>
<tbody id="rows">{"".join(rows)}</tbody></table></div>''' if items else
             '<div class="empty">Пока никто не играл. Аккаунт появится здесь, как только его создадут в любой из двух игр.</div>')
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Кто играл — Восемь ловушек</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/fonts.css">
<style>
{ADMIN_CSS}</style></head><body><main>
<header><img src="/favicon.svg" alt=""><div><h1>Админ данные</h1><p>Кто играл в «Восемь ловушек» и «Двойной удар»</p></div></header>
{tabs("players")}

<section class="stats">
<div class="tile"><span class="tile__k">Всего аккаунтов</span><b>{len(items)}</b></div>
<div class="tile"><span class="tile__k">Ученики</span><b>{len(pupils)}</b></div>
<div class="tile"><span class="tile__k">С сертификатом</span><b>{len(certs)}</b></div>
<div class="tile"><span class="tile__k">Играли за 7 дней</span><b>{week}</b></div>
</section>

<div class="filters">
<input type="search" id="q" placeholder="Фамилия, имя или ник" aria-label="Поиск">
<select id="fGame" aria-label="Игра"><option value="">Обе игры</option><option value="lovushki">Восемь ловушек</option><option value="udar">Двойной удар</option></select>
<select id="fGrade" aria-label="Класс"><option value="">Все классы</option>{grade_opts}</select>
<label><input type="checkbox" id="fPupil"> только ученики</label>
<label><input type="checkbox" id="fCert"> только с сертификатом</label>
</div>
<div class="bar"><a class="btn" id="xlsx" href="igroki.xlsx">Скачать список — Excel</a>
{'<button class="link" id="pickMode" type="button">Выбрать для удаления</button>' if items else ''}</div>
<div id="listWrap">
<div class="selbar"><button class="btn btn--danger" id="delPicked" type="button" disabled>Убрать выбранных (0)</button>
<button class="link" id="pickCancel" type="button">Отмена</button><span id="delStatus" role="status"></span></div>
<p class="count" id="count"></p>
{table}
</div>
</main>
<script>
var rows = Array.prototype.slice.call(document.querySelectorAll("#rows tr"));
var q = document.getElementById("q"), fg = document.getElementById("fGame"), fk = document.getElementById("fGrade"),
    fp = document.getElementById("fPupil"), fc = document.getElementById("fCert"), cnt = document.getElementById("count"),
    xl = document.getElementById("xlsx");
function apply() {{
  var words = q.value.toLowerCase().split(/\\s+/).filter(Boolean), n = 0;
  rows.forEach(function (r) {{
    var ok = (!fg.value || r.dataset.game === fg.value) && (!fk.value || r.dataset.grade === fk.value) &&
      (!fp.checked || r.dataset.pupil === "1") && (!fc.checked || r.dataset.cert === "1") &&
      words.every(function (w) {{ return r.dataset.q.indexOf(w) >= 0; }});
    r.hidden = !ok; if (ok) n++;
  }});
  if (cnt) cnt.textContent = rows.length ? "Показано: " + n + " из " + rows.length : "";
  var p = [];
  if (fg.value) p.push("game=" + encodeURIComponent(fg.value));
  if (fk.value) p.push("klass=" + encodeURIComponent(fk.value));
  xl.href = "igroki.xlsx" + (p.length ? "?" + p.join("&") : "");
  xl.textContent = fk.value ? "Скачать " + fk.value + " — Excel" : "Скачать список — Excel";
}}
[q, fg, fk, fp, fc].forEach(function (e) {{ e.addEventListener("input", apply); e.addEventListener("change", apply); }});
apply();

/* сортировка по нажатию на заголовок */
function gkey(g) {{ var m = /^(\\d+)(.*)$/.exec(g || ""); return m ? ("0" + m[1]).slice(-2) + m[2] : "99"; }}
document.querySelectorAll("th[data-sort]").forEach(function (th) {{
  th.addEventListener("click", function () {{
    var k = th.dataset.sort, asc = !th.classList.contains("asc");
    document.querySelectorAll("th[data-sort]").forEach(function (o) {{ o.classList.remove("asc", "desc"); }});
    th.classList.add(asc ? "asc" : "desc");
    rows.sort(function (a, b) {{
      var x = a.dataset[k], y = b.dataset[k];
      if (k === "grade") {{ x = gkey(x) + a.dataset.name; y = gkey(y) + b.dataset.name; }}
      return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
    }});
    var body = document.getElementById("rows");
    rows.forEach(function (r) {{ body.appendChild(r); }});
  }});
}});

/* удаление: выбрать строки и нажать дважды */
var wrap = document.getElementById("listWrap"), ds = document.getElementById("delStatus"), pm = document.getElementById("pickMode");
function picked() {{ return Array.prototype.map.call(document.querySelectorAll(".pick2 input:checked"), function (i) {{ return i.value; }}); }}
function syncPicked() {{ var n = picked().length, dp = document.getElementById("delPicked"); dp.textContent = "Убрать выбранных (" + n + ")"; dp.disabled = !n; }}
if (pm) {{
  pm.addEventListener("click", function () {{ wrap.classList.toggle("selecting"); }});
  document.getElementById("pickCancel").addEventListener("click", function () {{
    wrap.classList.remove("selecting"); document.querySelectorAll(".pick2 input").forEach(function (i) {{ i.checked = false; }}); syncPicked();
  }});
  wrap.addEventListener("change", syncPicked);
  var dp = document.getElementById("delPicked");
  dp.addEventListener("click", function () {{
    if (!dp.classList.contains("ask")) {{
      dp.classList.add("ask"); dp.textContent = "Точно убрать " + picked().length + "?";
      setTimeout(function () {{ dp.classList.remove("ask"); syncPicked(); }}, 4000); return;
    }}
    dp.disabled = true;
    fetch("igroki/delete", {{ method: "POST", headers: {{ "X-Admin-Action": "delete", "Content-Type": "application/json" }}, body: JSON.stringify({{ ids: picked() }}) }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{ if (d.ok) location.reload(); else throw new Error(d.error || ""); }})
      .catch(function (e) {{ dp.disabled = false; ds.className = "err"; ds.textContent = e.message || "Не получилось убрать."; }});
  }});
}}
</script>
</body></html>"""


# Заголовки защиты для всего, что отдаёт служба. Страницы админки получают свой CSP
# с одноразовой меткой (nonce): выполняется только наш скрипт, даже если бы в текст
# отзыва или в фамилию как-то попал чужой код.
BASE_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def admin_csp(nonce):
    return ("default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self'; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "lovushki-feedback"
    sys_version = ""
    timeout = 20            # зависшее соединение не держит поток вечно

    def log_message(self, fmt, *args):
        # без журнала запросов: в нём оказался бы адрес отправителя
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_bytes(code, body.encode("utf-8"), ctype, extra)

    def send_bytes(self, code, data, ctype, extra=None):
        headers = {
            "Content-Type": ctype,
            "Cache-Control": "no-store",
            "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": BASE_CSP,
        }
        headers.update(extra or {})
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def reply(self, code, payload):
        self.send(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def send_admin(self, page):
        """Страница админки: её скрипт помечаем одноразовой меткой и разрешаем только его."""
        nonce = secrets.token_urlsafe(18)
        self.send(200, page.replace("<script>", f'<script nonce="{nonce}">'), "text/html; charset=utf-8",
                  {"Content-Security-Policy": admin_csp(nonce)})

    def is_json(self):
        """Чужой сайт не может молча отправить application/json из браузера посетителя —
        так отзывы и список игроков нельзя засорять через чужие страницы."""
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json"

    def read_json(self, limit):
        """Тело запроса как словарь; None — если пусто, слишком велико или не JSON."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/feedback/stats":
            self.reply(200, stats())
        elif path in ("/admin", "/admin/"):
            self.send_admin(admin_page())
        elif path == "/admin/igroki":
            self.send_admin(players_page())
        elif path == "/admin/igroki.xlsx":
            query = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            game = (query.get("game") or [""])[0]
            grade = clean_grade((query.get("klass") or [""])[0])
            data = export_players_xlsx(game if game in GAMES else None, grade or None)
            name = "igroki-" + (grade + "-" if grade else "") + datetime.now(TZ).strftime("%Y-%m-%d") + ".xlsx"
            # имя файла с буквой класса — по-русски через filename*, латиницей — запасное
            ascii_name = re.sub(r"[^A-Za-z0-9.-]", "", name.replace(grade + "-", "")) if grade else name
            self.send_bytes(200, data, XLSX_MIME,
                            {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})
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
        if self.path.split("?")[0] == "/admin/igroki/delete":
            if self.headers.get("X-Admin-Action") != "delete":
                self.reply(403, {"ok": False, "error": "Запрос не со страницы «Админ данные»."})
                return
            data = self.read_json(256 * 1024) or {}
            ids = {str(v) for v in (data.get("ids") or []) if isinstance(v, str)}
            if not ids:
                self.reply(400, {"ok": False, "error": "Никто не выбран."})
                return
            self.reply(200, {"ok": True, "removed": delete_players(ids)})
            return
        if self.path.split("?")[0] == "/api/players":
            data = self.read_json(MAX_BODY) if self.is_json() else None
            if data is None:
                self.reply(400, {"ok": False, "error": "json"})
                return
            # адрес — только для суточного лимита новых аккаунтов: хэш в памяти, в файл не пишется
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            try:
                result = save_player(data, net=key("newpl", ip))
            except OSError as e:
                print("Игрок не записался:", e, file=sys.stderr, flush=True)
                self.reply(500, {"ok": False, "error": "write"})
                return
            self.reply({"ok": 200, "bad": 400, "busy": 429}[result], {"ok": result == "ok"})
            return
        if self.path.split("?")[0] != "/api/feedback":
            self.reply(404, {"ok": False})
            return
        if not self.is_json():
            self.reply(415, {"ok": False, "error": "type"})
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

        student = clean_student(data.get("student"))
        record = {
            "date": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "name": student["first"] if student else clean_text(data.get("name"), MAX_NAME).replace("\n", " "),
            "age": None if student else age,
            "rating": rating,
            "text": text,
            "wish": wish,
        }
        if student:
            record["student"] = student
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
    elif "--players" in sys.argv:
        rows = player_rows()
        for p in rows:
            st = p.get("student")
            print(f"{ru_date(p.get('last_seen'))}  {GAMES[p['game']]:<15} {full_name(st) + ', ' + st['grade'] if st else '(не ученик)':<32} "
                  f"ник {p.get('nick')}  ·  {progress_text(p)}" + ("  ·  сертификат" if p.get("cert") else ""))
        print(f"Всего аккаунтов: {len(rows)}")
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
