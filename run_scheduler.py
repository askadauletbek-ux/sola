# run_scheduler.py
import os
from apscheduler.schedulers.blocking import BlockingScheduler
from zoneinfo import ZoneInfo

# Импортируем app и воркер системных уведомлений из app.py
from app import app, _notification_worker
from meal_reminders import _tick as meal_tick


def run_meal_jobs():
    """Обертка для запуска напоминаний о еде"""
    with app.app_context():
        meal_tick()


# Функция _notification_worker уже содержит внутри себя with app.app_context():
# поэтому мы можем передавать её в планировщик напрямую.

if __name__ == '__main__':
    print("🚀 Starting standalone production scheduler...")

    scheduler = BlockingScheduler(timezone=ZoneInfo("Asia/Almaty"))

    # 1. Задача: Напоминания о приемах пищи (каждую 1 минуту)
    scheduler.add_job(
        run_meal_jobs,
        trigger='interval',
        minutes=1,
        id='meal_reminders_standalone',
        replace_existing=True
    )

    # 2. Задача: Тренировки, подписки и итоги Squads (каждую 1 минуту)
    scheduler.add_job(
        _notification_worker,
        trigger='interval',
        minutes=1,
        id='system_notifications_standalone',
        replace_existing=True
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Scheduler stopped.")