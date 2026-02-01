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

MODEL_NAME = os.getenv("KILOGRAI_MODEL", "gpt-4o")

# Параметры генерации
CLASSIFICATION_TEMPERATURE = 0.3
DEFAULT_TEMPERATURE = 0.5
DIET_TEMPERATURE = 0.7

client = OpenAI(api_key=OPENAI_API_KEY)
assistant_bp = Blueprint('assistant', __name__, url_prefix='/api')

# Импорт моделей
try:
    from models import User, Diet, BodyAnalysis, Activity, db
    from notification_service import send_user_notification
    from amplitude import BaseEvent
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
    """Собирает ПОЛНЫЙ портрет пользователя для ИИ."""
    user = User.query.get(user_id)
    if not user: return {}

    last_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
    today_act = Activity.query.filter_by(user_id=user.id, date=date.today()).first()

    week_ago = date.today() - timedelta(days=7)
    avg_steps = db.session.query(func.avg(Activity.steps)).filter(
        Activity.user_id == user_id, Activity.date >= week_ago
    ).scalar() or 0

    return {
        "profile": {
            "name": user.name,
            "gender": user.sex or "unknown",
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
            "avg_weekly_steps": int(avg_steps)
        }
    }


def _format_diet_summary(diet_obj):
    if not diet_obj: return "Нет активного рациона."
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


def format_diet_string(diet_plan):
    """Превращает JSON диеты в красивый текст для чата."""
    if not diet_plan or not isinstance(diet_plan, dict): return ""

    text = "\n\n🍽 **План питания:**\n"

    mapping = {
        "breakfast": "🍳 Завтрак",
        "lunch": "🍲 Обед",
        "dinner": "🥗 Ужин",
        "snack": "🥜 Перекус"
    }

    for key, title in mapping.items():
        items = diet_plan.get(key, [])
        if items and isinstance(items, list):
            text += f"\n**{title}:**"
            for item in items:
                if isinstance(item, dict):
                    name = item.get('name', 'Блюдо')
                    grams = item.get('grams', 0)
                    kcal = item.get('kcal', 0)
                    text += f"\n- {name} ({grams}г) — {kcal} ккал"
            text += "\n"

    total = diet_plan.get('total_kcal', 0)
    p = diet_plan.get('protein', 0)
    f = diet_plan.get('fat', 0)
    c = diet_plan.get('carbs', 0)

    text += f"\n🔥 **Итого:** {total} ккал (Б: {p} / Ж: {f} / У: {c})"
    return text


