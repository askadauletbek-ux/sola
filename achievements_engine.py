from datetime import date, timedelta
from sqlalchemy import func
from extensions import db
from models import User, MealLog, TrainingSignup, Activity, UserAchievement, Achievement

# Оставляем пустой словарь-заглушку, чтобы старые импорты в app.py не сломали сервер.
# Теперь все данные берутся динамически из БД.
ACHIEVEMENTS_METADATA = {}


def grant_achievement(user, slug):
    """Выдает ачивку из БД, если её еще нет, отправляет PUSH и пишет в ленту."""
    # 1. Проверяем, существует ли ачивка в БД (создана ли админом)
    ach_meta = Achievement.query.filter_by(slug=slug, is_active=True).first()
    if not ach_meta:
        return False

    # 2. Проверяем, нет ли её уже у юзера
    existing = UserAchievement.query.filter_by(user_id=user.id, slug=slug).first()
    if existing:
        return False

    # 3. Выдаем ачивку
    new_ach = UserAchievement(
        user_id=user.id,
        slug=slug,
        seen=False
    )
    db.session.add(new_ach)
    db.session.flush()  # Фиксируем для текущей транзакции

    # 4. Отправляем PUSH-уведомление
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
        print(f"Error sending push for achievement: {e}")

    # 5. Постим в AI-ленту (Squads), если юзер состоит в группе
    try:
        from app import trigger_ai_feed_post
        trigger_ai_feed_post(user, f"Получено новое достижение: «{ach_meta.title}» {ach_meta.icon}!")
    except Exception as e:
        print(f"Error posting achievement to feed: {e}")

    return True


def check_all_achievements(user):
    """Запускает проверку всех условий и выдает новые ачивки."""
    new_unlocks = []

    # 1. Первый прием пищи (slug: first_log)
    if _check_first_meal(user):
        if grant_achievement(user, "first_log"): new_unlocks.append("first_log")

    # 2. Первая тренировка (slug: first_workout)
    if _check_first_training(user):
        if grant_achievement(user, "first_workout"): new_unlocks.append("first_workout")

    # 3. Стрики (slugs: streak_3, streak_7)
    streak = user.current_streak
    if streak >= 3:
        if grant_achievement(user, "streak_3"): new_unlocks.append("streak_3")
    if streak >= 7:
        if grant_achievement(user, "streak_7"): new_unlocks.append("streak_7")

    # 4. Дефицит / сжигание жира (slug: fat_loss_start)
    if _calculate_total_fat_loss_kg(user) >= 1.0: # Выдаем за первый сожженный килограмм
        if grant_achievement(user, "fat_loss_start"): new_unlocks.append("fat_loss_start")

    # 5. Вступление в отряд (slug: squad_join)
    if user.own_group or user.groups.first():
        if grant_achievement(user, "squad_join"): new_unlocks.append("squad_join")

    if new_unlocks:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    return new_unlocks


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def _check_first_meal(user):
    return MealLog.query.filter_by(user_id=user.id).first() is not None

def _check_first_training(user):
    return TrainingSignup.query.filter_by(user_id=user.id).first() is not None

def _calculate_total_fat_loss_kg(user):
    """Считает накопленный дефицит за всё время (1 кг жира ≈ 7700 ккал)."""
    logs = db.session.query(
        MealLog.date, func.sum(MealLog.calories)
    ).filter_by(user_id=user.id).group_by(MealLog.date).all()

    if not logs:
        return 0.0

    activities = Activity.query.filter_by(user_id=user.id).all()
    act_map = {a.date: a.active_kcal for a in activities}

    bmr = user.metabolism or 2000
    total_deficit = 0

    for day_date, consumed_kcal in logs:
        active = act_map.get(day_date, 0)
        burned = bmr + active
        daily_diff = burned - consumed_kcal

        if daily_diff > 0:
            total_deficit += daily_diff

    return total_deficit / 7700.0