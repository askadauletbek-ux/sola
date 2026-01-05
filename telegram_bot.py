import os
import re
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

# === 1. КЛЮЧЕВОЙ ИМПОРТ ИЗ АССИСТЕНТА ===
# Импортируем единую точку входа из ассистента
from kilo_ai_assistant import kilo_entry_point_handler

import aiohttp
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TimedOut, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    PicklePersistence  # Импорт для сохранения состояния
)
from telegram.request import HTTPXRequest

# === CONFIG (без изменений) ===
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

ALMATY_TZ = pytz.timezone("Asia/Almaty")
TIMEZONE = "Asia/Almaty"

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:5000").rstrip("/")
BOT_SECRET_TOKEN = os.getenv("BOT_SECRET_TOKEN")
app_token = os.getenv("TELEGRAM_BOT_TOKEN")

os.makedirs("temp_photos", exist_ok=True)
os.makedirs("bot_data", exist_ok=True)  # Папка для файла состояния

# Conversation states (без изменений)
(ASK_CODE, SELECT_MENU, ASK_PHOTO, HANDLE_SAVE, OVERWRITE_CONFIRM, HISTORY_MENU, ACTIVITY_INPUT) = range(7)

# Keyboards (без изменений)
MAIN_MENU_KEYBOARD = [
    [InlineKeyboardButton("🍽️ Питание", callback_data="menu_nutrition"),
     InlineKeyboardButton("🏋️ Тренировки", callback_data="menu_training")],
    [InlineKeyboardButton("📈 Прогресс", callback_data="menu_progress"),
     InlineKeyboardButton("⚙️ Ещё", callback_data="menu_more")],
]
NUTRITION_MENU_KEYBOARD = [
    [InlineKeyboardButton("➕ Добавить приём пищи", callback_data="add")],
    [InlineKeyboardButton("🍽️ Приемы пищи за сегодня", callback_data="today_meals")],
    [InlineKeyboardButton("🥗 Текущая диета", callback_data="current")],
    [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")],
]
TRAININGS_MENU_KEYBOARD = [
    [InlineKeyboardButton("🏋️ Мои тренировки", callback_data="my_trainings")],
    [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")],
]
PROGRESS_MENU_KEYBOARD = [
    [InlineKeyboardButton("🚀 Мой прогресс", callback_data="progress")],
    [InlineKeyboardButton("📜 Моя история", callback_data="history")],
    [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")],
]
MORE_MENU_KEYBOARD = [
    [InlineKeyboardButton("➕ Добавить активность", callback_data="add_activity")],
    [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")],
]


# === HELPERS (без изменений) ===
async def cleanup_chat(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.user_data.get('chat_id')
    messages_to_delete = context.user_data.pop('messages_to_delete', [])
    main_menu_msg_id = context.user_data.pop('main_menu_message_id', None)
    if main_menu_msg_id:
        messages_to_delete.append(main_menu_msg_id)
    if not chat_id or not messages_to_delete:
        return
    for msg_id in set(messages_to_delete):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


def remember_msg(context: ContextTypes.DEFAULT_TYPE, message_id: int):
    lst = context.user_data.setdefault('messages_to_delete', [])
    if message_id not in lst:
        lst.append(message_id)


async def _is_registered(chat_id: int) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BACKEND_URL}/api/is_registered/{chat_id}") as resp:
                return resp.status == 200
    except aiohttp.ClientError:
        return False


async def _link_code(chat_id: int, code: str) -> tuple[bool, int, str]:
    code = code.strip()
    if not re.fullmatch(r"\d{8}", code):
        return False, 400, "Код должен состоять из 8 цифр."
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BACKEND_URL}/api/link_telegram",
                                    json={"code": code, "chat_id": chat_id}) as resp:
                if resp.status == 200:
                    return True, 200, "✅ Telegram привязан! Введите /start, чтобы открыть меню."
                else:
                    return False, resp.status, "⚠️ Не удалось привязать. Попробуйте позже."
    except aiohttp.ClientError as e:
        return False, 503, "⚠️ Сервер недоступен. Попробуйте позже."


# === MENUS (без изменений) ===
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup_chat(context)
    text = "👋 Выберите раздел:"
    reply_markup = InlineKeyboardMarkup(MAIN_MENU_KEYBOARD)

    # ИСПРАВЛЕНО: Убедимся, что отправляем сообщение, а не редактируем несуществующее
    if update.callback_query:
        sent = await update.effective_chat.send_message(text, reply_markup=reply_markup)
    else:
        sent = await update.message.reply_text(text, reply_markup=reply_markup)

    context.user_data['main_menu_message_id'] = sent.message_id
    context.user_data['messages_to_delete'] = []


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
    await show_main_menu(update, context)
    return SELECT_MENU


# === ENTRY POINTS (без изменений) ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("start() from %s", update.effective_user.id if update.effective_user else "unknown")
    context.user_data.clear()
    context.user_data['chat_id'] = update.effective_chat.id

    chat_id = update.effective_chat.id
    if await _is_registered(chat_id):
        context.user_data['is_registered'] = True
        await show_main_menu(update, context)
        return SELECT_MENU

    sent = await update.message.reply_text(
        "🔐 Введите *8-значный код* из личного кабинета:",
        parse_mode="Markdown"
    )
    remember_msg(context, sent.message_id)
    return ASK_CODE


async def handle_code_anywhere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id
    code = (msg.text or "").strip()

    if context.user_data.get('is_registered'):
        await show_main_menu(update, context)
        return SELECT_MENU

    waiting = await msg.reply_text("⏳ Проверяю код…")
    ok, status, text = await _link_code(chat_id, code)
    await waiting.delete()

    if ok:
        context.user_data['is_registered'] = True
        await cleanup_chat(context)
        await msg.reply_text(text)
        await start(update, context)
        return ConversationHandler.END

    sent = await msg.reply_text(text)
    remember_msg(context, sent.message_id)
    return ASK_CODE


async def verify_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_code_anywhere(update, context)


# ... (остальные функции меню и обработчики без изменений)
async def show_today_meals(update_or_query: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update_or_query.effective_chat
    chat_id = chat.id
    loading_msg = await chat.send_message("⏳ Загружаю приёмы пищи за сегодня...")
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BACKEND_URL}/api/meals/today/{chat_id}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    meals = data.get("meals")
                    total_calories = data.get("total_calories")

                    if not meals:
                        text = "🤷‍♂️ Вы ещё ничего не ели сегодня."
                    else:
                        text = "🍽️ *Ваши приёмы пищи за сегодня:*\n\n"
                        meal_type_map = {
                            'breakfast': '🍳 Завтрак',
                            'lunch': '🍛 Обед',
                            'dinner': '🍲 Ужин',
                            'snack': '🥜 Перекус'
                        }
                        for meal in meals:
                            meal_name = meal.get('name')
                            meal_calories = meal.get('calories')
                            meal_type_rus = meal_type_map.get(meal.get('meal_type'), 'Приём пищи')
                            text += f"*{meal_type_rus}*: {meal_name} — *{meal_calories} ккал*\n"
                        text += f"\n🔥 *Всего за день: {total_calories} ккал*"

                    await loading_msg.edit_text(
                        text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]])
                    )
                    remember_msg(context, loading_msg.message_id)
                else:
                    await loading_msg.edit_text("⚠️ Произошла ошибка при загрузке данных.")
    except aiohttp.ClientError as e:
        logging.error(f"Today's meals loading failed: {e}")
        await loading_msg.edit_text("⚠️ Ошибка сети. Не удалось загрузить данные.")


