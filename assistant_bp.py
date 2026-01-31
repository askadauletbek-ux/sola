import os
import logging
import json
from datetime import datetime, date, timedelta
from flask import Blueprint, request, jsonify, session
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import func

load_dotenv()
logger = logging.getLogger(__name__)

# === OpenAI / модель ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set in environment. OpenAI calls will fail.")

# Используем мощную модель для лучшего контекста
MODEL_NAME = os.getenv("KILOGRAI_MODEL", "gpt-4o")

# Параметры генерации
CLASSIFICATION_TEMPERATURE = 0.3
DEFAULT_TEMPERATURE = 0.5
DIET_TEMPERATURE = 0.7  # Чуть выше для креативности в рецептах

client = OpenAI(api_key=OPENAI_API_KEY)
assistant_bp = Blueprint('assistant', __name__, url_prefix='/api')

# Импорт моделей
try:
    from models import User, Diet, BodyAnalysis, Activity, db
except Exception as _e:
    User = None
    Diet = None
    BodyAnalysis = None
    Activity = None
    db = None
    logger.warning("Не удалось импортировать модели.")


# ------------------------------------------------------------------
# Хелперы
# ------------------------------------------------------------------

def calculate_age(born):
    if not born: return "Не указан"
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def get_full_user_context(user_id):
    """
    Собирает ПОЛНЫЙ портрет пользователя для ИИ:
    Имя, Пол, Возраст, Текущие метрики, Активность, Цели.
    """
    user = User.query.get(user_id)
    if not user: return {}

    # 1. Последний замер тела
    last_analysis = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()

    # 2. Активность за сегодня
    today_act = Activity.query.filter_by(user_id=user_id, date=date.today()).first()

    # 3. Средняя активность за неделю
    week_ago = date.today() - timedelta(days=7)
    avg_steps = db.session.query(func.avg(Activity.steps)).filter(
        Activity.user_id == user_id, Activity.date >= week_ago
    ).scalar() or 0

    return {
        "profile": {
            "name": user.name,
            "gender": user.sex or "unknown",  # 'male', 'female'
            "age": calculate_age(user.date_of_birth),
            "goal_weight": user.weight_goal,
            "goal_fat": user.fat_mass_goal,
            "start_weight": user.start_weight
        },
        "metrics": {
            "weight": last_analysis.weight if last_analysis else None,
            "height": last_analysis.height if last_analysis else None,
            "fat_mass": last_analysis.fat_mass if last_analysis else None,
            "muscle_mass": last_analysis.muscle_mass if last_analysis else None,
            "metabolism": last_analysis.metabolism if last_analysis else None
        },
        "activity": {
            "steps_today": today_act.steps if today_act else 0,
            "kcal_burned_today": today_act.active_kcal if today_act else 0,
            "avg_weekly_steps": int(avg_steps)
        }
    }


def _format_diet_summary(diet_obj):
    if not diet_obj: return "Диета пуста."
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


