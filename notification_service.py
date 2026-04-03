import json
import logging
from datetime import datetime
from firebase_admin import messaging
from extensions import db
from models import User, Notification

logger = logging.getLogger(__name__)

def send_user_notification(user_id: int, title: str, body: str, type: str = 'info',
                           data: dict = None, route: str = None, route_args: dict = None,
                           # Новые параметры оформления:
                           rich_title: str = None,
                           rich_body: str = None,
                           bg_color: str = None,      # hex, например '#FF5733'
                           image_url: str = None,
                           large_icon: str = None,
                           summary: str = None):
    try:
        final_data = data or {}
        if route:
            final_data['route'] = route
        if route_args:
            final_data['args'] = json.dumps(route_args)

        # Добавляем поля оформления, если переданы
        if rich_title:
            final_data['rich_title'] = rich_title
        if rich_body:
            final_data['rich_body'] = rich_body
        if bg_color:
            final_data['bg_color'] = bg_color
        if image_url:
            final_data['image_url'] = image_url
        if large_icon:
            final_data['large_icon'] = large_icon
        if summary:
            final_data['summary'] = summary

        # Сохранение в БД (без изменений)
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

        user = db.session.get(User, user_id)
        if user and user.fcm_device_token:
            send_fcm_push(user.fcm_device_token, title, body, final_data)

        return True
    except Exception as e:
        logger.error(f"Error sending notification to user {user_id}: {e}")
        return False

def send_fcm_push(token: str, title: str, body: str, data: dict = None):
    """Отправка Data-only пуша, чтобы Flutter AwesomeNotifications сам рисовал дизайн"""
    try:
        data_dict = data or {}

        # Обязательно прокидываем системные title и body внутрь data
        data_dict['title'] = title
        data_dict['body'] = body

        # FCM принимает данные только в формате строк
        str_data = {k: str(v) for k, v in data_dict.items()}

        # Отправляем сообщение БЕЗ блока notification
        # Это заставит Android/iOS разбудить Flutter, а не рисовать скучную серую карточку
        message = messaging.Message(
            data=str_data,
            token=token,
        )
        response = messaging.send(message)
        return True
    except Exception as e:
        logger.error(f"FCM error: {e}")
        return False