async def my_trainings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = str(chat.id)
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BACKEND_URL}/api/trainings/my", params={"chat_id": chat_id}) as resp:
                if resp.status != 200:
                    await chat.send_message("⚠️ Не удалось получить ваши тренировки. Попробуйте позже.")
                    return
                data = await resp.json()
    except aiohttp.ClientError:
        await chat.send_message("⚠️ Ошибка сети. Попробуйте позже.")
        return

    items = data.get("items", [])
    if not items:
        await chat.send_message("🏋️ У вас пока нет ближайших тренировок.")
        return

    lines = []
    for it in items:
        dt = None
        if it.get("start_time"):
            try:
                dt_utc = datetime.fromisoformat(it["start_time"].replace("Z", "+00:00"))
                dt = dt_utc.astimezone(ALMATY_TZ)
            except Exception:
                dt = None
        when = dt.strftime("%d.%m %H:%M") if dt else "время не указано"
        title = it.get("title") or "Тренировка"
        location = it.get("location")
        lines.append(f"• {when} — {title}" + (f" ({location})" if location else ""))

    text = "🏋️ *Мои ближайшие тренировки:*\n\n" + "\n".join(lines)
    msg = await chat.send_message(text, parse_mode="Markdown")
    remember_msg(context, msg.message_id)


