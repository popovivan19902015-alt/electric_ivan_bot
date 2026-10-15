import telebot
import json
import os
from datetime import datetime

# ═════════════════════════════════════════════════════════════════
# TELEGRAM BOT ДЛЯ ЭЛЕКТРИКОВ В КАЗАХСТАНЕ
# ═════════════════════════════════════════════════════════════════

TOKEN = os.getenv("TELEGRAM_TOKEN", "8898840159:AAFxHBpDS45tbTHJBnBjAXzAn49T3WDyav8")
KASPI_PHONE = "+7 777 4222256"  # ← ЗАМЕНИ НА СВОЙ НОМЕР

bot = telebot.TeleBot(TOKEN)

# ═════════════════════════════════════════════════════════════════
# ПРАЙС-ЛИСТ
# ═════════════════════════════════════════════════════════════════

PRICE_LIST = {
    "Розетка": {
        "Однофазная (5A)": {"материалы": 3000, "работа": 8000},
        "Мощная (16A)": {"материалы": 5000, "работа": 10000},
        "С заземлением": {"материалы": 6000, "работа": 12000},
    },
    "Выключатель": {
        "Однокнопочный": {"материалы": 2000, "работа": 5000},
        "Двухкнопочный": {"материалы": 3000, "работа": 6000},
        "Датчик движения": {"материалы": 8000, "работа": 8000},
    },
    "Люстра": {
        "Простая": {"материалы": 4000, "работа": 8000},
        "Сложная": {"материалы": 10000, "работа": 15000},
        "Точечные (за штуку)": {"материалы": 2000, "работа": 3000},
    },
    "Монтаж": {
        "Кабель (за метр)": {"материалы": 500, "работа": 1000},
        "Автомат в щитке": {"материалы": 3000, "работа": 5000},
        "Замена щитка": {"материалы": 20000, "работа": 30000},
    }
}

ROOM_MULTIPLIER = {
    "Спальня": 1.0,
    "Кухня": 1.2,
    "Гостиная": 1.3,
    "Ванная": 1.5,
    "Коридор": 1.0,
}

# ═════════════════════════════════════════════════════════════════
# РАБОТА С JSON
# ═════════════════════════════════════════════════════════════════

