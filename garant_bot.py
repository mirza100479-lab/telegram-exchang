import os
import sqlite3
import secrets
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ConversationHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")
BOT_USERNAME = os.getenv("BOT_USERNAME", "YourExchangeBot")
DB_PATH = os.getenv("DB_PATH", "exchange.db")
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "lolzguarantss")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "")
MANAGER_ID = int(MANAGER_CHAT_ID) if MANAGER_CHAT_ID.isdigit() else 0

DEAL_TYPE, DEAL_AMOUNT, DEAL_ROLE, DEAL_CURRENCY, DEAL_DESCRIPTION = range(5)

CURRENCIES = {
    "TON": "TON", "USDT": "USDT", "BTC": "BTC", "ETH": "ETH",
    "LTC": "LTC", "STARS": "Telegram Stars", "RUB": "RUB",
    "KZT": "KZT", "UAH": "UAH", "EUR": "EUR", "USD": "USD"
}
ROLES = {"seller": "Продавец", "buyer": "Покупатель"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        creator_id INTEGER NOT NULL,
        creator_username TEXT,
        deal_type TEXT NOT NULL,
        amount TEXT NOT NULL,
        role TEXT NOT NULL,
        currency TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        counterparty_id INTEGER,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


def save_user(user):
    conn = db()
    conn.execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user.id, user.username, user.first_name or "", now()))
    conn.commit()
    conn.close()


def generate_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        conn = db()
        exists = conn.execute("SELECT 1 FROM deals WHERE code=?", (code,)).fetchone()
        conn.close()
        if not exists:
            return code


def create_deal_record(creator, data):
    code = generate_code()
    conn = db()
    cur = conn.execute("""
        INSERT INTO deals
        (code, creator_id, creator_username, deal_type, amount, role,
         currency, description, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
    """, (
        code, creator.id, creator.username, data["deal_type"],
        data["amount"], data["role"], data["currency"],
        data["description"], now()
    ))
    deal_id = cur.lastrowid
    conn.commit()
    conn.close()
    return deal_id, code


def get_deal(code):
    conn = db()
    row = conn.execute("SELECT * FROM deals WHERE code=?", (code,)).fetchone()
    conn.close()
    return row


def update_deal(code, **fields):
    if not fields:
        return
    allowed = {"status", "counterparty_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    conn = db()
    sql = "UPDATE deals SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE code=?"
    conn.execute(sql, (*fields.values(), code))
    conn.commit()
    conn.close()


def esc(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def deal_status(status):
    return {
        "open": "Ожидает участника",
        "joined": "Участник присоединился",
        "payment_sent": "Оплата передана менеджеру",
        "payment_confirmed": "Оплата подтверждена",
        "gift_sent": "Актив/подарок передан менеджеру",
        "closed": "Сделка завершена",
        "cancelled": "Отменена",
    }.get(status, status)


def deal_text(row):
    return (
        "🛡 <b>Сделка</b>\n\n"
        f"Код: <code>{esc(row['code'])}</code>\n"
        f"Тип: <b>{esc(row['deal_type'])}</b>\n"
        f"Сумма: <b>{esc(row['amount'])} {esc(CURRENCIES.get(row['currency'], row['currency']))}</b>\n"
        f"Роль создателя: <b>{esc(ROLES.get(row['role'], row['role']))}</b>\n"
        f"Описание: {esc(row['description']) if row['description'] else '—'}\n"
        f"Статус: <b>{esc(deal_status(row['status']))}</b>"
    )


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Создать сделку", callback_data="create")],
        [InlineKeyboardButton("Войти по коду", callback_data="join_menu")],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Назад", callback_data="menu")]
    ])


def role_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Продавец", callback_data="role:seller")],
        [InlineKeyboardButton("Покупатель", callback_data="role:buyer")],
        [InlineKeyboardButton("Назад", callback_data="amount_back")],
    ])


def currency_keyboard():
    rows = []
    items = list(CURRENCIES.items())
    for i in range(0, len(items), 2):
        rows.append([
            InlineKeyboardButton(items[i][1], callback_data=f"currency:{items[i][0]}"),
            *([InlineKeyboardButton(items[i + 1][1], callback_data=f"currency:{items[i + 1][0]}")] if i + 1 < len(items) else [])
        ])
    rows.append([InlineKeyboardButton("Назад", callback_data="role_back")])
    return InlineKeyboardMarkup(rows)


