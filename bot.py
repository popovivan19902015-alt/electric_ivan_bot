# -*- coding: utf-8 -*-
"""
Telegram-бот электрика для Казахстана.

Смета считается на сервере по прайсу. Клиент не передаёт цену:
кнопки присылают только короткие коды услуги, типа и комнаты.

Переменные окружения:
    TELEGRAM_TOKEN   — токен BotFather, обязателен
    KASPI_PHONE      — номер Kaspi, по умолчанию заглушка
    KASPI_HOLDER     — имя получателя, по умолчанию «Иван»
    ELECTRICIAN_ID   — Telegram ID электрика для /stats
                       если не задан, статистику видят все
    RAILWAY_VOLUME_MOUNT_PATH — каталог тома Railway для orders.json
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
import telebot

def _ensure_package(pip_name, module_name):
    try:
        __import__(module_name)
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])


_ensure_package("pyTelegramBotAPI==4.14.0", "telebot")
_ensure_package("python-dotenv==1.0.0", "dotenv")
_ensure_package("requests==2.31.0", "requests")

import telebot
from dotenv import load_dotenv
from telebot import types
from telebot.apihelper import ApiTelegramException


# Каталог бота и файл заказов. На Railway файл ляжет на том, если он подключён.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
ORDERS_PATH = os.path.join(DATA_DIR, "orders.json")

# Казахстан с 2024 года живёт в UTC+5 без перехода на летнее время.
KZ_TZ = timezone(timedelta(hours=5), name="UTC+5")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Номер подставляется позже: достаточно задать переменные, код менять не нужно.
KASPI_PHONE = os.getenv("KASPI_PHONE", "+7 7774222256")
KASPI_HOLDER = os.getenv("KASPI_HOLDER", "Иван")

# Заглушка нужна только чтобы модуль импортировался без токена (проверки расчёта).
# В Telegram с ней запросов нет: main() останавливается, если токен не задан.
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PLACEHOLDER_TOKEN = "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# Один замок на файл заказов и черновики: pyTelegramBotAPI зовёт обработчики из разных потоков.
LOCK = threading.RLock()
DRAFTS = {}

# Статусы, которые пишем в JSON, и подписи для клиента.
STATUS_WAIT = "ожидание_оплаты"
STATUS_PAID = "оплачено"
STATUS_LABELS = {
    STATUS_WAIT: "💳 Ожидание оплаты",
    STATUS_PAID: "⏳ В очереди",
}

ORDER_ID_RE = re.compile(r"^order_(\d+)$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("electrician-bot")


# Прайс. factor комнаты — числитель из 10, чтобы не ловить погрешность float.
# Материалы = базовая цена × коэффициент, работа не умножается.
# list_label — короткая фраза для списка заказов («Розетка мощная»).
SERVICES = {
    "socket": {
        "title": "Розетка",
        "button": "🔌 Розетка",
        "types": {
            "5a": {
                "title": "Однофазная (5A)",
                "service": "розетка",
                "type": "однофазная",
                "list_label": "Розетка однофазная",
                "materials": 3000,
                "work": 8000,
            },
            "16a": {
                "title": "Мощная (16A)",
                "service": "розетка",
                "type": "мощная",
                "list_label": "Розетка мощная",
                "materials": 5000,
                "work": 10000,
            },
            "gnd": {
                "title": "С заземлением",
                "service": "розетка",
                "type": "с заземлением",
                "list_label": "Розетка с заземлением",
                "materials": 6000,
                "work": 12000,
            },
        },
    },
    "switch": {
        "title": "Выключатель",
        "button": "💡 Выключатель",
        "types": {
            "1btn": {
                "title": "Однокнопочный",
                "service": "выключатель",
                "type": "однокнопочный",
                "list_label": "Выключатель однокнопочный",
                "materials": 2000,
                "work": 5000,
            },
            "2btn": {
                "title": "Двухкнопочный",
                "service": "выключатель",
                "type": "двухкнопочный",
                "list_label": "Выключатель двухкнопочный",
                "materials": 3000,
                "work": 6000,
            },
            "motion": {
                "title": "Датчик движения",
                "service": "выключатель",
                "type": "датчик движения",
                "list_label": "Датчик движения",
                "materials": 8000,
                "work": 8000,
            },
        },
    },
    "light": {
        "title": "Люстра/светильник",
        "button": "🕯️ Люстра/светильник",
        "types": {
            "simple": {
                "title": "Люстра простая",
                "service": "люстра",
                "type": "простая",
                "list_label": "Люстра простая",
                "materials": 4000,
                "work": 8000,
            },
            "complex": {
                "title": "Люстра сложная",
                "service": "люстра",
                "type": "сложная",
                "list_label": "Люстра сложная",
                "materials": 10000,
                "work": 15000,
            },
            "spot": {
                "title": "Точечные светильники (за штуку)",
                "service": "светильник",
                "type": "точечный",
                "list_label": "Точечные светильники",
                "materials": 2000,
                "work": 3000,
            },
        },
    },
    "cable": {
        "title": "Монтаж кабеля",
        "button": "🧵 Монтаж кабеля",
        "types": {
            "meter": {
                "title": "Кабель (за метр)",
                "service": "кабель",
                "type": "за метр",
                "list_label": "Монтаж кабеля",
                "materials": 500,
                "work": 1000,
            },
        },
    },
    "panel": {
        "title": "Замена щитка",
        "button": "🧰 Замена щитка",
        "types": {
            "replace": {
                "title": "Замена распределительного щитка",
                "service": "щиток",
                "type": "замена",
                "list_label": "Замена щитка",
                "materials": 20000,
                "work": 30000,
            },
        },
    },
    # В шаге меню этой позиции не было, но она есть в прайсе — иначе цену нельзя выбрать.
    "breaker": {
        "title": "Автомат в щитке",
        "button": "⚙️ Автомат в щитке",
        "types": {
            "one": {
                "title": "Автомат в щитке",
                "service": "автомат",
                "type": "в щитке",
                "list_label": "Автомат в щитке",
                "materials": 3000,
                "work": 5000,
            },
        },
    },
}

ROOMS = {
    "bedroom": {"title": "Спальня", "store": "спальня", "prep": "спальне", "factor": 10},
    "kitchen": {"title": "Кухня", "store": "кухня", "prep": "кухне", "factor": 12},
    "living": {"title": "Гостиная", "store": "гостиная", "prep": "гостиной", "factor": 13},
    "bath": {"title": "Ванная", "store": "ванная", "prep": "ванной", "factor": 15},
    "hall": {"title": "Коридор", "store": "коридор", "prep": "коридоре", "factor": 10},
}

ROOM_PREP = {room["store"]: room["prep"] for room in ROOMS.values()}


def money(amount):
    """Сумма в тенге с пробелом между тысячами: 16 000 ₸."""
    return f"{int(amount):,}".replace(",", " ") + " ₸"


def now_kz():
    return datetime.now(KZ_TZ)


def timestamp():
    return now_kz().strftime(TIME_FORMAT)


def parse_time(value):
    """Время из JSON. Некорректная строка не роняет статистику."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=KZ_TZ)
    except ValueError:
        return None