async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat = update.effective_chat

    last_menu_id = context.user_data.pop('main_menu_message_id', None)
    if last_menu_id:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=last_menu_id)
        except Exception as e:
            logging.warning(f"Could not delete previous main menu ({last_menu_id}): {e}")

    if data == "menu_nutrition":
        sent = await chat.send_message(
            "🍽️ Раздел *Питание* — выберите действие:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(NUTRITION_MENU_KEYBOARD)
        )
        remember_msg(context, sent.message_id)
        return SELECT_MENU

    if data == "menu_training":
        sent = await chat.send_message(
            "🏋️ Раздел *Тренировки*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(TRAININGS_MENU_KEYBOARD)
        )
        remember_msg(context, sent.message_id)
        return SELECT_MENU

    if data == "menu_progress":
        sent = await chat.send_message(
            "📈 Раздел *Прогресс*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(PROGRESS_MENU_KEYBOARD)
        )
        remember_msg(context, sent.message_id)
        return SELECT_MENU

    if data == "menu_more":
        sent = await chat.send_message(
            "⚙️ Раздел *Ещё*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(MORE_MENU_KEYBOARD)
        )
        remember_msg(context, sent.message_id)
        return SELECT_MENU

    if data == "add":
        keyboard = [
            [InlineKeyboardButton("🍳 Завтрак", callback_data="meal_breakfast"),
             InlineKeyboardButton("🍛 Обед", callback_data="meal_lunch")],
            [InlineKeyboardButton("🍲 Ужин", callback_data="meal_dinner"),
             InlineKeyboardButton("🥜 Перекус", callback_data="meal_snack")],
            [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]
        ]
        sent_message = await chat.send_message("Выберите тип приёма пищи:", reply_markup=InlineKeyboardMarkup(keyboard))
        remember_msg(context, sent_message.message_id)
        return ASK_PHOTO

    if data == "today_meals":
        await show_today_meals(update, context)
        return SELECT_MENU

    if data == "add_activity":
        return await show_activity_prompt(update, context)

    if data == "progress":
        await show_progress(update, context)
        return SELECT_MENU

    if data == "history":
        return await show_history_menu(update, context)

    if data == "current":
        loading_msg = await chat.send_message("⏳ Загружаю вашу диету...")
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{BACKEND_URL}/api/current_diet/{chat.id}") as resp:
                    if resp.status == 200:
                        diet = await resp.json()
                        text = f"🥗 *Ваша диета на {diet['date']}*\n\n"
                        for meal_type, meal_name in [("breakfast", "Завтрак"), ("lunch", "Обед"), ("dinner", "Ужин"),
                                                     ("snack", "Перекус")]:
                            text += f"*{meal_name}*:\n"
                            items = diet.get(meal_type)
                            if items:
                                for item in items:
                                    text += f"- {item['name']} ({item['grams']} г, {item['kcal']} ккал)\n"
                            else:
                                text += "- нет данных\n"
                            text += "\n"
                        text += (
                            f"Итого: *{diet['total_kcal']} ккал* (Б: {diet['protein']} г, Ж: {diet['fat']} г, У: {diet['carbs']} г)")
                        await loading_msg.edit_text(text, parse_mode="Markdown")
                        remember_msg(context, loading_msg.message_id)
                    elif resp.status == 404:
                        await loading_msg.edit_text(
                            "🤷‍♂️ У вас пока нет сгенерированной диеты. Создайте её в профиле на сайте.")
                    else:
                        await loading_msg.edit_text("⚠️ Произошла ошибка при загрузке диеты.")
        except aiohttp.ClientError as e:
            logging.error(f"Diet loading failed: {e}")
            await loading_msg.edit_text("⚠️ Ошибка сети. Не удалось загрузить диету.")

        return SELECT_MENU

    if data == "my_trainings":
        await my_trainings(update, context)
        return SELECT_MENU

    await back_to_main_menu(update, context)
    return SELECT_MENU


