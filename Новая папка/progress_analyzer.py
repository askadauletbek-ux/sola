# progress_analyzer.py

import os
import json
from openai import OpenAI
from datetime import date, timedelta, timezone  # Импортируем timezone
from sqlalchemy import func
# Убедитесь, что модели импортируются корректно относительно структуры вашего проекта
from models import MealLog, Activity, BodyAnalysis, User
from extensions import db

# Инициализация клиента OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def calculate_age(born):
    """Helper function to calculate age. More robust now."""
    if not born:
        return 'не указан'
    today = date.today()
    try:
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    except AttributeError:
        # Fallback if 'born' is not a date object
        return 'не указан'


def get_period_description(hours):
    """Возвращает человекочитаемое описание периода."""
    if hours <= 1:
        return "за последний час"
    if hours < 24:
        return f"за последние {max(1, round(hours))} часов"
    days = round(hours / 24)
    if days == 1:
        return "за прошедший день"
    else:
        return f"за последние {days} дней"


def generate_progress_commentary(user: User, previous_analysis: BodyAnalysis, latest_analysis: BodyAnalysis):
    """
    Генерирует комментарий ИИ на основе прогресса пользователя между двумя замерами.
    """
    print(f"DEBUG: Вход в функцию generate_progress_commentary для пользователя ID={user.id}")

    if not previous_analysis or not latest_analysis:
        print("DEBUG: Ошибка - отсутствует один из анализов.")
        return None

    # --- НАЧАЛО ИСПРАВЛЕНИЯ: Приведение времени к aware-формату ---
    start_ts = previous_analysis.timestamp
    end_ts = latest_analysis.timestamp

    # Если у объекта datetime нет информации о таймзоне, добавляем UTC
    if start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    if end_ts.tzinfo is None:
        end_ts = end_ts.replace(tzinfo=timezone.utc)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    if end_ts <= start_ts:
        print(f"DEBUG: Ошибка - новый замер ({end_ts}) не позже предыдущего ({start_ts}).")
        return "Новый замер должен быть сделан после предыдущего, чтобы проанализировать прогресс."

    # 1. Сбор данных между замерами
    # Теперь сравнение будет работать корректно
    meal_logs = MealLog.query.filter(
        MealLog.user_id == user.id,
        MealLog.created_at.between(start_ts, end_ts)
    ).all()

    activity_logs = Activity.query.filter(
        Activity.user_id == user.id,
        Activity.date.between(start_ts.date(), end_ts.date())
    ).all()
    print(f"DEBUG: Найдено {len(meal_logs)} приемов пищи и {len(activity_logs)} записей активности.")

    # 2. Агрегация и подготовка данных для ИИ
    total_hours = (end_ts - start_ts).total_seconds() / 3600
    period_description = get_period_description(total_hours)

    total_calories = sum(log.calories for log in meal_logs)
    avg_daily_calories = (total_calories / total_hours * 24) if total_hours > 0 else 0

    activity_by_date = {log.date: log for log in activity_logs}
    total_steps = sum(log.steps or 0 for log in activity_by_date.values())
    total_active_kcal = sum(log.active_kcal or 0 for log in activity_by_date.values())
    num_activity_days = len(activity_by_date)

    avg_daily_steps = (total_steps / num_activity_days) if num_activity_days > 0 else 0
    avg_daily_active_kcal = (total_active_kcal / num_activity_days) if num_activity_days > 0 else 0

    # 3. Формирование JSON-объекта для промпта
    data_for_ai = {
        "user_info": {
            "name": user.name,
            "sex": user.sex,
            "age": calculate_age(user.date_of_birth)
        },
        "period": {
            "description": period_description,
            "start_time": start_ts.isoformat(),
            "end_time": end_ts.isoformat(),
        },
        "previous_metrics": {
            "weight_kg": previous_analysis.weight,
            "fat_mass_kg": previous_analysis.fat_mass,
            "muscle_mass_kg": previous_analysis.muscle_mass,
        },
        "latest_metrics": {
            "weight_kg": latest_analysis.weight,
            "fat_mass_kg": latest_analysis.fat_mass,
            "muscle_mass_kg": latest_analysis.muscle_mass,
        },
        "nutrition_summary_for_period": {
            "total_calories_consumed": round(total_calories, 0),
            "equivalent_average_daily_calories": round(avg_daily_calories, 0),
            "meal_count": len(meal_logs)
        },
        "activity_summary_for_period": {
            "average_daily_steps": round(avg_daily_steps, 0),
            "average_daily_active_kcal": round(avg_daily_active_kcal, 0)
        }
    }

    # 4. Создание промпта для ИИ
    prompt = f"""
Ты — эмпатичный и профессиональный фитнес-тренер и диетолог. Проанализируй данные о прогрессе пользователя по имени {user.name} ({period_description}).

Вот данные в формате JSON:
```json
{json.dumps(data_for_ai, indent=2, ensure_ascii=False)}
```

Твоя задача — дать развернутый, но легко читаемый комментарий (2-4 абзаца).

1.  **Сравни ключевые показатели**: вес, жировую и мышечную массу. Обязательно отметь изменения в кг (например, "жировая масса уменьшилась на 1.2 кг").
2.  **Проанализируй причину**: Свяжи изменения с данными по питанию и активности ЗА УКАЗАННЫЙ ПЕРИОД.
    * **Если период короткий (менее суток)**, сфокусируйся на приемах пищи. Например: "За эти несколько часов вы съели X калорий, что соответствует вашему плану".
    * **Если прогресс положительный** (жир ушел, мышцы выросли/сохранились): Похвали пользователя! Укажи, что его питание и активность способствовали результату.
    * **Если прогресс отрицательный** (жир прибавился, мышцы ушли): Будь деликатен. Предположи возможную причину. Например: "Похоже, калорийность рациона за этот период была немного выше, чем нужно для жиросжигания" или "Возможно, стоит добавить немного активности". Дай конкретный, мягкий совет.
    * **Если результаты смешанные** (например, ушел и жир, и мышцы): Объясни, почему так могло произойти (например, слишком большой дефицит калорий или недостаток белка) и дай совет.
3.  **Заверши комментарий** мотивирующей и поддерживающей фразой.

Ответ должен быть только в виде текста комментария, без лишних фраз вроде "Вот ваш комментарий".
"""
    print("DEBUG: Отправляю промпт в OpenAI...")
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты — эмпатичный и профессиональный фитнес-тренер и диетолог."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1000,
            temperature=0.7
        )
        comment = response.choices[0].message.content.strip()
        print("DEBUG: Ответ от OpenAI получен.")
        return comment
    except Exception as e:
        print(f"ОШИБКА при генерации комментария ИИ: {e}")
        return "Не удалось сгенерировать комментарий. Пожалуйста, попробуйте позже."

