import threading
import time
import os
from datetime import date, datetime, timedelta
from flask import Blueprint
from sqlalchemy import func
from extensions import db
from models import User, MealLog, Activity
from firebase_admin import messaging
import firebase_admin

streak_bp = Blueprint('streak_bp', __name__)

def recalculate_streak(user):
    """
    Рассчитывает стрик ТОЛЬКО по ЗАВЕРШЕННЫМ дням (до вчерашнего включительно).
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    # --- 1. Питание (Дефицит) ---
    daily_limit = getattr(user, 'daily_calories', 2000) or 2000

    # Берем только дни СТРОГО ДО СЕГОДНЯ (< today)
    meal_rows = db.session.query(MealLog.date) \
        .filter(MealLog.user_id == user.id) \
        .filter(MealLog.date < today) \
        .group_by(MealLog.date) \
        .having(func.sum(MealLog.calories) > 0) \
        .having(func.sum(MealLog.calories) <= daily_limit) \
        .order_by(MealLog.date.desc()) \
        .all()

    meal_dates = {row.date for row in meal_rows}

    # --- 2. Активность (Шаги) ---
    step_goal = getattr(user, 'step_goal', 10000) or 10000

    # Тоже строго до сегодня
    activity_rows = db.session.query(Activity.date) \
        .filter(Activity.user_id == user.id) \
        .filter(Activity.steps >= step_goal) \
        .filter(Activity.date < today) \
        .order_by(Activity.date.desc()) \
        .all()

    activity_dates = {row.date for row in activity_rows}

    # --- 3. Общий (Пересечение) ---
    total_dates = meal_dates.intersection(activity_dates)

    # --- Внутренняя функция подсчета ---
    def calc_streak_from_dates(dates_set):
        if not dates_set:
            return 0
        # Превращаем в сортированный список
        sorted_dates = sorted(list(dates_set), reverse=True)

        # Если последняя успешная дата была ПОЗАВЧЕРА (или раньше), значит ВЧЕРА пропущено -> стрик 0
        last_success = sorted_dates[0]

        if last_success < yesterday:
            return 0

        # Считаем серию
        streak = 0
        check = yesterday  # Начинаем проверку со вчерашнего дня

        for d in sorted_dates:
            if d == check:
                streak += 1
                check -= timedelta(days=1)
            else:
                break
        return streak

    user.streak_nutrition = calc_streak_from_dates(meal_dates)
    user.streak_activity = calc_streak_from_dates(activity_dates)
    user.current_streak = calc_streak_from_dates(total_dates)

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
    1. В 18:00 напоминает, если пользователь забыл поесть.
    2. В 00:00 (Полночь) пересчитывает стрики всем пользователям.
       Если вчера план не выполнен — стрик обнулится автоматически.
    """
    with app.app_context():
        while True:
            now = datetime.now()

            # --- 1. ВЕЧЕРНЯЯ ПРОВЕРКА (18:00) ---
            if now.hour == 18 and 0 <= now.minute < 5:
                print("[Streak] Запуск вечерней проверки...")
                today = date.today()

                users = User.query.filter(User.fcm_device_token.isnot(None)).all()

                count = 0
                for u in users:
                    settings = getattr(u, 'settings', None)
                    if settings and not settings.notify_meals:
                        continue

                    # Если сегодня уже что-то записал - не трогаем
                    has_meal_today = db.session.query(MealLog.id).filter_by(
                        user_id=u.id,
                        date=today
                    ).first() is not None

                    if has_meal_today:
                        continue

                        # Проверяем, есть ли запись за вчера (как индикатор активности)
                    yesterday = today - timedelta(days=1)
                    has_meal_yesterday = db.session.query(MealLog.id).filter_by(
                        user_id=u.id,
                        date=yesterday
                    ).first() is not None

                    if has_meal_yesterday:
                        # Важно: пересчитываем, чтобы убедиться, что стрик не 0
                        recalculate_streak(u)
                        if u.current_streak > 0:
                            msg = f"Вы не отметили еду сегодня! Ваш стрик из {u.current_streak} дней сгорит в полночь 🔥"
                            _send_push(u.fcm_device_token, "😱 Стрик под угрозой!", msg)
                            count += 1

                # Коммитим после рассылки (если были изменения в пересчете)
                db.session.commit()
                print(f"[Streak] Отправлено {count} предупреждений.")
                time.sleep(60 * 10)

                # --- 2. ПОЛНОЧНЫЙ СБРОС (00:00) ---
            elif now.hour == 0 and 0 <= now.minute < 5:
                print("[Streak] Полночь. Финализация дня и обновление стриков...")

                # Берем ВСЕХ пользователей, чтобы обновить статистику
                all_users = User.query.all()

                for u in all_users:
                    # Функция recalculate_streak смотрит на дни < today.
                    # В 00:00 "today" стало новым днем.
                    # Значит, "вчера" (которое только что закончилось) теперь проверяется на выполнение.
                    # Если вчера не было дефицита/активности -> стрик станет 0.
                    # Если вчера все ок -> стрик увеличится на +1.
                    recalculate_streak(u)

                db.session.commit()
                print(f"[Streak] Стрики обновлены для {len(all_users)} пользователей.")

                # Спим 10 минут, чтобы не запустить повторно в этот же час
                time.sleep(60 * 10)

            time.sleep(60)


def start_streak_scheduler(app):
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        t = threading.Thread(target=_streak_checker_worker, args=(app,), daemon=True)
        t.start()