async def ask_photo_for_meal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_to_main":
        await query.message.delete()
        return await back_to_main_menu(update, context)

    context.user_data["meal_type"] = query.data.split('_')[1]

    # ИСПРАВЛЕНО: Теперь эта функция может быть вызвана как от кнопки, так и от текстового сообщения
    # `edit_message_text` работает только для кнопок, поэтому используем его с проверкой
    if hasattr(query, 'edit_message_text') and callable(query.edit_message_text):
        await query.edit_message_text(
            "📸 Пожалуйста, отправьте фото еды:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]])
        )
    else:  # Если вызвано из ассистента, у нас нет сообщения для редактирования
        await update.effective_chat.send_message(
            "📸 Пожалуйста, отправьте фото еды:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]])
        )

    return ASK_PHOTO


async def process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_msg(context, update.message.message_id)

    old_ids = context.user_data.get('messages_to_delete', [])
    if old_ids:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_ids[0])
            context.user_data['messages_to_delete'] = old_ids[1:]
        except Exception:
            pass

    analyzing_msg = await update.message.reply_text("⏳ Анализирую фото, это может занять до 30 секунд...")

    file_id = update.message.photo[-1].file_id
    try:
        photo_file = await context.bot.get_file(file_id)
        photo_bytes = await photo_file.download_as_bytearray()

        form_data = aiohttp.FormData()
        form_data.add_field('file', photo_bytes, filename='meal.jpg', content_type='image/jpeg')
        form_data.add_field('chat_id', str(update.effective_chat.id))

        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # subscription check
            async with session.get(f"{BACKEND_URL}/api/subscription/status",
                                   params={"chat_id": str(update.effective_chat.id)}) as s:
                if s.status == 200:
                    sub = await s.json()
                    if not sub.get("has_subscription"):
                        await analyzing_msg.delete()
                        await update.message.reply_text(
                            "🔒 Анализ по фото доступен по подписке.\n"
                            "✍️ Для ручного ввода отправьте сообщение вида:\n"
                            "«гречка 150 г, куриная грудка 120 г, салат 80 г»."
                        )
                        return await show_main_menu(update, context)
                else:
                    await analyzing_msg.delete()
                    await update.message.reply_text(
                        "⚠️ Не удалось проверить подписку. Попробуйте позже или введите приём пищи вручную.")
                    return await show_main_menu(update, context)

            async with session.post(f"{BACKEND_URL}/analyze_meal_photo", data=form_data) as resp:
                await analyzing_msg.delete()
                if resp.status == 200:
                    result_data = await resp.json()
                    context.user_data["analysis_result"] = result_data

                    text = (f"📊 *Результат анализа:*\n\n"
                            f"Название: *{result_data.get('name', 'N/A')}*\n"
                            f"Вердикт: *{result_data.get('verdict', 'N/A')}*\n\n"
                            f"Калории: *{result_data.get('calories', 0)} ккал*\n"
                            f"Белки: {result_data.get('protein', 0.0)} г\n"
                            f"Жиры: {result_data.get('fat', 0.0)} г\n"
                            f"Углеводы: {result_data.get('carbs', 0.0)} г\n\n"
                            f"_{result_data.get('analysis', '')}_")
                    kb = [[InlineKeyboardButton("✅ Сохранить", callback_data="save_yes"),
                           InlineKeyboardButton("❌ Отмена", callback_data="save_no")]]
                    result_msg = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                                                 parse_mode="Markdown")
                    remember_msg(context, result_msg.message_id)
                    return HANDLE_SAVE
                else:
                    error_text = await resp.text()
                    logging.error(f"Backend photo analysis failed: {resp.status} - {error_text}")
                    await update.message.reply_text("⚠️ Ошибка анализа на сервере. Попробуйте другое фото или позже.")
                    return await show_main_menu(update, context)

    except Exception as e:
        logging.error(f"Failed to process photo: {e}")
        try:
            await analyzing_msg.delete()
        except Exception:
            pass
        await update.message.reply_text("⚠️ Не удалось обработать фото. Попробуйте ещё раз.")
        return ASK_PHOTO


