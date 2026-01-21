import threading
import time
import os
from datetime import date, datetime, timedelta
from flask import Blueprint
from sqlalchemy import func
from extensions import db
from models import User, MealLog
from firebase_admin import messaging
import firebase_admin

streak_bp = Blueprint('streak_bp', __name__)


# --- ЧЕСТНЫЙ ПЕРЕСЧЕТ СТРИКА ---

from models import Activity  # Не забудьте добавить импорт Activity


def _calculate_consecutive_days(dates_list):
    """Вспомогательная функция: считает подряд идущие даты"""
    if not dates_list:
        return 0

    today = date.today()
    yesterday = today - timedelta(days=1)

    # Сортируем на всякий случай (но база должна выдавать сортированно)
    dates_list = sorted(list(set(dates_list)), reverse=True)

    latest_date = dates_list[0]

    # Если последняя активность была позавчера или раньше — стрик сгорел
    if latest_date < yesterday:
        return 0

    streak = 0
    # Если последняя запись сегодня — начинаем проверку с сегодня
    # Если последняя запись вчера — начинаем проверку со вчера (стрик еще жив)
    check_date = today if (latest_date == today) else yesterday

    for d in dates_list:
        if d == check_date:
            streak += 1
            check_date -= timedelta(days=1)
        elif d > check_date:
            # Дубликат или дата из будущего (игнорируем)
            continue
        else:
            # Разрыв цепочки
            break
    return streak


def recalculate_streak(user):
    """
    Рассчитывает 3 вида стриков:
    1. Nutrition: Дефицит калорий (Съедено <= Цель)
    2. Activity: Шаги >= Цели
    3. Total: И то, и другое
    """
    # --- 1. Питание (Даты, где соблюден дефицит) ---
    # Получаем цель пользователя (если не задана, берем дефолт 2000)
    daily_limit = getattr(user, 'daily_calories', 2000) or 2000

    # Группируем по дате, суммируем калории.
    # Условие: Сумма калорий > 0 (что-то ел) И Сумма калорий <= Лимита (дефицит)
    # Если переел (профицит), день не попадет в выборку, и стрик прервется.
    meal_rows = db.session.query(MealLog.date) \
        .filter_by(user_id=user.id) \
        .group_by(MealLog.date) \
        .having(func.sum(MealLog.calories) > 0) \
        .having(func.sum(MealLog.calories) <= daily_limit) \
        .order_by(MealLog.date.desc()) \
        .all()

    meal_dates = {row.date for row in meal_rows}  # Set для быстрого поиска

    # --- 2. Активность (Даты, где steps >= step_goal) ---
    goal = getattr(user, 'step_goal', 10000) or 10000

    activity_rows = db.session.query(Activity.date) \
        .filter(Activity.user_id == user.id, Activity.steps >= goal) \
        .order_by(Activity.date.desc()) \
        .all()

    activity_dates = {row.date for row in activity_rows}

    # --- 3. Общий (Пересечение дат) ---
    # Общий стрик будет только в те дни, когда был И дефицит, И активность
    total_dates = meal_dates.intersection(activity_dates)

    # --- Расчет ---
    user.streak_nutrition = _calculate_consecutive_days(list(meal_dates))
    user.streak_activity = _calculate_consecutive_days(list(activity_dates))

    # Главный стрик (current_streak) теперь равен общему
    user.current_streak = _calculate_consecutive_days(list(total_dates))

# --- УВЕДОМЛЕНИЯ О РИСКЕ ПОТЕРИ ---

def _send_push(token, title, body):
    if not token or not firebase_admin._apps:
        return
    try:
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token
        )
        messaging.send(msg)
    except Exception as e:
        print(f"[Streak] Push error: {e}")


def _streak_checker_worker(app):
    """
    Фоновый процесс.
    Каждый вечер проверяет, загрузил ли пользователь еду СЕГОДНЯ.
    Если нет, но у него есть накопленный стрик (за вчера) — шлёт алерт.
    """
    with app.app_context():
        while True:
            now = datetime.now()

            # Время проверки: 20:00 (или любое другое вечернее время)
            if now.hour == 18 and 0 <= now.minute < 5:
                print("[Streak] Запуск вечерней проверки...")
                today = date.today()

                # 1. Берем пользователей, у которых есть FCM токен
                users = User.query.filter(User.fcm_device_token.isnot(None)).all()

                count = 0
                for u in users:
                    # Проверяем настройки уведомлений
                    settings = getattr(u, 'settings', None)
                    if settings and not settings.notify_meals:
                        continue

                    # 2. Проверяем, ел ли он СЕГОДНЯ
                    # (Просто запрос в базу: есть ли MealLog за today)
                    has_meal_today = db.session.query(MealLog.id).filter_by(
                        user_id=u.id,
                        date=today
                    ).first() is not None

                    if has_meal_today:
                        continue  # Всё ок, он уже молодец

                    # 3. Если сегодня не ел, проверяем, есть ли у него стрик, который можно потерять.
                    # Мы доверяем полю u.current_streak, так как оно обновлялось при последней активности.
                    # Но на всякий случай можно перепроверить "есть ли запись за вчера".

                    yesterday = today - timedelta(days=1)
                    has_meal_yesterday = db.session.query(MealLog.id).filter_by(
                        user_id=u.id,
                        date=yesterday
                    ).first() is not None

                    if has_meal_yesterday:
                        # У него есть стрик, который держится на вчерашнем дне.
                        # Если не загрузит сегодня — стрик сгорит.

                        # Пересчитываем на всякий случай, чтобы цифра была точной
                        recalculate_streak(u)
                        if u.current_streak > 0:
                            msg = f"Вы не отметили еду сегодня! Ваш стрик из {u.current_streak} дней сгорит в полночь 🔥"
                            _send_push(u.fcm_device_token, "😱 Стрик под угрозой!", msg)
                            count += 1
                            # Коммитим пересчет
                            db.session.commit()

                print(f"[Streak] Отправлено {count} предупреждений.")
                time.sleep(60 * 10)  # Спим 10 минут, чтобы не спамить в этот же час

            time.sleep(60)


def start_streak_scheduler(app):
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        t = threading.Thread(target=_streak_checker_worker, args=(app,), daemon=True)
        t.start()