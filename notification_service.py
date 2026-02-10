import json
import logging
from datetime import datetime
from firebase_admin import messaging
from extensions import db
from models import User, Notification

logger = logging.getLogger(__name__)


def send_user_notification(user_id: int, title: str, body: str, type: str = 'info', data: dict = None,
                           route: str = None, route_args: dict = None):
    """
    1. Сохраняет уведомление в БД.
    2. Отправляет Push-уведомление через FCM (если у пользователя есть токен).

    :param route: Маршрут для перехода в приложении (напр. '/chat', '/meal')
    :param route_args: Аргументы для маршрута
    """
    try:
        # Подготовка данных для навигации
        final_data = data or {}
        if route:
            final_data['route'] = route
        if route_args:
            final_data['args'] = json.dumps(route_args)

        # 1. Сохранение в БД
        new_notif = Notification(
            user_id=user_id,
            title=title,
            body=body,
            type=type,
            data_json=json.dumps(final_data) if final_data else None,
            created_at=datetime.utcnow()
        )
        db.session.add(new_notif)
        db.session.commit()

        # 2. Отправка FCM (Push)
        # Получаем пользователя для токена
        user = db.session.get(User, user_id)
        if user and user.fcm_device_token:
            send_fcm_push(user.fcm_device_token, title, body, final_data)

        return True
    except Exception as e:
        logger.error(f"Error sending notification to user {user_id}: {e}")
        # Не падать если ошибка сохранения, главное попытаться отправить пуш
        return False


def send_fcm_push(token: str, title: str, body: str, data: dict = None):
    """Отправка только пуша (вспомогательная функция)"""
    try:
        # FCM принимает данные только в формате строк
        str_data = {k: str(v) for k, v in (data or {}).items()}

        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=str_data,
            token=token,
        )
        response = messaging.send(message)
        return True
    except Exception as e:
        logger.error(f"FCM error: {e}")
        return False