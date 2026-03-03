import traceback
import logging
import sys
from datetime import date, timedelta
from sqlalchemy import func
from extensions import db
from models import User, MealLog, TrainingSignup, Activity, UserAchievement, Achievement, BodyAnalysis

# Настраиваем логгер, чтобы он пробивал буфер Gunicorn
logger = logging.getLogger("achievements")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)  # Направляем прямо в консоль Gunicorn
    handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(handler)

ACHIEVEMENTS_METADATA = {}


def grant_achievement(user, slug):
    """Выдает ачивку из БД, если её еще нет."""
    try:
        logger.warning(f"[ACHIEVEMENTS] 🔍 Проверка выдачи '{slug}' для юзера ID {user.id}")

        # 1. Существует ли ачивка в БД
        ach_meta = Achievement.query.filter_by(slug=slug, is_active=True).first()
        if not ach_meta:
            logger.warning(f"[ACHIEVEMENTS] ❌ Ачивка '{slug}' отключена или не существует в БД.")
            return False

        # 2. Нет ли её уже у юзера
        existing = UserAchievement.query.filter_by(user_id=user.id, slug=slug).first()
        if existing:
            logger.warning(f"[ACHIEVEMENTS] ⏩ У юзера {user.id} уже есть '{slug}'. Пропускаем.")
            return False

        # 3. Выдаем
        new_ach = UserAchievement(
            user_id=user.id,
            slug=slug,
            seen=False
        )
        db.session.add(new_ach)
        db.session.flush()
        logger.warning(f"[ACHIEVEMENTS] 🎉 УСПЕХ! Добавлена '{slug}' юзеру {user.id}!")

        # 4. Отправляем PUSH
        try:
            from notification_service import send_user_notification
            send_user_notification(
                user_id=user.id,
                title=f"🏆 Новое достижение: {ach_meta.title} {ach_meta.icon}",
                body=ach_meta.description,
                type='success',
                data={"route": "/achievements"}
            )
        except Exception as e:
            logger.error(f"[ACHIEVEMENTS] ⚠️ Ошибка PUSH: {e}")

        # 5. Постим в AI-ленту (Squads)
        try:
            from app import trigger_ai_feed_post
            trigger_ai_feed_post(user, f"Получил(а) новое достижение: «{ach_meta.title}» {ach_meta.icon}!")
        except Exception as e:
            logger.error(f"[ACHIEVEMENTS] ⚠️ Ошибка ленты: {e}")

        return True

    except Exception as e:
        logger.error(f"[ACHIEVEMENTS] 💥 КРИТИЧЕСКАЯ ОШИБКА: {e}")
        return False


def check_all_achievements(user):
    logger.warning(f"[ACHIEVEMENTS] ⚙️ Запуск движка проверки для {user.id}")
    new_unlocks = []

    try:
        db.session.flush()  # Гарантируем, что свежая еда видна БД

        if _check_first_meal(user):
            if grant_achievement(user, "first_log"): new_unlocks.append("first_log")

        if _check_first_training(user):
            if grant_achievement(user, "first_workout"): new_unlocks.append("first_workout")

        streak = user.current_streak or 0
        if streak >= 3:
            if grant_achievement(user, "streak_3"): new_unlocks.append("streak_3")
        if streak >= 7:
            if grant_achievement(user, "streak_7"): new_unlocks.append("streak_7")

        if _calculate_total_fat_loss_kg(user) >= 1.0:
            if grant_achievement(user, "fat_loss_start"): new_unlocks.append("fat_loss_start")

        if user.own_group or user.groups.first():
            if grant_achievement(user, "squad_join"): new_unlocks.append("squad_join")

    except Exception as e:
        logger.error(f"[ACHIEVEMENTS] 💥 Ошибка в check_all_achievements: {e}")

    return new_unlocks


def _check_first_meal(user):
    count = MealLog.query.filter_by(user_id=user.id).count()
    logger.warning(f"[ACHIEVEMENTS] У юзера {user.id} записей еды: {count}")
    return count > 0


def _check_first_training(user):
    return TrainingSignup.query.filter_by(user_id=user.id).first() is not None


def _calculate_total_fat_loss_kg(user):
    logs = db.session.query(MealLog.date, func.sum(MealLog.calories)).filter_by(user_id=user.id).group_by(
        MealLog.date).all()
    if not logs: return 0.0

    activities = Activity.query.filter_by(user_id=user.id).all()
    act_map = {a.date: (a.active_kcal or 0) for a in activities}  # Безопасное извлечение

    # Берем метаболизм из последнего замера тела
    latest_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
    bmr = latest_analysis.metabolism if latest_analysis and latest_analysis.metabolism else 2000

    # Безопасное извлечение kcal (защита от None, если в базе есть пустые значения)
    total_deficit = sum([max(0, (bmr + act_map.get(d, 0)) - (kcal or 0)) for d, kcal in logs])

    return total_deficit / 7700.0