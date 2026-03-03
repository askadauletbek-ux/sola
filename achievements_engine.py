import traceback
from datetime import date, timedelta
from sqlalchemy import func
from extensions import db
from models import User, MealLog, TrainingSignup, Activity, UserAchievement, Achievement

ACHIEVEMENTS_METADATA = {}


def grant_achievement(user, slug):
    """Выдает ачивку из БД, если её еще нет."""
    try:
        print(f"[ACHIEVEMENTS] 🔍 Проверка выдачи '{slug}' для юзера ID {user.id}", flush=True)

        # 1. Существует ли ачивка в БД
        ach_meta = Achievement.query.filter_by(slug=slug, is_active=True).first()
        if not ach_meta:
            print(f"[ACHIEVEMENTS] ❌ Ачивка '{slug}' отключена или не существует в БД.", flush=True)
            return False

        # 2. Нет ли её уже у юзера
        existing = UserAchievement.query.filter_by(user_id=user.id, slug=slug).first()
        if existing:
            print(f"[ACHIEVEMENTS] ⏩ У юзера {user.id} уже есть '{slug}'. Пропускаем.", flush=True)
            return False

        # 3. Выдаем
        new_ach = UserAchievement(
            user_id=user.id,
            slug=slug,
            seen=False
        )
        db.session.add(new_ach)
        db.session.flush()
        print(f"[ACHIEVEMENTS] 🎉 УСПЕХ! Добавлена '{slug}' юзеру {user.id}!", flush=True)

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
            print(f"[ACHIEVEMENTS] ⚠️ Ошибка PUSH: {e}", flush=True)

        # 5. Постим в AI-ленту (Squads)
        try:
            from app import trigger_ai_feed_post
            trigger_ai_feed_post(user, f"Получил(а) новое достижение: «{ach_meta.title}» {ach_meta.icon}!")
        except Exception as e:
            print(f"[ACHIEVEMENTS] ⚠️ Ошибка ленты: {e}", flush=True)

        return True

    except Exception as e:
        print(f"[ACHIEVEMENTS] 💥 КРИТИЧЕСКАЯ ОШИБКА: {e}", flush=True)
        traceback.print_exc()
        return False


def check_all_achievements(user):
    print(f"[ACHIEVEMENTS] ⚙️ Запуск движка проверки для {user.id}", flush=True)
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

        if getattr(user, 'own_group', None) or user.groups.first():
            if grant_achievement(user, "squad_join"): new_unlocks.append("squad_join")

    except Exception as e:
        print(f"[ACHIEVEMENTS] 💥 Ошибка в check_all_achievements: {e}", flush=True)
        traceback.print_exc()

    return new_unlocks


def _check_first_meal(user):
    count = MealLog.query.filter_by(user_id=user.id).count()
    print(f"[ACHIEVEMENTS] У юзера {user.id} записей еды: {count}", flush=True)
    return count > 0


def _check_first_training(user):
    return TrainingSignup.query.filter_by(user_id=user.id).first() is not None


def _calculate_total_fat_loss_kg(user):
    logs = db.session.query(MealLog.date, func.sum(MealLog.calories)).filter_by(user_id=user.id).group_by(
        MealLog.date).all()
    if not logs: return 0.0

    activities = Activity.query.filter_by(user_id=user.id).all()
    # ИСПРАВЛЕНИЕ ЗДЕСЬ: (a.active_kcal or 0) защищает от NoneType TypeError
    act_map = {a.date: (a.active_kcal or 0) for a in activities}

    bmr = user.metabolism or 2000

    total_deficit = 0
    for d, kcal in logs:
        active = act_map.get(d, 0)
        daily_def = (bmr + active) - kcal
        if daily_def > 0:
            total_deficit += daily_def

    return total_deficit / 7700.0