def calculate(service_key, type_key, room_key):
    """
    Смета по кодам кнопок.
    Пример: мощная розетка на кухне = 5 000 × 1.2 + 10 000 = 16 000.
    """
    service = SERVICES[service_key]
    item = service["types"][type_key]
    room = ROOMS[room_key]
    materials = (item["materials"] * room["factor"] + 5) // 10
    work = item["work"]
    return {
        "service_title": service["title"],
        "type_title": item["title"],
        "room_title": room["title"],
        "service": item["service"],
        "type": item["type"],
        "room": room["store"],
        "list_label": item["list_label"],
        "materials": materials,
        "work": work,
        "total": materials + work,
    }


def render_estimate(estimate):
    return (
        "📋 СМЕТА\n"
        "═════════════════════\n"
        f"Услуга: {estimate['service_title']}\n"
        f"Тип: {estimate['type_title']}\n"
        f"Комната: {estimate['room_title']}\n"
        "\n"
        f"Материалы: {money(estimate['materials'])}\n"
        f"Работа: {money(estimate['work'])}\n"
        "═════════════════════\n"
        f"ИТОГО К ОПЛАТЕ: {money(estimate['total'])}"
    )


def render_payment(order_id, price):
    return (
        "💰 СПОСОБ ОПЛАТЫ\n"
        "\n"
        f"Заказ: {order_id}\n"
        "\n"
        f"Переведите {money(price)} на Kaspi:\n"
        f"📱 Номер: {KASPI_PHONE} ({KASPI_HOLDER})\n"
        "\n"
        "После перевода нажмите:"
    )