async def handle_save_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "save_no":
        await query.message.delete()
        await query.message.reply_text("❌ Операция отменена.")
        await show_main_menu(update, context)
        return SELECT_MENU

    chat_id = update.effective_chat.id
    meal_type = context.user_data.get("meal_type")
    analysis_result = context.user_data.get("analysis_result")

    if not meal_type or not analysis_result:
        await query.message.edit_text("⚠️ Внутренняя ошибка. Попробуйте снова.")
        await show_main_menu(update, context)
        return SELECT_MENU

    payload = {"chat_id": chat_id, "meal_type": meal_type, **analysis_result}

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BACKEND_URL}/api/log_meal", json=payload) as resp:
                if resp.status == 200:
                    await query.message.edit_text("✅ Приём пищи сохранён.")
                    await show_main_menu(update, context)
                    return SELECT_MENU
                elif resp.status == 409:
                    kb = [[InlineKeyboardButton("Да, перезаписать", callback_data="overwrite_yes"),
                           InlineKeyboardButton("Нет, отмена", callback_data="overwrite_no")]]
                    await query.message.edit_text(
                        f"🥣 Приём пищи '{meal_type}' за сегодня уже существует. Перезаписать?",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                    return OVERWRITE_CONFIRM
                else:
                    error_text = await resp.text()
                    logging.error(f"Backend save failed: {resp.status} - {error_text}")
                    await query.message.edit_text("⚠️ Ошибка сохранения на сервере.")
                    await show_main_menu(update, context)
                    return SELECT_MENU
    except aiohttp.ClientError as e:
        logging.error(f"Save failed (network): {e}")
        await query.message.edit_text("⚠️ Ошибка сети. Не удалось сохранить данные.")
        await show_main_menu(update, context)
        return SELECT_MENU


async def handle_overwrite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "overwrite_no":
        await query.message.edit_text("❌ Перезапись отменена.")
        await show_main_menu(update, context)
        return SELECT_MENU

    chat_id = update.effective_chat.id
    meal_type = context.user_data.get("meal_type")
    analysis_result = context.user_data.get("analysis_result")

    payload = {"chat_id": chat_id, "meal_type": meal_type}
    save_payload = {"chat_id": chat_id, "meal_type": meal_type, **analysis_result}

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(f"{BACKEND_URL}/api/log_meal", json=payload) as del_resp:
                if del_resp.status not in [200, 204, 404]:
                    await query.message.edit_text("⚠️ Не удалось удалить старую запись. Перезапись отменена.")
                    await show_main_menu(update, context)
                    return SELECT_MENU
            async with aiohttp.ClientSession(timeout=timeout) as session2:
                async with session2.post(f"{BACKEND_URL}/api/log_meal", json=save_payload) as post_resp:
                    if post_resp.status == 200:
                        await query.message.edit_text("🔄 Приём пищи успешно перезаписан.")
                    else:
                        await query.message.edit_text("⚠️ Не удалось сохранить новую запись после удаления.")
    except aiohttp.ClientError as e:
        logging.error(f"Overwrite failed (network): {e}")
        await query.message.edit_text("⚠️ Ошибка сети при перезаписи.")

    await show_main_menu(update, context)
    return SELECT_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup_chat(context)
    if update.message:
        await update.message.reply_text("🚫 Операция отменена.")
    elif update.callback_query:
        await update.callback_query.message.reply_text("🚫 Операция отменена.")
    await show_main_menu(update, context)
    context.user_data.clear()
    return ConversationHandler.END


async def show_progress(update_or_query: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update_or_query.effective_chat
    chat_id = chat.id
    loading_msg = await chat.send_message("⏳ Загружаю ваш прогресс...")

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BACKEND_URL}/api/user_progress/{chat_id}") as resp:
                if resp.status != 200:
                    try:
                        data = await resp.json()
                        error_msg = data.get("error", "Недостаточно данных для анализа прогресса.")
                    except Exception:
                        error_msg = "Недостаточно данных для анализа прогресса."
                    await loading_msg.edit_text(f"⚠️ {error_msg}")
                    return
                data = await resp.json()
    except aiohttp.ClientError as e:
        logging.error(f"Progress loading failed: {e}")
        await loading_msg.edit_text("⚠️ Ошибка сети. Не удалось загрузить прогресс.")
        return

    await loading_msg.delete()
    latest = data.get("latest")
    previous = data.get("previous")

    if not latest:
        await chat.send_message(
            "⚠️ Данные не найдены.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]])
        )
        return

    text = f"🚀 *Ваш прогресс (замер от {latest['date']})*\n\n"
    text += f"⚖️ Вес: *{latest.get('weight', 'N/A')} кг*\n"
    text += f"🧈 Жировая масса: *{latest.get('fat_mass', 'N/A')} кг*\n"
    text += f"💪 Мышечная масса: *{latest.get('muscle_mass', 'N/A')} кг*\n"

    if previous:
        def get_diff_str(latest_val, prev_val):
            if latest_val is None or prev_val is None:
                return "– нет данных"
            diff = latest_val - prev_val
            if diff > 0.01: return f"🔺 +{diff:.1f}"
            if diff < -0.01: return f"✅ {diff:.1f}"
            return "– без изменений"

        text += f"\n*Изменения с прошлого замера ({previous['date']})*:\n"
        text += f"⚖️ Вес: {get_diff_str(latest.get('weight'), previous.get('weight'))}\n"
        text += f"🧈 Жир: {get_diff_str(latest.get('fat_mass'), previous.get('fat_mass'))}\n"
        text += f"💪 Мышцы: {get_diff_str(latest.get('muscle_mass'), previous.get('muscle_mass'))}"

    msg = await chat.send_message(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]])
    )
    remember_msg(context, msg.message_id)


