from flask import Blueprint, jsonify, request, session
from sqlalchemy import func, cast, Date
from datetime import datetime, timedelta
from extensions import db
from models import User, Notification, MealLog, Activity, BodyAnalysis, Diet, Subscription, TrainingSignup
from notification_service import send_user_notification

user_bp = Blueprint('user_bp', __name__)


def _current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


# --- ИСТОРИЯ ДЕФИЦИТА И ЗАМЕРОВ (НОВОЕ) ---

@user_bp.route('/api/history/deficit', methods=['GET'])
def get_deficit_history():
    user = _current_user()
    if not user:
        return jsonify([]), 401

    history = []
    today = datetime.now().date()

    # Берем данные за последние 30 дней
    for i in range(30):
        current_date = today - timedelta(days=i)

        # 1. Считаем съеденное (MealLog)
        logs = MealLog.query.filter(
            MealLog.user_id == user.id,
            func.date(MealLog.created_at) == current_date
        ).all()
        consumed = sum(l.calories for l in logs)

        # 2. Считаем сожженное (Activity + BMR)
        # Упрощенно берем BMR из профиля или дефолт 1600, плюс активность
        bmr = user.profile.get('metabolism', 1600) if user.profile else 1600

        activities = Activity.query.filter(
            Activity.user_id == user.id,
            func.date(Activity.created_at) == current_date
        ).all()
        active_burned = sum(a.burned_kcal for a in activities)
        total_burned = int(bmr + active_burned)  # BMR считается за сутки

        # 3. Ищем ЗАМЕР ВЕСА за этот день (BodyAnalysis)
        # Важно: приводим created_at к дате для сравнения
        analysis = BodyAnalysis.query.filter(
            BodyAnalysis.user_id == user.id,
            func.date(BodyAnalysis.created_at) == current_date
        ).order_by(BodyAnalysis.created_at.desc()).first()

        # Формируем объект
        day_data = {
            "date": current_date.strftime("%d.%m.%Y"),
            "consumed": int(consumed),
            "total_burned": int(total_burned),
            "deficit": int(total_burned - consumed),

            # ДАННЫЕ ЗАМЕРА (если есть)
            "is_measurement_day": True if analysis else False,
            "weight": analysis.weight_kg if analysis else None,
            "bmi": analysis.bmi if analysis else None,
            "fat_mass": analysis.fat_mass if analysis else None,
        }

        # Добавляем в список (если день не пустой или это сегодня/вчера)
        # Можно фильтровать пустые дни, чтобы не забивать список
        if consumed > 0 or analysis or i < 3:
            history.append(day_data)

    return jsonify(history)


# --- УВЕДОМЛЕНИЯ ---

@user_bp.route('/api/notifications', methods=['GET'])
def get_notifications():
    user = _current_user()
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # Берем последние 50 уведомлений
    notifs = Notification.query.filter_by(user_id=user.id) \
        .order_by(Notification.created_at.desc()) \
        .limit(50).all()

    return jsonify({
        "ok": True,
        "notifications": [n.to_dict() for n in notifs]
    })


@user_bp.route('/api/notifications/<int:n_id>/read', methods=['POST'])
def mark_read(n_id):
    user = _current_user()
    if not user:
        return jsonify({"ok": False}), 401

    notif = Notification.query.filter_by(id=n_id, user_id=user.id).first()
    if notif:
        notif.is_read = True
        db.session.commit()

    return jsonify({"ok": True})


@user_bp.route('/api/notifications/test', methods=['POST'])
def test_notif():
    """Тестовый роут для проверки (можно вызывать через Postman/Flutter)"""
    user = _current_user()
    if not user:
        return jsonify({"ok": False}), 401

    send_user_notification(
        user.id,
        "Тестовое уведомление 🚀",
        "Это уведомление сохранено в БД и отправлено как пуш.",
        type="success"
    )
    return jsonify({"ok": True})


# --- УДАЛЕНИЕ АККАУНТА ---

@user_bp.route('/api/me/delete', methods=['POST'])
def delete_my_account():
    user = _current_user()
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        # Каскадное удаление данных (ручное, для надежности)
        # 1. Логи и активность
        MealLog.query.filter_by(user_id=user.id).delete()
        Activity.query.filter_by(user_id=user.id).delete()
        BodyAnalysis.query.filter_by(user_id=user.id).delete()
        Diet.query.filter_by(user_id=user.id).delete()

        # 2. Подписки и тренировки
        Subscription.query.filter_by(user_id=user.id).delete()
        TrainingSignup.query.filter_by(user_id=user.id).delete()

        # 3. Уведомления
        Notification.query.filter_by(user_id=user.id).delete()

        # 4. Сам пользователь
        db.session.delete(user)
        db.session.commit()

        # 5. Очистка сессии
        session.clear()

        return jsonify({"ok": True, "message": "Account deleted"})

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500