def order_phrase(order):
    """Строка вида «Розетка мощная в кухне»."""
    label = None
    for service in SERVICES.values():
        for item in service["types"].values():
            if item["service"] == order.get("service") and item["type"] == order.get("type"):
                label = item["list_label"]
                break
    if not label:
        label = f"{order.get('service', 'Услуга')} {order.get('type', '')}".strip()
    prep = ROOM_PREP.get(order.get("room"), order.get("room") or "комнате")
    return f"{label} в {prep}"


def render_order_card(order_id, order):
    number = order_id.split("_", 1)[-1].lstrip("0") or "0"
    created = parse_time(order.get("created_at"))
    date_text = created.strftime("%Y-%m-%d") if created else str(order.get("created_at") or "—")
    status = STATUS_LABELS.get(order.get("status"), str(order.get("status") or "—"))
    try:
        price = money(int(order["price"]))
    except (KeyError, TypeError, ValueError):
        price = "—"
    return (
        f"╭─ Заказ #{number}\n"
        f"│ {order_phrase(order)}\n"
        f"│ Сумма: {price}\n"
        f"│ Статус: {status}\n"
        f"│ Дата: {date_text}\n"
        "╰─"
    )


def render_orders(user_id, orders):
    cards = []
    for order_id, order in sorted_user_orders(user_id, orders):
        cards.append(render_order_card(order_id, order))
    if not cards:
        return (
            "📭 У вас пока нет заказов.\n"
            "\n"
            "Нажмите «Новый заказ», чтобы рассчитать смету."
        )
    return "📦 МОИ ЗАКАЗЫ\n\n" + "\n\n".join(cards)


def render_stats(orders):
    """В цифры попадают только оплаченные заказы. Доход — сумма их смет."""
    moment = now_kz()
    start_today = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start_week = start_today - timedelta(days=start_today.weekday())
    start_month = start_today.replace(day=1)

    def collect(start):
        count = 0
        income = 0
        for order in orders.values():
            if not isinstance(order, dict) or order.get("status") != STATUS_PAID:
                continue
            paid_at = parse_time(order.get("paid_at")) or parse_time(order.get("created_at"))
            if paid_at is None or paid_at < start or paid_at > moment:
                continue
            count += 1
            price = order.get("price")
            if isinstance(price, int) and not isinstance(price, bool) and price >= 0:
                income += price
        return count, income

    today_count, today_income = collect(start_today)
    week_count, week_income = collect(start_week)
    month_count, month_income = collect(start_month)
    return (
        "📊 ВАША СТАТИСТИКА\n"
        "\n"
        "📅 СЕГОДНЯ:\n"
        f"Заказов: {today_count}\n"
        f"Доход: {money(today_income)}\n"
        "\n"
        "📅 НА ЭТОЙ НЕДЕЛЕ:\n"
        f"Заказов: {week_count}\n"
        f"Доход: {money(week_income)}\n"
        "\n"
        "📅 НА ЭТОМ МЕСЯЦЕ:\n"
        f"Заказов: {month_count}\n"
        f"Доход: {money(month_income)}"
    )


