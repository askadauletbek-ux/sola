# assistant.py
import os
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, session
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
logger = logging.getLogger(__name__)

# === OpenAI / модель ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set in environment. OpenAI calls will fail.")

MODEL_NAME = os.getenv("KILOGRAI_MODEL", "gpt-4o")
CLASSIFICATION_TEMPERATURE = float(os.getenv("KILOGRAI_CLASSIFY_TEMPERATURE", "0.3"))
CLASSIFICATION_MAX_TOKENS = int(os.getenv("KILOGRAI_CLASSIFY_MAX_TOKENS", "16"))
DEFAULT_TEMPERATURE = float(os.getenv("KILOGRAI_TEMPERATURE", "0.5"))
DEFAULT_MAX_TOKENS = int(os.getenv("KILOGRAI_MAX_TOKENS", "400"))

DIET_TEMPERATURE = float(os.getenv("KILOGRAI_DIET_TEMPERATURE", "0.35"))
DIET_MAX_TOKENS = int(os.getenv("KILOGRAI_DIET_MAX_TOKENS", "500"))

BODY_TEMPERATURE = float(os.getenv("KILOGRAI_BODY_TEMPERATURE", "0.35"))
BODY_MAX_TOKENS = int(os.getenv("KILOGRAI_BODY_MAX_TOKENS", "500"))

client = OpenAI(api_key=OPENAI_API_KEY)
assistant_bp = Blueprint('assistant', __name__, url_prefix='/api')
# ------------------------------------------------------------------
# Контекст платформы и системный промпт
# ------------------------------------------------------------------
PLATFORM_CONTEXT = """
Это твоя база знаний о платформе Kilogr.app. Ты знаешь всё об этих функциях и как ими пользоваться.

## 🚀 Основные функции:
- 🎯 Профиль, 👤 Анализ тела, 🥗 AI-Диета, 🍽️ Анализ еды по фото, 🏃 Активность, 💪 Тренировки, 💬 Группы, ✨ AI-Визуализация, 💳 Подписка, 🤖 Telegram-Бот.
(Пошаговые инструкции и детали доступны в полном контексте платформы.)
"""

SYSTEM_PROMPT = f"""
Ты — Kilo, дружелюбный и профессиональный AI-ассистент платформы Kilogr.app.  Твоя миссия — помогать пользователям достигать их фитнес-целей с улыбкой! 😊

---
ТВОИ ПРАВИЛА:

1.  **Будь экспертом по Kilogr.app:** Используй свою базу знаний, чтобы четко и по делу отвечать на любые вопросы о функциях платформы.
2.  **Всегда будь доброжелательным:** Используй позитивный тон и смайлики (например, 💪, 🥗, ✨, 🎯), чтобы общение было легким и мотивирующим.
3.  **Только по теме:** Отвечай СТРОГО на вопросы, связанные с Kilogr.app, фитнесом и питанием. Если тебя спрашивают о чем-то другом, вежливо откажись.
4.  **Четкость и краткость:** Давай прямые и понятные ответы. Избегай "воды". Всегда используй Markdown (списки, жирный шрифт), чтобы структурировать ответ и сделать его легким для чтения.
5.  **Используй пошаговые инструкции:** Когда пользователь спрашивает "как что-то сделать?", используй детальные инструкции из своей базы знаний.
6.  **Твоя цель:** Сделать путь пользователя к здоровью проще и приятнее. Подбадривай и помогай!

---
Важные правила-детекторы (classification-by-prompt):

1) **Диетические интенты:** 
Если и только если пользовательский запрос прямо или косвенно относится к работе с текущей персональной диетой пользователя 
(замена блюда в текущей диете, просьба изменить/адаптировать рацион, вопросы "чем заменить", "заменить в диете", "в моей диете" и т.п.), 
ты **не** даёшь обычного развернутого ответа. Вместо этого ты **всегда** отвечаешь ровно одним словом, без кавычек и других символов:

Диета

Ничего другого в ответе быть не должно.

2) **Анализ показателей (body metrics) интенты:**
Если и только если пользователь явно или косвенно просит проанализировать/разобрать/оценить свои показатели тела (веса, %жира, мышечной массы, метаболизма, BMI, возраст тела и т.п.), 
ты **всегда** отвечаешь ровно одним словом, без кавычек и других символов:

Показатели

Ничего другого в ответе быть не должно.

---
После того как сервер увидит маркер `Диета` или `Показатели`, он:
- не сохраняет маркер в истории,
- получает из БД соответствующие данные (Diet или BodyAnalysis) + имя пользователя + последнее сообщение,
- формирует отдельный целевой промпт и выполняет второй вызов модели,
- сохраняет и возвращает **только второй** (полезный) ответ пользователю.
---

{PLATFORM_CONTEXT}
"""

