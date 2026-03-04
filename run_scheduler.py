# run_scheduler.py
import os
from apscheduler.schedulers.blocking import BlockingScheduler
from zoneinfo import ZoneInfo

# Импортируем ваше приложение и функции
from app import create_app
from meal_reminders import _tick as meal_tick

# Инициализируем Flask-приложение (чтобы был доступ к БД)
app = create_app()


def run_meal_jobs():
    """Обертка для запуска таски внутри контекста приложения"""
    with app.app_context():
        meal_tick()


if __name__ == '__main__':
    print("🚀 Starting standalone production scheduler...")

    scheduler = BlockingScheduler(timezone=ZoneInfo("Asia/Almaty"))

    # Добавляем задачу каждую 1 минуту
    scheduler.add_job(
        run_meal_jobs,
        trigger='interval',
        minutes=1,
        id='meal_reminders_standalone',
        replace_existing=True
    )

    # Запускаем бесконечный цикл планировщика
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Scheduler stopped.")