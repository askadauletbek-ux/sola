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

def recalculate_streak(user):
    """
    Смотрит в таблицу MealLog, ищет непрерывную последовательность дней.
    Не использует счетчик +1. Считает реальные даты.
    """
    # 1. Достаем уникальные даты, когда пользователь ел, в обратном порядке
    # (DISTINCT date ORDER BY date DESC)
    logs = db.session.query(MealLog.date) \
        .filter_by(user_id=user.id) \
        .group_by(MealLog.date) \
        .order_by(MealLog.date.desc()) \
        .limit(365) \
        .all()

    # Превращаем в список объектов date: [2023-10-25, 2023-10-24, 2023-10-22...]
    dates = [row.date for row in logs]

    if not dates:
        user.current_streak = 0
        return

    today = date.today()
    yesterday = today - timedelta(days=1)

    streak = 0

    # Логика: Стрик жив, если последняя запись была Сегодня или Вчера.
    # Если последняя запись была позавчера - стрик уже 0 (сгорел).

    latest_log = dates[0]

    if latest_log < yesterday:
        # Стрик прервался
        user.current_streak = 0
        return

    # Начинаем проверку цепочки
    # Если есть запись за сегодня, начинаем отсчет с сегодня.
    # Если нет за сегодня, но есть за вчера - начинаем со вчера.

    check_date = today if (latest_log == today) else yesterday

    # Проходим по датам и смотрим, нет ли разрывов
    for d in dates:
        if d == check_date:
            streak += 1
            check_date -= timedelta(days=1)  # Идем на день назад
        else:
            # Нашли разрыв (например, в базе 20-е число, а мы ждали 21-е)
            break

    user.current_streak = streak
    # db.session.commit() — делает вызывающая функция


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