async def show_history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🍽️ История питания", callback_data="history_meals_1")],
        [InlineKeyboardButton("🏃‍♂️ История активности", callback_data="history_activity_1")],
        [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]
    ]
    text = "📜 Какую историю вы хотите посмотреть?"

    chat = update.effective_chat
    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    sent_message = await chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard))
    remember_msg(context, sent_message.message_id)
    return HISTORY_MENU


async def handle_history_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    try:
        _, history_type, page_str = query.data.split("_")
        page = int(page_str)
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка: некорректные данные пагинации.")
        return HISTORY_MENU

    api_endpoint = "meal_history" if history_type == "meals" else "activity_history"
    title = "История питания" if history_type == "meals" else "История активности"

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BACKEND_URL}/api/{api_endpoint}/{chat_id}?page={page}") as resp:
                if resp.status != 200:
                    await query.edit_message_text("⚠️ Не удалось загрузить историю.")
                    return HISTORY_MENU
                data = await resp.json()
    except aiohttp.ClientError as e:
        logging.error(f"History loading failed: {e}")
        await query.edit_message_text("⚠️ Ошибка сети при загрузке истории.")
        return HISTORY_MENU

    text = f"📜 *{title} (Страница {page})*\n\n"
    days = data.get("days", [])
    if not days:
        text += "Здесь пока пусто."
    else:
        for day in days:
            if history_type == "meals":
                text += f"*{day['date']}*: {day['total_calories']} ккал ({day['meal_count']} приёма пищи)\n"
            else:
                text += f"*{day['date']}*: {day['steps']} шагов, {day['active_kcal']} ккал\n"

    nav_buttons = []
    if data.get("has_prev"):
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"history_{history_type}_{page - 1}"))
    if data.get("has_next"):
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"history_{history_type}_{page + 1}"))

    keyboard_layout = [
        nav_buttons,
        [InlineKeyboardButton("🔙 Назад к выбору истории", callback_data="back_to_history")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_layout), parse_mode="Markdown")
    return HISTORY_MENU