def _call_openai(messages, temperature=0.5, max_tokens=1000, json_mode=False):
    try:
        kwargs = {
            "model": MODEL_NAME,
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

    # Получаем историю чата
    chat_history = session.get('chat_history', [])
    chat_history.append({"role": "user", "content": user_message})
    chat_history = chat_history[-15:]  # Держим контекст компактным

    # 1. КЛАССИФИКАЦИЯ ИНТЕНТА
    # Генерация = "Что мне поесть?", "Составь рацион", "Хочу новую диету"
    # Диета = "Убери рыбу", "Что у меня на ужин?", "Не нравится, хочу другое" (работа с ТЕКУЩЕЙ)
    # Показатели = "Как мой вес?", "Проанализируй прогресс"

    CLASSIFICATION_PROMPT = """
    Определи намерение пользователя одним словом:
    1. 'Генерация' - если просит составить НОВЫЙ рацион с нуля, спрашивает "что мне есть сегодня".
    2. 'Диета' - если хочет изменить ТЕКУЩУЮ диету ("замени ужин", "убери лук", "не нравится", "хочу другое") или спрашивает о ней.
    3. 'Показатели' - если спрашивает про вес, жир, прогресс, анализ тела.
    4. 'Общее' - любой другой разговор.
    """

    msgs_classify = [{"role": "system", "content": CLASSIFICATION_PROMPT}] + chat_history[-1:]
    classifier_text = _call_openai(msgs_classify, temperature=0.3, max_tokens=20) or "Общее"

    logger.info(f"User {user_id} intent: {classifier_text}")

    # Подгружаем данные пользователя
    user_context = get_full_user_context(user_id)
    user_name = user_context['profile']['name'] or "Пользователь"
    user_gender = user_context['profile']['gender']  # 'male' / 'female'

    # =================================================================================
    # СЦЕНАРИЙ 1: ГЕНЕРАЦИЯ ДИЕТЫ (С НУЛЯ)
    # =================================================================================
    if "Генерация" in classifier_text or "Generat" in classifier_text:

        gen_system_prompt = f"""
        Ты — Kilo, элитный персональный нутрициолог.
        Твоя задача: Составить идеальный рацион на сегодня.

        ПОЛЬЗОВАТЕЛЬ:
        Имя: {user_name}
        Пол: {user_gender}
        Данные: {json.dumps(user_context, ensure_ascii=False)}

        ПРАВИЛА ОБЩЕНИЯ:
        1. Если пол 'male': Стиль тренерский, уверенный. ("потенциал", "дисциплина", "атлет").
        2. Если пол 'female': Стиль заботливый, вдохновляющий. ("умница", "отличные показатели", "сияешь").

        ЛОГИКА:
        1. Если ЦЕЛЬ (goal_weight/goal_fat) НЕ ЯСНА -> Спроси пользователя в поле 'chat_message'. 'diet_plan' = null.
        2. Если ЦЕЛЬ ЕСТЬ -> Генерируй рацион. Заполни 'chat_message' текстом, а 'diet_plan' полным JSON.

        ФОРМАТ ОТВЕТА (JSON):
        {{
            "chat_message": "Текст ответа...",
            "diet_plan": {{ "breakfast": [...], "lunch": [...], "dinner": [...], "snack": [...], "total_kcal": 0, "protein": 0, "fat": 0, "carbs": 0 }} ИЛИ null
        }}
        """

        messages = [{"role": "system", "content": gen_system_prompt}] + chat_history

        response_json_str = _call_openai(messages, temperature=DIET_TEMPERATURE, max_tokens=2000, json_mode=True)

        if response_json_str:
            try:
                resp_data = json.loads(response_json_str)
                ai_text = resp_data.get('chat_message', 'Готово!')
                diet_plan = resp_data.get('diet_plan')

                # Если план сгенерирован -> сохраняем в БД
                if diet_plan:
                    # Удаляем старое за сегодня
                    Diet.query.filter_by(user_id=user_id, date=date.today()).delete()

                    new_diet = Diet(
                        user_id=user_id,
                        date=date.today(),
                        breakfast=json.dumps(diet_plan.get('breakfast', []), ensure_ascii=False),
                        lunch=json.dumps(diet_plan.get('lunch', []), ensure_ascii=False),
                        dinner=json.dumps(diet_plan.get('dinner', []), ensure_ascii=False),
                        snack=json.dumps(diet_plan.get('snack', []), ensure_ascii=False),
                        total_kcal=diet_plan.get('total_kcal'),
                        protein=diet_plan.get('protein'),
                        fat=diet_plan.get('fat'),
                        carbs=diet_plan.get('carbs')
                    )
                    db.session.add(new_diet)
                    db.session.commit()
                    logger.info(f"AI generated diet for {user_id}")

                # Отдаем ответ
                chat_history.append({"role": "assistant", "content": ai_text})
                session['chat_history'] = chat_history
                return jsonify({"role": "ai", "content": ai_text}), 200

            except Exception as e:
                logger.error(f"Diet Gen Error: {e}")
                return jsonify({"role": "ai", "content": "Что-то пошло не так. Попробуйте еще раз."}), 200

    # =================================================================================
    # СЦЕНАРИЙ 2: РАБОТА С ТЕКУЩЕЙ ДИЕТОЙ (ВОПРОСЫ И ИЗМЕНЕНИЯ) - ИСПРАВЛЕНО
    # =================================================================================
    elif "Диета" in classifier_text:
        current_diet = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
        if not current_diet:
            return jsonify({"role": "ai",
                            "content": "У вас еще нет активной диеты. Напишите 'Составь рацион', чтобы начать!"}), 200

        diet_json = _format_diet_summary(current_diet)

        # Строгий системный промпт для модификации
        mod_system_prompt = f"""
        Ты — Kilo, диетолог. Работаешь с текущим рационом пользователя (JSON):
        {diet_json}

        ТВОЯ ЗАДАЧА:
        Проанализировать запрос пользователя "{user_message}" и вернуть JSON СТРОГО одного из двух типов:

        ТИП 1: Пользователь просто задает вопрос (например: "что на ужин?", "сколько там белка?").
        Верни:
        {{
            "action": "answer",
            "text": "Твой текстовый ответ на вопрос..."
        }}

        ТИП 2: Пользователь хочет изменений ("не нравится", "хочу другое", "убери рыбу", "замени ужин").
        Верни:
        {{
            "action": "update",
            "text": "Короткий комментарий (например: 'Понял, полностью заменил меню на новое.')",
            "diet_plan": {{ ...полностью обновленная структура диеты (breakfast, lunch, dinner, snack, total_kcal, macros)... }}
        }}

        ВАЖНО: 
        - Если пользователь пишет "не нравится" или "хочу другое" без уточнений — предложи ПОЛНОСТЬЮ НОВЫЕ БЛЮДА (весь рацион), подходящие под его калораж.
        - Возвращай валидный JSON.
        """

        messages = [{"role": "system", "content": mod_system_prompt}]

        # json_mode=True ГАРАНТИРУЕТ, что не будет простого текста
        response_json_str = _call_openai(messages, temperature=0.7, max_tokens=2000, json_mode=True)

        if response_json_str:
            try:
                resp_data = json.loads(response_json_str)
                action = resp_data.get("action")
                ai_text = resp_data.get("text", "Готово.")

                if action == "answer":
                    # Просто ответ
                    chat_history.append({"role": "assistant", "content": ai_text})
                    session['chat_history'] = chat_history
                    return jsonify({"role": "ai", "content": ai_text}), 200

                elif action == "update":
                    # Обновляем базу
                    new_plan = resp_data.get("diet_plan")
                    if new_plan:
                        current_diet.breakfast = json.dumps(new_plan.get('breakfast', []), ensure_ascii=False)
                        current_diet.lunch = json.dumps(new_plan.get('lunch', []), ensure_ascii=False)
                        current_diet.dinner = json.dumps(new_plan.get('dinner', []), ensure_ascii=False)
                        current_diet.snack = json.dumps(new_plan.get('snack', []), ensure_ascii=False)
                        current_diet.total_kcal = new_plan.get('total_kcal')
                        current_diet.protein = new_plan.get('protein')
                        current_diet.fat = new_plan.get('fat')
                        current_diet.carbs = new_plan.get('carbs')

                        db.session.commit()
                        logger.info(f"Diet updated via chat for user {user_id}")

                        chat_history.append({"role": "assistant", "content": ai_text})
                        session['chat_history'] = chat_history
                        return jsonify({"role": "ai", "content": ai_text}), 200
                    else:
                        return jsonify(
                            {"role": "ai", "content": "Не удалось перестроить план. Попробуйте уточнить запрос."}), 200

                else:
                    return jsonify({"role": "ai", "content": ai_text}), 200

            except Exception as e:
                logger.error(f"Diet Modify JSON Error: {e}")
                return jsonify({"role": "ai", "content": "Произошла ошибка при изменении. Попробуйте еще раз."}), 200
        else:
            return jsonify({"role": "ai", "content": "ИИ не ответил. Попробуйте позже."}), 200

    # =================================================================================
    # СЦЕНАРИЙ 3: ПОКАЗАТЕЛИ (АНАЛИЗ)
    # =================================================================================
    elif "Показатели" in classifier_text:
        current_ba = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()
        if not current_ba:
            return jsonify({"role": "ai", "content": "Нет данных анализа тела. Загрузите фото с весов в профиле!"}), 200

        ba_sum = _format_body_summary(current_ba)
        reply = _call_openai([
            {"role": "system", "content": "Ты фитнес-аналитик. Проанализируй данные пользователя, дай совет."},
            {"role": "user", "content": f"Данные: {ba_sum}. Вопрос: {user_message}"}
        ])
        chat_history.append({"role": "assistant", "content": reply})
        session['chat_history'] = chat_history
        return jsonify({"role": "ai", "content": reply}), 200

    # =================================================================================
    # СЦЕНАРИЙ 4: ОБЩИЙ ЧАТ
    # =================================================================================
    else:
        general_prompt = f"""
        Ты — Kilo, помощник Kilogr.app.
        Пользователь: {user_name}, Пол: {user_gender}.
        Данные: {json.dumps(user_context['profile'], ensure_ascii=False)}
        Отвечай кратко, дружелюбно и по делу.
        """
        messages = [{"role": "system", "content": general_prompt}] + chat_history
        reply = _call_openai(messages, temperature=DEFAULT_TEMPERATURE)

        chat_history.append({"role": "assistant", "content": reply})
        session['chat_history'] = chat_history
        return jsonify({"role": "ai", "content": reply}), 200


@assistant_bp.route('/assistant/history', methods=['GET'])
def get_history():
    return jsonify({"messages": session.get('chat_history', [])}), 200


@assistant_bp.route('/assistant/clear', methods=['POST'])
def clear_history():
    session.pop('chat_history', None)
    return jsonify({"status": "ok"}), 200