def participant_keyboard(code, status):
    rows = []
    if status == "joined":
        rows.append([InlineKeyboardButton("Я передал оплату менеджеру", callback_data=f"paid:{code}")])
    elif status == "payment_confirmed":
        rows.append([InlineKeyboardButton("Я передал актив менеджеру", callback_data=f"gift:{code}")])
    if status in {"open", "joined"}:
        rows.append([InlineKeyboardButton("Отменить сделку", callback_data=f"cancel:{code}")])
    return InlineKeyboardMarkup(rows) if rows else None


def manager_keyboard(code, status):
    if status == "payment_sent":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Подтвердить оплату", callback_data=f"manager_confirm_payment:{code}")],
            [InlineKeyboardButton("Отклонить оплату", callback_data=f"manager_reject_payment:{code}")],
        ])
    if status == "gift_sent":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Подтвердить получение", callback_data=f"manager_confirm_gift:{code}")],
            [InlineKeyboardButton("Отклонить получение", callback_data=f"manager_reject_gift:{code}")],
        ])
    return None


def deep_link(code):
    return f"https://t.me/{BOT_USERNAME}?start=deal_{code}"


async def notify_participants(context, row, text):
    ids = [row["creator_id"], row["counterparty_id"]]
    for uid in dict.fromkeys(x for x in ids if x):
        try:
            await context.bot.send_message(
                uid, text, parse_mode=ParseMode.HTML,
                reply_markup=participant_keyboard(row["code"], row["status"])
            )
        except Exception:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)

    args = context.args
    if args and args[0].startswith("deal_"):
        code = args[0][5:].upper()
        await show_join_offer(update, context, code)
        return

    await update.message.reply_text(
        "🛡 <b>GARANT</b>\n\n"
        "Безопасное сопровождение сделки через менеджера.\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


async def show_join_offer(update, context, code):
    row = get_deal(code)
    if not row:
        await update.message.reply_text("Сделка не найдена.")
        return
    if row["creator_id"] == update.effective_user.id:
        await update.message.reply_text("Это ваша собственная сделка.")
        return
    if row["status"] != "open":
        await update.message.reply_text(
            "Эта сделка уже недоступна для присоединения.\n\n" + deal_text(row),
            parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text(
        deal_text(row) + "\n\n"
        "Проверьте условия перед присоединением.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Присоединиться", callback_data=f"join:{code}")],
            [InlineKeyboardButton("Отмена", callback_data="menu")]
        ])
    )


async def create_deal(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "🛡 <b>Создание сделки</b>\n\nВведите тип сделки:",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu()
    )
    return DEAL_TYPE


async def receive_deal_type(update, context):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("Введите тип сделки.")
        return DEAL_TYPE
    context.user_data["deal_type"] = value
    await update.message.reply_text(
        "Введите сумму сделки:",
        reply_markup=back_menu()
    )
    return DEAL_AMOUNT


async def receive_amount(update, context):
    value = update.message.text.strip().replace(",", ".")
    try:
        amount = Decimal(value)
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Введите корректную сумму, например 100 или 100.50.")
        return DEAL_AMOUNT

    context.user_data["amount"] = value
    await update.message.reply_text(
        "Выберите вашу роль:",
        reply_markup=role_keyboard()
    )
    return DEAL_ROLE


async def choose_role(update, context):
    query = update.callback_query
    await query.answer()
    role = query.data.split(":", 1)[1]
    context.user_data["role"] = role
    await query.edit_message_text(
        "Выберите валюту / способ расчёта:",
        reply_markup=currency_keyboard()
    )
    return DEAL_CURRENCY


async def choose_currency(update, context):
    query = update.callback_query
    await query.answer()
    currency = query.data.split(":", 1)[1]
    context.user_data["currency"] = currency
    await query.edit_message_text(
        "Введите описание сделки.\n\n"
        "Если описание не требуется, напишите «нет».",
        reply_markup=back_menu()
    )
    return DEAL_DESCRIPTION