def load_orders():
    if os.path.exists("orders.json"):
        with open("orders.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_orders(orders):
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

def get_next_order_id():
    orders = load_orders()
    if not orders:
        return "order_001"
    last_id = max(orders.keys(), key=lambda x: int(x.split("_")[1]))
    num = int(last_id.split("_")[1]) + 1
    return f"order_{num:03d}"

# ═════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═════════════════════════════════════════════════════════════════

@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📝 Новый заказ", "📦 Мои заказы")
    markup.add("📊 Статистика", "❓ Помощь")
    
    bot.send_message(
        message.chat.id,
        "🔌 Добро пожаловать в Electric Bot!\n\nВыбери действие:",
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: message.text == "📝 Новый заказ")
def new_order(message):
    markup = telebot.types.InlineKeyboardMarkup()
    
    for service in PRICE_LIST.keys():
        markup.add(
            telebot.types.InlineKeyboardButton(
                service,
                callback_data=f"service_{service}"
            )
        )
    
    bot.send_message(
        message.chat.id,
        "🔧 Выбери услугу:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("service_"))
def select_service(call):
    service = call.data.replace("service_", "")
    
    markup = telebot.types.InlineKeyboardMarkup()
    for option in PRICE_LIST[service].keys():
        markup.add(
            telebot.types.InlineKeyboardButton(
                option,
                callback_data=f"option_{service}_{option}"
            )
        )
    
    bot.edit_message_text(
        f"📌 Тип {service}:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("option_"))
def select_option(call):
    parts = call.data.replace("option_", "").split("_", 1)
    service, option = parts[0], parts[1]
    
    markup = telebot.types.InlineKeyboardMarkup()
    for room in ROOM_MULTIPLIER.keys():
        markup.add(
            telebot.types.InlineKeyboardButton(
                room,
                callback_data=f"room_{service}_{option}_{room}"
            )
        )
    
    bot.edit_message_text(
        "🏠 В какой комнате?",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("room_"))
def calculate_price(call):
    parts = call.data.replace("room_", "").split("_")
    service, option, room = parts[0], "_".join(parts[1:-1]), parts[-1]
    
    base_price = PRICE_LIST[service][option]
    multiplier = ROOM_MULTIPLIER[room]
    
    materials = int(base_price["материалы"] * multiplier)
    labor = base_price["работа"]
    total = materials + labor
    
    invoice = f"""
📋 СМЕТА
═══════════════════════════════════
Услуга: {service}
Тип: {option}
Комната: {room}

💵 Материалы: {materials:,} ₸
🔧 Работа: {labor:,} ₸
═══════════════════════════════════
💰 ИТОГО: {total:,} ₸
"""
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "✅ Согласен",
            callback_data=f"confirm_{service}_{option}_{room}_{total}"
        ),
        telebot.types.InlineKeyboardButton(
            "❌ Изменить",
            callback_data="cancel"
        )
    )
    
    bot.edit_message_text(
        invoice,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_"))
def confirm_order(call):
    parts = call.data.replace("confirm_", "").split("_")
    service, option, room, price = parts[0], "_".join(parts[1:-2]), parts[-2], int(parts[-1])
    
    order_id = get_next_order_id()
    orders = load_orders()
    
    orders[order_id] = {
        "client_id": call.message.chat.id,
        "service": service,
        "type": option,
        "room": room,
        "price": price,
        "status": "ожидание_оплаты",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paid_at": None
    }
    
    save_orders(orders)
    
    payment_message = f"""
💰 СПОСОБ ОПЛАТЫ

Переведите {price:,} ₸ на Kaspi:
📱 {KASPI_PHONE}

После перевода нажми: [✅ Я оплатил]
"""
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "✅ Я оплатил",
            callback_data=f"paid_{order_id}"
        )
    )
    
    bot.edit_message_text(
        payment_message,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def confirm_payment(call):
    order_id = call.data.replace("paid_", "")
    orders = load_orders()
    
    if order_id in orders:
        orders[order_id]["status"] = "оплачено"
        orders[order_id]["paid_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_orders(orders)
        
        bot.send_message(
            call.message.chat.id,
            "✅ Спасибо! Заказ принят. Электрик свяжется с вами в ближайшее время."
        )

@bot.message_handler(func=lambda message: message.text == "📦 Мои заказы")
def my_orders(message):
    orders = load_orders()
    client_orders = [
        (oid, o) for oid, o in orders.items()
        if o["client_id"] == message.chat.id
    ]
    
    if not client_orders:
        bot.send_message(message.chat.id, "📭 У вас нет заказов")
        return
    
    text = "📦 ВАШ ЗАКАЗЫ:\n\n"
    for oid, order in client_orders:
        status_emoji = "⏳" if order["status"] == "ожидание_оплаты" else "✅"
        text += f"{status_emoji} {oid}\n"
        text += f"  {order['service']} ({order['type']})\n"
        text += f"  Комната: {order['room']}\n"
        text += f"  Сумма: {order['price']:,} ₸\n"
        text += f"  Дата: {order['created_at']}\n\n"
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda message: message.text == "📊 Статистика")
def stats(message):
    orders = load_orders()
    client_orders = [
        o for o in orders.values()
        if o["client_id"] == message.chat.id
    ]
    
    today_count = sum(1 for o in client_orders if o["created_at"].startswith(datetime.now().strftime("%Y-%m-%d")))
    today_sum = sum(o["price"] for o in client_orders if o["created_at"].startswith(datetime.now().strftime("%Y-%m-%d")))
    
    total_count = len(client_orders)
    total_sum = sum(o["price"] for o in client_orders)
    
    text = f"""
📊 СТАТИСТИКА
═════════════════════════════════
📅 СЕГОДНЯ:
Заказов: {today_count}
Доход: {today_sum:,} ₸

📈 ВСЕГО:
Заказов: {total_count}
Доход: {total_sum:,} ₸
"""
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda message: message.text == "❓ Помощь")
def help_command(message):
    text = """
❓ СПРАВКА

🔌 Electric Bot помогает:
- Быстро считать смету для заказа
- Отправлять заказ в систему
- Видеть историю своих заказов

📝 КАК ИСПОЛЬЗОВАТЬ:
1. Нажми "Новый заказ"
2. Выбери услугу (розетка, выключатель, люстра)
3. Выбери тип услуги
4. Укажи комнату
5. Смета рассчитается автоматически
6. Переведи на Kaspi
7. Нажми "Я оплатил"

Готово! Электрик свяжется с тобой.

💬 Вопросы? Напиши в поддержку
"""
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda message: True)
def unknown(message):
    bot.send_message(
        message.chat.id,
        "❌ Команда не распознана. Используй кнопки меню ниже."
    )

# ═════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🤖 Бот запущен...")
    bot.infinity_polling()