async def remind_missing_meals(app: Application):
    logging.info("Running scheduled job: evening reminders")
    today_local_str = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y")

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BACKEND_URL}/api/registered_chats") as resp:
                if resp.status != 200:
                    logging.warning("registered_chats failed")
                    return
                reg = await resp.json()
                chat_ids = reg.get("chat_ids", [])

            for chat_id in chat_ids:
                meals_missing = True
                try:
                    async with session.get(f"{BACKEND_URL}/api/meals/today/{chat_id}") as r_meal:
                        if r_meal.status == 200:
                            d = await r_meal.json()
                            total = d.get("total_calories", 0) or 0
                            meals_missing = (total == 0)
                except Exception as e:
                    logging.warning(f"meals check failed for {chat_id}: {e}")

                activity_missing = True
                try:
                    async with session.get(f"{BACKEND_URL}/api/activity/today/{chat_id}") as r_act:
                        if r_act.status == 200:
                            a = await r_act.json()
                            activity_missing = (not a.get("present"))
                        else:
                            async with session.get(f"{BACKEND_URL}/api/activity_history/{chat_id}?page=1") as r_hist:
                                if r_hist.status == 200:
                                    h = await r_hist.json()
                                    days = h.get("days", [])
                                    if days and days[0].get("date") == today_local_str:
                                        activity_missing = False
                except Exception as e:
                    logging.warning(f"activity check failed for {chat_id}: {e}")

                if meals_missing or activity_missing:
                    parts = ["🌙 *Вечернее напоминание*"]
                    if meals_missing:
                        parts.append("🍽️ Сегодня вы ещё не добавили приёмы пищи.")
                    if activity_missing:
                        parts.append("🏃‍♂️ Активность за сегодня отсутствует.")

                    text = "\n\n".join(parts)
                    kb = []
                    if activity_missing:
                        kb.append([InlineKeyboardButton("➕ Добавить активность", callback_data="add_activity")])
                    if meals_missing:
                        kb.append([InlineKeyboardButton("➕ Добавить приём пищи", callback_data="add")])

                    try:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=text + "\n\n📌 Это займёт минуту — данные помогут точнее считать дефицит 💪",
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(kb) if kb else None
                        )
                    except Exception as e:
                        logging.warning(f"send reminder failed {chat_id}: {e}")

    except Exception as e:
        logging.error(f"evening reminders error: {e}")


async def show_activity_prompt(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        q = update_or_query.callback_query
        await q.answer()
        chat = update_or_query.effective_chat
        try:
            await q.message.delete()
        except Exception:
            pass
        msg = await chat.send_message(
            "📝 Введите *активные калории* и *шаги* в одном сообщении.\n\n"
            "Примеры:\n• `450 8200`\n• `ккал 520, шаги 9000`\n\n"
            "_Можно любым порядком, я сам разберу._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]])
        )
    else:
        chat = update_or_query.effective_chat
        msg = await chat.send_message(
            "📝 Введите *активные калории* и *шаги* в одном сообщении.\n\n"
            "Примеры:\n• `450 8200`\n• `ккал 520, шаги 9000`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main")]])
        )
    remember_msg(context, msg.message_id)
    return ACTIVITY_INPUT


async def handle_activity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").replace(",", " ")
    nums = re.findall(r"\d+", text)
    if len(nums) < 2:
        await update.message.reply_text("⚠️ Нужно два числа: ккал и шаги. Пример: `480 9500`", parse_mode="Markdown")
        return ACTIVITY_INPUT

    a, b = int(nums[0]), int(nums[1])
    active_kcal, steps = (a, b) if a < b else (b, a)

    loading = await update.message.reply_text("⏳ Сохраняю активность...")
    payload = {"chat_id": update.effective_chat.id, "active_kcal": active_kcal, "steps": steps}

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BACKEND_URL}/api/activity/log", json=payload) as resp:
                if resp.status == 200:
                    await loading.edit_text(f"✅ Готово! Сохранено: *{active_kcal}* ккал, *{steps}* шагов.",
                                            parse_mode="Markdown")
                else:
                    err = await resp.text()
                    logging.error(f"activity save failed: {resp.status} - {err}")
                    await loading.edit_text("⚠️ Не удалось сохранить активность. Попробуйте позже.")
    except aiohttp.ClientError as e:
        logging.error(f"activity save network error: {e}")
        await loading.edit_text("⚠️ Ошибка сети. Попробуйте позже.")

    await show_main_menu(update, context)
    return SELECT_MENU