async def receive_description(update, context):
    description = update.message.text.strip()
    if description.lower() in {"нет", "-", "—"}:
        description = ""

    data = {
        "deal_type": context.user_data["deal_type"],
        "amount": context.user_data["amount"],
        "role": context.user_data["role"],
        "currency": context.user_data["currency"],
        "description": description,
    }
    _, code = create_deal_record(update.effective_user, data)
    row = get_deal(code)
    context.user_data.clear()

    await update.message.reply_text(
        "✅ <b>Сделка создана</b>\n\n" + deal_text(row) +
        "\n\n🔗 <b>Ссылка для второго участника:</b>\n"
        f"<code>{esc(deep_link(code))}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыть ссылку", url=deep_link(code))],
            [InlineKeyboardButton("В меню", callback_data="menu")]
        ])
    )
    return ConversationHandler.END


async def join_callback(update, context):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    row = get_deal(code)

    if not row:
        await query.edit_message_text("Сделка не найдена.", reply_markup=back_menu())
        return
    if row["creator_id"] == query.from_user.id:
        await query.answer("Это ваша собственная сделка.", show_alert=True)
        return
    if row["status"] != "open":
        await query.answer("Сделка уже занята или закрыта.", show_alert=True)
        return

    update_deal(code, counterparty_id=query.from_user.id, status="joined")
    row = get_deal(code)

    try:
        await context.bot.send_message(
            row["creator_id"],
            "🤝 <b>К сделке присоединился участник.</b>\n\n" + deal_text(row),
            parse_mode=ParseMode.HTML,
            reply_markup=participant_keyboard(code, row["status"])
        )
    except Exception:
        pass

    await query.edit_message_text(
        "🤝 <b>Вы присоединились к сделке.</b>\n\n" + deal_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=participant_keyboard(code, row["status"])
    )


async def deal_action_callback(update, context):
    query = update.callback_query
    await query.answer()
    action, code = query.data.split(":", 1)
    row = get_deal(code)

    if not row:
        await query.edit_message_text("Сделка не найдена.", reply_markup=back_menu())
        return

    uid = query.from_user.id
    participants = {row["creator_id"], row["counterparty_id"]}

    if action.startswith("manager_"):
        if not MANAGER_ID or uid != MANAGER_ID:
            await query.answer("Только менеджер может выполнить это действие.", show_alert=True)
            return

        if action == "manager_confirm_payment" and row["status"] == "payment_sent":
            update_deal(code, status="payment_confirmed")
            notice = "✅ <b>Оплата подтверждена менеджером.</b>"
        elif action == "manager_reject_payment" and row["status"] == "payment_sent":
            update_deal(code, status="joined")
            notice = "❌ <b>Оплата не подтверждена менеджером.</b>"
        elif action == "manager_confirm_gift" and row["status"] == "gift_sent":
            update_deal(code, status="closed")
            notice = "🏁 <b>Сделка завершена. Передача подтверждена менеджером.</b>"
        elif action == "manager_reject_gift" and row["status"] == "gift_sent":
            update_deal(code, status="payment_confirmed")
            notice = "❌ <b>Получение актива не подтверждено менеджером.</b>"
        else:
            await query.answer("Этот этап уже изменён.", show_alert=True)
            return

        row = get_deal(code)
        await notify_participants(context, row, notice + "\n\n" + deal_text(row))
        await query.edit_message_text(
            notice + "\n\n" + deal_text(row),
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )
        return

    if uid not in participants:
        await query.answer("Вы не участник этой сделки.", show_alert=True)
        return

    if action == "paid" and row["status"] == "joined":
        update_deal(code, status="payment_sent")
        notice = "💳 <b>Покупатель отметил передачу оплаты менеджеру.</b>"
    elif action == "gift" and row["status"] == "payment_confirmed":
        update_deal(code, status="gift_sent")
        notice = "🎁 <b>Продавец отметил передачу актива менеджеру.</b>"
    else:
        await query.answer("Это действие сейчас недоступно.", show_alert=True)
        return

    row = get_deal(code)
    await notify_participants(context, row, notice + "\n\n" + deal_text(row))

    if MANAGER_ID:
        kb = manager_keyboard(code, row["status"])
        if kb:
            try:
                await context.bot.send_message(
                    MANAGER_ID,
                    "🛡 <b>Требуется действие менеджера</b>\n\n" + deal_text(row),
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb
                )
            except Exception:
                pass

    await query.edit_message_text(
        notice + "\n\n" + deal_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=participant_keyboard(code, row["status"])
    )


