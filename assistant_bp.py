import os
import logging
import json  # <--- Добавлено
from datetime import datetime, date  # <--- Добавлено date
from flask import Blueprint, request, jsonify, session
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import func  # <--- Добавлено

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
DIET_MAX_TOKENS = int(os.getenv("KILOGRAI_DIET_MAX_TOKENS", "1500"))  # <--- Увеличено для JSON генерации

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
"""

SYSTEM_PROMPT = f"""
Ты — Kilo, дружелюбный и профессиональный AI-ассистент платформы Kilogr.app. Твоя миссия — помогать пользователям достигать их фитнес-целей с улыбкой! 😊

---
ТВОИ ПРАВИЛА:
1. **Будь экспертом по Kilogr.app.**
2. **Всегда будь доброжелательным.**
3. **Только по теме.**
4. **Четкость и краткость.**
5. **Используй пошаговые инструкции.**

---
Важные правила-детекторы (classification-by-prompt):

1) **Генерация новой диеты:** 

[Image of balanced meal plan]

Если пользователь просит "составить рацион", "что мне поесть", "сгенерируй диету", "хочу новую диету", ты **всегда** отвечаешь ровно одним словом:

Генерация

2) **Работа с текущей диетой (изменение или вопрос):**
Если пользователь просит изменить текущую диету ("замени рыбу", "убери завтрак", "добавь орехи") или спрашивает о ней ("что у меня на обед?"), ты **всегда** отвечаешь ровно одним словом:

Диета

3) **Анализ показателей:**
Если пользователь просит проанализировать вес, жир, мышцы и т.д., ты отвечаешь:

Показатели