async def on_startup(app: Application):
    try:
        await app.bot.set_my_commands([("start", "Перезапустить бота"), ("cancel", "Отменить текущую операцию")])
    except TimedOut:
        logging.warning("set_my_commands timed out, retrying in 2s…")
        await asyncio.sleep(2)
        try:
            await app.bot.set_my_commands([("start", "Перезапустить бота"), ("cancel", "Отменить текущую операцию")])
        except Exception as e:
            logging.error(f"set_my_commands failed: {e}")
    except (NetworkError, TelegramError) as e:
        logging.error(f"set_my_commands error: {e}")

    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
    except TimedOut:
        logging.warning("delete_webhook timed out; ignore")
    except (NetworkError, TelegramError) as e:
        logging.warning(f"delete_webhook error: {e}")

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(remind_missing_meals, 'cron', hour=21, minute=00, args=[app])
    scheduler.start()
    logging.info("APScheduler started.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error {context.error}")


# === MAIN ===
def main():
    if not app_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    request = HTTPXRequest(
        connection_pool_size=50,
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=5.0,
    )

    # --- 2. ИЗМЕНЕНИЕ: Активация сохранения состояния ---
    persistence = PicklePersistence(filepath="bot_data/bot_persistence")

    application = (
        Application.builder()
        .token(app_token)
        .request(request)
        .persistence(persistence)  # <--- Добавлено
        .post_init(on_startup)
        .build()
    )

    # --- 3. ИЗМЕНЕНИЕ: Полная перестройка обработчиков ---
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            # --- AI-ассистент теперь главная точка входа для текста ---
            MessageHandler(filters.TEXT & ~filters.COMMAND, kilo_entry_point_handler),
        ],
        states={
            ASK_CODE: [
                MessageHandler(filters.Regex(r"^\s*\d{8}\s*$"), verify_code),
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_code),
            ],
            # СТАЛО
            SELECT_MENU: [
                # Обработчики кнопок из всех меню
                CallbackQueryHandler(handle_menu_selection,
                                     pattern=r"^(menu_nutrition|menu_training|menu_progress|menu_more|add|add_activity|today_meals|progress|history|current|my_trainings)$"),
                CallbackQueryHandler(back_to_main_menu, pattern=r"^back_to_main$"),

                # === ДОБАВЛЕНО: Теперь AI слушает сообщения и в главном меню ===
                MessageHandler(filters.TEXT & ~filters.COMMAND, kilo_entry_point_handler),
            ],
            ACTIVITY_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_activity_input),
                CallbackQueryHandler(back_to_main_menu, pattern=r"^back_to_main$")
            ],
            ASK_PHOTO: [
                CallbackQueryHandler(ask_photo_for_meal, pattern=r"^meal_"),
                MessageHandler(filters.PHOTO, process_photo),
                CallbackQueryHandler(back_to_main_menu, pattern=r"^back_to_main$")
            ],
            HANDLE_SAVE: [CallbackQueryHandler(handle_save_confirmation, pattern=r"^save_")],
            OVERWRITE_CONFIRM: [CallbackQueryHandler(handle_overwrite, pattern=r"^overwrite_")],
            HISTORY_MENU: [
                CallbackQueryHandler(handle_history_pagination, pattern=r"^history_"),
                CallbackQueryHandler(show_history_menu, pattern=r"^back_to_history$"),
                CallbackQueryHandler(back_to_main_menu, pattern=r"^back_to_main$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            # Добавляем кнопку "Назад" в качестве fallback для многих состояний
            CallbackQueryHandler(back_to_main_menu, pattern=r"^back_to_main$"),
        ],
        allow_reentry=True,
    )

    # --- РЕГИСТРАЦИЯ ВСЕХ ОБРАБОТЧИКОВ ---
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    # Глобальный ловец 8-значного кода (если пользователь не в диалоге)
    application.add_handler(MessageHandler(filters.Regex(r"^\s*\d{8}\s*$") & ~filters.COMMAND, handle_code_anywhere))

    # Доп. команда вне беседы
    application.add_handler(CommandHandler("my_trainings", my_trainings))

    logging.info("✅ Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, poll_interval=1.0)


if __name__ == "__main__":
    main()