def generate_diet_for_user(user_id, amplitude_instance=None):
    """
    Генерирует диету + обоснование, сохраняет в БД и добавляет в контекст чата.
    """
    user = User.query.get(user_id)
    if not user:
        return {"error": "User not found", "code": 404}

    # 1. Сбор данных
    context = get_full_user_context(user_id)
    profile = context['profile']
    metrics = context['metrics']

    name = profile['name'] or "Пользователь"
    current_weight = metrics['weight'] or "неизвестен"
    goal_weight = profile['goal_weight'] or "не указан"
    bmr = metrics['metabolism'] or 1600

    # Расчет TDEE
    activity_factor = 1.2
    if context['activity']['avg_weekly_steps'] > 10000:
        activity_factor = 1.55
    elif context['activity']['avg_weekly_steps'] > 5000:
        activity_factor = 1.375
    tdee = int(bmr * activity_factor)

    # Цель
    goal_instruction = "поддержание веса"
    if user.fat_mass_goal:
        goal_instruction = "потеря жира (дефицит калорий, высокий белок)"
    elif user.muscle_mass_goal:
        goal_instruction = "набор мышечной массы (профицит калорий)"

    # 2. Промпт
    prompt = f"""
    Роль: Ты — профессиональный спортивный диетолог Kilo.
    Клиент: {name}.
    Параметры: Вес {current_weight}кг, BMR {bmr}, Расход (TDEE) ~{tdee} ккал.
    Цель: {goal_instruction}. Желаемый вес: {goal_weight}кг.

    ЗАДАЧА:
    1. Рассчитай КБЖУ под цель.
    2. Составь КОНКРЕТНЫЙ рацион на 1 день.

    ВАЖНО:
    - ЗАПРЕЩЕНО писать "Блюдо", "Dish". Пиши реальные названия (напр. "Омлет с помидорами").
    - ЗАПРЕЩЕНО писать вес "0г". Вес должен быть реальным.
    - Сумма калорий блюд должна совпадать с total_kcal.

    3. Напиши ОБОСНОВАНИЕ (justification). 
       Обращайся к пользователю на "Вы" или по имени.
       Используй первое лицо ("Я составил", "Я рекомендую").
       Объясни, почему выбраны такие калории и БЖУ.

    Верни JSON:
    {{
        "justification": "Текст обоснования...",
        "diet_plan": {{
            "breakfast": [{{"name": "...", "grams": 200, "kcal": 300, "recipe": "..."}}],
            "lunch": [...],
            "dinner": [...],
            "snack": [...],
            "total_kcal": 0,
            "protein": 0,
            "fat": 0,
            "carbs": 0
        }}
    }}
    """

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Ты диетолог Kilo. Отвечай только валидным JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=DIET_TEMPERATURE,
            max_tokens=2500,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content.strip()
        data = json.loads(content)

        diet_plan = data.get("diet_plan")
        justification = data.get("justification", f"Рацион составлен для цели: {goal_instruction}")

        if not diet_plan or diet_plan.get('total_kcal', 0) < 500:
            return {"error": "Сгенерирован некорректный план (слишком мало калорий или пустой).", "code": 500}

        # 3. Сохранение в БД
        Diet.query.filter_by(user_id=user.id, date=date.today()).delete()

        new_diet = Diet(
            user_id=user.id,
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

        # 4. Формируем текстовое сообщение для истории чата
        menu_text = format_diet_string(diet_plan)
        final_message_text = f"{justification}\n{menu_text}"

        # Сохраняем в сессию (чтобы чат помнил контекст)
        if 'chat_history' in session:
            chat_history = session['chat_history']
            chat_history.append({"role": "assistant", "content": final_message_text})
            session['chat_history'] = chat_history[-15:]

        # 5. Уведомление
        send_user_notification(
            user_id=user.id,
            title="🍽️ План питания готов!",
            body=f"Калории: {diet_plan.get('total_kcal')}. {justification[:40]}...",
            type='success',
            data={"route": "/diet"}
        )

        # 6. Аналитика
        if amplitude_instance:
            try:
                amplitude_instance.track(BaseEvent(
                    event_type="Diet Generated AI",
                    user_id=str(user.id),
                    event_properties={
                        "calories": diet_plan.get('total_kcal'),
                        "goal": goal_instruction
                    }
                ))
            except Exception as e:
                print(f"Amplitude error: {e}")

        # Возвращаем полный текст ответа для использования в API
        return {"success": True, "justification": justification, "full_text": final_message_text}

    except Exception as e:
        logger.exception("Error in generate_diet_for_user")
        return {"error": str(e), "code": 500}


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
    chat_history = chat_history[-15:]  # Храним последние 15

    # 1. КЛАССИФИКАЦИЯ
    CLASSIFICATION_PROMPT = """
    Определи намерение пользователя:
    1. 'Генерация' - если просит НОВЫЙ рацион с нуля ("составь диету", "хочу есть").
    2. 'Диета' - если хочет изменить ТЕКУЩУЮ диету ("убери рыбу", "что на ужин?") или обсуждает её.
    3. 'Показатели' - анализ веса, жира, прогресса.
    4. 'Общее' - остальное.
    """
    msgs_classify = [{"role": "system", "content": CLASSIFICATION_PROMPT}] + chat_history[-1:]
    classifier_text = _call_openai(msgs_classify, temperature=0.3, max_tokens=20) or "Общее"

    user_context = get_full_user_context(user_id)

    # Пытаемся получить текущую диету для контекста в любом случае
    current_diet_obj = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
    current_diet_json = _format_diet_summary(current_diet_obj) if current_diet_obj else "Нет данных"

    # =================================================================================
    # СЦЕНАРИЙ 1: ГЕНЕРАЦИЯ ДИЕТЫ (С НУЛЯ)
    # =================================================================================
    if "Генерация" in classifier_text or "Generat" in classifier_text:
        # Используем единую функцию генерации!
        result = generate_diet_for_user(user_id)

        if result.get("success"):
            final_text = result.get("full_text")
            # Примечание: generate_diet_for_user уже добавила ответ в session['chat_history']
            return jsonify({"role": "ai", "content": final_text}), 200
        else:
            return jsonify({"role": "ai", "content": f"Ошибка генерации: {result.get('error')}"}), 200

    # =================================================================================
    # СЦЕНАРИЙ 2: РАБОТА С ТЕКУЩЕЙ ДИЕТОЙ (Вопросы или Правки)
    # =================================================================================
    elif "Диета" in classifier_text:
        if not current_diet_obj:
            return jsonify({"role": "ai", "content": "У вас еще нет активной диеты. Напишите 'Составь рацион'!"}), 200

        mod_system_prompt = f"""
        Ты — Kilo, диетолог. 
        ТЫ составил этот рацион для пользователя: {current_diet_json}.

        Твоя задача: Отвечать на вопросы по этому рациону или менять его.
        Никогда не говори "в предоставленном рационе", говори "в твоем рационе".

        Запрос: "{user_message}"

        Верни JSON СТРОГО одного из двух типов:

        ТИП 1 (Вопрос/Уточнение): "что на ужин?", "почему столько белка?".
        {{ "action": "answer", "text": "Твой ответ от первого лица..." }}

        ТИП 2 (Изменение): "не нравится", "убери рыбу", "хочу другое".
        {{ 
           "action": "update", 
           "text": "Комментарий ('Хорошо, я заменил рыбу на курицу...').", 
           "diet_plan": {{ ...полностью новая структура с учетом правок... }}
        }}
        """

        messages = [{"role": "system", "content": mod_system_prompt}]
        response_json_str = _call_openai(messages, temperature=0.7, max_tokens=2000, json_mode=True)

        if response_json_str:
            try:
                resp_data = json.loads(response_json_str)
                action = resp_data.get("action")
                ai_text = resp_data.get("text", "Готово.")
                final_text = ai_text

                if action == "update":
                    new_plan = resp_data.get("diet_plan")
                    # Защита от string
                    if isinstance(new_plan, str):
                        try:
                            new_plan = json.loads(new_plan)
                        except:
                            new_plan = None

                    if new_plan and isinstance(new_plan, dict):
                        # Обновляем БД
                        current_diet_obj.breakfast = json.dumps(new_plan.get('breakfast', []), ensure_ascii=False)
                        current_diet_obj.lunch = json.dumps(new_plan.get('lunch', []), ensure_ascii=False)
                        current_diet_obj.dinner = json.dumps(new_plan.get('dinner', []), ensure_ascii=False)
                        current_diet_obj.snack = json.dumps(new_plan.get('snack', []), ensure_ascii=False)
                        current_diet_obj.total_kcal = new_plan.get('total_kcal')
                        current_diet_obj.protein = new_plan.get('protein')
                        current_diet_obj.fat = new_plan.get('fat')
                        current_diet_obj.carbs = new_plan.get('carbs')
                        db.session.commit()

                        menu_string = format_diet_string(new_plan)
                        final_text = f"{ai_text}\n{menu_string}"
                    else:
                        final_text = "Не удалось изменить план. Попробуйте переформулировать."

                chat_history.append({"role": "assistant", "content": final_text})
                session['chat_history'] = chat_history
                return jsonify({"role": "ai", "content": final_text}), 200

            except Exception as e:
                logger.error(f"Diet Modify Error: {e}")
                return jsonify({"role": "ai", "content": "Произошла ошибка при обработке запроса."}), 200
        else:
            return jsonify({"role": "ai", "content": "ИИ не ответил."}), 200

    # =================================================================================
    # СЦЕНАРИЙ 3: ПОКАЗАТЕЛИ
    # =================================================================================
    elif "Показатели" in classifier_text:
        current_ba = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()
        if not current_ba:
            return jsonify({"role": "ai", "content": "Нет данных анализа тела. Загрузите фото с весов!"}), 200

        ba_sum = _format_body_summary(current_ba)
        reply = _call_openai([
            {"role": "system",
             "content": "Ты фитнес-аналитик Kilo. Твоя задача — анализировать прогресс пользователя."},
            {"role": "user", "content": f"Мои данные: {ba_sum}. Вопрос: {user_message}"}
        ])
        chat_history.append({"role": "assistant", "content": reply})
        session['chat_history'] = chat_history
        return jsonify({"role": "ai", "content": reply}), 200

    # =================================================================================
    # СЦЕНАРИЙ 4: ОБЩИЙ ЧАТ
    # =================================================================================
    else:
        # ВАЖНО: Добавляем контекст диеты, чтобы он знал, что пользователь ест
        general_prompt = f"""
        Ты — Kilo, личный нутрициолог и тренер.
        Пользователь: {user_context['profile']['name']}.

        КОНТЕКСТ:
        Пользователь сейчас придерживается этого рациона (ТЫ его составил):
        {current_diet_json}

        Отвечай на вопросы пользователя, помогай ему придерживаться плана.
        Будь поддерживающим и мотивирующим.
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