# ------------------------------------------------------------------
# Попытка импортировать модели (User, Diet, BodyAnalysis, db)
# ------------------------------------------------------------------
try:
    # Замените 'models' на реальный модуль в вашем проекте
    from models import User, Diet, BodyAnalysis, db
except Exception as _e:
    User = None
    Diet = None
    BodyAnalysis = None
    db = None
    logger.warning("Не удалось импортировать модели User/Diet/BodyAnalysis/db — исправьте путь импорта (from models import ...).")


# ------------------------------------------------------------------
# Форматирование сводок
# ------------------------------------------------------------------
def _format_diet_summary(diet_obj):
    if not diet_obj:
        return "Диета пуста."
    parts = []
    if getattr(diet_obj, "breakfast", None):
        parts.append(f"Завтрак: {diet_obj.breakfast}")
    if getattr(diet_obj, "lunch", None):
        parts.append(f"Обед: {diet_obj.lunch}")
    if getattr(diet_obj, "dinner", None):
        parts.append(f"Ужин: {diet_obj.dinner}")
    if getattr(diet_obj, "snack", None):
        parts.append(f"Перекус: {diet_obj.snack}")
    kcal = getattr(diet_obj, "total_kcal", None)
    protein = getattr(diet_obj, "protein", None)
    fat = getattr(diet_obj, "fat", None)
    carbs = getattr(diet_obj, "carbs", None)
    summary = "\n".join(parts) or "Диета пуста."
    if any(v is not None for v in (kcal, protein, fat, carbs)):
        summary += f"\nИтого: {kcal or 0} ккал, Б: {protein or 0} г, Ж: {fat or 0} г, У: {carbs or 0} г"
    return summary


def _format_body_summary(ba_obj):
    if not ba_obj:
        return "Данные анализа тела отсутствуют."
    lines = []
    # Берём только интересные поля и печатаем их аккуратно
    def g(name):
        return getattr(ba_obj, name, None)
    fields = [
        ("Дата измерения", getattr(ba_obj, "timestamp", None)),
        ("Рост (см)", g("height")),
        ("Вес (кг)", g("weight")),
        ("Мышечная масса (кг)", g("muscle_mass")),
        ("% мышц", g("muscle_percentage")),
        ("Водность тела (%)", g("body_water")),
        ("% белка", g("protein_percentage")),
        ("% минеральной массы костей", g("bone_mineral_percentage")),
        ("Скелетная мышечная масса (SMM)", g("skeletal_muscle_mass")),
        ("Висцеральный жир (оценка)", g("visceral_fat_rating")),
        ("Метаболизм (BMR)", g("metabolism")),
        ("Талиево-бедренное отношение (WHR)", g("waist_hip_ratio")),
        ("Возраст тела", g("body_age")),
        ("Жировая масса (кг)", g("fat_mass")),
        ("BMI", g("bmi")),
        ("Безжировая масса (кг)", g("fat_free_body_weight"))
    ]
    for label, val in fields:
        if val is None:
            continue
        # форматируем datetime красиво
        if isinstance(val, datetime):
            val = val.isoformat(sep=' ', timespec='seconds')
        lines.append(f"- **{label}**: {val}")
    return "\n".join(lines) or "Данные анализа тела пусты."


# ------------------------------------------------------------------
# Хелпер для вызова OpenAI
# ------------------------------------------------------------------
def _call_openai(messages, temperature=0.5, max_tokens=400, model=MODEL_NAME):
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI call failed: %s", e)
        return None


