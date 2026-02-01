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
    # Добавляем MealLog, если нужно для проверок, но пока хватит этих
    from models import User, Diet, BodyAnalysis, Activity, db
    from notification_service import send_user_notification # Импорт для уведомлений
    from amplitude import BaseEvent # Для аналитики (если amplitude инициализирована глобально, передадим из app, но здесь импортируем класс)
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

    # Определяем параметры для промпта
    name = profile['name'] or "Пользователь"
    current_weight = metrics['weight'] or "неизвестен"
    goal_weight = profile['goal_weight'] or "не указан"
    bmr = metrics['metabolism'] or 1600
    # Простая эвристика для TDEE (расход калорий)
    activity_factor = 1.2  # Sedentary
    if context['activity']['avg_weekly_steps'] > 10000:
        activity_factor = 1.55
    elif context['activity']['avg_weekly_steps'] > 5000:
        activity_factor = 1.375

    tdee = int(bmr * activity_factor)

    # Формируем цель для ИИ
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
    1. Рассчитай целевые КБЖУ для этой цели.
    2. Составь рацион на 1 день (завтрак, обед, ужин, перекус).
    3. Напиши ОБОСНОВАНИЕ для клиента (обращайся по имени), почему ты выбрал именно такие цифры. 
       Пример: "{name}, я составил рацион на 1800 ккал. Это дефицит 15% от твоей нормы, что обеспечит сжигание жира. Белка 160г, чтобы сохранить мышцы..."

    Верни JSON строго по формату:
    {{
        "justification": "Текст обоснования...",
        "diet_plan": {{
            "breakfast": [{{"name": "...", "grams": 0, "kcal": 0, "recipe": "..."}}],
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
                {"role": "system", "content": "Ты диетолог. Отвечай только валидным JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=DIET_TEMPERATURE,
            max_tokens=2000,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content.strip()
        data = json.loads(content)

        diet_plan = data.get("diet_plan")
        justification = data.get("justification", f"Рацион составлен для цели: {goal_instruction}")

        if not diet_plan:
            return {"error": "AI generation failed", "code": 500}

        # 3. Сохранение в БД
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

        # 4. Обновление контекста чата (Session)
        # Добавляем "системное" сообщение в историю, будто ассистент только что это сказал
        # Это позволяет пользователю сразу спросить "А чем заменить обед?"
        chat_history = session.get('chat_history', [])

        # Сохраняем краткое саммари для контекста
        diet_context_msg = {
            "role": "assistant",
            "content": f"{justification}\n(Сгенерирован рацион: {diet_plan.get('total_kcal')} ккал, БЖУ: {diet_plan.get('protein')}/{diet_plan.get('fat')}/{diet_plan.get('carbs')})"
        }
        chat_history.append(diet_context_msg)
        session['chat_history'] = chat_history[-15:]  # Ограничиваем историю

        # 5. Уведомление
        send_user_notification(
            user_id=user.id,
            title="🍽️ Индивидуальный план готов!",
            body=f"Калории: {diet_plan.get('total_kcal')}. {justification[:50]}...",
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

        return {"success": True, "justification": justification}

    except Exception as e:
        logger.exception("Error in generate_diet_for_user")
        return {"error": str(e), "code": 500}

def calculate_age(born):
    if not born: return "Не указан"
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def get_full_user_context(user_id):
    """
    Собирает ПОЛНЫЙ портрет пользователя для ИИ.
    """
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


def format_diet_string(diet_plan):
    """Превращает JSON диеты в красивый текст для чата."""
    if not diet_plan or not isinstance(diet_plan, dict): return ""

    text = "\n\n🍽 **Твой план питания:**\n"

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

    # 1. КЛАССИФИКАЦИЯ
    CLASSIFICATION_PROMPT = """
    Определи намерение:
    1. 'Генерация' - если просит НОВЫЙ рацион с нуля.
    2. 'Диета' - если хочет изменить ТЕКУЩУЮ диету или спрашивает о ней.
    3. 'Показатели' - анализ веса/прогресса.
    4. 'Общее' - остальное.
    """
    msgs_classify = [{"role": "system", "content": CLASSIFICATION_PROMPT}] + chat_history[-1:]
    classifier_text = _call_openai(msgs_classify, temperature=0.3, max_tokens=20) or "Общее"

    user_context = get_full_user_context(user_id)
    user_name = user_context['profile']['name'] or "Пользователь"
    user_gender = user_context['profile']['gender']

    # =================================================================================
    # СЦЕНАРИЙ 1: ГЕНЕРАЦИЯ ДИЕТЫ (С НУЛЯ)
    # =================================================================================
    if "Генерация" in classifier_text or "Generat" in classifier_text:

        gen_system_prompt = f"""
        Ты — Kilo, нутрициолог. Задача: Составить рацион.

        ПОЛЬЗОВАТЕЛЬ: {user_name}, Пол: {user_gender}.
        Данные: {json.dumps(user_context, ensure_ascii=False)}

        ПРАВИЛА:
        1. Если ЦЕЛЬ (goal_weight) НЕ ЯСНА -> Спроси пользователя в поле 'chat_message'. 'diet_plan' = null.
        2. Если ЦЕЛЬ ЕСТЬ -> Генерируй рацион. 
           - В 'chat_message' напиши ТОЛЬКО мотивирующее вступление (2-3 предложения). НЕ ПИШИ СПИСОК БЛЮД СЮДА.
           - В 'diet_plan' положи полный JSON.

        ФОРМАТ (JSON):
        {{
            "chat_message": "Только мотивация и вступление...",
            "diet_plan": {{ "breakfast": [...], "lunch": [...], "dinner": [...], "snack": [...], "total_kcal": 0, "protein": 0, "fat": 0, "carbs": 0 }} ИЛИ null
        }}
        """

        messages = [{"role": "system", "content": gen_system_prompt}] + chat_history

        response_json_str = _call_openai(messages, temperature=DIET_TEMPERATURE, max_tokens=2000, json_mode=True)

        if response_json_str:
            try:
                resp_data = json.loads(response_json_str)

                # ЗАЩИТА: Проверяем, что resp_data это словарь
                if not isinstance(resp_data, dict):
                    raise ValueError("OpenAI returned non-dict JSON")

                ai_intro = resp_data.get('chat_message', 'Готово!')
                diet_plan = resp_data.get('diet_plan')

                final_text = ai_intro

                # ЗАЩИТА: Если diet_plan пришел строкой (бывает у LLM), парсим его
                if isinstance(diet_plan, str):
                    try:
                        diet_plan = json.loads(diet_plan)
                    except:
                        diet_plan = None

                # Если план сгенерирован (и это словарь) -> сохраняем и ФОРМИРУЕМ ТЕКСТ
                if diet_plan and isinstance(diet_plan, dict):
                    # 1. Сохраняем в БД
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

                    # 2. Добавляем красивый текст меню к ответу
                    menu_string = format_diet_string(diet_plan)
                    final_text = f"{ai_intro}\n{menu_string}"

                chat_history.append({"role": "assistant", "content": final_text})
                session['chat_history'] = chat_history
                return jsonify({"role": "ai", "content": final_text}), 200

            except Exception as e:
                logger.error(f"Diet Gen Error: {e}")
                # Возвращаем дружелюбную ошибку, не роняя сервер
                return jsonify({"role": "ai",
                                "content": "Произошла техническая заминка при создании меню. Попробуйте спросить еще раз!"}), 200

    # =================================================================================
    # СЦЕНАРИЙ 2: РАБОТА С ТЕКУЩЕЙ ДИЕТОЙ
    # =================================================================================
    elif "Диета" in classifier_text:
        current_diet = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
        if not current_diet:
            return jsonify({"role": "ai", "content": "У вас еще нет активной диеты. Напишите 'Составь рацион'!"}), 200

        diet_json = _format_diet_summary(current_diet)

        mod_system_prompt = f"""
        Ты — Kilo. Текущий рацион (JSON): {diet_json}
        Запрос: "{user_message}"

        Верни JSON СТРОГО одного из двух типов:

        ТИП 1 (Вопрос): "что на ужин?", "сколько белка?".
        {{ "action": "answer", "text": "Твой ответ..." }}

        ТИП 2 (Изменение): "не нравится", "хочу другое", "убери рыбу".
        {{ 
           "action": "update", 
           "text": "Комментарий ('Заменил меню'). НЕ пиши сюда список блюд.", 
           "diet_plan": {{ ...полностью новая структура... }}
        }}
        ВАЖНО: Если "не нравится" без деталей -> предложи ПОЛНОСТЬЮ НОВЫЙ сбалансированный вариант.
        """

        messages = [{"role": "system", "content": mod_system_prompt}]
        response_json_str = _call_openai(messages, temperature=0.7, max_tokens=2000, json_mode=True)

        if response_json_str:
            try:
                resp_data = json.loads(response_json_str)
                if not isinstance(resp_data, dict): raise ValueError("Not a dict")

                action = resp_data.get("action")
                ai_text = resp_data.get("text", "Готово.")

                final_text = ai_text

                if action == "answer":
                    pass  # Просто текст

                elif action == "update":
                    new_plan = resp_data.get("diet_plan")

                    # ЗАЩИТА от строкового diet_plan
                    if isinstance(new_plan, str):
                        try:
                            new_plan = json.loads(new_plan)
                        except:
                            new_plan = None

                    if new_plan and isinstance(new_plan, dict):
                        # Обновляем БД
                        current_diet.breakfast = json.dumps(new_plan.get('breakfast', []), ensure_ascii=False)
                        current_diet.lunch = json.dumps(new_plan.get('lunch', []), ensure_ascii=False)
                        current_diet.dinner = json.dumps(new_plan.get('dinner', []), ensure_ascii=False)
                        current_diet.snack = json.dumps(new_plan.get('snack', []), ensure_ascii=False)
                        current_diet.total_kcal = new_plan.get('total_kcal')
                        current_diet.protein = new_plan.get('protein')
                        current_diet.fat = new_plan.get('fat')
                        current_diet.carbs = new_plan.get('carbs')
                        db.session.commit()

                        # Формируем красивый вывод
                        menu_string = format_diet_string(new_plan)
                        final_text = f"{ai_text}\n{menu_string}"
                    else:
                        final_text = "Не удалось перестроить план. Попробуйте еще раз."

                chat_history.append({"role": "assistant", "content": final_text})
                session['chat_history'] = chat_history
                return jsonify({"role": "ai", "content": final_text}), 200

            except Exception as e:
                logger.error(f"Diet Modify Error: {e}")
                return jsonify({"role": "ai", "content": "Ошибка при изменении диеты. Попробуйте еще раз."}), 200
        else:
            return jsonify({"role": "ai", "content": "ИИ не ответил."}), 200

        # =================================================================================
        # СЦЕНАРИЙ 3: ПОКАЗАТЕЛИ
        # =================================================================================
    elif "Показатели" in classifier_text:
        # ИСПРАВЛЕНИЕ: используем user_id, так как объект user здесь не определен
        current_ba = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()
        if not current_ba:
            return jsonify({"role": "ai", "content": "Нет данных анализа тела. Загрузите фото с весов!"}), 200
        ba_sum = _format_body_summary(current_ba)
        reply = _call_openai([
            {"role": "system", "content": "Ты фитнес-аналитик. Дай совет."},
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