HELP_TEXT = (
    "ℹ️ ПОМОЩЬ\n"
    "\n"
    "Как оформить заказ:\n"
    "1. Нажмите «Новый заказ»\n"
    "2. Выберите услугу и тип\n"
    "3. Укажите комнату\n"
    "4. Проверьте смету\n"
    "5. Подтвердите и переведите сумму на Kaspi\n"
    "6. Нажмите «Я оплатил»\n"
    "\n"
    "Как считается цена:\n"
    "Материалы умножаются на коэффициент комнаты.\n"
    "Работа берётся из прайса как есть.\n"
    "Спальня и коридор — базовая цена.\n"
    "Кухня +20%, гостиная +30%, ванная +50%.\n"
    "Кабель считается за 1 метр, точечный светильник — за 1 штуку.\n"
    "\n"
    "Команды:\n"
    "/start — главное меню\n"
    "/new_order — новый заказ\n"
    "/orders — мои заказы\n"
    "/stats — статистика электрика\n"
    "/help — эта справка\n"
    "/myid — ваш Telegram ID"
)


def help_text():
    return HELP_TEXT


def is_electrician(user_id):
    """Пустой ELECTRICIAN_ID оставляет статистику открытой, пока идёт фаза 1."""
    raw = os.getenv("ELECTRICIAN_ID", "").strip()
    if not raw:
        return True
    allowed = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            allowed.add(int(part))
    return user_id in allowed


def keyboard(rows):
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        markup.row(
            *[types.InlineKeyboardButton(text, callback_data=data) for text, data in row]
        )
    return markup


def main_menu_keyboard():
    return keyboard(
        [
            [("🆕 Новый заказ", "m:new"), ("📦 Мои заказы", "m:orders")],
            [("📊 Статистика", "m:stats"), ("ℹ️ Помощь", "m:help")],
        ]
    )


def services_keyboard():
    rows = [[(service["button"], f"s:{key}")] for key, service in SERVICES.items()]
    rows.append([("🏠 В меню", "m:menu")])
    return keyboard(rows)


def types_keyboard(service_key):
    service = SERVICES[service_key]
    rows = [
        [(item["title"], f"t:{service_key}:{type_key}")]
        for type_key, item in service["types"].items()
    ]
    rows.append([("◀️ К услугам", "m:new"), ("🏠 В меню", "m:menu")])
    return keyboard(rows)


def rooms_keyboard():
    rows = [[(room["title"], f"r:{key}")] for key, room in ROOMS.items()]
    rows.append([("🏠 В меню", "m:menu")])
    return keyboard(rows)


def estimate_keyboard():
    return keyboard(
        [
            [("✅ Согласен", "a:yes"), ("❌ Изменить", "a:no")],
        ]
    )


def payment_keyboard(order_id):
    return keyboard(
        [
            [("✅ Я оплатил", f"p:{order_id}")],
            [("🏠 В меню", "m:menu")],
        ]
    )


def orders_keyboard(user_id, orders):
    rows = []
    for order_id, order in sorted_user_orders(user_id, orders):
        if order.get("status") == STATUS_WAIT:
            rows.append([(f"💳 Оплатить {order_id}", f"w:{order_id}")])
    rows.append([("🆕 Новый заказ", "m:new"), ("🏠 В меню", "m:menu")])
    return keyboard(rows)


def menu_back_keyboard():
    return keyboard([[("🏠 В меню", "m:menu")]])


