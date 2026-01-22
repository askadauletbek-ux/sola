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

    # Пытаемся получить рост пользователя для расчета ИМТ
    # Проверяем разные варианты хранения (атрибут или в профиле)
    user_height = getattr(user, 'height', None)
    if not user_height and hasattr(user, 'profile') and user.profile:
        user_height = user.profile.get('height')

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
        bmr = user.profile.get('metabolism', 1600) if (hasattr(user, 'profile') and user.profile) else 1600

        activities = Activity.query.filter(
            Activity.user_id == user.id,
            func.date(Activity.created_at) == current_date
        ).all()
        active_burned = sum(a.burned_kcal for a in activities)
        total_burned = int(bmr + active_burned)

        # 3. Ищем ЗАМЕР ВЕСА за этот день (BodyAnalysis)
        analysis = BodyAnalysis.query.filter(
            BodyAnalysis.user_id == user.id,
            func.date(BodyAnalysis.created_at) == current_date
        ).order_by(BodyAnalysis.created_at.desc()).first()

        # Получаем вес и ИМТ
        weight_val = None
        bmi_val = None
        fat_val = None

        if analysis:
            # ИСПРАВЛЕНИЕ 1: Используем правильное имя поля .weight
            weight_val = getattr(analysis, 'weight', None)
            fat_val = getattr(analysis, 'fat_mass', None)

            # ИСПРАВЛЕНИЕ 2: Пытаемся взять bmi из базы, если нет — считаем сами
            bmi_val = getattr(analysis, 'bmi', None)

            if not bmi_val and weight_val and user_height:
                try:
                    h_m = user_height / 100.0
                    bmi_val = round(weight_val / (h_m * h_m), 1)
                except:
                    bmi_val = None

        day_data = {
            "date": current_date.strftime("%d.%m.%Y"),
            "consumed": int(consumed),
            "total_burned": int(total_burned),
            "deficit": int(total_burned - consumed),

            "is_measurement_day": True if analysis else False,
            "weight": weight_val,
            "bmi": bmi_val,
            "fat_mass": fat_val,
        }

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