# ------------------------------------------------------------------
# Эндпоинты: /assistant/chat, /assistant/history, /assistant/clear
# ------------------------------------------------------------------
@assistant_bp.route('/assistant/chat', methods=['POST'])
def handle_chat():
    data = request.json or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({"role": "error", "content": "Сообщение не может быть пустым"}), 400

    chat_history = session.get('chat_history', [])
    chat_history.append({"role": "user", "content": user_message})
    chat_history = chat_history[-20:]
    session['chat_history'] = chat_history
    session.modified = True

    messages_for_api = [{"role": "system", "content": SYSTEM_PROMPT}] + chat_history

    try:
        classification_resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages_for_api,
            temperature=CLASSIFICATION_TEMPERATURE,
            max_tokens=CLASSIFICATION_MAX_TOKENS
        )

        # --- ИСПРАВЛЕНИЕ 1: Безопасное получение ответа ---
        classifier_text = ""
        if classification_resp.choices and classification_resp.choices[0].message:
            classifier_text = (classification_resp.choices[0].message.content or "").strip()
        else:
            logger.warning("OpenAI classification returned no choices.")
        # --- КОНЕЦ ИСПРАВЛЕНИЯ 1 ---

        logger.debug("Classifier response: %r", classifier_text)

    except Exception as e:
        logger.exception("OpenAI classification call failed")
        return jsonify({"role": "error", "content": "Ошибка при обращении к ассистенту."}), 500

    # --- Если модель вернула маркер "Диета" ---
    if classifier_text == "Диета":
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({"role": "ai", "content": "Пользователь не авторизован (нет user_id в сессии)."}), 200

        if Diet is None or User is None:
            logger.error("Diet/User model not available - check imports")
            return jsonify({"role": "ai", "content": "Ошибка сервера: модель Diet/User недоступна."}), 200

        try:
            user = User.query.get(user_id)
        except Exception:
            user = None
            logger.exception("DB error when fetching user")

        if not user:
            return jsonify({"role": "ai", "content": "Пользователь не найден в базе."}), 200

        user_name = getattr(user, "name", None) or "Пользователь"

        try:
            current_diet = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
        except Exception:
            current_diet = None
            logger.exception("DB error when fetching diet")

        if not current_diet:
            return jsonify({"role": "ai",
                            "content": f"{user_name}, я не нашёл вашу текущую диету в базе. Пожалуйста, сохраните диету в профиле."}), 200

        diet_summary = _format_diet_summary(current_diet)
        diet_system = (
            f"Ты — экспертный диетолог-ассистент Kilogr.app. Всегда обращайся к пользователю по имени: {user_name}. "
            "Твоя задача — работать с конкретной сохранённой диетой пользователя. Используй данные диеты ниже и последнее сообщение пользователя. "
            "Отвечай практично: давай 1–2 варианта замены, с указанием примерных граммов и приближённых КБЖУ, если возможно. Отвечай коротко и по делу."
        )
        diet_user_prompt = (
            f"Имя пользователя: {user_name}\n"
            f"Текущая сохранённая диета пользователя (последняя запись: {getattr(current_diet, 'date', 'неизвестна')}):\n\n"
            f"{diet_summary}\n\n"
            f"---\nПользователь написал: \"{user_message}\"\n\n"
            "Дай конкретное предложение замены/вариантов для указанного в запросе блюда (указывать граммы и прибл. КБЖУ, если возможно). "
            "Ответь коротко и ясными пунктами, и в начале обращения обязательно обратись к пользователю по имени (например: \"Аскар, ...\")."
        )

        messages_for_diet_api = [
            {"role": "system", "content": diet_system},
            {"role": "user", "content": diet_user_prompt}
        ]

        diet_reply = _call_openai(messages_for_diet_api, temperature=DIET_TEMPERATURE, max_tokens=DIET_MAX_TOKENS)
        if diet_reply is None:
            return jsonify({"role": "error", "content": "Ошибка при получении ответа диетического ассистента."}), 500

        chat_history.append({"role": "assistant", "content": diet_reply})
        session['chat_history'] = chat_history
        session.modified = True

        return jsonify({"role": "ai", "content": diet_reply}), 200

    # --- Если модель вернула маркер "Показатели" ---
    if classifier_text == "Показатели":
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({"role": "ai", "content": "Пользователь не авторизован (нет user_id в сессии)."}), 200

        if BodyAnalysis is None or User is None:
            logger.error("BodyAnalysis/User model not available - check imports")
            return jsonify({"role": "ai", "content": "Ошибка сервера: модель BodyAnalysis/User недоступна."}), 200

        try:
            user = User.query.get(user_id)
        except Exception:
            user = None
            logger.exception("DB error when fetching user")

        if not user:
            return jsonify({"role": "ai", "content": "Пользователь не найден в базе."}), 200

        user_name = getattr(user, "name", None) or "Пользователь"

        try:
            current_ba = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()
        except Exception:
            current_ba = None
            logger.exception("DB error when fetching body analysis")

        if not current_ba:
            return jsonify({"role": "ai", "content": f"{user_name}, у вас нет сохранённых данных анализа тела."}), 200

        body_summary = _format_body_summary(current_ba)
        body_system = (
            f"Ты — экспертный специалист по анализу тела Kilogr.app. Всегда обращайся к пользователю по имени: {user_name}. Разговаривай очень дружелюбно, смайлики используй. Не здаровайся если пользователь сам не здаровается первым. Пытайся отвечать максимально по делу без лишней воды и выдуманной инфы. Лучше отвечай коротко по возможности"
            "Твоя задача — дать тактичный и полезный анализ последних показателей тела, указать сильные/слабые места, дать простые рекомендации (питание/тренировки/поведение) и, при необходимости, предложить варианты коррекции. И говори о том что Kilogr.app поможет ему в достижений целей. "
        )
        body_user_prompt = (
            f"Имя пользователя: {user_name}\n"
            f"Последние сохранённые показатели тела (включая пояснения):\n\n"
            f"{body_summary}\n\n"
            f"---\nПользователь написал: \"{user_message}\"\n\n"
            "Дай компактный и понятный анализ показателей, укажи 2–3 практические рекомендации и, если есть тревожные признаки, предупреди. "
            "В начале ответа обязательно обратись к пользователю по имени (например: \"Аскар, ...\")."
        )

        messages_for_body_api = [
            {"role": "system", "content": body_system},
            {"role": "user", "content": body_user_prompt}
        ]

        body_reply = _call_openai(messages_for_body_api, temperature=BODY_TEMPERATURE, max_tokens=BODY_MAX_TOKENS)
        if body_reply is None:
            return jsonify({"role": "error", "content": "Ошибка при получении ответа по показателям."}), 500

        chat_history.append({"role": "assistant", "content": body_reply})
        session['chat_history'] = chat_history
        session.modified = True

        return jsonify({"role": "ai", "content": body_reply}), 200

    # --- Иначе: обычный полноценный поток ассистента ---
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages_for_api,
            temperature=DEFAULT_TEMPERATURE,
            max_tokens=DEFAULT_MAX_TOKENS
        )

        # --- ИСПРАВЛЕНИЕ 2: Безопасное получение ответа ---
        bot_response = ""
        if completion.choices and completion.choices[0].message:
            bot_response = (completion.choices[0].message.content or "").strip()
        else:
            logger.warning("OpenAI general chat returned no choices.")

        if not bot_response:
            # Если ответа все равно нет, возвращаем вежливую ошибку
            bot_response = "Извините, я не смог обработать ваш запрос. Попробуйте переформулировать."
        # --- КОНЕЦ ИСПРАВЛЕНИЯ 2 ---

        chat_history.append({"role": "assistant", "content": bot_response})
        session['chat_history'] = chat_history
        session.modified = True

        return jsonify({"role": "ai", "content": bot_response}), 200

    except Exception as e:
        logger.exception("OpenAI general error")
        return jsonify({"role": "error", "content": "Не удалось связаться с ассистентом. Попробуйте позже."}), 500

@assistant_bp.route('/assistant/history', methods=['GET'])
def get_history():
    chat_history = session.get('chat_history', [])
    # ИСПРАВЛЕНИЕ: Меняем ключ 'history' на 'messages'
    return jsonify({"messages": chat_history}), 200

@assistant_bp.route('/assistant/clear', methods=['POST'])
def clear_history():
    session.pop('chat_history', None)
    session.modified = True
    return jsonify({"status": "ok"}), 200
