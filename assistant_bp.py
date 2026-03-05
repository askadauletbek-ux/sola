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
    from models import User, Diet, BodyAnalysis, Activity, db, WeightLog
    from notification_service import send_user_notification
    from amplitude import BaseEvent
except Exception as _e:
    User = None
    Diet = None
    BodyAnalysis = None
    Activity = None
    db = None
    WeightLog = None
    logger.warning("Не удалось импортировать модели.")


# ------------------------------------------------------------------
# Хелперы
# ------------------------------------------------------------------

def calculate_age(born):
    if not born: return None
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


def generate_diet_for_user(user_id, amplitude_instance=None, force_basic=False):
    """
    Генерирует диету + обоснование.
    force_basic=True -> генерировать даже если нет точных данных (использовать дефолты).
    """
    user = User.query.get(user_id)
    if not user:
        return {"error": "User not found", "code": 404}

    # 1. Сбор данных
    context = get_full_user_context(user_id)
    profile = context['profile']
    metrics = context['metrics']
    activity = context['activity']

    # Пытаемся найти вес
    weight = metrics.get('weight')
    if not weight:
        last_log = WeightLog.query.filter_by(user_id=user.id).order_by(WeightLog.date.desc()).first()
        if last_log:
            weight = last_log.weight
    if not weight:
        weight = profile.get('start_weight')

    height = metrics.get('height')
    age = profile.get('age')
    gender = profile.get('gender', 'unknown')

    missing_data = []
    if not weight: missing_data.append("вес")
    if not height: missing_data.append("рост")
    if not age: missing_data.append("возраст")

    if missing_data and not force_basic:
        missing_str = ", ".join(missing_data)
        msg = (
            f"Для точного расчета мне не хватает данных: {missing_str}.\n"
            "Пожалуйста, загрузи фото с умных весов или заполни профиль.\n\n"
            "Если хочешь, я могу составить **базовый рацион** на основе усредненных показателей. "
            "Просто напиши: **«Составь базовую диету»**."
        )
        chat_history = session.get('chat_history', [])
        chat_history.append({
            "role": "assistant",
            "content": msg,
            "type": "require_data",
            "actions": [{"label": "📸 Загрузить замеры", "route": "/weight"}]
        })
        session['chat_history'] = chat_history[-15:]

        return {
            "success": False,
            "require_data": True,
            "full_text": msg,
            "type": "require_data",
            "actions": [{"label": "📸 Загрузить замеры", "route": "/weight"}]
        }

    is_estimation = False
    if not weight:
        weight = 70.0 if gender != 'female' else 60.0
        is_estimation = True
    else:
        weight = float(weight)

    if not height:
        height = 175.0 if gender != 'female' else 165.0
        is_estimation = True
    else:
        height = float(height)

    if not age:
        age = 30
        is_estimation = True

    bmr = metrics.get('metabolism')
    if not bmr:
        if gender == 'female':
            bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
        else:
            bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5

    avg_steps = activity.get('avg_weekly_steps', 0)
    activity_factor = 1.2
    if avg_steps > 12000:
        activity_factor = 1.55
    elif avg_steps > 7000:
        activity_factor = 1.375

    tdee = int(bmr * activity_factor)

    goal_type = "maintain"
    target_calories = tdee

    if user.fat_mass_goal:
        goal_type = "lose_fat"
        target_calories = int(tdee * 0.85)
        if target_calories < bmr: target_calories = int(bmr)
    elif user.muscle_mass_goal:
        goal_type = "gain_muscle"
        target_calories = int(tdee * 1.10)

    goal_desc_map = {
        "lose_fat": f"Сжигание жира. Дефицит калорий (Цель: {target_calories} ккал). Высокий белок.",
        "gain_muscle": f"Набор мышечной массы. Профицит калорий (Цель: {target_calories} ккал).",
        "maintain": f"Поддержание веса и тонуса (Цель: {target_calories} ккал)."
    }
    goal_instruction = goal_desc_map.get(goal_type)

    estimation_note = ""
    if is_estimation:
        estimation_note = (
            "ВНИМАНИЕ: У пользователя нет точных данных. Использованы средние значения. "
            "В обосновании ОБЯЗАТЕЛЬНО укажи, что это базовый рацион."
        )

    prompt = f"""
    Роль: Ты — профессиональный спортивный диетолог Kilo.
    Клиент: {profile['name']}.
    Параметры: Вес {weight}кг, Рост {height}см, Возраст {age}.
    Расчеты: BMR {int(bmr)}, TDEE {tdee}.
    {estimation_note}

    ГЛАВНАЯ ЦЕЛЬ: {goal_instruction}

    ЗАДАЧА:
    Составь подробный рацион на 1 день, строго попадая в {target_calories} ккал (+/- 50 ккал).

    СТРОГИЕ ПРАВИЛА:
    1. ЗАПРЕЩЕНО писать "Блюдо", "Dish", "Еда". Пиши конкретные названия.
    2. ЗАПРЕЩЕНО указывать вес "0г".
    3. Сумма калорий ВСЕХ блюд должна быть равна {target_calories}.

    СТРУКТУРА ОТВЕТА (JSON):
    {{
        "justification": "Обращение к клиенту по имени. Объясни выбор калорийности.",
        "diet_plan": {{
            "breakfast": [
                {{"name": "Овсянка", "grams": 250, "kcal": 300, "recipe": "..."}}
            ],
            "lunch": [ ... ],
            "dinner": [ ... ],
            "snack": [ ... ],
            "total_kcal": {target_calories},
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
                {"role": "system", "content": "Ты диетолог. Отвечай только валидным JSON."},
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
            return {"error": "Сгенерирован некорректный план.", "code": 500}

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

        menu_text = format_diet_string(diet_plan)
        final_message_text = f"{justification}\n{menu_text}"

        chat_history = session.get('chat_history', [])
        chat_history.append({
            "role": "assistant",
            "content": final_message_text,
            "type": "diet_generated",
            "payload": {
                "total_kcal": diet_plan.get('total_kcal'),
                "protein": diet_plan.get('protein'),
                "fat": diet_plan.get('fat'),
                "carbs": diet_plan.get('carbs')
            },
            "actions": [{"label": "🍽 Смотреть меню", "route": "/meals"}]
        })
        session['chat_history'] = chat_history[-15:]

        send_user_notification(
            user_id=user.id,
            title="🍽️ План питания готов!",
            body=f"Калории: {diet_plan.get('total_kcal')}.",
            type='success',
            data={"route": "/diet"}
        )

        if amplitude_instance:
            try:
                amplitude_instance.track(BaseEvent(
                    event_type="Diet Generated AI",
                    user_id=str(user.id),
                    event_properties={"calories": diet_plan.get('total_kcal'), "is_basic": is_estimation}
                ))
            except Exception:
                pass

        return {
            "success": True,
            "justification": justification,
            "full_text": final_message_text,
            "type": "diet_generated",
            "payload": {
                "total_kcal": diet_plan.get('total_kcal'),
                "protein": diet_plan.get('protein'),
                "fat": diet_plan.get('fat'),
                "carbs": diet_plan.get('carbs')
            },
            "actions": [{"label": "🍽 Смотреть меню", "route": "/meals"}]
        }

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
    chat_history = chat_history[-15:]

    # Очищаем историю от кастомных ключей перед отправкой в OpenAI
    clean_history = [{"role": m["role"], "content": m.get("content", "")} for m in chat_history]

    # 1. КЛАССИФИКАЦИЯ
    CLASSIFICATION_PROMPT = """
    Определи намерение пользователя:
    1. 'Генерация' - если просит НОВЫЙ рацион с нуля ("составь диету", "хочу есть").
    2. 'Диета' - если хочет изменить ТЕКУЩУЮ диету ("убери рыбу", "что на ужин?") или обсуждает её.
    3. 'Показатели' - анализ веса, жира, прогресса.
    4. 'Сканер' - если хочет отсканировать еду, загрузить прием пищи.
    5. 'Общее' - остальное.
    """
    msgs_classify = [{"role": "system", "content": CLASSIFICATION_PROMPT}] + clean_history[-1:]
    classifier_text = _call_openai(msgs_classify, temperature=0.3, max_tokens=20) or "Общее"

    user_context = get_full_user_context(user_id)
    current_diet_obj = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
    current_diet_json = _format_diet_summary(current_diet_obj) if current_diet_obj else "Нет данных"

    # =================================================================================
    # СЦЕНАРИЙ 1: ГЕНЕРАЦИЯ ДИЕТЫ (С НУЛЯ)
    # =================================================================================
    if "Генерация" in classifier_text or "Generat" in classifier_text:
        msg_lower = user_message.lower()
        force_basic = any(kw in msg_lower for kw in ["базов", "прост", "все равно", "basic", "любую", "без весов"])
        result = generate_diet_for_user(user_id, force_basic=force_basic)

        if result.get("success") or result.get("require_data"):
            return jsonify({
                "role": "ai",
                "content": result.get("full_text"),
                "type": result.get("type"),
                "payload": result.get("payload"),
                "actions": result.get("actions")
            }), 200
        else:
            return jsonify({"role": "ai", "content": f"Ошибка генерации: {result.get('error')}"}), 200

    # =================================================================================
    # СЦЕНАРИЙ 2: РАБОТА С ТЕКУЩЕЙ ДИЕТОЙ
    # =================================================================================
    elif "Диета" in classifier_text:
        if not current_diet_obj:
            return jsonify({"role": "ai",
                            "content": "У вас еще нет активной диеты. Напишите 'Составь рацион', чтобы начать!"}), 200

        mod_system_prompt = f"""
        Ты — Kilo, диетолог. ТЫ составил этот рацион: {current_diet_json}.
        Твоя задача: Отвечать на вопросы по рациону или менять его.
        Запрос: "{user_message}"

        Верни JSON СТРОГО:
        ТИП 1 (Вопрос): {{"action": "answer", "text": "ответ..."}}
        ТИП 2 (Изменение): {{"action": "update", "text": "Комментарий...", "diet_plan": {{ ...новая структура... }} }}
        """
        response_json_str = _call_openai([{"role": "system", "content": mod_system_prompt}], temperature=0.7,
                                         max_tokens=2000, json_mode=True)

        if response_json_str:
            try:
                resp_data = json.loads(response_json_str)
                action = resp_data.get("action")
                ai_text = resp_data.get("text", "Готово.")
                final_text = ai_text

                if action == "update":
                    new_plan = resp_data.get("diet_plan")
                    if isinstance(new_plan, str):
                        try:
                            new_plan = json.loads(new_plan)
                        except:
                            new_plan = None

                    if new_plan and isinstance(new_plan, dict):
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
    # СЦЕНАРИЙ 3: СКАНЕР ЕДЫ
    # =================================================================================
    elif "Сканер" in classifier_text:
        reply_text = "Отличная идея! Давай запишем, что ты съел. Открываю сканер еды..."
        ai_msg = {
            "role": "ai",
            "content": reply_text,
            "type": "scan_food",
            "actions": [{"label": "📸 Открыть сканер", "route": "/scan"}]
        }
        chat_history.append(ai_msg)
        session['chat_history'] = chat_history
        return jsonify(ai_msg), 200

    # =================================================================================
    # СЦЕНАРИЙ 4: ПОКАЗАТЕЛИ (Точно как на главной)
    # =================================================================================
    elif "Показатели" in classifier_text:
        user_obj = User.query.get(user_id)
        current_ba = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()

        start_w = user_obj.start_weight
        goal_w = user_obj.weight_goal

        # 1. Точный алгоритм получения текущего веса (как в app.py)
        last_log = WeightLog.query.filter_by(user_id=user_id).order_by(WeightLog.date.desc(),
                                                                       WeightLog.created_at.desc()).first()
        if last_log:
            curr_w = last_log.weight
        elif current_ba and current_ba.weight:
            curr_w = current_ba.weight
        else:
            curr_w = start_w

        if not curr_w and not current_ba:
            ai_msg = {
                "role": "ai",
                "content": "У меня пока нет данных твоих замеров. Пожалуйста, зафиксируй вес или загрузи фото с весов!",
                "type": "require_data",
                "actions": [{"label": "📸 Загрузить замеры", "route": "/weight"}]
            }
            chat_history.append(ai_msg)
            session['chat_history'] = chat_history
            return jsonify(ai_msg), 200

        # Формируем строку для ИИ
        ba_sum = f"Текущий вес: {curr_w} кг."
        if current_ba and current_ba.fat_mass:
            ba_sum += f" Жир: {current_ba.fat_mass} кг."

        reply = _call_openai([
            {"role": "system",
             "content": "Ты фитнес-аналитик Kilo. Отвечай МАКСИМАЛЬНО коротко (1 предложение). Никакой воды, не перечисляй цифры, они будут на графике ниже. Просто коротко оцени динамику веса от старта к цели и подбодри."},
            {"role": "user",
             "content": f"Старт: {start_w} кг. Цель: {goal_w} кг. Сейчас: {ba_sum}. Вопрос: {user_message}"}
        ])

        payload = {
            "weight": curr_w,
            "fat": current_ba.fat_mass if current_ba else None,
            "muscle": current_ba.muscle_mass if current_ba else None,
            "start_weight": start_w,
            "goal_weight": goal_w
        }

        ai_msg = {
            "role": "ai",
            "content": reply,
            "type": "metrics_summary",
            "payload": payload
        }

        chat_history.append(ai_msg)
        session['chat_history'] = chat_history
        return jsonify(ai_msg), 200

    # =================================================================================
    # СЦЕНАРИЙ 5: ОБЩИЙ ЧАТ
    # =================================================================================
    else:
        general_prompt = f"""
        Ты — Kilo, личный нутрициолог и тренер.
        Пользователь: {user_context['profile']['name']}.

        КОНТЕКСТ:
        Пользователь сейчас придерживается этого рациона:
        {current_diet_json}

        Отвечай на вопросы пользователя. Будь поддерживающим и мотивирующим.
        """
        messages = [{"role": "system", "content": general_prompt}] + clean_history
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