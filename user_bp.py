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

from collections import defaultdict


@user_bp.route('/api/history/deficit', methods=['GET'])
def get_deficit_history():
    user = _current_user()
    if not user:
        return jsonify([]), 401

    history = []
    today = datetime.now().date()
    start_date = today - timedelta(days=29)  # Последние 30 дней включая сегодня

    profile_height = None
    bmr = 1600
    if hasattr(user, 'profile') and user.profile:
        profile_height = user.profile.get('height')
        bmr = user.profile.get('metabolism', 1600)

    # 1. Запрашиваем всю еду за 30 дней (1 запрос)
    meals = MealLog.query.filter(
        MealLog.user_id == user.id,
        func.date(MealLog.created_at) >= start_date
    ).all()

    meals_by_date = defaultdict(int)
    for m in meals:
        meals_by_date[m.created_at.date()] += m.calories

    # 2. Запрашиваем всю активность за 30 дней (1 запрос)
    activities = Activity.query.filter(
        Activity.user_id == user.id,
        func.date(Activity.created_at) >= start_date
    ).all()

    activities_by_date = defaultdict(int)
    for a in activities:
        activities_by_date[
            a.created_at.date()] += a.burned_kcal  # Убедитесь, что поле называется burned_kcal или active_kcal

    # 3. Запрашиваем все замеры за 30 дней (1 запрос)
    analyses = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        func.date(BodyAnalysis.timestamp) >= start_date
    ).order_by(BodyAnalysis.timestamp.desc()).all()

    # Оставляем только самый последний замер для каждого дня
    analysis_by_date = {}
    for a in analyses:
        date_key = a.timestamp.date()
        if date_key not in analysis_by_date:
            analysis_by_date[date_key] = a

    # Собираем данные в цикле (БЕЗ запросов к БД)
    for i in range(30):
        current_date = today - timedelta(days=i)

        consumed = meals_by_date.get(current_date, 0)
        active_burned = activities_by_date.get(current_date, 0)
        total_burned = int(bmr + active_burned)

        analysis = analysis_by_date.get(current_date)
        weight_val = bmi_val = fat_val = None

        if analysis:
            weight_val = analysis.weight
            fat_val = analysis.fat_mass
            bmi_val = analysis.bmi

            if not bmi_val and weight_val:
                h_val = analysis.height or profile_height
                if h_val:
                    try:
                        h_m = h_val / 100.0
                        bmi_val = round(weight_val / (h_m * h_m), 1)
                    except:
                        pass

        day_data = {
            "date": current_date.strftime("%d.%m.%Y"),
            "consumed": int(consumed),
            "total_burned": int(total_burned),
            "deficit": int(total_burned - consumed),
            "is_measurement_day": bool(analysis),
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
        # Отвязываем файлы, чтобы не получить конфликт внешних ключей
        user.avatar_file_id = None
        if hasattr(user, 'full_body_photo_id'):
            user.full_body_photo_id = None
        user.initial_body_analysis_id = None

        # Удаляем группу, если юзер — тренер-владелец
        if getattr(user, "own_group", None):
            db.session.delete(user.own_group)

        # Удаляем самого юзера. Все связанные таблицы (MealLog, Activity, BodyAnalysis и т.д.)
        # будут удалены каскадно благодаря настройкам relationship в models.py
        db.session.delete(user)
        db.session.commit()
        session.clear()

        return jsonify({"ok": True, "message": "Account deleted successfully"})

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"Failed to delete account: {str(e)}"}), 500