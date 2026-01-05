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