def load_orders():
    if not os.path.exists(ORDERS_PATH):
        return {}
    with open(ORDERS_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("orders.json должен содержать объект с заказами")
    return data


def save_orders(orders):
    os.makedirs(DATA_DIR, exist_ok=True)
    temporary = ORDERS_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(orders, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, ORDERS_PATH)


def ensure_store():
    """Пустой orders.json появляется при первом запуске."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(ORDERS_PATH):
        save_orders({})
        logger.info("Создан файл заказов: %s", ORDERS_PATH)
        return
    # Повреждённый файл не перезаписываем пустым.
    load_orders()


def next_order_id(orders):
    maximum = 0
    for key in orders:
        match = ORDER_ID_RE.fullmatch(str(key))
        if match:
            maximum = max(maximum, int(match.group(1)))
    return f"order_{maximum + 1:03d}"


def sorted_user_orders(user_id, orders):
    found = []
    for order_id, order in orders.items():
        if not ORDER_ID_RE.fullmatch(str(order_id)) or not isinstance(order, dict):
            continue
        try:
            owner = int(order.get("client_id"))
        except (TypeError, ValueError):
            continue
        if owner == user_id:
            found.append((order_id, order))
    found.sort(key=lambda pair: int(pair[0].split("_", 1)[1]), reverse=True)
    return found


def confirm_draft(user_id):
    """
    Создаёт заказ из черновика. Повторное «Согласен» не плодит второй заказ.
    Цена считается здесь ещё раз, а не берётся из сообщения.
    """
    with LOCK:
        draft = DRAFTS.get(user_id)
        if not isinstance(draft, dict):
            return None, "incomplete", None
        service_key = draft.get("service")
        type_key = draft.get("type")
        room_key = draft.get("room")
        if not (service_key and type_key and room_key):
            return None, "incomplete", None
        try:
            estimate = calculate(service_key, type_key, room_key)
        except KeyError:
            return None, "invalid", None

        orders = load_orders()
        existing = draft.get("order_id")
        if existing and existing in orders:
            return existing, "exists", orders[existing]

        order_id = next_order_id(orders)
        order = {
            "client_id": user_id,
            "service": estimate["service"],
            "type": estimate["type"],
            "room": estimate["room"],
            "price": estimate["total"],
            "status": STATUS_WAIT,
            "created_at": timestamp(),
            "paid_at": None,
        }
        orders[order_id] = order
        save_orders(orders)
        draft["order_id"] = order_id
        logger.info("Создан %s для клиента %s на %s", order_id, user_id, estimate["total"])
        return order_id, "created", order


def mark_paid(user_id, order_id):
    """Переводит заказ клиента в статус «оплачено». Чужой заказ не трогает."""
    if not ORDER_ID_RE.fullmatch(order_id):
        return "invalid", None
    with LOCK:
        orders = load_orders()
        order = orders.get(order_id)
        if not isinstance(order, dict):
            return "missing", None
        try:
            owner = int(order.get("client_id"))
        except (TypeError, ValueError):
            return "foreign", None
        if owner != user_id:
            return "foreign", None
        if order.get("status") == STATUS_PAID:
            return "already", order
        if order.get("status") != STATUS_WAIT:
            return "status", order
        order["status"] = STATUS_PAID
        order["paid_at"] = timestamp()
        save_orders(orders)
        logger.info("Оплачен %s клиентом %s", order_id, user_id)
        # Фаза 2: здесь будет уведомление электрику. Сейчас ничего не отправляем.
        return "paid", order


def get_user_order(user_id, order_id):
    if not ORDER_ID_RE.fullmatch(order_id or ""):
        return None
    with LOCK:
        order = load_orders().get(order_id)
    if not isinstance(order, dict):
        return None
    try:
        owner = int(order.get("client_id"))
    except (TypeError, ValueError):
        return None
    if owner != user_id:
        return None
    return order


def clear_draft(user_id):
    with LOCK:
        DRAFTS.pop(user_id, None)


def update_draft(user_id, **fields):
    with LOCK:
        draft = DRAFTS.setdefault(user_id, {})
        # Новый выбор услуги начинает заказ заново, чтобы не оплатить старую смету.
        if "service" in fields:
            draft.pop("order_id", None)
            draft.pop("type", None)
            draft.pop("room", None)
        if "type" in fields:
            draft.pop("order_id", None)
            draft.pop("room", None)
        if "room" in fields:
            draft.pop("order_id", None)
        draft.update(fields)
        return dict(draft)


bot = telebot.TeleBot(TOKEN or PLACEHOLDER_TOKEN, parse_mode=None, threaded=True)


def show(chat_id, text, markup=None, message_id=None):
    """Меняет текущее сообщение с кнопками. Если Telegram не даёт, шлёт новое."""
    if message_id is None:
        bot.send_message(chat_id, text, reply_markup=markup)
        return
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    except ApiTelegramException as error:
        description = str(error).lower()
        if "message is not modified" in description:
            return
        logger.info("Сообщение не изменилось, отправляю новое: %s", error)
        bot.send_message(chat_id, text, reply_markup=markup)


def send_chunks(chat_id, text, markup=None, message_id=None):
    """Длинный список заказов режется по карточкам: лимит Telegram — 4096 символов."""
    if len(text) <= 3900:
        show(chat_id, text, markup, message_id)
        return
    blocks = text.split("\n\n")
    chunks = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > 3900 and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    show(chat_id, chunks[0], None, message_id)
    for index, chunk in enumerate(chunks[1:], start=1):
        attached = markup if index == len(chunks) - 1 else None
        bot.send_message(chat_id, chunk, reply_markup=attached)


def show_main_menu(chat_id, message_id=None):
    show(
        chat_id,
        "⚡ Электрик Иван — Казахстан\n"
        "\n"
        "Рассчитаю стоимость работ и приму заказ.\n"
        "Выберите действие:",
        main_menu_keyboard(),
        message_id,
    )


def show_services(chat_id, message_id=None):
    show(
        chat_id,
        "🆕 Новый заказ\n"
        "\n"
        "Шаг 1 из 3. Выберите услугу:",
        services_keyboard(),
        message_id,
    )


def show_types(chat_id, service_key, message_id=None):
    service = SERVICES.get(service_key)
    if service is None:
        show(chat_id, "Такой услуги нет. Выберите её из списка.", services_keyboard(), message_id)
        return
    show(
        chat_id,
        f"🆕 {service['title']}\n"
        "\n"
        "Шаг 2 из 3. Выберите вариант:",
        types_keyboard(service_key),
        message_id,
    )


def show_rooms(chat_id, message_id=None):
    show(
        chat_id,
        "🆕 Новый заказ\n"
        "\n"
        "Шаг 3 из 3. В какой комнате работа?",
        rooms_keyboard(),
        message_id,
    )


def show_estimate(chat_id, user_id, message_id=None):
    with LOCK:
        draft = dict(DRAFTS.get(user_id) or {})
    try:
        estimate = calculate(draft["service"], draft["type"], draft["room"])
    except KeyError:
        show(chat_id, "Смета ещё не собрана. Начните заказ заново.", services_keyboard(), message_id)
        return
    show(chat_id, render_estimate(estimate), estimate_keyboard(), message_id)


def show_payment(chat_id, order_id, order, message_id=None):
    try:
        price = int(order["price"])
    except (KeyError, TypeError, ValueError):
        show(chat_id, "У заказа нет суммы. Начните новый заказ.", menu_back_keyboard(), message_id)
        return
    show(chat_id, render_payment(order_id, price), payment_keyboard(order_id), message_id)


def show_orders(chat_id, user_id, message_id=None):
    with LOCK:
        orders = load_orders()
    send_chunks(
        chat_id,
        render_orders(user_id, orders),
        orders_keyboard(user_id, orders),
        message_id,
    )


def show_stats(chat_id, user_id, message_id=None):
    if not is_electrician(user_id):
        show(
            chat_id,
            "📊 Статистика доступна только электрику.",
            menu_back_keyboard(),
            message_id,
        )
        return
    with LOCK:
        orders = load_orders()
    show(chat_id, render_stats(orders), menu_back_keyboard(), message_id)


def show_help(chat_id, message_id=None):
    show(chat_id, help_text(), menu_back_keyboard(), message_id)


def chat_id_of(message_or_call):
    if isinstance(message_or_call, types.CallbackQuery):
        if message_or_call.message is not None:
            return message_or_call.message.chat.id
        return message_or_call.from_user.id
    return message_or_call.chat.id


def message_id_of(call):
    if call.message is None:
        return None
    return call.message.message_id


@bot.message_handler(commands=["start"])
def command_start(message):
    try:
        show_main_menu(message.chat.id)
    except Exception:
        logger.exception("Ошибка /start")
        bot.send_message(message.chat.id, "Не получилось открыть меню. Напишите /start ещё раз.")


@bot.message_handler(commands=["new_order"])
def command_new_order(message):
    try:
        clear_draft(message.from_user.id)
        show_services(message.chat.id)
    except Exception:
        logger.exception("Ошибка /new_order")
        bot.send_message(message.chat.id, "Не получилось начать заказ. Напишите /new_order ещё раз.")


@bot.message_handler(commands=["orders"])
def command_orders(message):
    try:
        show_orders(message.chat.id, message.from_user.id)
    except Exception:
        logger.exception("Ошибка /orders")
        bot.send_message(message.chat.id, "Не получилось показать заказы. Попробуйте /orders позже.")


@bot.message_handler(commands=["stats"])
def command_stats(message):
    try:
        show_stats(message.chat.id, message.from_user.id)
    except Exception:
        logger.exception("Ошибка /stats")
        bot.send_message(message.chat.id, "Не получилось посчитать статистику. Попробуйте /stats позже.")


@bot.message_handler(commands=["help"])
def command_help(message):
    try:
        show_help(message.chat.id)
    except Exception:
        logger.exception("Ошибка /help")
        bot.send_message(message.chat.id, help_text())


@bot.message_handler(commands=["myid"])
def command_myid(message):
    try:
        bot.send_message(
            message.chat.id,
            f"Ваш Telegram ID: {message.from_user.id}\n"
            "Его можно записать в ELECTRICIAN_ID, чтобы статистику видел только электрик.",
            reply_markup=menu_back_keyboard(),
        )
    except Exception:
        logger.exception("Ошибка /myid")


@bot.message_handler(content_types=["text"])
def command_unknown(message):
    text = message.text or ""
    # Известные команды забирает свой обработчик. Чужую команду не оставляем без ответа.
    if text.startswith("/"):
        command_name = text.split()[0][1:].split("@", 1)[0].lower()
        if command_name in {"start", "new_order", "orders", "stats", "help", "myid"}:
            return
        reply = "Такой команды нет. Откройте меню: /start"
    else:
        reply = "Я понимаю команды и кнопки под сообщениями.\nНажмите /start, чтобы открыть меню."
    try:
        bot.send_message(message.chat.id, reply, reply_markup=main_menu_keyboard())
    except Exception:
        logger.exception("Ошибка текстового сообщения")


@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    try:
        route_callback(call)
    except Exception:
        logger.exception("Ошибка кнопки: %s", getattr(call, "data", ""))
        try:
            bot.answer_callback_query(call.id, "Не получилось обработать кнопку. Попробуйте ещё раз.")
        except Exception:
            logger.exception("Не удалось ответить на callback")


def route_callback(call):
    data = call.data if isinstance(call.data, str) else ""
    user_id = call.from_user.id
    chat_id = chat_id_of(call)
    message_id = message_id_of(call)
    parts = data.split(":")

    if data == "m:menu":
        bot.answer_callback_query(call.id)
        show_main_menu(chat_id, message_id)
        return
    if data == "m:new":
        bot.answer_callback_query(call.id)
        clear_draft(user_id)
        show_services(chat_id, message_id)
        return
    if data == "m:orders":
        bot.answer_callback_query(call.id)
        show_orders(chat_id, user_id, message_id)
        return
    if data == "m:stats":
        bot.answer_callback_query(call.id)
        show_stats(chat_id, user_id, message_id)
        return
    if data == "m:help":
        bot.answer_callback_query(call.id)
        show_help(chat_id, message_id)
        return

    if len(parts) == 2 and parts[0] == "s" and parts[1] in SERVICES:
        bot.answer_callback_query(call.id)
        update_draft(user_id, service=parts[1])
        show_types(chat_id, parts[1], message_id)
        return

    if len(parts) == 3 and parts[0] == "t" and parts[1] in SERVICES and parts[2] in SERVICES[parts[1]]["types"]:
        bot.answer_callback_query(call.id)
        update_draft(user_id, service=parts[1], type=parts[2])
        show_rooms(chat_id, message_id)
        return

    if len(parts) == 2 and parts[0] == "r" and parts[1] in ROOMS:
        with LOCK:
            draft = DRAFTS.get(user_id) or {}
            ready = draft.get("service") in SERVICES and draft.get("type") in SERVICES.get(draft.get("service"), {}).get("types", {})
        if not ready:
            bot.answer_callback_query(call.id, "Сначала выберите услугу.", show_alert=True)
            show_services(chat_id, message_id)
            return
        bot.answer_callback_query(call.id)
        update_draft(user_id, room=parts[1])
        show_estimate(chat_id, user_id, message_id)
        return

    if data == "a:yes":
        order_id, state, order = confirm_draft(user_id)
        if state in ("incomplete", "invalid") or order is None:
            bot.answer_callback_query(call.id, "Смета не собрана. Начните заказ заново.", show_alert=True)
            show_services(chat_id, message_id)
            return
        bot.answer_callback_query(call.id, "Заказ записан." if state == "created" else "Заказ уже создан.")
        show_payment(chat_id, order_id, order, message_id)
        return

    if data == "a:no":
        bot.answer_callback_query(call.id)
        clear_draft(user_id)
        show_main_menu(chat_id, message_id)
        return

    if len(parts) == 2 and parts[0] == "w":
        order = get_user_order(user_id, parts[1])
        if order is None:
            bot.answer_callback_query(call.id, "Заказ не найден.", show_alert=True)
            return
        if order.get("status") == STATUS_PAID:
            bot.answer_callback_query(call.id, "Этот заказ уже оплачен.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_payment(chat_id, parts[1], order, message_id)
        return

    if len(parts) == 2 and parts[0] == "p":
        state, order = mark_paid(user_id, parts[1])
        if state == "foreign":
            bot.answer_callback_query(call.id, "Это не ваш заказ.", show_alert=True)
            return
        if state in ("missing", "invalid"):
            bot.answer_callback_query(call.id, "Заказ не найден.", show_alert=True)
            return
        if state == "already":
            bot.answer_callback_query(call.id, "Оплата уже отмечена.")
        elif state == "paid":
            bot.answer_callback_query(call.id, "Оплата отмечена.")
        else:
            bot.answer_callback_query(call.id, "Этот заказ нельзя отметить оплаченным.", show_alert=True)
            return
        number = parts[1].split("_", 1)[-1].lstrip("0") or "0"
        show(
            chat_id,
            "✅ ОПЛАТА ОТМЕЧЕНА\n"
            "\n"
            f"Заказ #{number} ({parts[1]}) в очереди.\n"
            f"Сумма: {money(order['price'])}\n"
            "\n"
            "Электрик свяжется с вами, чтобы согласовать время.",
            menu_back_keyboard(),
            message_id,
        )
        return

    bot.answer_callback_query(call.id, "Кнопка устарела. Откройте /start.", show_alert=True)


def main():
    if not TOKEN:
        logger.error("Не задана переменная окружения TELEGRAM_TOKEN")
        sys.exit(1)
    # Подменяем заглушку, если модуль импортировали раньше, чем появился токен.
    bot.token = TOKEN
    try:
        ensure_store()
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("Не удалось подготовить orders.json")
        sys.exit(1)
    try:
        me = bot.get_me()
    except Exception:
        logger.exception("Telegram не принял токен. Проверьте TELEGRAM_TOKEN")
        sys.exit(1)
    try:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Главное меню"),
                types.BotCommand("new_order", "Новый заказ"),
                types.BotCommand("orders", "Мои заказы"),
                types.BotCommand("stats", "Статистика"),
                types.BotCommand("help", "Помощь"),
            ]
        )
    except Exception:
        logger.exception("Не удалось записать меню команд, бот всё равно запущен")
    logger.info("Бот запущен: @%s", me.username)
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    main()
