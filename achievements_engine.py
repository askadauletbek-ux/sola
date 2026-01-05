from datetime import date, timedelta
from sqlalchemy import func
from extensions import db
from models import User, MealLog, TrainingSignup, Activity, UserAchievement

# --- КОНФИГУРАЦИЯ АЧИВОК ---
ACHIEVEMENTS_METADATA = {
    "first_meal": {
        "title": "Первый шаг",
        "description": "Запишите свой первый прием пищи.",
        "icon": "restaurant",
        "color": "green"
    },
    "first_training": {
        "title": "Спортсмен",
        "description": "Запишитесь на первую тренировку.",
        "icon": "fitness_center",
        "color": "blue"
    },
    "streak_5": {
        "title": "Набираем обороты",
        "description": "Держите стрик 5 дней подряд.",
        "icon": "fire",
        "color": "orange"
    },
    "streak_10": {
        "title": "В огне!",
        "description": "Держите стрик 10 дней подряд.",
        "icon": "bolt",
        "color": "red"
    },
    "fat_loss_5kg": {
        "title": "Минус 5 кг жира",
        "description": "Накопите дефицит калорий, равный сжиганию 5 кг жира (38,500 ккал).",
        "icon": "whatshot",
        "color": "purple"
    }
}


# --- ДВИЖОК ПРОВЕРКИ ---
def check_all_achievements(user):
    """Запускает проверку всех условий и выдает новые ачивки."""
    new_unlocks = []

    # 1. Первый прием пищи
    if _check_first_meal(user):
        if _grant(user, "first_meal"): new_unlocks.append("first_meal")

    # 2. Первая тренировка
    if _check_first_training(user):
        if _grant(user, "first_training"): new_unlocks.append("first_training")

    # 3. Стрики
    streak = user.current_streak
    if streak >= 5:
        if _grant(user, "streak_5"): new_unlocks.append("streak_5")
    if streak >= 10:
        if _grant(user, "streak_10"): new_unlocks.append("streak_10")

    # 4. Дефицит -5 кг жира
    if _calculate_total_fat_loss_kg(user) >= 5.0:
        if _grant(user, "fat_loss_5kg"): new_unlocks.append("fat_loss_5kg")

    if new_unlocks:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    return new_unlocks


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def _grant(user, slug):
    """Выдает ачивку, если её еще нет. Возвращает True, если выдал."""
    exists = UserAchievement.query.filter_by(user_id=user.id, slug=slug).first()
    if not exists:
        new_ach = UserAchievement(user_id=user.id, slug=slug, seen=False)
        db.session.add(new_ach)
        return True
    return False


def _check_first_meal(user):
    # Проверяем, есть ли хотя бы один лог еды
    return MealLog.query.filter_by(user_id=user.id).first() is not None


def _check_first_training(user):
    return TrainingSignup.query.filter_by(user_id=user.id).first() is not None


def _calculate_total_fat_loss_kg(user):
    """
    Считает накопленный дефицит за всё время.
    1 кг жира ≈ 7700 ккал.
    """
    # Получаем данные за все дни, когда были записи
    logs = db.session.query(
        MealLog.date, func.sum(MealLog.calories)
    ).filter_by(user_id=user.id).group_by(MealLog.date).all()

    if not logs:
        return 0.0

    # Кэшируем активность и BMR
    activities = Activity.query.filter_by(user_id=user.id).all()
    act_map = {a.date: a.active_kcal for a in activities}

    # Берем BMR из последнего анализа (упрощение, но рабочее)
    bmr = user.metabolism or 2000

    total_deficit = 0
    for day_date, consumed_kcal in logs:
        active = act_map.get(day_date, 0)
        burned = bmr + active

        daily_diff = burned - consumed_kcal

        # Считаем только дефицит (если профицит - жир копится?
        # Обычно в ачивках считают "чистый сожженный".
        # Если хотите честный баланс, уберите max(0, ...))
        if daily_diff > 0:
            total_deficit += daily_diff

    return total_deficit / 7700.0