Ничего другого в ответе быть не должно.
---
{PLATFORM_CONTEXT}
"""

try:
    from models import User, Diet, BodyAnalysis, db
except Exception as _e:
    User = None
    Diet = None
    BodyAnalysis = None
    db = None
    logger.warning("Не удалось импортировать модели.")


# ------------------------------------------------------------------
# Хелперы
# ------------------------------------------------------------------
def _format_diet_summary(diet_obj):
    if not diet_obj: return "Диета пуста."
    # Просто возвращаем JSON строку для AI, чтобы ему было легче парсить структуру
    summary = {
        "breakfast": json.loads(diet_obj.breakfast) if diet_obj.breakfast else [],
        "lunch": json.loads(diet_obj.lunch) if diet_obj.lunch else [],
        "dinner": json.loads(diet_obj.dinner) if diet_obj.dinner else [],
        "snack": json.loads(diet_obj.snack) if diet_obj.snack else [],
        "total_kcal": diet_obj.total_kcal,
        "protein": diet_obj.protein,
        "fat": diet_obj.fat,
        "carbs": diet_obj.carbs
    }
    return json.dumps(summary, ensure_ascii=False)


def _format_body_summary(ba_obj):
    if not ba_obj: return "Данные анализа отсутствуют."
    return f"Рост: {ba_obj.height}, Вес: {ba_obj.weight}, Жир: {ba_obj.fat_mass}, Мышцы: {ba_obj.muscle_mass}, Метаболизм: {ba_obj.metabolism}"


def _call_openai(messages, temperature=0.5, max_tokens=400, model=MODEL_NAME, json_mode=False):
    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI call failed: %s", e)
        return None


# ------------------------------------------------------------------
# Эндпоинты
# ------------------------------------------------------------------
@assistant_bp.route('/assistant/chat', methods=['POST'])
def handle_chat():
    data = request.json or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({"role": "error", "content": "Пустое сообщение"}), 400

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"role": "ai", "content": "Пожалуйста, авторизуйтесь."}), 401

    chat_history = session.get('chat_history', [])
    chat_history.append({"role": "user", "content": user_message})
    chat_history = chat_history[-20:]  # Keep context short

    # 1. Классификация
    messages_for_api = [{"role": "system", "content": SYSTEM_PROMPT}] + chat_history
    classifier_text = _call_openai(messages_for_api, temperature=CLASSIFICATION_TEMPERATURE,
                                   max_tokens=CLASSIFICATION_MAX_TOKENS) or ""

    logger.info(f"User: {user_id}, Intent: {classifier_text}")

    user = User.query.get(user_id)
    user_name = getattr(user, "name", "Пользователь")

    # =================================================================================
    # СЦЕНАРИЙ 1: ГЕНЕРАЦИЯ НОВОЙ ДИЕТЫ
    # =================================================================================
    if classifier_text == "Генерация":
        # Собираем данные для генерации
        latest_analysis = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()

        if not latest_analysis:
            return jsonify({"role": "ai",
                            "content": "Чтобы я мог составить рацион, сначала загрузите анализ тела (фото с весов) в профиле! 📊"}), 200

        # Промпт для генерации
        gen_system = "Ты — профессиональный диетолог. Твоя задача — составить рацион на 1 день в формате JSON."
        gen_prompt = f"""
        Пользователь: {user_name}.
        Параметры: Рост {latest_analysis.height}, Вес {latest_analysis.weight}, Жир {latest_analysis.fat_mass}, Метаболизм {latest_analysis.metabolism}.
        Запрос пользователя: "{user_message}"

        Составь сбалансированный рацион (завтрак, обед, ужин, перекус).
        Верни СТРОГО JSON в формате:
        {{
            "breakfast": [{{"name": "...", "grams": 0, "kcal": 0, "recipe": "..."}}],
            "lunch": [...],
            "dinner": [...],
            "snack": [...],
            "total_kcal": 0, "protein": 0, "fat": 0, "carbs": 0
        }}
        """

        json_resp = _call_openai([{"role": "system", "content": gen_system}, {"role": "user", "content": gen_prompt}],
                                 temperature=0.7, max_tokens=1500, json_mode=True)

        if json_resp:
            try:
                diet_data = json.loads(json_resp)

                # Удаляем старую за сегодня
                existing = Diet.query.filter_by(user_id=user_id, date=date.today()).first()
                if existing: db.session.delete(existing)

                new_diet = Diet(
                    user_id=user_id,
                    date=date.today(),
                    breakfast=json.dumps(diet_data.get('breakfast', []), ensure_ascii=False),
                    lunch=json.dumps(diet_data.get('lunch', []), ensure_ascii=False),
                    dinner=json.dumps(diet_data.get('dinner', []), ensure_ascii=False),
                    snack=json.dumps(diet_data.get('snack', []), ensure_ascii=False),
                    total_kcal=diet_data.get('total_kcal'),
                    protein=diet_data.get('protein'),
                    fat=diet_data.get('fat'),
                    carbs=diet_data.get('carbs')
                )
                db.session.add(new_diet)
                db.session.commit()

                msg = f"🥗 Готово, {user_name}! Я составил новый рацион на {diet_data.get('total_kcal')} ккал. Загляните в раздел 'Диета'!"

                chat_history.append({"role": "assistant", "content": msg})
                session['chat_history'] = chat_history
                return jsonify({"role": "ai", "content": msg}), 200
            except Exception as e:
                logger.error(f"Diet gen parsing error: {e}")
                return jsonify(
                    {"role": "ai", "content": "Произошла ошибка при составлении меню. Попробуйте еще раз."}), 200

    # =================================================================================
    # СЦЕНАРИЙ 2: РАБОТА С ТЕКУЩЕЙ ДИЕТОЙ (ИЗМЕНЕНИЕ ИЛИ ВОПРОС)
    # =================================================================================
    elif classifier_text == "Диета":
        current_diet = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
        if not current_diet:
            return jsonify(
                {"role": "ai", "content": "У вас еще нет активной диеты. Попросите меня 'составить рацион'!"}), 200

        diet_json = _format_diet_summary(current_diet)

        # Шаг А: Понимаем, хочет ли юзер ИЗМЕНИТЬ данные или просто СПРОСИТЬ
        router_prompt = f"""
        Текущая диета (JSON): {diet_json}
        Запрос пользователя: "{user_message}"

        Определи намерение:
        1. Если пользователь хочет ЗАМЕНИТЬ блюдо, УБРАТЬ что-то, ИЗМЕНИТЬ калораж — верни обновленный JSON всей диеты с учетом изменений.
        2. Если это просто вопрос ("что на ужин?", "сколько калорий?") — верни строку "TEXT_ONLY".

        Верни либо JSON (структура diet), либо строку "TEXT_ONLY".
        """

        router_resp = _call_openai([{"role": "system", "content": "Ты технический роутер."},
                                    {"role": "user", "content": router_prompt}],
                                   temperature=0.3, max_tokens=1500)

        # Шаг Б: Обработка
        if "TEXT_ONLY" in router_resp:
            # Обычный текстовый ответ (как было раньше)
            diet_system = f"Ты диетолог. Диета пользователя: {diet_json}. Ответь на вопрос пользователя."
            text_reply = _call_openai(
                [{"role": "system", "content": diet_system}, {"role": "user", "content": user_message}],
                temperature=0.5)
            chat_history.append({"role": "assistant", "content": text_reply})
            session['chat_history'] = chat_history
            return jsonify({"role": "ai", "content": text_reply}), 200

        else:
            # Это запрос на изменение -> Пришел JSON
            try:
                new_diet_data = json.loads(router_resp)

                # Обновляем БД
                current_diet.breakfast = json.dumps(new_diet_data.get('breakfast', []), ensure_ascii=False)
                current_diet.lunch = json.dumps(new_diet_data.get('lunch', []), ensure_ascii=False)
                current_diet.dinner = json.dumps(new_diet_data.get('dinner', []), ensure_ascii=False)
                current_diet.snack = json.dumps(new_diet_data.get('snack', []), ensure_ascii=False)
                current_diet.total_kcal = new_diet_data.get('total_kcal')
                current_diet.protein = new_diet_data.get('protein')
                current_diet.fat = new_diet_data.get('fat')
                current_diet.carbs = new_diet_data.get('carbs')

                db.session.commit()

                success_msg = f"✅ Сделано, {user_name}! Я обновил вашу диету. Новая калорийность: {current_diet.total_kcal} ккал."
                chat_history.append({"role": "assistant", "content": success_msg})
                session['chat_history'] = chat_history
                return jsonify({"role": "ai", "content": success_msg}), 200

            except json.JSONDecodeError:
                # Fallback если ИИ вернул ерунду
                return jsonify({"role": "ai",
                                "content": "Я попытался изменить диету, но что-то пошло не так. Попробуйте переформулировать запрос."}), 200

    # =================================================================================
    # СЦЕНАРИЙ 3: ПОКАЗАТЕЛИ (Остался без изменений, только кратко)
    # =================================================================================
    elif classifier_text == "Показатели":
        # ... (Код анализа показателей, аналогичный вашему старому, только убедитесь, что импорты работают)
        current_ba = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()
        if not current_ba:
            return jsonify({"role": "ai", "content": "Нет данных для анализа."}), 200

        ba_summary = _format_body_summary(current_ba)
        sys_msg = "Ты фитнес-аналитик. Дай краткий анализ и рекомендацию."
        reply = _call_openai([{"role": "system", "content": sys_msg},
                              {"role": "user", "content": f"Показатели: {ba_summary}. Вопрос: {user_message}"}])

        chat_history.append({"role": "assistant", "content": reply})
        session['chat_history'] = chat_history
        return jsonify({"role": "ai", "content": reply}), 200

    # =================================================================================
    # ОБЩИЙ ЧАТ
    # =================================================================================
    else:
        completion = _call_openai(messages_for_api, temperature=DEFAULT_TEMPERATURE, max_tokens=DEFAULT_MAX_TOKENS)
        chat_history.append({"role": "assistant", "content": completion})
        session['chat_history'] = chat_history
        return jsonify({"role": "ai", "content": completion}), 200


@assistant_bp.route('/assistant/history', methods=['GET'])
def get_history():
    return jsonify({"messages": session.get('chat_history', [])}), 200


@assistant_bp.route('/assistant/clear', methods=['POST'])
def clear_history():
    session.pop('chat_history', None)
    return jsonify({"status": "ok"}), 200