async def cancel_callback(update, context):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    row = get_deal(code)

    if not row:
        await query.edit_message_text("Сделка не найдена.", reply_markup=back_menu())
        return
    if row["creator_id"] != query.from_user.id:
        await query.answer("Отменить сделку может только создатель.", show_alert=True)
        return
    if row["status"] != "open":
        await query.answer("Эту сделку уже нельзя отменить.", show_alert=True)
        return

    update_deal(code, status="cancelled")
    await query.edit_message_text(
        f"Сделка <code>{esc(code)}</code> отменена.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu()
    )


async def menu_cancel(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "🛡 <b>GARANT</b>\n\nВыберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )
    return ConversationHandler.END


async def back_to_type(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📝 Введите тип сделки:",
        reply_markup=back_menu()
    )
    return DEAL_TYPE


async def back_to_currency(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Выберите валюту / способ расчёта:",
        reply_markup=currency_keyboard()
    )
    return DEAL_CURRENCY


async def join_menu(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data["waiting_join_code"] = True
    await query.edit_message_text(
        "🔎 <b>Вход в сделку</b>\n\nВведите код сделки:",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu()
    )


async def receive_join_code(update, context):
    if not context.user_data.get("waiting_join_code"):
        return
    code = update.message.text.strip().upper()
    context.user_data.clear()
    row = get_deal(code)

    if not row:
        await update.message.reply_text("Сделка не найдена.", reply_markup=main_menu())
        return

    if row["creator_id"] == update.effective_user.id:
        await update.message.reply_text("Это ваша собственная сделка.", reply_markup=main_menu())
        return

    if row["status"] != "open":
        await update.message.reply_text("Эта сделка уже недоступна.", reply_markup=main_menu())
        return

    await update.message.reply_text(
        deal_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Присоединиться", callback_data=f"join:{code}")],
            [InlineKeyboardButton("В меню", callback_data="menu")]
        ])
    )


async def error_handler(update, context):
    print("Ошибка:", context.error)


def main():
    if TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError("Укажи BOT_TOKEN.")

    init_db()
    app = Application.builder().token(TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_deal, pattern=r"^create$")],
        states={
            DEAL_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_deal_type),
                CallbackQueryHandler(menu_cancel, pattern=r"^menu$")
            ],
            DEAL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount),
                CallbackQueryHandler(back_to_type, pattern=r"^type_back$")
            ],
            DEAL_ROLE: [
                CallbackQueryHandler(choose_role, pattern=r"^role:(seller|buyer)$"),
                CallbackQueryHandler(back_to_type, pattern=r"^amount_back$")
            ],
            DEAL_CURRENCY: [
                CallbackQueryHandler(choose_currency, pattern=r"^currency:[A-Z]+$"),
                CallbackQueryHandler(choose_role, pattern=r"^role_back$")
            ],
            DEAL_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description),
                CallbackQueryHandler(back_to_currency, pattern=r"^currency_back$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(join_menu, pattern=r"^join_menu$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_join_code))
    app.add_handler(CallbackQueryHandler(join_callback, pattern=r"^join:"))
    app.add_handler(CallbackQueryHandler(
        deal_action_callback,
        pattern=r"^(paid|gift|manager_confirm_payment|manager_reject_payment|manager_confirm_gift|manager_reject_gift):"
    ))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(menu_cancel, pattern=r"^menu$"))
    app.add_error_handler(error_handler)

    print("GARANT bot started.")
    app.run_polling()


async def cancel_conversation(update, context):
    context.user_data.clear()
    if update.message:
        await update.message.reply_text(
            "Создание сделки отменено.",
            reply_markup=main_menu()
        )
    return ConversationHandler.END


if __name__ == "__main__":
    main()
