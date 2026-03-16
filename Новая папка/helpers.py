from flask_login import current_user
from models import User, BodyAnalysis

def get_current_user():
    """Возвращает текущего аутентифицированного пользователя."""
    if current_user.is_authenticated:
        # Убедимся, что мы возвращаем "живой" объект из сессии SQLAlchemy
        return User.query.get(current_user.id)
    return None


def _latest_analysis_for(user_id):
    """Возвращает последнюю запись анализа тела для пользователя."""
    return BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()


# --- НОВЫЙ КОД ---
from functools import wraps
from flask import request, jsonify, current_app, g, session
import jwt
from models import User


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        current_user = None

        # 1. Проверяем заголовок (для JWT)
        if 'x-access-token' in request.headers:
            token = request.headers['x-access-token']
        elif 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header.split(" ")[1]
            else:
                token = auth_header

        # 2. Если токена нет, проверяем сессию (для совместимости с текущим вебом)
        if not token and 'user_id' in session:
            current_user = User.query.get(session['user_id'])
            if current_user:
                g.user = current_user
                return f(*args, **kwargs)

        if not token:
            return jsonify({'message': 'Token is missing!'}), 401

        try:
            data = jwt.decode(token, current_app.config.get('SECRET_KEY', 'supersecret'), algorithms=["HS256"])
            current_user = User.query.filter_by(id=data.get('id')).first()
            if not current_user:
                return jsonify({'message': 'User invalid!'}), 401
            g.user = current_user
        except Exception as e:
            return jsonify({'message': 'Token is invalid!', 'error': str(e)}), 401

        return f(*args, **kwargs)

    return decorated