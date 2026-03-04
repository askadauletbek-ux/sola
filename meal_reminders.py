import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from extensions import db
from models import User, UserSettings, MealReminderLog

# Фиксированные времена (по времени сервера, Asia/Almaty)
MEAL_SCHEDULE = {
    "breakfast": ("🍳 Завтрак", "08:00"),
    "lunch": ("🍲 Обед", "10:35"),
    "dinner": ("🍝 Ужин", "18:00"),
}

_scheduler = None


# --- НОВАЯ ЛОГИКА _tick() ---

def _tick():
    """
    Запускается каждую минуту по времени сервера (Asia/Almaty).
    Проверяет, совпадает ли текущее время сервера с расписанием.
    Если да - отправляет PUSH всем пользователям, у кого включены
    уведомления и кто еще не получал его СЕГОДНЯ (по дате сервера).
    """

    # --- 1. Получаем текущее время и дату СЕРВЕРА (Almaty) ---
    try:
        # Убедимся, что работаем в Asia/Almaty, как и планировщик
        tz_almaty = ZoneInfo("Asia/Almaty")
        now_almaty = datetime.now(tz_almaty)
        current_hhmm = now_almaty.strftime("%H:%M")
        current_date = now_almaty.date()
    except Exception as e:
        print(f"[meal_scheduler] ERROR: Failed to get Almaty time: {e}")
        return

    print(f"[meal_scheduler] _tick() RUNNING at {current_hhmm} (Almaty Time)")

    # --- 2. Проверяем, совпадает ли время с расписанием ---
    meal_to_send = None
    for key, (title, scheduled_hhmm) in MEAL_SCHEDULE.items():
        if current_hhmm == scheduled_hhmm:
            meal_to_send = (key, title)
            break

    # Если сейчас не время отправки (например, 14:10) - выходим
    if not meal_to_send:
        print(f"[meal_scheduler] No schedule match for {current_hhmm}. Exiting.")
        return

    meal_key, title = meal_to_send
    print(f"[meal_scheduler] MATCH FOUND: Sending '{meal_key}' for {current_date}")

    # --- 3. Импорт PUSH-функции ---
    try:
        from app import _send_mobile_push
    except ImportError:
        print("ERROR: Could not import _send_mobile_push. Push notifications will fail.")
        _send_mobile_push = None
        return

    # --- 4. Получаем ВСЕХ пользователей, кому можно отправлять ---
    users_query = (
        User.query
        .join(UserSettings, UserSettings.user_id == User.id)
        .filter(
            UserSettings.notify_meals.is_(True),
            User.fcm_device_token.isnot(None)  # Убедимся, что у них есть токен
        )
        .all()
    )

    if not users_query:
        print("[meal_scheduler] No users found with notify_meals=True and FCM token.")
        return

    print(f"[meal_scheduler] Found {len(users_query)} total eligible users.")

    # --- 5. Получаем ID тех, кто УЖЕ получил это уведомление СЕГОДНЯ ---
    # (Мы используем current_date - дату сервера)
    user_ids_already_sent = db.session.query(MealReminderLog.user_id).filter(
        MealReminderLog.meal_type == meal_key,
        MealReminderLog.date_sent == current_date
    ).all()

    # Превращаем список кортежей [(33,), (34,)] в set {33, 34} для быстрой проверки
    sent_user_id_set = {uid[0] for uid in user_ids_already_sent}

    if sent_user_id_set:
        print(f"[meal_scheduler] Found {len(sent_user_id_set)} users who already received '{meal_key}' today.")

    # --- 6. Фильтруем и отправляем ---
    logs_to_add = []

    # Оставляем только тех, кто не в 'sent_user_id_set'
    final_batch_to_send = [
        user for user in users_query
        if user.id not in sent_user_id_set
    ]

    if not final_batch_to_send:
        print("[meal_scheduler] All eligible users have already received the notification. Nothing to send.")
        return

    print(f"[meal_scheduler] Sending notifications to {len(final_batch_to_send)} users...")

    for user in final_batch_to_send:
        sent = False
        fcm_token = getattr(user, "fcm_device_token", None)

        if fcm_token and _send_mobile_push:
            sent = _send_mobile_push(
                fcm_token=fcm_token,
                title=title,
                body="Нажмите, чтобы зафиксировать его.",
                data={"type": "meal_reminder", "meal_key": meal_key}
            )

        if sent:
            logs_to_add.append(MealReminderLog(
                user_id=user.id,
                meal_type=meal_key,
                date_sent=current_date  # Логгируем по дате сервера
            ))

    # --- 7. Сохраняем все логи ОДНОЙ транзакцией ---
    if logs_to_add:
        try:
            db.session.add_all(logs_to_add)
            db.session.commit()
            print(f"[meal_scheduler] Successfully sent and logged {len(logs_to_add)} notifications.")
        except Exception:
            db.session.rollback()
            print("[meal_scheduler] ERROR: Failed to save logs to database.")


# --- ПУБЛИЧНЫЕ ФУНКЦИИ (без изменений) ---

def get_scheduler():
    """Вернуть текущий инстанс APScheduler (или None)."""
    return _scheduler


def pause_job(job_id: str):
    if _scheduler:
        _scheduler.pause_job(job_id)


def resume_job(job_id: str):
    if _scheduler:
        _scheduler.resume_job(job_id)


def run_tick_now(app):
    """Принудительно вызвать тик рассылки напоминаний (в Flask-контексте)."""
    with app.app_context():
        _tick()


def start_meal_scheduler(app):
    """Создать и запустить шедулер (если ещё не создан). Вернуть инстанс."""
    global _scheduler
    if _scheduler:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Almaty")

    def _job():
        # print("[meal_scheduler] JOB FIRING...") # (Убираем лишний лог)
        # даём Flask-контекст внутри джобы
        with app.app_context():
            _tick()

    # регистрируем периодическую задачу и стартуем шедулер
    # Интервал в 1 минуту - это ПРАВИЛЬНО.
    _scheduler.add_job(_job, "interval", minutes=1, id="meal-reminders", replace_existing=True)
    _scheduler.start()
    print("[meal_scheduler] BackgroundScheduler started (Server Timezone: Asia/Almaty).")
    return _scheduler