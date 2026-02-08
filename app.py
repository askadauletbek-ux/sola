import base64
import json
import os
import random
import re
import string
import uuid
from datetime import date, datetime, timedelta, time as dt_time, UTC
from zoneinfo import ZoneInfo  # <--- Добавлено
from functools import wraps
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from sqlalchemy import or_ # <--- Добавьте это в импорты sqlalchemy
import tempfile  # Добавить в импорты вверху файла
from assistant_bp import assistant_bp, generate_diet_for_user # <--- Добавили импорт функции

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image
from openai import OpenAI
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import subqueryload
from sqlalchemy.exc import IntegrityError

from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    session,
    url_for,
    Blueprint,
    request,
)
from flask_bcrypt import Bcrypt
from flask_login import current_user
from werkzeug.utils import secure_filename
from amplitude import Amplitude, BaseEvent  # <-- Amplitude
import jwt  # <-- Добавлен импорт для Apple Sign-In

# --- Импорты для Google Sign-In ---
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
# ----------------------------------

from assistant_bp import assistant_bp
from streak_bp import streak_bp, start_streak_scheduler, recalculate_streak # <-- Добавлено
from gemini_visualizer import create_record, generate_for_user, _compute_pct
from meal_reminders import (
    get_scheduler,
    pause_job,
    resume_job,
    run_tick_now,
    start_meal_scheduler,
)
from shopping_bp import shopping_bp
from user_bp import user_bp
# Добавляем этот импорт, чтобы отправка работала в админке
from notification_service import send_user_notification
from models import BodyVisualization, SubscriptionApplication, EmailVerification, SquadScoreLog, SupportTicket, SupportMessage
from flask import send_file
from io import BytesIO
from progress_analyzer import generate_progress_commentary
from flask import make_response
import firebase_admin
from firebase_admin import credentials, messaging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

if not firebase_admin._apps:
    try:
        cred_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY_PATH", "serviceAccountKey.json")
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized.")
    except Exception as e:
        print(f"WARNING: Firebase Admin SDK failed to initialize: {e}")
        print("Push notifications will NOT work.")
else:
    print("Firebase Admin SDK already initialized (likely due to Flask reloader).")

load_dotenv()

# Инициализация Amplitude
amplitude = Amplitude(api_key=os.getenv("AMPLITUDE_API_KEY", "c9572b73ece4f73786a764fa197c2161"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")
app.jinja_env.globals.update(getattr=getattr)

# Config DB — задаём ДО init_app
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///35healthclubs.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from extensions import db
db.init_app(app)

from models import (
    User, Subscription, Order, Group, GroupMember, GroupMessage, MessageReaction,
    GroupTask, MealLog, Activity, Diet, Training, TrainingSignup, BodyAnalysis,
    UserSettings, MealReminderLog, AuditLog, PromptTemplate, UploadedFile,
    UserAchievement, MessageReport, AnalyticsEvent, RecipeCategory, Recipe, WeightLog)
# <-- Добавьте это ниже импортов models
from achievements_engine import check_all_achievements, ACHIEVEMENTS_METADATA


# --- Image Resizing Configuration ---
CHAT_IMAGE_MAX_SIZE = (200, 200)  # Max width and height for chat images

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


def get_dynamic_calorie_goal(user):
    """
    Рассчитывает цель калорий: BMR (из замера) + Средняя активность (7 дней) - 15% дефицит.
    Если данных нет, возвращает дефолтное значение (например, 2000).
    """
    try:
        # 1. Получаем BMR из последнего анализа
        latest_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
        bmr = latest_analysis.metabolism if latest_analysis and latest_analysis.metabolism else 0

        # Если BMR нет, пробуем рассчитать по формуле Миффлина-Сан Жеора (если есть вес/рост)
        if not bmr and latest_analysis and latest_analysis.weight and latest_analysis.height:
            age = calculate_age(user.date_of_birth) if user.date_of_birth else 30
            s = -161 if (getattr(user, 'sex', 'female') == 'female') else 5
            bmr = (10 * latest_analysis.weight) + (6.25 * latest_analysis.height) - (5 * age) + s

        if not bmr:
            return 2000  # Fallback, если совсем нет данных

        # 2. Считаем среднюю активность за последние 7 дней
        week_ago = date.today() - timedelta(days=7)
        avg_activity = db.session.query(func.avg(Activity.active_kcal)).filter(
            Activity.user_id == user.id,
            Activity.date >= week_ago
        ).scalar() or 400  # Если активности нет, берем ~400 как умеренную

        # 3. Формула: (BMR + Активность) - 15% (дефицит)
        tdee = bmr + avg_activity
        target = int(tdee * 0.85)  # 15% дефицит для похудения

        # Защита от слишком низких значений
        return max(target, 1200)

    except Exception as e:
        print(f"Error calculating dynamic goal: {e}")
        return 2000

def resize_image(filepath, max_size):
    """Resizes an image and saves it back to the same path."""
    try:
        with Image.open(filepath) as img:
            print(f"DEBUG: Resizing image: {filepath}, original size: {img.size}")
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            img.save(filepath)  # Overwrites the original
            print(f"DEBUG: Image resized to: {img.size}")
    except Exception as e:
        print(f"ERROR: Failed to resize image {filepath}: {e}")


def award_squad_points(user, category, base_points, description=None):
    """
    Начисляет баллы с учетом множителя стрика (x1.2 если стрик >= 3).
    Возвращает начисленные баллы.
    """
    # Если пользователь не в активном скваде, баллы не идут в зачет лидерборда
    # (но можно сохранять для личной статистики, здесь реализуем строгую привязку к группе)

    # Определяем текущую группу
    group_id = None
    if user.own_group:
        group_id = user.own_group.id
    else:
        membership = GroupMember.query.filter_by(user_id=user.id).first()
        if membership:
            group_id = membership.group_id

    if not group_id:
        return 0

        # Множитель за стрик
    multiplier = 1.2 if getattr(user, 'current_streak', 0) >= 3 else 1.0
    final_points = int(base_points * multiplier)

    log = SquadScoreLog(
        user_id=user.id,
        group_id=group_id,
        points=final_points,
        category=category,
        description=description
    )
    db.session.add(log)
    return final_points


def trigger_ai_feed_post(user, event_text):
    """
    Генерирует короткий AI-пост в группу пользователя о его достижении.
    """
    # 1. Определяем группу пользователя
    group_id = None
    if user.own_group:
        group_id = user.own_group.id
    else:
        mem = GroupMember.query.filter_by(user_id=user.id).first()
        if mem:
            group_id = mem.group_id

    if not group_id:
        return

    # 2. Генерируем текст через GPT-4o
    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system",
                 "content": "Ты — энергичный бот-комментатор в фитнес-группе. Твоя задача: написать ОЧЕНЬ КОРОТКОЕ (максимум 20 слов), хайповое и веселое поздравление участнику. Используй эмодзи (🔥, 🚀, 🏆). Пиши в третьем лице (называй по имени). Не будь скучным!"},
                {"role": "user",
                 "content": f"Напиши пост об этом событии: {event_text}. Пользователя зовут {user.name}."}
            ],
            max_tokens=100
        )
        content = completion.choices[0].message.content.strip()

        # 3. Сохраняем сообщение в ленту (тип system)
        msg = GroupMessage(
            group_id=group_id,
            user_id=user.id,
            text=content,
            type='system',  # Специальный тип для выделения в UI
            timestamp=datetime.now(UTC)
        )
        db.session.add(msg)
        db.session.commit()

        # 4. Отправляем PUSH-уведомление соотрядцам
        group = db.session.get(Group, group_id)
        if group:
            recipients = set([m.user_id for m in group.members])
            if group.trainer_id:
                recipients.add(group.trainer_id)

            # Себе не отправляем
            if user.id in recipients:
                recipients.remove(user.id)

            for rid in recipients:
                from notification_service import send_user_notification
                send_user_notification(
                    user_id=rid,
                    title=f"Новости отряда {group.name} ⚡️",
                    body=content,
                    type='info',
                    data={"route": "/squad"}
                )

    except Exception as e:
        print(f"Error triggering AI feed post: {e}")


ADMIN_EMAIL = "admin@healthclub.local"

def _magic_serializer():
    # соль зафиксирована, чтобы токены были совместимы между рестартами
    secret = app.secret_key or app.config.get("SECRET_KEY")
    return URLSafeTimedSerializer(secret, salt="magic-login")


def log_audit(action: str, entity: str, entity_id: str, old=None, new=None):
    try:
        entry = AuditLog(
            actor_id=session.get('user_id'),
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            old_data=old,
            new_data=new,
            ip=request.headers.get('X-Forwarded-For') or request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()

def track_event(event_type, user_id=None, data=None):
        """Сохраняет событие аналитики в БД."""
        try:
            if not user_id and session.get('user_id'):
                user_id = session.get('user_id')

            event = AnalyticsEvent(
                user_id=user_id,
                event_type=event_type,
                event_data=data or {}
            )
            db.session.add(event)
            db.session.commit()
        except Exception as e:
            print(f"Analytics Error: {e}")
            # Не роняем основной поток из-за ошибки аналитики
            db.session.rollback()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)

    return decorated_function


def get_current_user():
    user_id = session.get('user_id')
    if user_id:
        return db.session.get(User, user_id)
    return None


def is_admin():
    user = get_current_user()
    return user and user.email == ADMIN_EMAIL


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.url))
        if not is_admin():
            abort(403)  # Forbidden
        return f(*args, **kwargs)

    return decorated_function


# --- MAGIC LOGIN (вход по ссылке, 1 час) ---
if "magic_login" not in app.view_functions:
    @app.get("/auth/magic/<token>", endpoint="magic_login")
    def magic_login(token):
        s = _magic_serializer()
        try:
            user_id = int(s.loads(token, max_age=3600))
        except SignatureExpired:
            flash("Ссылка истекла. Сгенерируйте новую.", "error")
            return redirect(url_for("login"))
        except BadSignature:
            flash("Ссылка недействительна.", "error")
            return redirect(url_for("login"))
        user = db.session.get(User, user_id) or abort(404)
        session["user_id"] = user.id
        flash("Вы вошли через магическую ссылку.", "success")
        return redirect(url_for("profile"))



client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
bcrypt = Bcrypt(app)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# важно: чтобы meal_reminders видел токен/базовый урл
app.config["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
app.config["PUBLIC_BASE_URL"]    = os.getenv("APP_BASE_URL", "").rstrip("/")


import os, threading, time as time_mod, requests

def _dt(date_obj, time_obj):
    return datetime.combine(date_obj, time_obj)

def _send_telegram(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        return r.ok
    except Exception:
        return False


def _send_mobile_push(fcm_token: str, title: str, body: str, data: dict = None):
    """
    Отправляет PUSH-уведомление через FCM.
    """
    # Проверяем, что токен есть и Firebase Admin SDK инициализирован
    if not fcm_token or not firebase_admin._apps:
        return False

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
        token=fcm_token,
    )

    try:
        response = messaging.send(message)
        print(f"Successfully sent push notification: {response}")
        return True
    except Exception as e:
        print(f"Error sending push notification: {e}")
        # ВАЖНО: Если ошибка 'InvalidRegistrationToken',
        # токен протух, и его нужно удалить из БД (user.fcm_device_token = None).
        # (Эту логику можно добавить позже)
        return False

@app.before_request
def set_tz():
    if db.engine.url.get_backend_name() == "postgresql":
        with db.engine.connect() as con:
            con.exec_driver_sql("SET TIME ZONE 'Asia/Almaty'")

@app.before_request
def expire_subscriptions_if_needed():
    """Перед каждым запросом помечаем подписку текущего пользователя как inactive, если истекла."""
    try:
        u = get_current_user()
        if not u:
            return
        sub = getattr(u, "subscription", None)
        if sub and sub.status == 'active' and sub.end_date and sub.end_date < date.today():
            sub.status = 'inactive'
            db.session.commit()
    except Exception:
        db.session.rollback()

@app.route('/api/activity/today/<int:chat_id>')
def activity_today(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if not user:
        return jsonify({"error": "not found"}), 404
    a = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    if not a:
        return jsonify({"present": False})
    return jsonify({"present": True, "steps": a.steps or 0, "active_kcal": a.active_kcal or 0})

_notifier_started = False
def _notification_worker():
    # ВАЖНО: весь цикл работает внутри контекста приложения
    with app.app_context():
        while True:
            try:
                # --- ВРЕМЯ АЛМАТЫ ---
                # Используем явную таймзону, чтобы не зависеть от времени сервера
                now = datetime.now(ZoneInfo("Asia/Almaty"))
                now_d = now.date()
                target = now + timedelta(hours=1)

                # ⛔️ Деактивируем просроченные подписки (end_date < today)
                try:
                    db.session.query(Subscription).filter(
                        Subscription.status == 'active',
                        Subscription.end_date.isnot(None),
                        Subscription.end_date < now_d
                    ).update({"status": "inactive"}, synchronize_session=False)
                    db.session.commit()
                except Exception:
                    db.session.rollback()

                # 1) Напоминания за 1 час (как было)
                trainings = Training.query.filter(
                    Training.date == target.date(),
                    func.extract('hour', Training.start_time) == target.hour,
                    func.extract('minute', Training.start_time) == target.minute
                ).all()

                for t in trainings:
                    # СЦЕНАРИЙ 1: Групповая тренировка (уведомляем ВСЕХ участников)
                    if t.group_id is not None:
                        if not t.group_notified_1h:
                            # Берем всех участников группы
                            members = GroupMember.query.filter_by(group_id=t.group_id).all()

                            # Также добавляем тренера, если он не участник, чтобы он тоже знал
                            recipients_ids = {m.user_id for m in members}
                            if t.trainer_id:
                                recipients_ids.add(t.trainer_id)

                            for uid in recipients_ids:
                                u = db.session.get(User, uid)
                                if not u: continue

                                # Проверка настроек юзера (общая)
                                settings = get_effective_user_settings(u)
                                if not settings.notify_trainings: continue

                                from notification_service import send_user_notification
                                send_user_notification(
                                    user_id=u.id,
                                    title="⏰ Скоро тренировка!",
                                    body=f"Команда собирается через час: «{t.title}». Не опаздывайте!",
                                    type='reminder',
                                    data={"training_id": str(t.id), "route": "/squad"}  # Ведем в сквад
                                )

                            # Помечаем тренировку как "оповещенную"
                            t.group_notified_1h = True

                    # СЦЕНАРИЙ 2: Публичная тренировка (по старой логике Signups)
                    else:
                        rows = TrainingSignup.query.filter_by(training_id=t.id, notified_1h=False).all()
                        for s in rows:
                            u = db.session.get(User, s.user_id)

                            # --- 1. Проверяем ОБЩИЕ настройки ---
                            if (not u or not getattr(u, "telegram_notify_enabled", True)  # (Оставляем старую настройку)
                                    or not getattr(u, "notify_trainings", True)):
                                s.notified_1h = True  # Помечаем, чтобы не спамить
                                continue

                            # --- 2. Формируем контент для PUSH ---
                            when = t.start_time.strftime("%H:%M")
                            date_s = t.date.strftime("%d.%m.%Y")
                            title = "⏰ Напоминание о тренировке!"
                            body = (
                                f"Через 1 час: «{t.title or 'Онлайн-тренировка'}» с "
                                f"{(t.trainer.name if t.trainer and getattr(t.trainer, 'name', None) else 'тренером')} в {when}."
                            )

                            # --- 3. Отправляем уведомление (БД + PUSH) ---
                            # Импорт внутри функции для избежания циклических ссылок
                            from notification_service import send_user_notification

                            sent_mobile = send_user_notification(
                                user_id=u.id,
                                title=title,
                                body=body,
                                type='reminder',
                                data={"training_id": str(t.id), "route": "/calendar"}
                            )
                            # Fallback на Telegram ПОЛНОСТЬЮ УБРАН

                            # --- 4. Помечаем как "уведомлено" ---
                            if sent_mobile:
                                s.notified_1h = True
                startings = Training.query.filter(
                    Training.date == now.date(),
                    func.extract('hour', Training.start_time) == now.hour,
                    func.extract('minute', Training.start_time) == now.minute
                ).all()

                for t in startings:
                    # СЦЕНАРИЙ 1: Групповая
                    if t.group_id is not None:
                        if not t.group_notified_start:
                            members = GroupMember.query.filter_by(group_id=t.group_id).all()
                            recipients_ids = {m.user_id for m in members}
                            if t.trainer_id: recipients_ids.add(t.trainer_id)

                            for uid in recipients_ids:
                                u = db.session.get(User, uid)
                                if not u: continue
                                settings = get_effective_user_settings(u)
                                if not settings.notify_trainings: continue

                                from notification_service import send_user_notification
                                send_user_notification(
                                    user_id=u.id,
                                    title="🚀 Тренировка началась!",
                                    body=f"Заходите в видео-чат: «{t.title}».",
                                    type='info',
                                    data={"training_id": str(t.id), "route": "/squad"}
                                )
                            t.group_notified_start = True

                    # СЦЕНАРИЙ 2: Публичная
                    else:
                        rows = TrainingSignup.query.filter_by(training_id=t.id).all()
                        for s in rows:
                            # пропускаем, если уже отмечали старт
                            if getattr(s, "notified_start", False):
                                continue
                            u = db.session.get(User, s.user_id)

                            # --- 1. Проверяем ОБЩИЕ настройки ---
                            if (not u or not getattr(u, "telegram_notify_enabled", True)
                                    or not getattr(u, "notify_trainings", True)):
                                s.notified_start = True  # Помечаем, чтобы не спамить
                                continue

                            # --- 2. Формируем контент для PUSH ---
                            when = t.start_time.strftime("%H:%M")
                            date_s = t.date.strftime("%d.%m.%Y")
                            title = "🏁 Тренировка начинается!"
                            body = f"«{t.title or 'Онлайн-тренировка'}» началась. Тренер: {(t.trainer.name if t.trainer and getattr(t.trainer, 'name', None) else 'тренер')}."

                            # --- 3. Отправляем уведомление (БД + PUSH) ---
                            from notification_service import send_user_notification

                            sent_mobile = send_user_notification(
                                user_id=u.id,
                                title=title,
                                body=body,
                                type='info',
                                data={"training_id": str(t.id), "route": "/calendar"}
                            )

                            # Fallback на Telegram ПОЛНОСТЬЮ УБРАН

                            # --- 4. Помечаем как "уведомлено" ---
                            if sent_mobile:
                                s.notified_start = True

                users = User.query.all()
                for u in users:
                    sub = getattr(u, "subscription", None)
                    if not sub or sub.status != 'active' or not sub.end_date:
                        continue
                    days_left = (sub.end_date - now_d).days

                    # --- ИЗМЕНЕНИЕ: Проверяем fcm_token и настройки ---
                    fcm_token = getattr(u, "fcm_device_token", None)
                    settings = get_effective_user_settings(u)

                    if days_left == 5 and not u.renewal_telegram_sent and fcm_token and settings.notify_subscription:
                        try:
                            # ссылка на продление
                            base = os.getenv("APP_BASE_URL", "").rstrip("/")
                            purchase_path = url_for("purchase_page") if app and app.app_context else "/purchase"
                            link = f"{base}{purchase_path}" if base else purchase_path

                            title = "⏳ Подписка истекает"
                            body = "Осталось 5 дней. Не теряйте доступ к тренировкам — продлите сейчас."

                            # --- ИЗМЕНЕНИЕ: Отправляем уведомление (БД + PUSH) ---
                            from notification_service import send_user_notification

                            if send_user_notification(
                                    user_id=u.id,
                                    title=title,
                                    body=body,
                                    type='warning',
                                    data={"route": "/purchase"}
                            ):
                                u.renewal_telegram_sent = True
                        except Exception:
                            pass

                if now.minute == 0 and now.hour == 10:
                    two_weeks_ago = now_d - timedelta(days=14)

                    # --- ИЗМЕНЕНИЕ: Ищем пользователей с FCM токеном ---
                    users_to_remind = User.query.filter(User.fcm_device_token.isnot(None)).all()

                    for u in users_to_remind:
                        # Проверяем настройки уведомлений пользователя
                        settings = get_effective_user_settings(u)

                        # --- ИЗМЕНЕНИЕ: Проверяем общие PUSH-настройки (можно заменить на спец. настройку) ---
                        if not settings.notify_meals:  # (Используем notify_meals как общий флаг для ЗОЖ)
                            continue

                        # Найти последний замер пользователя
                        latest_analysis = BodyAnalysis.query.filter_by(user_id=u.id).order_by(
                            BodyAnalysis.timestamp.desc()).first()

                        if latest_analysis:
                            # Проверяем, прошло ли 14 дней с последнего замера
                            if latest_analysis.timestamp.date() <= two_weeks_ago:
                                # Проверяем, не отправляли ли мы уже напоминание в последние 13 дней
                                if u.last_measurement_reminder_sent_at is None or \
                                        (now - u.last_measurement_reminder_sent_at).days >= 14:

                                    # --- ИЗМЕНЕНИЕ: Отправляем уведомление (БД + PUSH) ---
                                    from notification_service import send_user_notification

                                    title = "⏰ Пора сделать замер!"
                                    body = f"Привет, {u.name}! Прошло 2 недели с последнего замера. Пора обновить данные."

                                    if send_user_notification(
                                            user_id=u.id,
                                            title=title,
                                            body=body,
                                            type='info',
                                            data={"route": "/profile"}  # Открываем профиль для замера
                                    ):
                                        u.last_measurement_reminder_sent_at = now
                                        db.session.commit()

                                        # --- ЕЖЕНЕДЕЛЬНЫЕ ИТОГИ (Понедельник 09:00 Алматы) ---
                                    if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
                                        # 1. Определяем даты прошлой недели (Пн-Вс)
                                        today_date = now.date()
                                        start_of_last_week = today_date - timedelta(days=7)
                                        end_of_last_week = today_date - timedelta(days=1)

                                        # 2. Проходим по всем группам
                                        groups = Group.query.all()
                                        for group in groups:
                                            # Считаем очки за прошлую неделю
                                            scores = db.session.query(
                                                SquadScoreLog.user_id,
                                                func.sum(SquadScoreLog.points).label('total')
                                            ).filter(
                                                SquadScoreLog.group_id == group.id,
                                                func.date(SquadScoreLog.created_at) >= start_of_last_week,
                                                func.date(SquadScoreLog.created_at) <= end_of_last_week
                                            ).group_by(SquadScoreLog.user_id).order_by(text('total DESC')).all()

                                            # 3. Рассылаем уведомления Топ-3
                                            for rank, (uid, score) in enumerate(scores[:3]):
                                                place = rank + 1
                                                medals = {1: "🥇", 2: "🥈", 3: "🥉"}

                                                title_msg = f"Итоги недели: {place} место! {medals.get(place, '')}"
                                                body_msg = f"Так держать! Вы набрали {score} баллов и заняли {place} место в отряде {group.name}."

                                                from notification_service import send_user_notification
                                                send_user_notification(
                                                    user_id=uid,
                                                    title=title_msg,
                                                    body=body_msg,
                                                    type='success',
                                                    data={"route": "/squad", "args": "stories"}  # Откроет сторис
                                                )

                                            # Уведомление для остальных (чтобы зашли посмотреть сторис)
                                            for uid, score in scores[3:]:
                                                from notification_service import send_user_notification
                                                send_user_notification(
                                                    user_id=uid,
                                                    title="Итоги недели подведены 📊",
                                                    body=f"Посмотрите результаты битвы в отряде {group.name}!",
                                                    type='info',
                                                    data={"route": "/squad", "args": "stories"}
                                                )

                db.session.commit()
            except Exception:
                db.session.rollback()
            finally:
                db.session.remove()
                time_mod.sleep(60)

def create_app():
    app = Flask(__name__)

    with app.app_context():
        # Автозапуск напоминалок по приёмам пищи
        start_meal_scheduler(app)
        start_streak_scheduler(app) # <-- Добавлено

    return app

def get_effective_user_settings(u):
    from models import UserSettings, db
    s = getattr(u, "settings", None)
    if s is None:
        # создаём и сразу наполняем значениями из User (если там уже выставлено)
        s = UserSettings(
            user_id=u.id,
            telegram_notify_enabled=bool(getattr(u, "telegram_notify_enabled", False)),
            notify_trainings=bool(getattr(u, "notify_trainings", False)),
            notify_subscription=bool(getattr(u, "notify_subscription", False)),
            notify_meals=bool(getattr(u, "notify_meals", False)),
            meal_timezone="Asia/Almaty",  # ← дефолт

        )
        db.session.add(s)
        db.session.commit()
    return s
def start_training_notifier():
    global _notifier_started
    if _notifier_started:
        return
    _notifier_started = True
    if os.getenv("ENABLE_TRAINING_NOTIFIER", "1") == "1":
        th = threading.Thread(target=_notification_worker, daemon=True)
        th.start()

def _ensure_column(table, column, ddl):
    # инспектору передаём «сырое» имя (без кавычек), он сам разберётся
    insp = inspect(db.engine)
    cols = [c['name'] for c in insp.get_columns(table)]
    if column not in cols:
        # но в самом SQL-выражении имена нужно корректно квотировать под конкретный диалект
        preparer = db.engine.dialect.identifier_preparer
        table_q = preparer.quote(table)     # например -> "user"
        column_q = preparer.quote(column)   # например -> "sex"
        with db.engine.connect() as con:
            con.execute(text(f'ALTER TABLE {table_q} ADD COLUMN {column_q} {ddl}'))


def _auto_migrate_diet_schema():
    insp = inspect(db.engine)
    # Создадим недостающие таблицы по моделям (включая WeightLog)
    db.create_all()

    # === Новое поле для фиксации веса ===
    _ensure_column("user", "start_weight", "FLOAT")

    _ensure_column("user", "height", "INTEGER")  # <--- Добавлено

    # === Новые поля пользователя для визуализаций ===
    _ensure_column("user", "sex", "TEXT DEFAULT 'male'")
    _ensure_column("user", "face_consent", "BOOLEAN DEFAULT FALSE")

    _ensure_column("user", "streak_nutrition", "INTEGER DEFAULT 0")
    _ensure_column("user", "streak_activity", "INTEGER DEFAULT 0")
    _ensure_column("user", "target_calories", "INTEGER")  # <--- Smart Target
    _ensure_column("meal_logs", "is_flagged", "BOOLEAN DEFAULT FALSE")
    _ensure_column("meal_logs", "created_at", "TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP)")

    # (опционально) Заполнить created_at там где NULL
    try:
        with db.engine.connect() as con:
            con.execute(text("UPDATE meal_logs SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
    except Exception as e:
        print(f"[auto-migrate] backfill created_at failed: {e}")


# ------------------ ONBOARDING API ------------------
def _auto_migrate_onboarding_schema():
    insp = inspect(db.engine)
    # Создадим недостающие таблицы по моделям
    db.create_all()

    # === Новые поля пользователя для визуализаций ===
    _ensure_column("user", "sex", "TEXT DEFAULT 'male'")
    _ensure_column("user", "face_consent", "BOOLEAN DEFAULT FALSE")

    # === НОВОЕ ПОЛЕ ДЛЯ ТУРА ===
    _ensure_column("user", "onboarding_complete", "BOOLEAN DEFAULT FALSE")

    # === НОВОЕ ПОЛЕ ДЛЯ ОНБОРДИНГА V2 (ПО ТЗ) ===
    _ensure_column("user", "onboarding_v2_complete", "BOOLEAN DEFAULT FALSE")
    _ensure_column("user", "scanner_onboarding_seen", "BOOLEAN DEFAULT FALSE")

    # --- НОВАЯ СТРОКА ---
    _ensure_column("user", "fcm_device_token", "TEXT")  # Добавляем колонку для FCM
    # ---

    # === Поля для верификации почты ===
    _ensure_column("user", "verification_code", "TEXT")
    _ensure_column("user", "verification_code_expires_at", "TIMESTAMP")
    _ensure_column("user", "is_verified", "BOOLEAN DEFAULT FALSE")

    # === ВАЖНО: meal_logs нужные поля ===
    _ensure_column("meal_logs", "image_path", "TEXT")

with app.app_context():
    # Мини-миграции для новых полей в user
    _auto_migrate_diet_schema()
    _auto_migrate_onboarding_schema()

    # Запускаем фоновые задачи ТОЛЬКО после инициализации БД

    # (Мы убрали try/except отсюда и перенесли вызов ниже)

    # Запускаем автогенерацию диет один раз (не в мастер-процессе reloader’a)
    import os as _os

    if _os.environ.get("WERKZEUG_RUN_MAIN") == "true":

        # --- ИЗМЕНЕНИЕ: ПЕРЕМЕСТИЛИ ПЛАНИРОВЩИК ЕДЫ СЮДА ---
        try:
            start_meal_scheduler(app)
            # (Логгирование будет в самой функции)
        except Exception as e:
            print(f"[meal_scheduler] scheduler error: {e}")  # <-- Добавили лог ошибки

    start_training_notifier()



def send_email_code(to_email, code):
    sender_email = os.getenv("MAIL_USERNAME")
    sender_password = os.getenv("MAIL_PASSWORD")
    smtp_server = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("MAIL_PORT", 587))

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = to_email
    msg['Subject'] = "Код подтверждения Sola"

    body = f"Ваш код: {code}\n\nДействителен 10 минут."
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, to_email, text)
        server.quit()
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def calculate_age(born):
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

# ------------------ TRAININGS API ------------------

def _parse_date_yyyy_mm_dd(s: str) -> date:
    try:
        y, m, d = map(int, s.split('-'))
        return date(y, m, d)
    except Exception:
        abort(400, description="Некорректная дата (ожидается YYYY-MM-DD)")

def _parse_hh_mm(s: str):
    try:
        hh, mm = map(int, s.split(':'))
        return dt_time(hh, mm)
    except Exception:
        abort(400, description="Некорректное время (ожидается HH:MM)")

def _validate_meeting_link(url: str):
    url = (url or "").strip()
    try:
        u = urlparse(url)
        if u.scheme in ("http", "https") and u.netloc:
            return url
    except Exception:
        pass
    abort(400, description="Некорректная ссылка на занятие (ожидается http/https)")

def _month_bounds(yyyy_mm: str):
    try:
        y, m = map(int, yyyy_mm.split('-'))
        start = date(y, m, 1)
    except Exception:
        abort(400, description="Некорректный параметр month (ожидается YYYY-MM)")
    if m == 12:
        next_month = date(y+1, 1, 1)
    else:
        next_month = date(y, m+1, 1)
    end = next_month - timedelta(days=1)
    return start, end


@app.route('/trainings')
def trainings_page():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    u = get_current_user()
    return render_template('trainings.html', is_trainer=bool(u and u.is_trainer), me_id=(u.id if u else None))


@app.route('/api/trainings', methods=['GET'])
def list_trainings():
    if not session.get('user_id'):
        abort(401)

    month = request.args.get('month')
    requested_group_id = request.args.get('group_id')  # Получаем ID группы из запроса

    if not month:
        today = date.today()
        month = f"{today.year:04d}-{today.month:02d}"
    start, end = _month_bounds(month)

    me = get_current_user()
    me_id = me.id if me else None

    # --- ЛОГИКА ФИЛЬТРАЦИИ ---
    query = Training.query.options(subqueryload(Training.signups)) \
        .filter(Training.date >= start, Training.date <= end)

    if requested_group_id:
        # Сценарий А: Запрошена конкретная группа (внутри Squads)
        # (Тут можно добавить проверку, имеет ли юзер право смотреть эту группу)
        query = query.filter(Training.group_id == int(requested_group_id))
    else:
        # Сценарий Б: Главный календарь
        # Показываем:
        # 1. Публичные тренировки (group_id IS NULL)
        # 2. Тренировки групп, где я участник
        # 3. Тренировки группы, где я тренер

        # Собираем ID групп пользователя
        my_group_ids = [m.group_id for m in GroupMember.query.filter_by(user_id=me.id).all()]

        # Если я тренер, добавляем мою группу
        if me.own_group:
            my_group_ids.append(me.own_group.id)

        if my_group_ids:
            # Публичные ИЛИ Мои группы
            query = query.filter(
                or_(
                    Training.group_id.is_(None),
                    Training.group_id.in_(my_group_ids)
                )
            )
        else:
            # Только публичные (если нет групп)
            query = query.filter(Training.group_id.is_(None))

    items = query.order_by(Training.date, Training.start_time).all()

    # 2. ГАРАНТИЯ: Отдельно получаем ID всех тренировок, на которые записан этот юзер
    # (код ниже остается почти без изменений, только фильтр по датам)
    my_signed_training_ids = set()
    if me_id:
        rows = db.session.query(TrainingSignup.training_id) \
            .join(Training) \
            .filter(TrainingSignup.user_id == me_id,
                    Training.date >= start,
                    Training.date <= end).all()
        my_signed_training_ids = {r[0] for r in rows}

    # 3. Собираем ответ
    data_list = []
    for t in items:
        d = t.to_dict(me_id)
        if t.id in my_signed_training_ids:
            d['is_signed_up_by_me'] = True
        data_list.append(d)

    resp = jsonify({"ok": True, "data": data_list})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route('/api/trainings/mine', methods=['GET'])
def my_trainings():
    u = get_current_user()
    if not u:
        abort(401)
    if not u.is_trainer:
        abort(403)
    items = Training.query.filter_by(trainer_id=u.id)\
                          .order_by(Training.date.desc(), Training.start_time).all()
    return jsonify({"ok": True, "data": [t.to_dict(u.id) for t in items]})

@app.route('/api/trainings', methods=['POST'])
def create_training():
    u = get_current_user()
    if not u:
        abort(401)
    if not u.is_trainer:
        abort(403, description="Доступ только для тренеров")

    data = request.get_json(force=True, silent=True) or {}

    dt = _parse_date_yyyy_mm_dd(data.get('date') or '')
    st = _parse_hh_mm(data.get('start_time') or '')
    et = _parse_hh_mm(data.get('end_time') or '')
    if et <= st:
        abort(400, description="Время окончания должно быть позже начала")

    meeting_link = _validate_meeting_link(data.get('meeting_link') or '')

    # Глобальная защита: в этот слот уже есть ЛЮБАЯ тренировка
    exists = Training.query.filter(Training.date == dt, Training.start_time == st).first()
    if exists:
        abort(409, description="На это время уже есть тренировка")

    t = Training(
        trainer_id=u.id,
        meeting_link=meeting_link,
        # опциональные поля (для совместимости)
        title=(data.get('title') or 'Онлайн-тренировка').strip() or "Онлайн-тренировка",
        description=data.get('description') or '',
        date=dt,
        start_time=st,
        end_time=et,
        location=(data.get('location') or '').strip(),
        capacity=int(data.get('capacity') or 10),
        is_public=bool(data.get('is_public')) if data.get('is_public') is not None else True
    )
    db.session.add(t)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # страхуемся на случай гонок по trainer_id uniq
        abort(409, description="На это время уже есть тренировка")

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/api/trainings/<int:tid>', methods=['PUT'])
def update_training(tid):
    u = get_current_user()
    if not u:
        abort(401)
    t = Training.query.get_or_404(tid)
    if t.trainer_id != u.id:
        abort(403)

    data = request.get_json(force=True, silent=True) or {}

    if 'meeting_link' in data:
        t.meeting_link = _validate_meeting_link(data.get('meeting_link') or '')

    if 'date' in data:
        t.date = _parse_date_yyyy_mm_dd(data.get('date') or '')
    if 'start_time' in data:
        t.start_time = _parse_hh_mm(data.get('start_time') or '')
    if 'end_time' in data:
        t.end_time = _parse_hh_mm(data.get('end_time') or '')
    if t.end_time <= t.start_time:
        abort(400, description="Время окончания должно быть позже начала")

    # опциональные поля — оставляем совместимость
    if 'title' in data:
        title = (data.get('title') or '').strip()
        t.title = title or "Онлайн-тренировка"
    if 'description' in data:
        t.description = data.get('description') or ''
    if 'location' in data:
        t.location = (data.get('location') or '').strip()
    if 'capacity' in data:
        try:
            t.capacity = int(data.get('capacity') or 10)
        except Exception:
            abort(400, description="Некорректная вместимость")
    if 'is_public' in data:
        t.is_public = bool(data.get('is_public'))

    # Глобальная защита: проверяем конфликт по дата+старт (кроме самой записи)
    conflict = Training.query.filter(
        Training.id != t.id,
        Training.date == t.date,
        Training.start_time == t.start_time
    ).first()
    if conflict:
        abort(409, description="На это время уже есть тренировка")

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, description="На это время уже есть тренировка")

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/api/trainings/<int:tid>', methods=['DELETE'])
def delete_training(tid):
    u = get_current_user()
    if not u:
        abort(401)
    t = Training.query.get_or_404(tid)
    if t.trainer_id != u.id:
        abort(403)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})

# ------------------ UTILS ------------------
@app.context_processor
def inject_flags():
    u = get_current_user()
    return dict(is_trainer_user=bool(u and u.is_trainer))

@app.context_processor
def utility_processor():
    def get_bmi_category(bmi):
        if bmi is None:
            return ""
        if bmi < 18.5:
            return "Недостаточный вес"
        elif bmi < 25:
            return "Норма"
        elif bmi < 30:
            return "Избыточный вес"
        else:
            return "Ожирение"

    return dict(
        get_bmi_category=get_bmi_category,
        calculate_age=calculate_age,  # <-- теперь в шаблоне доступна
        today=date.today(),  # <-- и переменная today
    )


@app.context_processor
def inject_user():
    return {'current_user': get_current_user()}

# NEW: глобальные флаги для помощи новичкам и наличия анализа тела
@app.context_processor
def inject_help_flags():
    u = get_current_user()

    # Есть ли уже анализы тела
    has_body_analysis = False
    if u:
        try:
            has_body_analysis = db.session.query(BodyAnalysis.id).filter_by(user_id=u.id).first() is not None
        except Exception:
            has_body_analysis = False

    # Новичок: либо нет анализов, либо профиль «моложе 7 дней».
    # Безопасно берём первую доступную дату: created_at / created / registered_at / updated_at.
    is_newbie = False
    if u:
        joined = (
            getattr(u, 'created_at', None)
            or getattr(u, 'created', None)
            or getattr(u, 'registered_at', None)
            or getattr(u, 'updated_at', None)
        )
        try:
            if joined:
                # поддержка date и datetime
                if isinstance(joined, date) and not isinstance(joined, datetime):
                    is_newbie = (date.today() - joined).days < 7
                else:
                    is_newbie = (datetime.now(UTC).date() - joined.date()).days < 7
        except Exception:
            # в крайнем случае ориентируемся только на отсутствие анализов
            is_newbie = False

    # если анализов нет — всё равно показываем кнопку помощи
    if not has_body_analysis:
        is_newbie = True

    return dict(show_help_button=is_newbie, has_body_analysis=has_body_analysis)

def _month_deltas(user):
    # Первый день месяца в виде datetime, чтобы сравнивать с BodyAnalysis.timestamp
    start_dt = datetime.combine(date.today().replace(day=1), dt_time.min)

    # Берём первый и последний анализ ТОЛЬКО за текущий месяц по timestamp
    first = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        BodyAnalysis.timestamp >= start_dt
    ).order_by(BodyAnalysis.timestamp.asc()).first()

    last = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        BodyAnalysis.timestamp >= start_dt
    ).order_by(BodyAnalysis.timestamp.desc()).first()

    fat_delta = 0.0
    muscle_delta = 0.0
    if first and last and first.id != last.id:
        try:
            fat_delta = float((last.fat_mass or 0) - (first.fat_mass or 0))
            muscle_delta = float((last.muscle_mass or 0) - (first.muscle_mass or 0))
        except Exception:
            pass
    return {"fat_delta": fat_delta, "muscle_delta": muscle_delta}

@app.context_processor
def inject_renewal_reminder():
    u = get_current_user()  # у тебя уже есть helper для текущего пользователя
    show = False
    summary = {"fat_delta": 0.0, "muscle_delta": 0.0}
    days_left = None
    if u and getattr(u, "subscription", None) and u.subscription.status == 'active' and u.subscription.end_date:
        days_left = (u.subscription.end_date - date.today()).days
        if days_left is not None and 0 < days_left <= 5:
            # показываем 1 раз в день
            last = u.renewal_reminder_last_shown_on
            if last != date.today():
                show = True
        summary = _month_deltas(u)
    return dict(renewal_reminder_due=show, monthly_summary=summary, subscription_days_left=days_left)

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    """Обработчик для ошибки 404 (страница не найдена)."""
    return render_template('errors/404.html'), 404

@app.errorhandler(403)
def forbidden_error(error):
    """Обработчик для ошибки 403 (доступ запрещен)."""
    return render_template('errors/403.html'), 403

@app.errorhandler(500)
def internal_error(error):
    """Обработчик для ошибки 500 (внутренняя ошибка сервера)."""
    # Важно откатить сессию, чтобы избежать "зависших" транзакций в БД
    db.session.rollback()
    return render_template('errors/500.html'), 500
# ------------------ ROUTES ------------------

@app.route('/')
def index():
    if session.get('user_id'):
        return redirect(url_for('profile'))
    return render_template('index.html')

# алиас для /index, чтобы не было дубля логики
@app.route('/index')
def index_alias():
    return redirect(url_for('index'))

@app.route('/instructions')
def instructions_page():
    # Можно прокинуть ?section=scales чтобы автоскроллить к «весам»
    section = request.args.get('section')
    return render_template('instructions.html', scroll_to=section)


@app.route('/api/app/profile_data')
@login_required
def app_profile_data():
    """
    Отдает один большой JSON со всеми данными,
    нужными для главной страницы профиля в приложении.
    """
    user = get_current_user()

    # --- 1. ИНИЦИАЛИЗАЦИЯ ВСЕХ ПЕРЕМЕННЫХ ---
    diet_data = None
    fat_loss_progress_data = None
    progress_checkpoints = []
    latest_analysis_data = None
    weight_progress = None

    coach_name = None
    squad_name = None

    if user.own_group:
        squad_name = user.own_group.name
        if user.own_group.trainer:
            coach_name = user.own_group.trainer.name
    else:
        # Безопасно берем первую группу
        membership = user.groups.first()
        if membership and membership.group:
            squad_name = membership.group.name
            if membership.group.trainer:
                coach_name = membership.group.trainer.name

    # Рассчитываем статус за последние 30 дней для календаря
    today = date.today()
    start_30_days = today - timedelta(days=30)

    # 1. Еда: считаем количество приемов пищи, СУММУ калорий и БЖУ
    meals_stats_query = db.session.query(
        MealLog.date,
        func.count(func.distinct(MealLog.meal_type)),  # 0: Кол-во приемов
        func.sum(MealLog.calories),  # 1: Калории
        func.sum(MealLog.protein),  # 2: Белки
        func.sum(MealLog.fat),  # 3: Жиры
        func.sum(MealLog.carbs)  # 4: Углеводы
    ).filter(
        MealLog.user_id == user.id,
        MealLog.date >= start_30_days,
        MealLog.date <= today
    ).group_by(MealLog.date).all()

    # Формируем словарь данных по еде
    meals_detailed_map = {}
    for row in meals_stats_query:
        d_str = row[0].strftime("%Y-%m-%d")
        meals_detailed_map[d_str] = {
            "count": row[1],
            "calories": int(row[2] or 0),
            "protein": int(row[3] or 0),
            "fat": int(row[4] or 0),
            "carbs": int(row[5] or 0)
        }

    # 2. Активность: шаги и сожженные калории
    step_goal = getattr(user, "step_goal", 10000) or 10000
    activity_stats_query = db.session.query(Activity).filter(
        Activity.user_id == user.id,
        Activity.date >= start_30_days,
        Activity.date <= today
    ).all()

    activity_detailed_map = {}
    for act in activity_stats_query:
        d_str = act.date.strftime("%Y-%m-%d")
        activity_detailed_map[d_str] = {
            "steps": act.steps or 0,
            "burned_kcal": act.active_kcal or 0,
            "distance_m": int((act.distance_km or 0) * 1000)
        }

    # Собираем общий словарь истории с полными данными
    calendar_history = {}
    for i in range(31):
        d = start_30_days + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")

        m_data = meals_detailed_map.get(d_str, {})
        a_data = activity_detailed_map.get(d_str, {})

        # Расчет прогресса для колец
        meal_progress = min(1.0, m_data.get("count", 0) * 0.25)

        steps = a_data.get("steps", 0)
        activity_progress = 0.0
        if step_goal > 0:
            activity_progress = min(1.0, steps / step_goal)

        calendar_history[d_str] = {
            # Проценты для UI
            "meal_progress": meal_progress,
            "activity_progress": activity_progress,
            # Реальные данные для Dashboard
            "calories": m_data.get("calories", 0),
            "protein": m_data.get("protein", 0),
            "fat": m_data.get("fat", 0),
            "carbs": m_data.get("carbs", 0),
            "steps": steps,
            "burned_kcal": a_data.get("burned_kcal", 0),
            "distance_m": a_data.get("distance_m", 0)
        }

    # --- СБОР ДАННЫХ ПОЛЬЗОВАТЕЛЯ ---
    show_popup = bool(getattr(user, 'show_welcome_popup', False))

    # Получаем статус последней заявки на доставку (если есть)
    latest_app = SubscriptionApplication.query.filter_by(user_id=user.id).order_by(
        SubscriptionApplication.created_at.desc()).first()
    delivery_status = latest_app.status if latest_app else None

    # --- ВЫЧИСЛЯЕМ ТЕКУЩИЙ ВЕС ДЛЯ USER_DATA ---
    # Логика: 1. WeightLog -> 2. BodyAnalysis -> 3. StartWeight
    current_weight_val = None

    # 1. Проверяем последний лог веса (Приоритет №1)
    last_weight_log = WeightLog.query.filter_by(user_id=user.id) \
        .order_by(WeightLog.date.desc(), WeightLog.created_at.desc()).first()

    if last_weight_log:
        current_weight_val = last_weight_log.weight
    else:
        # 2. Если нет лога, берем из последнего анализа
        _latest_analysis_temp = BodyAnalysis.query.filter_by(user_id=user.id).order_by(
            BodyAnalysis.timestamp.desc()).first()
        if _latest_analysis_temp:
            current_weight_val = _latest_analysis_temp.weight
        else:
            # 3. Если нет анализов, берем стартовый вес
            current_weight_val = user.start_weight

    user_data = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        # --- ВАЖНО: Добавляем вес и цель в основной объект пользователя ---
        "weight": current_weight_val,
        "weight_goal": user.weight_goal,
        # -----------------------------------------------------------------
        "height": user.height,
        "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
        "gender": user.sex,  # Фронтенд ищет ключ 'gender', а в БД поле называется 'sex'
        "has_subscription": bool(getattr(user, 'has_subscription', False)),
        "is_trainer": bool(getattr(user, 'is_trainer', False)),
        "avatar_filename": user.avatar.filename if user.avatar else None,
        "current_streak": getattr(user, "current_streak", 0),
        "streak_nutrition": getattr(user, "streak_nutrition", 0),
        "streak_activity": getattr(user, "streak_activity", 0),
        "scanner_onboarding_seen": bool(getattr(user, "scanner_onboarding_seen", False)),
        "calendar_history": calendar_history,
        "show_welcome_popup": show_popup,
        "step_goal": getattr(user, "step_goal", 10000),
        "delivery_status": delivery_status
        "is_new_squad_member": bool(getattr(user, "is_new_squad_member", False)),
        "squad_name": squad_name,
        "coach_name": coach_name
    }

    # --- 3. Данные о диете ---
    diet_obj = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()

    # Считаем динамическую цель
    dynamic_goal = get_dynamic_calorie_goal(user)

    if diet_obj:
        try:
            diet_data = {
                "id": diet_obj.id,
                "total_kcal": dynamic_goal,  # <--- СТАЛО: подставляем динамическую цель
                "protein": diet_obj.protein,
                "fat": diet_obj.fat,
                "carbs": diet_obj.carbs,
                "meals": {
                    "breakfast": json.loads(diet_obj.breakfast or "[]"),
                    "lunch": json.loads(diet_obj.lunch or "[]"),
                    "dinner": json.loads(diet_obj.dinner or "[]"),
                    "snack": json.loads(diet_obj.snack or "[]"),
                }
            }
        except Exception:
            diet_data = None  # Ошибка парсинга JSON

    # --- 4. Данные о прогрессе (BodyAnalysis) ---
    latest_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()

    if latest_analysis:
        calculated_fat_percentage = 0.0
        try:
            if latest_analysis.weight and latest_analysis.weight > 0 and latest_analysis.fat_mass:
                calculated_fat_percentage = (latest_analysis.fat_mass / latest_analysis.weight) * 100
        except Exception:
            pass

        latest_analysis_data = {
            'timestamp': latest_analysis.timestamp.isoformat() if latest_analysis.timestamp else None,
            'height': latest_analysis.height,
            'weight_kg': latest_analysis.weight,
            'muscle_mass_kg': latest_analysis.muscle_mass,
            'body_fat_percentage': calculated_fat_percentage,
            'body_water': latest_analysis.body_water,
            'protein_percentage': latest_analysis.protein_percentage,
            'skeletal_muscle_mass': latest_analysis.skeletal_muscle_mass,
            'visceral_fat_level': latest_analysis.visceral_fat_rating,
            'metabolism': latest_analysis.metabolism,
            'waist_hip_ratio': latest_analysis.waist_hip_ratio,
            'body_age': latest_analysis.body_age,
            'fat_mass_kg': latest_analysis.fat_mass,
            'bmi': latest_analysis.bmi,
            'fat_free_body_weight': latest_analysis.fat_free_body_weight
        }

    # Логика расчета прогресса жира (зависит от BodyAnalysis, может быть пустым)
    initial_analysis = db.session.get(BodyAnalysis, user.initial_body_analysis_id) if user.initial_body_analysis_id else None

    if initial_analysis and latest_analysis and latest_analysis.fat_mass and user.fat_mass_goal and initial_analysis.fat_mass is not None and user.fat_mass_goal is not None and initial_analysis.fat_mass > user.fat_mass_goal:
        try:
            initial_fat_mass = float(initial_analysis.fat_mass)
            current_fat_mass = latest_analysis.fat_mass
            goal_fat_mass = user.fat_mass_goal

            # --- ПРОГНОЗ ПОТЕРИ ЖИРА ---
            KCAL_PER_KG_FAT = 7700
            start_datetime = latest_analysis.timestamp
            today_date = date.today()

            meal_logs_since = MealLog.query.filter(
                MealLog.user_id == user.id,
                MealLog.date >= start_datetime.date()
            ).all()

            activity_logs_since = Activity.query.filter(
                Activity.user_id == user.id,
                Activity.date >= start_datetime.date()
            ).all()

            meals_map = {}
            for log in meal_logs_since:
                meals_map.setdefault(log.date, 0)
                meals_map[log.date] += log.calories

            activity_map = {log.date: log.active_kcal for log in activity_logs_since}

            total_accumulated_deficit = 0
            metabolism = latest_analysis.metabolism or 0
            delta_days = (today_date - start_datetime.date()).days

            if delta_days >= 0:
                for i in range(delta_days + 1):
                    current_day = start_datetime.date() + timedelta(days=i)
                    consumed = meals_map.get(current_day, 0)
                    burned_active = activity_map.get(current_day, 0)

                    if i == 0:
                        calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                            MealLog.user_id == user.id,
                            MealLog.date == current_day,
                            MealLog.created_at < start_datetime
                        ).scalar() or 0
                        consumed -= calories_before_analysis
                        burned_active = 0

                    daily_deficit = (metabolism + burned_active) - consumed
                    if daily_deficit > 0:
                        total_accumulated_deficit += daily_deficit

            estimated_burned_kg = total_accumulated_deficit / KCAL_PER_KG_FAT
            current_fat_mass = current_fat_mass - estimated_burned_kg

            total_fat_to_lose_kg = initial_fat_mass - goal_fat_mass
            fat_lost_so_far_kg = initial_fat_mass - current_fat_mass

            percentage = 0
            if total_fat_to_lose_kg > 0:
                percentage = (fat_lost_so_far_kg / total_fat_to_lose_kg) * 100

            fat_loss_progress_data = {
                'percentage': min(100, max(0, percentage)),
                'burned_kg': fat_lost_so_far_kg,
                'total_to_lose_kg': total_fat_to_lose_kg,
                'initial_kg': initial_fat_mass,
                'goal_kg': goal_fat_mass,
                'current_kg': current_fat_mass
            }
        except Exception as e:
            print(f"Error calculating fat loss: {e}")
            fat_loss_progress_data = None

        # Чекпоинты прогресса
        all_analyses_for_progress_data = []
        if user.initial_body_analysis_id:
            initial_analysis_for_chart = db.session.get(BodyAnalysis, user.initial_body_analysis_id)
            if initial_analysis_for_chart:
                analyses_objects = BodyAnalysis.query.filter(
                    BodyAnalysis.user_id == user.id,
                    BodyAnalysis.timestamp >= initial_analysis_for_chart.timestamp
                ).order_by(BodyAnalysis.timestamp.asc()).all()

                all_analyses_for_progress_data = [
                    {
                        "timestamp": analysis.timestamp.isoformat(),
                        "fat_mass": analysis.fat_mass
                    }
                    for analysis in analyses_objects
                ]

        if fat_loss_progress_data and all_analyses_for_progress_data and fat_loss_progress_data['total_to_lose_kg'] > 0:
            initial_fat = fat_loss_progress_data['initial_kg']
            total_to_lose = fat_loss_progress_data['total_to_lose_kg']

            for i, analysis_data in enumerate(all_analyses_for_progress_data):
                current_fat_at_point = analysis_data.get('fat_mass') or initial_fat
                fat_lost_at_point = initial_fat - current_fat_at_point
                percentage_at_point = (fat_lost_at_point / total_to_lose) * 100

                progress_checkpoints.append({
                    "number": i + 1,
                    "percentage": min(100, max(0, percentage_at_point))
                })

    # --- 5. РАСЧЕТ ПРОГРЕССА ВЕСА (НОВАЯ ЛОГИКА: WeightLog + user.start_weight) ---
    # Мы больше не зависим от initial_body_analysis_id для веса
    if user.start_weight and user.weight_goal:
        start_w = user.start_weight
        goal_w = user.weight_goal

        # Получаем последний лог веса из таблицы WeightLog
        last_log = WeightLog.query.filter_by(user_id=user.id)\
            .order_by(WeightLog.date.desc(), WeightLog.created_at.desc()).first()

        # Если логов нет (только зарегистрировался), используем стартовый вес
        curr_w = last_log.weight if last_log else start_w

        total_dist = abs(start_w - goal_w)
        done_dist = abs(start_w - curr_w)

        pct = 0
        if total_dist > 0.1:
            # Проверяем, движемся ли мы к цели
            is_weight_loss = start_w > goal_w

            if is_weight_loss:
                 # Худеем
                 if curr_w <= start_w:
                     pct = (done_dist / total_dist) * 100
                 else:
                     pct = 0 # Вес вырос, прогресс 0
            else:
                 # Набираем
                 if curr_w >= start_w:
                     pct = (done_dist / total_dist) * 100
                 else:
                     pct = 0 # Вес упал, прогресс 0

        weight_progress = {
            "start_weight": start_w,
            "current_weight": curr_w,
            "goal_weight": goal_w,
            "percent": min(100, max(0, pct))
        }

    return jsonify({
            "ok": True,
            "data": {
                "user": user_data,
                "diet": diet_data,
                "weight_progress": weight_progress,
                "fat_loss_progress": fat_loss_progress_data,
                "progress_checkpoints": progress_checkpoints,
                "latest_analysis": latest_analysis_data
            }
        })

@app.route('/api/app/update_step_goal', methods=['POST'])
@login_required
def app_update_step_goal():
    user = get_current_user()
    data = request.get_json(force=True, silent=True) or {}

    try:
        new_goal = int(data.get('step_goal', 10000))
        if new_goal > 0:
            user.step_goal = new_goal
            db.session.commit()
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": "Invalid goal"}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/app/meals/today')
@login_required
def app_get_today_meals():
    """ API-версия /api/meals/today/<chat_id> , но использующая сессию """
    user = get_current_user()
    logs = MealLog.query.filter_by(user_id=user.id, date=date.today()).order_by(MealLog.created_at).all()
    total_calories = sum(m.calories for m in logs)

    meal_data = [
        {
            'meal_type': m.meal_type,
            'name': m.name or "Без названия",
            'calories': m.calories,
            'protein': m.protein,
            'fat': m.fat,
            'carbs': m.carbs
        }
        for m in logs
    ]

    # Добавим целевые БЖУ из диеты
    diet_macros = {"protein": 0, "fat": 0, "carbs": 0}
    diet = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()

    # Используем нашу новую функцию
    diet_calories = get_dynamic_calorie_goal(user)

    if diet:
        # diet_calories = diet.total_kcal or 2500  <-- ЭТУ СТРОКУ УБИРАЕМ/КОММЕНТИРУЕМ
        diet_macros = {
            "protein": diet.protein or 0,
            "fat": diet.fat or 0,
            "carbs": diet.carbs or 0
        }

    return jsonify({
        "meals": meal_data,
        "total_calories": total_calories,
        "diet_total_calories": diet_calories,
        "diet_macros": diet_macros
    }), 200


@app.route('/api/app/log_meal', methods=['POST'])
@login_required
def app_log_meal():
    """ API-версия /api/log_meal , но использующая сессию """
    user = get_current_user()
    data = request.get_json()

    # --- ИСПРАВЛЕНИЕ: Дата из запроса или Алматы ---
    date_str = data.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
    else:
        # Если даты нет, берем "сегодня" по Алматы
        target_date = datetime.now(ZoneInfo("Asia/Almaty")).date()
    # -----------------------------------------------

    # Ищем существующий (для обновления)
    meal = MealLog.query.filter_by(
        user_id=user.id,
        date=target_date,  # Используем target_date
        meal_type=data['meal_type']
    ).first()

    if not meal:
        # Создаем новый
        meal = MealLog(
            user_id=user.id,
            date=target_date,  # Используем target_date
            meal_type=data['meal_type']
        )

    meal.name = data.get('name', 'Без названия')
    meal.calories = int(data.get('calories', 0))
    meal.protein = float(data.get('protein', 0.0))
    meal.fat = float(data.get('fat', 0.0))
    meal.carbs = float(data.get('carbs', 0.0))
    meal.analysis = data.get('analysis') or ""

    try:
        db.session.add(meal)

        # 1. Пересчитываем стрик
        recalculate_streak(user)

        # --- AI FEED: STREAK MILESTONES ---
        s = getattr(user, 'current_streak', 0)
        if s > 0 and (s == 3 or s % 7 == 0):
            trigger_ai_feed_post(user, f"Участник держит стрик питания уже {s} дней подряд!")
        # ----------------------------------

        # --- SQUAD SCORING: FOOD LOG (10 pts) ---
        today = date.today()
        today_meals_query = db.session.query(MealLog.meal_type).filter_by(user_id=user.id, date=today).all()
        logged_types = {m[0] for m in today_meals_query}
        logged_types.add(data['meal_type'])

        required = {'breakfast', 'lunch', 'dinner'}
        if required.issubset(logged_types):
            existing_score = SquadScoreLog.query.filter(
                SquadScoreLog.user_id == user.id,
                SquadScoreLog.category == 'food_log',
                func.date(SquadScoreLog.created_at) == today
            ).first()

            if not existing_score:
                award_squad_points(user, 'food_log', 10, "Дневной рацион выполнен")
        # ----------------------------------------

        # --- ПРОВЕРКА АЧИВОК ---
        check_all_achievements(user)

        # Проверяем новые ачивки для поста в ленту
        try:
            if hasattr(UserAchievement, 'created_at'):
                recent_achievements = UserAchievement.query.filter(
                    UserAchievement.user_id == user.id,
                    UserAchievement.created_at >= datetime.now(UTC) - timedelta(seconds=15)
                ).all()

                for ach in recent_achievements:
                    meta = ACHIEVEMENTS_METADATA.get(ach.slug)
                    if meta:
                        title = meta['title']
                        trigger_ai_feed_post(user, f"Получено новое достижение: «{title}»!")
        except Exception as e:
            print(f"Error posting achievement feed: {e}")

        # -----------------------
        # ВАЖНО: Эти строки должны быть на уровне с try (не внутри except)
        db.session.commit()

        # ANALYTICS: Meal Logged (Backend backup)
        try:
            amplitude.track(BaseEvent(
                event_type="Meal Logged",
                user_id=str(user.id),
                event_properties={
                    "meal_type": data['meal_type'],
                    "calories": int(data.get('calories', 0)),
                    "has_analysis": bool(data.get('analysis'))
                }
            ))
        except Exception as e:
            print(f"Amplitude error: {e}")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error in log_meal: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/app/activity/today')
@login_required
def app_activity_today():
    """ API-версия /api/activity/today/<chat_id> , но использующая сессию """
    user = get_current_user()
    a = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    if not a:
        return jsonify({"present": False}), 404  # 404 - корректный ответ "не найдено"

    return jsonify({
        "present": True,
        "steps": a.steps or 0,
        "active_kcal": a.active_kcal or 0,
        "resting_kcal": a.resting_kcal or 0,
        "distance_km": a.distance_km or 0.0
    })


@app.route('/api/app/telegram_code')
@login_required
def app_generate_telegram_code():
    """ API-версия /generate_telegram_code , но возвращает JSON """
    user = get_current_user()
    code = ''.join(random.choices(string.digits, k=8))
    user.telegram_code = code
    db.session.commit()
    return jsonify({'code': code})


@app.route('/api/app/analyze_meal_photo', methods=['POST'])
@login_required
def app_analyze_meal_photo():
    """
    Защищенная сессией версия /analyze_meal_photo
    Она просто вызывает существующую функцию, но требует @login_required
    """
    return analyze_meal_photo()

@app.route('/api/mark_scanner_seen', methods=['POST'])
@login_required
def mark_scanner_seen():
    try:
        user = get_current_user()
        user.scanner_onboarding_seen = True
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post('/api/login')
def api_login():
    # 1. Получаем данные из запроса
    data = request.get_json(force=True, silent=True) or {}
    email_input = (data.get('email') or '').strip()
    password = (data.get('password') or '').strip()

    if not email_input or not password:
        return jsonify({"ok": False, "error": "MISSING_CREDENTIALS"}), 400

    # 2. Ищем пользователя (без учета регистра)
    user = User.query.filter(func.lower(User.email) == email_input.casefold()).first()

    # 3. Проверяем пароль
    if user and bcrypt.check_password_hash(user.password, password):
        # 4. Создаем сессию
        session['user_id'] = user.id

        # 5. Возвращаем успешный ответ с данными пользователя
        # (Структура совпадает с api_me и api_google_login)
        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "has_subscription": bool(getattr(user, 'has_subscription', False)),
                "is_trainer": bool(getattr(user, 'is_trainer', False)),
                "onboarding_complete": bool(getattr(user, 'onboarding_complete', False)),
                "onboarding_v2_complete": bool(getattr(user, 'onboarding_v2_complete', False))
            }
        }), 200

    # 6. Если неверный логин или пароль
    return jsonify({"ok": False, "error": "INVALID_CREDENTIALS"}), 401


@app.post('/api/login/google')
def api_google_login():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('id_token')

    if not token:
        return jsonify({"ok": False, "error": "TOKEN_MISSING"}), 400

    try:
        GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
        id_info = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)

        email = id_info.get('email')
        name = id_info.get('name')

        if not email:
            return jsonify({"ok": False, "error": "EMAIL_NOT_PROVIDED_BY_GOOGLE"}), 400

        user = User.query.filter(func.lower(User.email) == email.casefold()).first()

        if not user:
            # Пользователь не найден. Возвращаем 404 и данные для онбординга.
            return jsonify({
                "ok": False,
                "error": "USER_NOT_FOUND",
                "google_email": email,
                "google_name": name
            }), 404

        # Логиним пользователя (создаем сессию)
        session['user_id'] = user.id

        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "has_subscription": bool(getattr(user, 'has_subscription', False)),
                "is_trainer": bool(getattr(user, 'is_trainer', False)),
            }
        }), 200


    except ValueError as e:

        return jsonify({"ok": False, "error": f"INVALID_TOKEN: {str(e)}"}), 401

    except Exception as e:

        return jsonify({"ok": False, "error": f"SERVER_ERROR: {str(e)}"}), 500

@app.post('/api/login/apple')
def api_apple_login():

        data = request.get_json(force=True, silent=True) or {}

        token = data.get('id_token')

        # Apple присылает имя только при ПЕРВОМ входе. Фронтенд должен передать его нам.

        first_name = data.get('first_name')

        last_name = data.get('last_name')

        full_name = None

        if first_name or last_name:
            full_name = f"{first_name or ''} {last_name or ''}".strip()

        if not token:
            return jsonify({"ok": False, "error": "TOKEN_MISSING"}), 400

        try:

            # Декодируем токен Apple.

            # Внимание: Для полной безопасности здесь нужно валидировать подпись через Apple Public Keys.

            # Для базовой реализации мы доверяем токену, декодируя payload (verify=False),

            # полагая, что HTTPS соединение с приложением защищено.

            decoded = jwt.decode(token, options={"verify_signature": False})

            email = decoded.get('email')

            if not email:
                return jsonify({"ok": False, "error": "EMAIL_NOT_PROVIDED_BY_APPLE"}), 400

            user = User.query.filter(func.lower(User.email) == email.casefold()).first()

            if not user:
                # Пользователь не найден. Возвращаем 404 и данные для онбординга.

                return jsonify({

                    "ok": False,

                    "error": "USER_NOT_FOUND",

                    "apple_email": email,

                    "apple_name": full_name

                }), 404

            # Логиним пользователя (создаем сессию)

            session['user_id'] = user.id

            return jsonify({

                "ok": True,

                "user": {

                    "id": user.id,

                    "name": user.name,

                    "email": user.email,

                    "has_subscription": bool(getattr(user, 'has_subscription', False)),

                    "is_trainer": bool(getattr(user, 'is_trainer', False)),

                }

            }), 200


        except Exception as e:

            return jsonify({"ok": False, "error": f"APPLE_AUTH_ERROR: {str(e)}"}), 500

@app.route('/api/register_google', methods=['POST'])
def api_register_google():
    """
    Регистрация с использованием Google ID Token.
    Сохраняет рост и дату рождения.
    """
    token = request.form.get('id_token')

    try:
        GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
        id_info = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = id_info.get('email')
    except Exception as e:
        return jsonify({"ok": False, "errors": ["INVALID_GOOGLE_TOKEN"]}), 400

    name = request.form.get('name', '').strip()
    date_str = request.form.get('date_of_birth', '').strip()
    sex = request.form.get('sex', 'male').strip().lower()
    height = request.form.get('height')  # <--- Получаем рост
    face_consent = request.form.get('face_consent', 'false').lower() == 'true'
    file = request.files.get('avatar')

    # Генерируем пароль
    import secrets
    random_pw = secrets.token_urlsafe(16)
    hashed_pw = bcrypt.generate_password_hash(random_pw).decode('utf-8')

    # Аватар
    avatar_file_id = None
    if file and file.filename:
        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext in {'jpg', 'jpeg', 'png', 'webp'}:
            unique_filename = f"avatar_reg_{uuid.uuid4().hex}.{ext}"
            file_data = file.read()
            new_file = UploadedFile(
                filename=unique_filename,
                content_type=file.mimetype,
                data=file_data,
                size=len(file_data)
            )
            db.session.add(new_file)
            db.session.flush()
            avatar_file_id = new_file.id

        # Парсинг даты
        try:
            date_of_birth = _parse_date_yyyy_mm_dd(date_str)
        except:
            return jsonify({"ok": False, "errors": ["DATE_INVALID"]}), 400

        # --- ДОБАВИТЬ ЭТОТ БЛОК ---
        user_height = None
        if height:
            try:
                user_height = int(float(height))
            except:
                pass
        # --------------------------

        # 1. Создаем пользователя
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            sex=sex,
            height=user_height,  # Теперь переменная user_height существует
            face_consent=face_consent,
            avatar_file_id=avatar_file_id
        )
    db.session.add(user)
    db.session.flush()  # Получаем user.id

    if avatar_file_id:
        new_file.user_id = user.id

    # 2. Создаем запись анализа с РОСТОМ
    if height:
        try:
            height_val = float(height)
            # Создаем первую запись анализа, чтобы рост сразу отобразился в профиле
            analysis = BodyAnalysis(
                user_id=user.id,
                height=height_val,
                date=datetime.utcnow()
            )
            db.session.add(analysis)
        except:
            pass  # Если рост не число, игнорируем

    db.session.commit()
    session['user_id'] = user.id

    track_event('signup_completed', user.id, {"method": "google", "sex": sex})

    return jsonify({
        "ok": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            # Возвращаем дату строкой, чтобы Flutter мог её распарсить
            "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None
        }
    }), 201


@app.route('/api/register_apple', methods=['POST'])
def api_register_apple():
    """
    Регистрация с использованием Apple ID Token.
    """
    token = request.form.get('id_token')

    try:
        # Декодируем токен, чтобы убедиться в email
        decoded = jwt.decode(token, options={"verify_signature": False})
        email = decoded.get('email')
        if not email:
            return jsonify({"ok": False, "errors": ["INVALID_APPLE_TOKEN_NO_EMAIL"]}), 400
    except Exception as e:
        return jsonify({"ok": False, "errors": [f"INVALID_APPLE_TOKEN: {e}"]}), 400

    name = request.form.get('name', '').strip()
    date_str = request.form.get('date_of_birth', '').strip()
    sex = request.form.get('sex', 'male').strip().lower()
    height = request.form.get('height')
    face_consent = request.form.get('face_consent', 'false').lower() == 'true'
    file = request.files.get('avatar')

    # Генерируем случайный пароль
    import secrets
    random_pw = secrets.token_urlsafe(16)
    hashed_pw = bcrypt.generate_password_hash(random_pw).decode('utf-8')

    # Аватар
    avatar_file_id = None
    if file and file.filename:
        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext in {'jpg', 'jpeg', 'png', 'webp'}:
            unique_filename = f"avatar_apple_{uuid.uuid4().hex}.{ext}"
            file_data = file.read()
            new_file = UploadedFile(
                filename=unique_filename,
                content_type=file.mimetype,
                data=file_data,
                size=len(file_data)
            )
            db.session.add(new_file)
            db.session.flush()
            avatar_file_id = new_file.id

        # Парсинг даты
        try:
            date_of_birth = _parse_date_yyyy_mm_dd(date_str)
        except:
            return jsonify({"ok": False, "errors": ["DATE_INVALID"]}), 400

        # Проверяем, не занят ли email (на всякий случай)
        if User.query.filter(func.lower(User.email) == email.casefold()).first():
            return jsonify({"ok": False, "errors": ["EMAIL_EXISTS"]}), 400

        # --- ДОБАВИТЬ ЭТОТ БЛОК ---
        user_height = None
        if height:
            try:
                user_height = int(float(height))
            except:
                pass
        # --------------------------

        # 1. Создаем пользователя
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            sex=sex,
            height=user_height,  # Теперь переменная user_height существует
            face_consent=face_consent,
            avatar_file_id=avatar_file_id
        )
    db.session.add(user)
    db.session.flush()

    if avatar_file_id:
        new_file.user_id = user.id

    # 2. Создаем запись анализа с РОСТОМ
    if height:
        try:
            height_val = float(height)
            analysis = BodyAnalysis(
                user_id=user.id,
                height=height_val,
                date=datetime.utcnow()
            )
            db.session.add(analysis)
        except:
            pass

    db.session.commit()
    session['user_id'] = user.id

    track_event('signup_completed', user.id, {"method": "apple", "sex": sex})

    return jsonify({
        "ok": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None
        }
    }), 201


@app.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.get('/api/me')
def api_me():
    u = get_current_user()
    if not u:
        return jsonify({"ok": False}), 401
    return jsonify({
        "ok": True,
        "user": {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            # --- ДОБАВЛЕНО: возвращаем дату рождения и аватар ---
            "date_of_birth": u.date_of_birth.isoformat() if u.date_of_birth else None,
            "avatar_filename": u.avatar.filename if u.avatar else None,
            # ----------------------------------------------------
            "has_subscription": bool(getattr(u, 'has_subscription', False)),
            "is_trainer": bool(getattr(u, 'is_trainer', False)),
            'onboarding_complete': bool(getattr(u, 'onboarding_complete', False)),
            'onboarding_v2_complete': bool(getattr(u, 'onboarding_v2_complete', False)),
            'squad_status': getattr(u, 'squad_status', 'none'),
            "height": u.height,
            "gender": u.sex,
            "current_streak": getattr(u, "current_streak", 0),
            "streak_nutrition": getattr(u, "streak_nutrition", 0),
            "streak_activity": getattr(u, "streak_activity", 0),
            # --- NEW SQUAD MEMBER FLAG ---
            "is_new_squad_member": bool(getattr(u, "is_new_squad_member", False)),
            "squad_name": u.own_group.name if u.own_group else (u.groups.first().group.name if u.groups.first() else None),
            "coach_name": u.own_group.trainer.name if u.own_group and u.own_group.trainer else (u.groups.first().group.trainer.name if u.groups.first() and u.groups.first().group.trainer else None),
        }
    })


@app.post('/api/register')
def api_register():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    password = (data.get('password') or '').strip()

    # --- ДОБАВЛЕНО ---
    height = data.get('height')
    # -----------------

    errors = []
    if not name: errors.append("NAME_REQUIRED")
    if not email: errors.append("EMAIL_REQUIRED")
    if not password or len(password) < 6: errors.append("PASSWORD_SHORT")

    if User.query.filter(func.lower(User.email) == email.casefold()).first():
        errors.append("EMAIL_EXISTS")

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')

    # Создаем пользователя (добавляем рост, если модель User поддерживает поле height)
    user = User(
        name=name,
        email=email,
        password=hashed_pw,
        height=float(height) if height else None  # Сохраняем в профиль
    )
    db.session.add(user)
    db.session.commit()  # Коммитим, чтобы получить user.id

    # --- ДОБАВЛЕНО: Создаем запись в BodyAnalysis ---
    if height:
        try:
            analysis = BodyAnalysis(
                user_id=user.id,
                height=float(height),
                timestamp=datetime.utcnow()
            )
            db.session.add(analysis)
            db.session.commit()
        except Exception as e:
            print(f"Error creating initial analysis: {e}")
    # -----------------------------------------------

    session['user_id'] = user.id
    return jsonify({"ok": True, "user": {"id": user.id, "name": user.name, "email": user.email}}), 201

# --- НОВЫЙ ЭНДПОИНТ ДЛЯ РЕГИСТРАЦИИ V2 (С АВАТАРОМ) ---
@app.route('/api/register_v2', methods=['POST'])
def api_register_v2():
    """
    Новый флоу регистрации (Этап 1).
    Принимает все данные, включая аватар, создает пользователя и логинит его.
    """
    errors = []

    # 1. Получаем данные из multipart/form-data
    try:
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        date_str = request.form.get('date_of_birth', '').strip()
        # Собираем sex, если он будет добавлен во флоу; иначе ставим заглушку
        sex = request.form.get('sex', 'male').strip().lower()
        face_consent = request.form.get('face_consent', 'false').lower() == 'true'
        file = request.files.get('avatar')

        # 2. Валидация
        if not name:
            errors.append("NAME_REQUIRED")
        if not email:
            errors.append("EMAIL_REQUIRED")
        if not password or len(password) < 6:
            errors.append("PASSWORD_SHORT")
        if User.query.filter(func.lower(User.email) == email.casefold()).first():
            errors.append("EMAIL_EXISTS")

        date_of_birth = None
        if date_str:
            try:
                date_of_birth = _parse_date_yyyy_mm_dd(date_str)
            except Exception:
                errors.append("DATE_INVALID")
        else:
            errors.append("DATE_REQUIRED")

        if sex not in ('male', 'female'):
            errors.append("SEX_INVALID")

        if not file or not file.filename:
            errors.append("AVATAR_REQUIRED")

        if errors:
            return jsonify({"ok": False, "errors": errors}), 400

        # 3. Сохраняем аватар
        avatar_file_id = None
        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in {'jpg', 'jpeg', 'png', 'webp'}:
            return jsonify({"ok": False, "errors": ["AVATAR_FORMAT_INVALID"]}), 400

        unique_filename = f"avatar_reg_{uuid.uuid4().hex}.{ext}"
        file_data = file.read()

        new_file = UploadedFile(
            filename=unique_filename,
            content_type=file.mimetype,
            data=file_data,
            size=len(file_data)
        )
        db.session.add(new_file)
        db.session.flush()  # Получаем ID файла
        avatar_file_id = new_file.id

        # 4. Создаем пользователя
        # 4. Создаем пользователя
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')

        # Обработка роста
        height_val = request.form.get('height')
        if height_val:
            try:
                height_val = int(float(height_val))
            except:
                height_val = None
        else:
            height_val = None

        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            sex=sex,
            height=height_val,  # <--- Сохраняем рост прямо в User
            face_consent=face_consent,
            avatar_file_id=avatar_file_id
        )
        db.session.add(user)
        db.session.flush()  # Получаем ID пользователя

        # 5. Привязываем ID пользователя к файлу
        new_file.user_id = user.id

        # --- ДОБАВЛЕНО: Создаем первую запись в истории замеров ---
        if height_val:
            try:
                analysis = BodyAnalysis(
                    user_id=user.id,
                    height=float(height_val),
                    timestamp=datetime.utcnow()
                )
                db.session.add(analysis)
            except Exception as e:
                print(f"Error creating initial analysis: {e}")
        # ----------------------------------------------------------

        db.session.commit()

        # 6. Логиним пользователя (создаем сессию)
        session['user_id'] = user.id

        # ANALYTICS: Sign Up Completed
        try:
            amplitude.track(BaseEvent(
                event_type="Sign Up Completed",
                user_id=str(user.id),
                event_properties={
                    "method": "email",
                    "has_avatar": True,
                    "sex": sex
                }
            ))
            # INTERNAL ANALYTICS
            track_event('signup_completed', user.id, {"method": "email", "sex": sex})
        except Exception as e:
            print(f"Amplitude error: {e}")

        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "avatar_filename": new_file.filename
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "errors": [f"SERVER_ERROR: {e}"]}), 500


# --- НОВЫЕ ЭНДПОИНТЫ ДЛЯ ОНБОРДИНГА V2 (ПОЛНОСТЬЮ ПЕРЕДЕЛАННЫЙ ФЛОУ) ---

@app.route('/api/onboarding/analyze_scales_photo', methods=['POST'])
@login_required
def analyze_scales_photo():
    """
    НОВЫЙ ФЛОУ (ЭТАП 2): Анализирует скриншот "умных весов".
    Возвращает найденные метрики. Если чего-то нет — возвращает null в поле.
    """
    file = request.files.get('file')
    user = get_current_user()
    if not file or not user:
        return jsonify({"success": False, "error": "Файл не загружен или вы не авторизованы."}), 400

    try:
        # 1. Конвертируем фото в base64
        file_bytes = file.read()
        base64_image = base64.b64encode(file_bytes).decode("utf-8")

        # 2. Вызываем GPT-4o
        response_metrics = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — фитнес-аналитик. Извлеки следующие параметры из фото анализа тела (bioimpedance):"
                        "height, weight, muscle_mass, muscle_percentage, body_water, protein_percentage, "
                        "skeletal_muscle_mass, visceral_fat_rating, metabolism, "
                        "waist_hip_ratio, body_age, fat_mass, bmi, fat_free_body_weight. "
                        "Верни СТРОГО JSON с найденными числовыми значениями. "
                        "Если какое-то значение не найдено, верни null."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Извлеки параметры из этого скриншота весов."}
                    ]
                }
            ],
            max_tokens=1000,
            response_format={"type": "json_object"}
        )
        content = response_metrics.choices[0].message.content.strip()
        result_metrics = json.loads(content)

        # 3. Попытка дополнить рост из профиля пользователя, если AI не нашел
        if not result_metrics.get('height'):
            if user.height:
                result_metrics['height'] = user.height
            # Иначе оставляем null, фронтенд спросит пользователя

        # 4. Добавляем пол (нужен для фронтенда)
        result_metrics['sex'] = user.sex

        # === ВАЖНОЕ ИЗМЕНЕНИЕ: МЫ НЕ ВОЗВРАЩАЕМ ОШИБКУ, ЕСЛИ НЕТ РОСТА ===
        # Мы проверяем только совсем пустой результат (если даже веса нет — тогда ошибка)
        if not result_metrics.get('weight') and not result_metrics.get('fat_mass'):
            return jsonify({
                "success": False,
                "error": "Не удалось распознать данные на фото. Попробуйте сделать более четкий снимок."
            }), 400

        # <--- ИСПРАВЛЕНИЕ: Сдвинули влево
        # Возвращаем JSON с метриками (какие-то поля могут быть null)
        track_event('scales_analyzed', user.id, {"success": True})
        return jsonify({"success": True, "metrics": result_metrics})

    except Exception as e:
        track_event('scales_analyzed_error', user.id, {"error": str(e)})
        return jsonify({"success": False, "error": f"Ошибка AI-анализа: {e}"}), 500


def _calculate_target_metrics(user: User, metrics_current: dict) -> dict:
    """
    Рассчитывает целевые показатели ("Точка Б") на основе переданного веса, роста и процента жира.
    """
    try:
        # Исправление: используем 'or', чтобы обработать случай, когда значение равно None
        height_cm = float(metrics_current.get("height") or 170)
        weight_curr = float(metrics_current.get("weight") or 70)
        height_m = height_cm / 100.0

        # Целевой вес на основе здорового ИМТ 21.5
        target_weight = 21.5 * (height_m * height_m)

        # Целевой процент жира: 15% для мужчин, 22% для женщин
        target_fat_pct = 0.22 if user.sex == 'female' else 0.15
        target_fat_mass = target_weight * target_fat_pct

        # Расчет сухой мышечной массы
        target_muscle_mass = target_weight * (1.0 - target_fat_pct - 0.13)

        return {
            "height_cm": height_cm,
            "weight_kg": round(target_weight, 1),
            "fat_mass": round(target_fat_mass, 1),
            "muscle_mass": round(target_muscle_mass, 1),
            "sex": user.sex,
            "fat_pct": target_fat_pct * 100,
            "muscle_pct": (target_muscle_mass / target_weight) * 100
        }
    except Exception as e:
        app.logger.error(f"[calculate_target_metrics] FAILED: {e}")
        return metrics_current.copy()


@app.route('/api/onboarding/generate_visualization', methods=['POST'])
@login_required
def onboarding_generate_visualization():
    """
    Генерирует визуализацию на основе трех показателей (рост, вес, жир) и фото в рост.
    """
    user = get_current_user()
    try:
        metrics_current = json.loads(request.form.get('metrics'))
        file = request.files.get('full_body_photo')
        full_body_photo_bytes = file.read()

        # --- ИЗМЕНЕНИЕ: Сохраняем фото в полный рост в профиль пользователя ---
        if file and full_body_photo_bytes:
            filename = secure_filename(file.filename) or "body_photo.jpg"
            unique_filename = f"body_{user.id}_{uuid.uuid4().hex}.jpg"

            new_file = UploadedFile(
                filename=unique_filename,
                content_type=file.mimetype or 'image/jpeg',
                data=full_body_photo_bytes,
                size=len(full_body_photo_bytes),
                user_id=user.id
            )
            db.session.add(new_file)
            db.session.flush()  # Чтобы получить id

            # Привязываем к пользователю (предполагаем наличие поля full_body_photo_id)
            user.full_body_photo_id = new_file.id
            db.session.commit()
        # ---------------------------------------------------------------------

        # Сохраняем "Точку А" на основе 3-х параметров
        analysis = BodyAnalysis(
            user_id=user.id,
            timestamp=datetime.now(UTC),
            height=metrics_current.get('height'),
            weight=metrics_current.get('weight'),
            fat_mass=metrics_current.get('fat_mass'),
            muscle_mass=metrics_current.get('weight') * 0.4  # Дефолтное значение мышц для старта
        )
        db.session.add(analysis)
        db.session.flush()
        user.initial_body_analysis_id = analysis.id

        # Рассчитываем целевую "Точку Б"
        metrics_current["sex"] = user.sex
        metrics_target = _calculate_target_metrics(user, metrics_current)

        user.fat_mass_goal = metrics_target.get("fat_mass")
        user.muscle_mass_goal = metrics_target.get("muscle_mass")

        # AI Генерация
        before_filename, after_filename = generate_for_user(
            user=user,
            avatar_bytes=full_body_photo_bytes,
            metrics_current=metrics_current,
            metrics_target=metrics_target
        )

        create_record(user=user, curr_filename=before_filename, tgt_filename=after_filename,
                      metrics_current=metrics_current, metrics_target=metrics_target)

        db.session.commit()
        return jsonify({
            "success": True,
            "before_photo_url": url_for('serve_file', filename=before_filename),
            "after_photo_url": url_for('serve_file', filename=after_filename),
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/analytics/track', methods=['POST'])
def api_analytics_track():
    """
    Принимает любые события с фронтенда (нажатия, просмотры экранов)
    и сохраняет их в нашу независимую таблицу аналитики.
    """
    data = request.get_json(silent=True) or {}
    event_name = data.get('event_type')
    props = data.get('event_data')

    # track_event сам разберется с user_id из сессии (cookie)
    # Если сессии нет (юзер еще не вошел), событие запишется как анонимное (user_id=None),
    # но мы все равно увидим общее количество таких событий в воронке.
    track_event(event_name, data=props)

    return jsonify({"ok": True})


# lib/backend/app.py

# lib/backend/app.py

# lib/backend/app.py

@app.route('/api/onboarding/complete_flow', methods=['POST'])
@login_required
def complete_onboarding_flow():
    """
    НОВЫЙ ФЛОУ (ЭТАП 2): Пользователь нажал "Завершить".
    1. Сохраняем текущий вес из анализа в WeightLog (как первую точку).
    2. Фиксируем user.start_weight (Точка А).
    3. Очищаем "пристрелочные" BodyAnalysis.
    """
    user = get_current_user()
    try:
        # --- 1. ПЕРЕНОС ДАННЫХ В WEIGHTLOG ПЕРЕД ОЧИСТКОЙ ---

        # Пытаемся найти анализ, который был сделан во время онбординга
        latest_onboarding_analysis = BodyAnalysis.query.filter_by(user_id=user.id) \
            .order_by(BodyAnalysis.timestamp.desc()).first()

        if latest_onboarding_analysis and latest_onboarding_analysis.weight:
            w_val = latest_onboarding_analysis.weight

            # А. Фиксируем Точку А в профиле, если еще не стоит
            if not user.start_weight:
                user.start_weight = w_val

            # Б. Создаем запись в WeightLog (История веса)
            # Проверяем, нет ли уже записи за сегодня, чтобы не дублировать
            today = date.today()
            existing_log = WeightLog.query.filter_by(user_id=user.id, date=today).first()

            if not existing_log:
                new_log = WeightLog(
                    user_id=user.id,
                    weight=w_val,
                    date=today
                )
                db.session.add(new_log)

        # Сохраняем WeightLog и user.start_weight перед удалением анализов
        db.session.commit()

        # --- 2. СТАНДАРТНОЕ ЗАВЕРШЕНИЕ ---
        user.onboarding_v2_complete = True
        user.onboarding_complete = True

        # Сбрасываем ссылку на BodyAnalysis (так как мы его сейчас удалим)
        user.initial_body_analysis_id = None
        db.session.add(user)
        db.session.commit()

        # --- 3. ОЧИСТКА ВРЕМЕННЫХ АНАЛИЗОВ ---
        # Теперь безопасно удаляем, так как вес сохранен в WeightLog
        BodyAnalysis.query.filter_by(user_id=user.id).delete()
        db.session.commit()

        track_event('onboarding_finished', user.id)
        return jsonify({"success": True})

    except Exception as e:
        db.session.rollback()
        print(f"Error in complete_onboarding_flow: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# --- НАЧАЛО: НОВЫЙ ЭНДПОИНТ ДЛЯ FLUTTER LOGIN ---
@app.route('/api/check_user_email', methods=['POST'])
def api_check_user_email():
    """
    Проверяет email и возвращает публичные данные (имя, аватар)
    для многоступенчатого входа в Flutter-приложении.
    """
    data = request.get_json(force=True, silent=True) or {}
    email_input = (data.get('email') or '').strip()

    if not email_input:
        return jsonify({"ok": False, "error": "EMAIL_REQUIRED"}), 400

    # Используем тот же case-insensitive поиск, что и в /api/login
    user = User.query.filter(func.lower(User.email) == email_input.casefold()).first()

    if not user:
        # 404 - Пользователь не найден. Flutter-приложение ожидает эту ошибку.
        return jsonify({"ok": False, "error": "USER_NOT_FOUND"}), 404

    # Пользователь найден, возвращаем публичные данные
    # (Используем ту же логику получения аватара, что и в /api/app/profile_data)
    avatar_filename = user.avatar.filename if user.avatar else None

    return jsonify({
        "ok": True,
        "user_data": {
            "name": user.name,
            "avatar_filename": avatar_filename
            # Примечание: Flutter-клиент сам соберет полный URL,
            # используя AuthApi.baseUrl + "/files/" + avatar_filename
        }
    }), 200


# --- КОНЕЦ: НОВОГО ЭНДПОИНТА ---


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email_input = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        # Нормализуем email на стороне Python (работает и с не-ASCII)
        email_norm = email_input.casefold()

        # Ищем пользователя по email без учета регистра
        user = User.query.filter(func.lower(User.email) == email_norm).first()

        if user and bcrypt.check_password_hash(user.password, password):
            session['user_id'] = user.id
            return redirect(url_for('profile'))

        return render_template('login.html', error="Неверный логин или пароль")

    return render_template('login.html')


@app.route('/api/check_email', methods=['POST'])
def check_email():
    data = request.get_json()
    if not data or 'email' not in data:
        return jsonify({"error": "Email not provided"}), 400

    email = data['email'].strip().lower()
    # Поиск без учета регистра
    user = User.query.filter(func.lower(User.email) == email).first()

    return jsonify({"exists": user is not None})


@app.route('/register', methods=['GET', 'POST'])
def register():
    errors = []

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        date_str = request.form.get('date_of_birth', '').strip()
        sex = (request.form.get('sex') or '').strip().lower()
        face_consent = bool(request.form.get('face_consent'))

        # Проверка обязательных полей
        if not name:
            errors.append("Имя обязательно.")
        if not email:
            errors.append("Email обязателен.")
        if not password or len(password) < 6:
            errors.append("Пароль обязателен и должен содержать минимум 6 символов.")
        if sex not in ('male', 'female'):
            errors.append("Пожалуйста, выберите пол.")

        # Проверка уникальности email
        if User.query.filter_by(email=email).first():
            errors.append("Этот email уже зарегистрирован.")

        # Проверка даты рождения
        date_of_birth = None
        if date_str:
            try:
                date_of_birth = datetime.strptime(date_str, "%Y-%m-%d").date()
                if date_of_birth > datetime.now().date():
                    errors.append("Дата рождения не может быть в будущем.")
            except ValueError:
                errors.append("Некорректный формат даты рождения.")
        else:
            errors.append("Дата рождения обязательна.")

        if errors:
            return render_template('register.html', errors=errors)

        # Обработка аватара (опционально)
        avatar_file_id = None
        file = request.files.get('avatar')
        if file and file.filename:
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext in {'jpg', 'jpeg', 'png', 'webp'}:
                unique_filename = f"avatar_{uuid.uuid4().hex}.{ext}"
                file_data = file.read()

                new_file = UploadedFile(
                    filename=unique_filename,
                    content_type=file.mimetype,
                    data=file_data,
                    size=len(file_data)
                )
                db.session.add(new_file)
                db.session.flush()
                avatar_file_id = new_file.id
            else:
                errors.append("Неверный формат аватара (разрешены: jpg, jpeg, png, webp).")
                return render_template('register.html', errors=errors)

        # Хеширование пароля и сохранение пользователя
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            sex=sex,
            face_consent=face_consent,
            avatar_file_id=avatar_file_id
        )

        db.session.add(user)
        db.session.commit()
        return redirect('/login')

    return render_template('register.html')


@app.route('/generate_diet')
@login_required
def generate_diet():
    user = get_current_user()
    if not getattr(user, 'has_subscription', False):
        flash("Генерация диеты доступна только по подписке.", "warning")
        return redirect(url_for('profile'))

    user_id = session.get('user_id')

    # Используем новую логику из assistant_bp
    result = generate_diet_for_user(user_id, amplitude_instance=amplitude)

    if result.get("success"):
        # Показываем пользователю обоснование от ИИ во flash сообщении (или просто успех)
        justification = result.get("justification", "")
        # Ограничим длину flash сообщения, чтобы не забить экран
        short_msg = "Диета готова! " + (justification[:100] + "..." if len(justification) > 100 else justification)
        flash(short_msg, "success")
        return jsonify({"redirect": "/diet"})

    elif result.get("code") == 404:
        return jsonify({"error": "Unauthorized"}), 401
    else:
        error_msg = result.get("error", "Unknown error")
        flash(f"Ошибка генерации: {error_msg}", "error")
        return jsonify({"error": error_msg}), 500

@app.route('/profile')
@login_required
def profile():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    # --- Проверка валидности пользователя из сессии ---
    if not user:
        session.clear()
        flash("Ваша сессия была недействительна. Пожалуйста, войдите снова.", "warning")
        return redirect(url_for('login'))

    # Можно убрать, @login_required уже защищает, но оставим как «страховку»
    if not user_id:
        return redirect(url_for('login'))

    # Сохраняем «до изменений» email (нужно в UI)
    session['user_email_before_edit'] = user.email

    # Базовые данные
    age = calculate_age(user.date_of_birth) if user.date_of_birth else None
    diet_obj = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
    today_activity = Activity.query.filter_by(user_id=user_id, date=date.today()).first()

    analyses = (BodyAnalysis.query
                .filter_by(user_id=user_id)
                .order_by(BodyAnalysis.timestamp.desc())
                .limit(2)
                .all())
    latest_analysis = analyses[0] if len(analyses) > 0 else None
    previous_analysis = analyses[1] if len(analyses) > 1 else None

    total_meals = (db.session.query(func.sum(MealLog.calories))
                   .filter_by(user_id=user.id, date=date.today())
                   .scalar() or 0)
    today_meals = MealLog.query.filter_by(user_id=user.id, date=date.today()).all()

    metabolism = latest_analysis.metabolism if latest_analysis else 0
    active_kcal = today_activity.active_kcal if today_activity else None
    steps = today_activity.steps if today_activity else None
    distance_km = today_activity.distance_km if today_activity else None
    resting_kcal = today_activity.resting_kcal if today_activity else None

    missing_meals = (total_meals == 0)
    missing_activity = (active_kcal is None)
    just_activated = user.show_welcome_popup

    start_onboarding_tour = False
    try:
        start_onboarding_tour = not user.onboarding_complete
    except Exception:
        # На случай, если миграция еще не применилась
        pass


    deficit = None
    if not missing_meals and not missing_activity and metabolism is not None:
        deficit = (metabolism + (active_kcal or 0)) - total_meals

    # --- Какая у пользователя «основная» группа (если есть) ---
    user_memberships = GroupMember.query.filter_by(user_id=user.id).all()
    user_joined_group = user.own_group if user.own_group else (user_memberships[0].group if user_memberships else None)

    all_analyses_for_progress_data = []  # Use a new variable name
    if user.initial_body_analysis_id:
        initial_analysis_for_chart = db.session.get(BodyAnalysis, user.initial_body_analysis_id)
        if initial_analysis_for_chart:
            # Fetch the SQLAlchemy objects
            analyses_objects = BodyAnalysis.query.filter(
                BodyAnalysis.user_id == user.id,
                BodyAnalysis.timestamp >= initial_analysis_for_chart.timestamp
            ).order_by(BodyAnalysis.timestamp.asc()).all()

            # Convert objects to a list of dictionaries
            all_analyses_for_progress_data = [
                {
                    "timestamp": analysis.timestamp.isoformat(),
                    "fat_mass": analysis.fat_mass
                }
                for analysis in analyses_objects
            ]

    diet = None
    if diet_obj:
        diet = {
            "total_kcal": getattr(diet_obj, "total_kcal", None) or getattr(diet_obj, "calories", None),
            "protein": getattr(diet_obj, "protein", None),
            "fat": getattr(diet_obj, "fat", None),
            "carbs": getattr(diet_obj, "carbs", None),
            "meals": {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
        }

        meals_source = None
        if getattr(diet_obj, "meals", None):
            meals_source = diet_obj.meals
        if meals_source is None and getattr(diet_obj, "meals_json", None):
            try:
                meals_source = json.loads(diet_obj.meals_json)
            except Exception:
                meals_source = None
        if meals_source is None:
            per_meal = {}
            for key in ("breakfast", "lunch", "dinner", "snack"):
                val = getattr(diet_obj, key, None)
                if val:
                    if isinstance(val, str):
                        try:
                            per_meal[key] = json.loads(val)
                        except Exception:
                            per_meal[key] = []
                    else:
                        per_meal[key] = val
            if per_meal:
                meals_source = per_meal
        if meals_source is None and getattr(diet_obj, "items", None):
            meals_source = diet_obj.items

        def push(meal_type, name, grams=None, kcal=None):
            mt = (meal_type or "").lower()
            if mt in diet["meals"]:
                diet["meals"][mt].append({"name": name or "Блюдо", "grams": grams, "kcal": kcal})

        if isinstance(meals_source, dict):
            for k in ("breakfast", "lunch", "dinner", "snack"):
                for it in (meals_source.get(k, []) or []):
                    if isinstance(it, dict):
                        grams = it.get("grams") or it.get("weight_g")
                        kcal = it.get("kcal") or it.get("calories")
                        name = it.get("name") or it.get("title")
                    else:
                        grams = getattr(it, "grams", None) or getattr(it, "weight_g", None)
                        kcal = getattr(it, "kcal", None) or getattr(it, "calories", None)
                        name = getattr(it, "name", None) or getattr(it, "title", None)
                    push(k, name, grams, kcal)
        elif isinstance(meals_source, list):
            for it in meals_source:
                if isinstance(it, dict):
                    mt = it.get("meal_type") or it.get("type") or it.get("meal")
                    grams = it.get("grams") or it.get("weight_g")
                    kcal = it.get("kcal") or it.get("calories")
                    name = it.get("name") or it.get("title")
                else:
                    mt = getattr(it, "meal_type", None)
                    grams = getattr(it, "grams", None) or getattr(it, "weight_g", None)
                    kcal = getattr(it, "kcal", None) or getattr(it, "calories", None)
                    name = getattr(it, "name", None) or getattr(it, "title", None)
                push(mt, name, grams, kcal)

        if not diet["total_kcal"]:
            try:
                diet["total_kcal"] = sum((i.get("kcal") or 0) for lst in diet["meals"].values() for i in lst) or None
            except Exception:
                pass

    # --- Прогресс жиросжигания (УЛУЧШЕННАЯ ЛОГИКА С ПРОГНОЗОМ) ---
    fat_loss_progress = None
    progress_checkpoints = []  # <-- добавили дефолт
    KCAL_PER_KG_FAT = 7700  # Энергетическая ценность 1 кг жира

    # Получаем стартовый и последний анализы
    initial_analysis = db.session.get(BodyAnalysis,
                                      user.initial_body_analysis_id) if user.initial_body_analysis_id else None

    if initial_analysis and latest_analysis and latest_analysis.fat_mass and user.fat_mass_goal and initial_analysis.fat_mass is not None and initial_analysis.fat_mass > user.fat_mass_goal:
        # --- 1. Расчет фактического прогресса на момент последнего замера ---
        initial_fat_mass = initial_analysis.fat_mass
        last_measured_fat_mass = latest_analysis.fat_mass
        goal_fat_mass = user.fat_mass_goal

        total_fat_to_lose_kg = initial_fat_mass - goal_fat_mass
        fact_lost_so_far_kg = initial_fat_mass - last_measured_fat_mass

        # --- 2. Расчет прогнозируемого прогресса на основе дефицита калорий ПОСЛЕ последнего замера ---
        start_datetime = latest_analysis.timestamp
        today_date = date.today()

        meal_logs_since_last_analysis = MealLog.query.filter(
            MealLog.user_id == user.id,
            MealLog.date >= start_datetime.date()
        ).all()
        activity_logs_since_last_analysis = Activity.query.filter(
            Activity.user_id == user.id,
            Activity.date >= start_datetime.date()
        ).all()

        meals_map = {}
        for log in meal_logs_since_last_analysis:
            meals_map.setdefault(log.date, 0)
            meals_map[log.date] += log.calories

        activity_map = {log.date: log.active_kcal for log in activity_logs_since_last_analysis}

        total_accumulated_deficit = 0
        metabolism = latest_analysis.metabolism or 0

        delta_days = (today_date - start_datetime.date()).days

        if delta_days >= 0:
            for i in range(delta_days + 1):
                current_day = start_datetime.date() + timedelta(days=i)
                consumed = meals_map.get(current_day, 0)
                burned_active = activity_map.get(current_day, 0)

                if i == 0:
                    calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                        MealLog.user_id == user.id,
                        MealLog.date == current_day,
                        MealLog.created_at < start_datetime
                    ).scalar() or 0
                    consumed -= calories_before_analysis
                    burned_active = 0

                daily_deficit = (metabolism + burned_active) - consumed
                if daily_deficit > 0:
                    total_accumulated_deficit += daily_deficit

        estimated_burned_since_last_measurement_kg = total_accumulated_deficit / KCAL_PER_KG_FAT

        estimated_current_fat_mass = last_measured_fat_mass - estimated_burned_since_last_measurement_kg
        total_lost_so_far_kg = initial_fat_mass - estimated_current_fat_mass

        percentage = 0
        if total_fat_to_lose_kg > 0:
            percentage = (total_lost_so_far_kg / total_fat_to_lose_kg) * 100

        fat_loss_progress = {
            'percentage': min(100, max(0, percentage)),
            'burned_kg': total_lost_so_far_kg,
            'total_to_lose_kg': total_fat_to_lose_kg,
            'initial_kg': initial_fat_mass,
            'goal_kg': goal_fat_mass,
            'current_kg': estimated_current_fat_mass
        }

        # --- НАЧАЛО: Добавляем расчет чек-поинтов ---
        if all_analyses_for_progress_data and fat_loss_progress['total_to_lose_kg'] > 0:
            initial_fat = fat_loss_progress['initial_kg']
            total_to_lose = fat_loss_progress['total_to_lose_kg']

            for i, analysis_data in enumerate(all_analyses_for_progress_data):
                current_fat_at_point = analysis_data.get('fat_mass') or initial_fat
                fat_lost_at_point = initial_fat - current_fat_at_point
                percentage_at_point = (fat_lost_at_point / total_to_lose) * 100

                progress_checkpoints.append({
                    "number": i + 1,
                    "percentage": min(100, max(0, percentage_at_point))
                })
        # --- КОНЕЦ: Добавляем расчет чек-поинтов ---

    # <-- ЕДИНЫЙ возврат из функции, с учётом случая без прогресса
    return render_template(
        'profile.html',
        user=user,
        age=age,
        diet=diet,
        today_activity=today_activity,
        latest_analysis=latest_analysis,
        previous_analysis=previous_analysis,
        total_meals=total_meals,
        today_meals=today_meals,
        metabolism=metabolism,
        active_kcal=active_kcal,
        steps=steps,
        distance_km=distance_km,
        resting_kcal=resting_kcal,
        deficit=deficit,
        missing_meals=missing_meals,
        missing_activity=missing_activity,
        user_joined_group=user_joined_group,
        all_analyses_for_progress=all_analyses_for_progress_data,
        fat_loss_progress=fat_loss_progress,
        progress_checkpoints=progress_checkpoints,
        just_activated=just_activated,
        start_onboarding_tour=start_onboarding_tour
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/api/onboarding/complete', methods=['POST'])
@login_required
def complete_onboarding_tour():
    """Отмечает, что пользователь завершил онбординг-тур."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    try:
        if not user.onboarding_complete:
            user.onboarding_complete = True
            db.session.commit()
            # Опционально: логируем действие
            log_audit("onboarding_complete", "User", user.id)
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True})

@app.route('/upload_analysis', methods=['POST'])
@login_required
def upload_analysis():
    file = request.files.get('file')
    user = get_current_user()
    if not file or not user:
        return jsonify({"success": False, "error": "Файл не загружен или вы не авторизованы."}), 400

    try:
        # Читаем байты напрямую из памяти (без сохранения на диск!)
        file_bytes = file.read()
        base64_image = base64.b64encode(file_bytes).decode("utf-8")

        # --- ШАГ 1: Извлечение данных с изображения ---
        response_metrics = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — фитнес-аналитик. Извлеки следующие параметры из фото анализа тела (bioimpedance):"
                        "height, weight, muscle_mass, muscle_percentage, body_water, protein_percentage, "
                        "skeletal_muscle_mass, visceral_fat_rating, metabolism, "
                        "waist_hip_ratio, body_age, fat_mass, bmi, fat_free_body_weight. "
                        "Верни СТРОГО JSON с найденными числовыми значениями."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Извлеки параметры из анализа тела."}
                    ]
                }
            ],
            max_tokens=1000,
            response_format={"type": "json_object"}
        )
        content = response_metrics.choices[0].message.content.strip()
        result = json.loads(content)

        # --- FIX: Если рост не распознан, берем из профиля (user.height) ---
        if not result.get('height') and user.height:
            result['height'] = user.height
        # -------------------------------------------------------------------

        # Если данные не найдены, пытаемся взять их из последнего анализа (чтобы не сбрасывать прогресс в 0)
        last_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
        if last_analysis:
            # Если рост всё ещё не найден (нет ни в AI, ни в user.height), пробуем историю
            if not result.get('height') and last_analysis.height:
                result['height'] = last_analysis.height
            if not result.get('weight') and last_analysis.weight:
                result['weight'] = last_analysis.weight
            if not result.get('fat_mass') and last_analysis.fat_mass:
                result['fat_mass'] = last_analysis.fat_mass
            if not result.get('muscle_mass') and last_analysis.muscle_mass:
                result['muscle_mass'] = last_analysis.muscle_mass

                # Список обязательных полей (оставили только критически важные)
        required_keys = ['weight', 'muscle_mass', 'fat_mass', 'metabolism']
        missing_keys = [key for key in required_keys if key not in result or result.get(key) is None]

        if missing_keys:
                    # Словарь для перевода полей на русский
            field_names_ru = {
                        'weight': 'Вес',
                        'muscle_mass': 'Мышечная масса',
                        'fat_mass': 'Жировая масса',
                        'metabolism': 'Метаболизм (BMR)',
                        'body_age': 'Метаболический возраст'
            }
                    # Формируем список отсутствующих полей на русском
            missing_ru = [field_names_ru.get(k, k) for k in missing_keys]
            missing_str = ', '.join(missing_ru)

            return jsonify({
                        "success": False,
                        "error": f"Не удалось распознать обязательные показатели: {missing_str}. Попробуйте сделать более четкое фото или загрузить другой файл."
            }), 400

        # --- ШАГ 2: Генерация целей ---
        age = calculate_age(user.date_of_birth) if user.date_of_birth else 'не указан'
        prompt_goals = (
            f"Для пользователя с параметрами: возраст {age}, рост {result.get('height')} см, "
            f"вес {result.get('weight')} кг, жировая масса {result.get('fat_mass')} кг, "
            f"мышечная масса {result.get('muscle_mass')} кг. "
            f"Предложи реалистичные цели по снижению жировой массы и увеличению мышечной массы. "
            f"Верни СТРОГО JSON в формате: "
            f'{{"fat_mass_goal": <число>, "muscle_mass_goal": <число>}}'
        )
        response_goals = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты — профессиональный фитнес-тренер. Давай цели в формате JSON."},
                {"role": "user", "content": prompt_goals}
            ],
            max_tokens=200,
            response_format={"type": "json_object"}
        )
        goals_content = response_goals.choices[0].message.content.strip()
        goals_result = json.loads(goals_content)
        result.update(goals_result)

        return jsonify({"success": True, "data": result})

    except Exception as e:
        print(f"!!! ОШИБКА В UPLOAD_ANALYSIS: {e}")
        return jsonify({
            "success": False,
            "error": "Не удалось проанализировать изображение. Пожалуйста, попробуйте другое фото или загрузите файл лучшего качества."
        }), 500

# ЗАМЕНИТЕ СТАРУЮ ФУНКЦИЮ meals НА ЭТУ
@app.route("/meals", methods=["GET", "POST"])
@login_required
def meals():
    user = get_current_user()

    # --- ЛОГИКА СОХРАНЕНИЯ (POST-ЗАПРОС) ---
    if request.method == "POST":
        meal_type = request.form.get('meal_type')
        if not meal_type:
            flash("Произошла ошибка: не указан тип приёма пищи.", "error")
            return redirect(url_for('meals'))

        try:
            calories = int(request.form.get('calories', 0))
            protein = float(request.form.get('protein', 0.0))
            fat = float(request.form.get('fat', 0.0))
            carbs = float(request.form.get('carbs', 0.0))
            name = request.form.get('name')
            verdict = request.form.get('verdict')
            analysis = request.form.get('analysis', '')

            existing_meal = MealLog.query.filter_by(
                user_id=user.id, date=date.today(), meal_type=meal_type
            ).first()

            if existing_meal:
                existing_meal.calories = calories
                existing_meal.protein = protein
                existing_meal.fat = fat
                existing_meal.carbs = carbs
                existing_meal.name = name
                existing_meal.verdict = verdict
                existing_meal.analysis = analysis
                flash(f"Приём пищи '{meal_type.capitalize()}' успешно обновлён!", "success")
            else:
                new_meal = MealLog(
                    user_id=user.id, date=date.today(), meal_type=meal_type,
                    calories=calories, protein=protein, fat=fat, carbs=carbs,
                    name=name, verdict=verdict, analysis=analysis
                )
                db.session.add(new_meal)
                flash(f"Приём пищи '{meal_type.capitalize()}' успешно добавлен!", "success")

                # Честный пересчет
            recalculate_streak(user)  # <-- Добавлено

            db.session.commit()

        except (ValueError, TypeError) as e:
            db.session.rollback()
            flash(f"Ошибка в формате данных от AI. Не удалось сохранить. ({e})", "error")

        # После обработки POST-запроса, перенаправляем на ту же страницу
        # чтобы избежать повторной отправки формы при обновлении
        return redirect(url_for('meals'))

    # --- ЛОГИКА ОТОБРАЖЕНИЯ (GET-ЗАПРОС) ---
    today_meals = MealLog.query.filter_by(user_id=user.id, date=date.today()).all()
    grouped = {
        "breakfast": [], "lunch": [], "dinner": [], "snack": []
    }
    for m in today_meals:
        grouped[m.meal_type].append(m)

    latest_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()

    return render_template("profile.html",
                           user=user,
                           meals=grouped,
                           latest_analysis=latest_analysis,
                           tab='meals')

# --- НАЧАЛО ИЗМЕНЕНИЙ: Обновлённая функция для сохранения анализа ---
from flask import jsonify # Убедись, что jsonify импортирован вверху файла


@app.route('/confirm_analysis', methods=['GET', 'POST'])
@login_required
def confirm_analysis():
    user = get_current_user()

    # --- ЛОГИКА POST (Сохранение) ---
    if request.method == 'POST':
        # 1. Пытаемся получить JSON (для мобильного приложения)
        # force=True заставляет парсить JSON даже если заголовок Content-Type неверный
        api_data = request.get_json(force=True, silent=True)

        if api_data:
            print(f"DEBUG: API Request detected. Data: {api_data}")  # Лог в консоль

            # --- ВАЛИДАЦИЯ УДАЛЕНА ПО ЗАПРОСУ ---
            # Теперь не возвращаем 400, если нет muscle_mass, fat_mass или metabolism.

            try:
                # 2. Проверка времени (анти-спам замерами, 7 дней)
                last_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(
                    BodyAnalysis.timestamp.desc()).first()

                # Ограничение удалено
                # 3. Создаем запись
                new_analysis = BodyAnalysis(user_id=user.id, timestamp=datetime.now(UTC))

                # Заполняем поля безопасным методом (0 если нет данных)
                def get_val(key, default=0):
                    val = api_data.get(key)
                    if val is None or val == "":
                        return default
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        return default

                new_analysis.muscle_mass = get_val('muscle_mass')
                new_analysis.fat_mass = get_val('fat_mass')
                new_analysis.metabolism = get_val('metabolism')
                new_analysis.weight = get_val('weight')

                # Обновляем рост и в анализе, и в профиле
                h_val = get_val('height')
                new_analysis.height = h_val
                if h_val and h_val > 0:
                    user.height = int(h_val)  # Синхронизируем с User

                # Заполняем остальные поля
                new_analysis.body_age = get_val('body_age')
                new_analysis.visceral_fat_rating = get_val('visceral_fat_rating')
                new_analysis.muscle_percentage = get_val('muscle_percentage')
                new_analysis.body_water = get_val('body_water')
                new_analysis.protein_percentage = get_val('protein_percentage')
                new_analysis.skeletal_muscle_mass = get_val('skeletal_muscle_mass')
                new_analysis.waist_hip_ratio = get_val('waist_hip_ratio')
                new_analysis.bmi = get_val('bmi')
                new_analysis.fat_free_body_weight = get_val('fat_free_body_weight')

                # Обновляем цели, если прислали
                if 'fat_mass_goal' in api_data: user.fat_mass_goal = get_val('fat_mass_goal')
                if 'muscle_mass_goal' in api_data: user.muscle_mass_goal = get_val('muscle_mass_goal')

                # Обновляем согласие
                if 'face_consent' in api_data: user.face_consent = bool(api_data.get('face_consent'))

                user.updated_at = datetime.now(UTC)

                # Сохраняем
                db.session.add(new_analysis)
                db.session.flush()  # Получаем ID

                # Если это первый замер
                if not user.initial_body_analysis_id:
                    user.initial_body_analysis_id = new_analysis.id

                    # Логика AI комментария
                    ai_comment_text = None
                    if last_analysis:
                        try:
                            ai_comment_text = generate_progress_commentary(user, last_analysis, new_analysis)
                            if ai_comment_text: new_analysis.ai_comment = ai_comment_text
                        except Exception as e:
                            print(f"AI Comment generation warning: {e}")

                    # --- РАСЧЕТ SMART ЦЕЛИ (BMR + Activity - Deficit) ---
                    try:
                        # 1. BMR: Берем из весов или считаем формулу
                        bmr = new_analysis.metabolism
                        if not bmr and new_analysis.weight and new_analysis.height:
                            # Mifflin-St Jeor
                            age = calculate_age(user.date_of_birth) if user.date_of_birth else 30
                            # Для женщин -161, для мужчин +5
                            s = -161 if (getattr(user, 'sex', 'female') == 'female') else 5
                            bmr = (10 * new_analysis.weight) + (6.25 * new_analysis.height) - (5 * age) + s

                        if bmr:
                            # 2. Формула: BMR * 1.2 (сидячий) * 0.85 (дефицит 15%)
                            smart_target = int((bmr * 1.2) * 0.85)

                            # 3. Сохраняем в профиль
                            user.target_calories = smart_target

                            # 4. Обновляем текущую диету для синхронизации, чтобы Dashboard обновился сразу
                            active_diet = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()
                            if active_diet:
                                active_diet.total_kcal = smart_target
                    except Exception as e:
                        print(f"Smart Target Error: {e}")

                    # --- SQUAD SCORING: HEALTHY PROGRESS (30 pts) ---
                if last_analysis and last_analysis.weight and new_analysis.weight:
                    prev_w = float(last_analysis.weight)
                    curr_w = float(new_analysis.weight)
                    if prev_w > 0:
                        change_pct = (curr_w - prev_w) / prev_w
                        if -0.015 <= change_pct <= -0.001:
                            today = date.today()
                            start_of_week = today - timedelta(days=today.weekday())
                            existing_score = SquadScoreLog.query.filter(
                                SquadScoreLog.user_id == user.id,
                                SquadScoreLog.category == 'healthy_progress',
                                func.date(SquadScoreLog.created_at) >= start_of_week
                            ).first()
                            if not existing_score:
                                award_squad_points(user, 'healthy_progress', 30, "Здоровый прогресс веса")

                                # --- AI FEED POST LOGIC ---
                                if user.initial_body_analysis_id and user.fat_mass_goal:
                                    try:
                                        initial_analysis = BodyAnalysis.query.get(user.initial_body_analysis_id)
                                        if initial_analysis:
                                            total_lost = initial_analysis.fat_mass - new_analysis.fat_mass
                                            remaining = new_analysis.fat_mass - user.fat_mass_goal

                                            # Создаем пост в ленту
                                            # Создаем пост в ленту
                                            feed_content = f"Сбросил {total_lost:.1f}кг жира! До цели осталось {remaining:.1f}кг. Идем по графику! 🔥"

                                            # Определяем ID группы (своя или куда вступил)
                                            target_group_id = None
                                            if user.own_group:
                                                target_group_id = user.own_group.id
                                            else:
                                                membership = GroupMember.query.filter_by(user_id=user.id).first()
                                                if membership:
                                                    target_group_id = membership.group_id

                                            if target_group_id:
                                                new_post = GroupMessage(
                                                    group_id=target_group_id,
                                                    user_id=user.id,
                                                    text=feed_content,
                                                    type='system',  # Используем system для выделения в ленте
                                                    timestamp=datetime.now(UTC)
                                                )
                                                db.session.add(new_post)
                                    except Exception as feed_err:
                                        print(f"Feed post error: {feed_err}")

                db.session.commit()
                print("DEBUG: Analysis saved successfully via API")

                # ANALYTICS
                try:
                    amplitude.track(BaseEvent(
                        event_type="Body Analysis Confirmed",
                        user_id=str(user.id),
                        event_properties={
                            "weight": new_analysis.weight,
                            "is_initial": (user.initial_body_analysis_id == new_analysis.id)
                        }
                    ))
                    track_event('analysis_confirmed', user.id,
                                {"is_initial": (user.initial_body_analysis_id == new_analysis.id)})
                except:
                    pass

                # 4. ВОЗВРАЩАЕМ JSON 200 (ВАЖНО!)
                return jsonify({"success": True, "ai_comment": ai_comment_text})

            except Exception as e:
                db.session.rollback()
                print(f"DEBUG: Error saving analysis: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        else:
            # 5. Если JSON не пришел, значит это ВЕБ-ФОРМА (Web)
            print("DEBUG: Web Form Request detected")

            # Логика для веба (редиректы)
            analysis_data = session.get('temp_analysis')
            if not analysis_data:
                flash("Данные устарели. Загрузите анализ заново.", "error")
                return redirect(url_for('profile'))

            # В Веб-версии сохранение может требовать дублирования логики или выноса в сервис
            session.pop('temp_analysis', None)
            flash("Функция сохранения через веб в разработке. Пожалуйста, используйте приложение.", "info")
            return redirect(url_for('profile'))

    # --- ЛОГИКА GET (Отображение страницы подтверждения в Вебе) ---
    if 'temp_analysis' in session:
        return render_template('confirm_analysis.html',
                               data=session['temp_analysis'],
                               user=user)

    flash("Сначала загрузите фото анализа.", "warning")
    return redirect(url_for('profile'))


@app.route('/api/app/update_weight_simple', methods=['POST'])
@login_required
def update_weight_simple():
    user = get_current_user()
    data = request.get_json(force=True, silent=True) or {}
    new_weight = data.get('weight')

    if new_weight is None:
        return jsonify({"success": False, "error": "Вес не указан"}), 400

    try:
        weight_val = float(new_weight)
    except ValueError:
        return jsonify({"success": False, "error": "Некорректный формат веса"}), 400

    # 1. СОХРАНЯЕМ ТОЛЬКО В WEIGHT LOG (Дневник веса)
    today = date.today()

    # Ищем, есть ли уже запись за сегодня, чтобы обновить её
    todays_log = WeightLog.query.filter_by(user_id=user.id, date=today).first()

    if todays_log:
        todays_log.weight = weight_val
    else:
        new_log = WeightLog(user_id=user.id, weight=weight_val, date=today)
        db.session.add(new_log)

    # 2. ФИКСАЦИЯ СТАРТОВОГО ВЕСА (если это самое первое взвешивание вообще)
    if not user.start_weight:
        user.start_weight = weight_val

    # BodyAnalysis НЕ ТРОГАЕМ

    db.session.commit()

    return jsonify({"success": True, "new_weight": weight_val})

@app.route('/generate_telegram_code')
def generate_telegram_code():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    code = ''.join(random.choices(string.digits, k=8))
    user = db.session.get(User, user_id)
    user.telegram_code = code
    db.session.commit()

    return jsonify({'code': code})





@app.route('/edit_profile', methods=['POST'])
@login_required
def edit_profile():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        # --- Обновление текстовых полей ---
        new_name = request.form.get('name')
        if new_name and new_name.strip():
            user.name = new_name.strip()

        new_email = request.form.get('email')
        if new_email and new_email.strip() and new_email.strip().lower() != (user.email or '').lower():
            if User.query.filter(func.lower(User.email) == new_email.strip().lower(), User.id != user.id).first():
                flash("Этот email уже используется другим пользователем.", "error")
                return redirect(url_for('profile'))
            user.email = new_email.strip()

        date_of_birth_str = request.form.get('date_of_birth')
        if date_of_birth_str:
            user.date_of_birth = datetime.strptime(date_of_birth_str, '%Y-%m-%d').date()

        # --- ДОБАВЛЕНО: Обновление целевого веса ---
        new_target_weight = request.form.get('target_weight')
        if new_target_weight:
            try:
                user.weight_goal = float(new_target_weight)
            except ValueError:
                pass
        # ------------------------------------------

        # --- Обработка аватара (ИСПРАВЛЕННАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ) ---
        file = request.files.get('avatar')
        if file and file.filename:
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext not in {'jpg', 'jpeg', 'png', 'webp'}:
                flash("Неверный формат аватара (разрешены: jpg, jpeg, png, webp).", "error")
                return redirect(url_for('profile'))

            old_avatar_to_delete = user.avatar if user.avatar_file_id else None

            if old_avatar_to_delete:
                user.avatar_file_id = None
                db.session.flush()

            unique_filename = f"avatar_{user.id}_{uuid.uuid4().hex}.{ext}"
            file_data = file.read()
            new_file = UploadedFile(
                filename=unique_filename,
                content_type=file.mimetype,
                data=file_data,
                size=len(file_data),
                user_id=user.id
            )
            db.session.add(new_file)
            db.session.flush()

            user.avatar_file_id = new_file.id

            if old_avatar_to_delete:
                db.session.delete(old_avatar_to_delete)

        db.session.commit()
        flash("Профиль успешно обновлен!", "success")

    except ValueError:
        db.session.rollback()
        flash("Неверный формат даты рождения.", "error")
    except Exception as e:
        db.session.rollback()
        print(f"!!! ОШИБКА ПРИ ОБНОВЛЕНИИ ПРОФИЛЯ: {e}")
        flash("Произошла ошибка при обновлении профиля.", "error")

    return redirect(url_for('profile'))


@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    if not new_password:
        flash("Новый пароль не может быть пустым.", "error")
        return redirect(url_for('profile'))

    if new_password != confirm_password:
        flash("Пароли не совпадают.", "error")
        return redirect(url_for('profile'))

    if len(new_password) < 6:
        flash("Пароль должен содержать не менее 6 символов.", "error")
        return redirect(url_for('profile'))

    try:
        user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
        db.session.commit()
        flash("Пароль успешно изменен!", "success")
    except Exception as e:
        db.session.rollback()
        print(f"!!! ОШИБКА ПРИ СМЕНЕ ПАРОЛЯ: {e}")
        flash("Произошла ошибка при смене пароля.", "error")

    return redirect(url_for('profile'))

@app.route('/diet')
@login_required
def diet():
    if not get_current_user().has_subscription:
        flash("Просмотр диеты доступен только по подписке.", "warning")
        return redirect(url_for('profile'))

    user = get_current_user()
    if not user.has_subscription:
        flash("Доступно только по подписке. Активируйте подписку для полного доступа.", "warning")
        return redirect('/profile')

    diet = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()
    if not diet:
        flash("Диета ещё не сгенерирована. Сгенерируйте ее из профиля.", "info")
        return redirect('/profile')

    return render_template("confirm_diet.html", diet=diet,
                           breakfast=json.loads(diet.breakfast),
                           lunch=json.loads(diet.lunch),
                           dinner=json.loads(diet.dinner),
                           snack=json.loads(diet.snack))

@app.route('/upload_activity', methods=['POST'])
def upload_activity():
    data = request.json
    email = data.get('email')
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404

    # Удаляем старую активность за сегодня, если она есть
    existing_activity = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    if existing_activity:
        db.session.delete(existing_activity)
        db.session.commit()

    activity = Activity(
        user_id=user.id,
        date=date.today(),
        steps=data.get('steps'),
        active_kcal=data.get('active_kcal'),
        resting_kcal=data.get('resting_kcal'),
        heart_rate_avg=data.get('heart_rate_avg'),
        distance_km=data.get('distance_km'),
        source=data.get('source', 'manual')
    )
    db.session.add(activity)
    db.session.commit()

    return jsonify({'message': 'Активность сохранена'})


@app.route('/manual_activity', methods=['GET', 'POST'])
@login_required
def manual_activity():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    if request.method == 'POST':
        steps = request.form.get('steps')
        active_kcal = request.form.get('active_kcal')
        resting_kcal = request.form.get('resting_kcal')
        heart_rate_avg = request.form.get('heart_rate_avg')
        distance_km = request.form.get('distance_km')

        # Удаляем старую активность за сегодня, если она есть
        existing_activity = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
        if existing_activity:
            db.session.delete(existing_activity)
            db.session.commit()

        activity = Activity(
            user_id=user.id,
            date=date.today(),
            steps=int(steps or 0),
            active_kcal=int(active_kcal or 0),
            resting_kcal=int(resting_kcal or 0),
            heart_rate_avg=int(heart_rate_avg or 0),
            distance_km=float(distance_km or 0),
            source='manual'
        )
        db.session.add(activity)
        db.session.commit()
        flash("Активность за сегодня успешно обновлена!", "success")
        return redirect('/profile')

    # Предзаполнение формы текущими данными, если они есть
    today_activity = Activity.query.filter_by(user_id=user_id, date=date.today()).first()
    return render_template('manual_activity.html', user=user, today_activity=today_activity)


@app.route('/diet_history')
@login_required
def diet_history():
    if not get_current_user().has_subscription:
        flash("История диет доступна только по подписке.", "warning")
        return redirect(url_for('profile'))

    user_id = session.get('user_id')

    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    diets = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).all()
    week_total = db.session.query(func.sum(Diet.total_kcal)).filter(
        Diet.user_id == user_id,
        Diet.date >= week_ago
    ).scalar() or 0

    month_total = db.session.query(func.sum(Diet.total_kcal)).filter(
        Diet.user_id == user_id,
        Diet.date >= month_ago
    ).scalar() or 0

    # 📊 График за 7 дней
    last_7_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    chart_labels = [d.strftime("%d.%m") for d in last_7_days]
    chart_values = []

    for d in last_7_days:
        total = db.session.query(func.sum(Diet.total_kcal)).filter_by(user_id=user_id, date=d).scalar()
        chart_values.append(total or 0)

    return render_template(
        "diet_history.html",
        diets=diets,
        week_total=week_total,
        month_total=month_total,
        chart_labels=json.dumps(chart_labels),
        chart_values=json.dumps(chart_values)
    )

# === TELEGRAM: лог активности по chat_id ===
@app.route('/api/activity/log', methods=['POST'])
def api_activity_log():
    data = request.get_json(force=True, silent=True) or {}
    chat_id = str(data.get('chat_id') or '').strip()
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    user = User.query.filter_by(telegram_chat_id=chat_id).first()
    if not user:
        return jsonify({"error": "user not found"}), 404

    try:
        steps = int(data.get('steps') or 0)
        active_kcal = int(data.get('active_kcal') or 0)
    except Exception:
        return jsonify({"error": "invalid numbers"}), 400

    # перезаписываем активность за сегодня
    today = date.today()
    existing = Activity.query.filter_by(user_id=user.id, date=today).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()

    act = Activity(
        user_id=user.id,
        date=today,
        steps=steps,
        active_kcal=active_kcal,
        source='telegram'
    )
    db.session.add(act)
    db.session.commit()
    return jsonify({"ok": True, "message": "activity saved"})

@app.route('/add_meal', methods=['POST'])
@login_required
def add_meal():
    if not get_current_user().has_subscription:
        flash("Доступ к группам и сообществу открыт только по подписке.", "warning")
        return redirect(url_for('profile'))

    user_id = session.get('user_id')
    meal_type = request.form.get('meal_type')
    today = date.today()

    if not meal_type:
        flash("Произошла ошибка: не указан тип приёма пищи.", "error")
        return redirect(url_for('meals')) # Перенаправляем на страницу с приёмами пищи

    try:
        # Безопасно получаем данные из формы с помощью .get()
        name = request.form.get('name')
        verdict = request.form.get('verdict')
        analysis = request.form.get('analysis', '')
        # Преобразуем в числа с обработкой ошибок
        calories = int(request.form.get('calories', 0))
        protein = float(request.form.get('protein', 0.0))
        fat = float(request.form.get('fat', 0.0))
        carbs = float(request.form.get('carbs', 0.0))

        # Ищем существующую запись для обновления или создаём новую
        existing_meal = MealLog.query.filter_by(
            user_id=user_id,
            date=today,
            meal_type=meal_type
        ).first()

        if existing_meal:
            # Обновляем существующую запись
            existing_meal.name = name
            existing_meal.verdict = verdict
            existing_meal.calories = calories
            existing_meal.protein = protein
            existing_meal.fat = fat
            existing_meal.carbs = carbs
            existing_meal.analysis = analysis
            flash(f"Приём пищи '{meal_type.capitalize()}' успешно обновлён!", "success")
        else:
            # Создаём новую запись
            new_meal = MealLog(
                user_id=user_id,
                date=today,
                meal_type=meal_type,
                name=name,
                verdict=verdict,
                calories=calories,
                protein=protein,
                fat=fat,
                carbs=carbs,
                analysis=analysis
            )
            db.session.add(new_meal)
            flash(f"Приём пищи '{meal_type.capitalize()}' успешно добавлен!", "success")

        db.session.commit()

    except (ValueError, TypeError) as e:
        # Ловим ошибки, если данные от AI пришли в неверном формате
        db.session.rollback()
        flash(f"Ошибка сохранения данных. Пожалуйста, попробуйте снова. ({e})", "error")

    # Перенаправляем пользователя обратно на вкладку "Приёмы пищи"
    return redirect(url_for('meals'))

@app.route('/diet/<int:diet_id>')
@login_required
def view_diet(diet_id):
    user_id = session.get('user_id')
    diet = Diet.query.filter_by(id=diet_id, user_id=user_id).first()
    if not diet:
        flash("Диета не найдена.", "error")
        return redirect('/diet_history')

    return render_template("confirm_diet.html", diet=diet,
                           breakfast=json.loads(diet.breakfast),
                           lunch=json.loads(diet.lunch),
                           dinner=json.loads(diet.dinner),
                           snack=json.loads(diet.snack))


@app.route('/reset_diet', methods=['POST'])
@login_required
def reset_diet():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    diet = Diet.query.filter_by(user_id=user.id, date=date.today()).first()
    if diet:
        try:
            db.session.delete(diet)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Рацион успешно сброшен.'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)}), 500
    else:
        # Этот случай тоже обрабатываем, хотя он маловероятен
        return jsonify({'success': True, 'message': 'Нет рациона для сброса.'})

@app.route('/api/link_telegram', methods=['POST'])
def link_telegram():
    data = request.json
    code = data.get("code")
    chat_id = data.get("chat_id")

    user = User.query.filter_by(telegram_code=code).first()
    if not user:
        return jsonify({"error": "Неверный код"}), 404

    user.telegram_chat_id = str(chat_id)
    user.telegram_code = None
    db.session.commit()
    return jsonify({"message": "OK"}), 200


@app.route('/api/is_registered/<int:chat_id>')
def is_registered(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if user:
        return jsonify({"ok": True}), 200
    return jsonify({"ok": False}), 404


@app.route('/api/current_diet/<int:chat_id>')
def api_current_diet(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if not user:
        return jsonify({"error": "not found"}), 404

    diet = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()
    if not diet:
        return jsonify({"error": "no diet"}), 404

    return jsonify({
        "date": diet.date.isoformat(),
        "breakfast": json.loads(diet.breakfast),
        "lunch": json.loads(diet.lunch),
        "dinner": json.loads(diet.dinner),
        "snack": json.loads(diet.snack),
        "total_kcal": diet.total_kcal,
        "protein": diet.protein,
        "fat": diet.fat,
        "carbs": diet.carbs
    })


@app.route('/activity')
@login_required
def activity():
    user_id = session.get('user_id')

    user = db.session.get(User, user_id)
    today_activity = Activity.query.filter_by(user_id=user_id, date=date.today()).first()

    # Получаем активность за последние 7 дней для графиков
    week_ago = date.today() - timedelta(days=7)
    activities = Activity.query.filter(
        Activity.user_id == user_id,
        Activity.date >= week_ago
    ).order_by(Activity.date).all()

    # Подготавливаем данные для графиков
    chart_data = {
        'dates': [],
        'steps': [],
        'calories': [],
        'heart_rate': []
    }

    for day in (date.today() - timedelta(days=i) for i in range(6, -1, -1)):
        chart_data['dates'].append(day.strftime('%d.%m'))
        activity_for_day = next((a for a in activities if a.date == day),
                                None)  # Переименовано, чтобы избежать конфликта
        chart_data['steps'].append(activity_for_day.steps if activity_for_day else 0)
        chart_data['calories'].append(activity_for_day.active_kcal if activity_for_day else 0)
        chart_data['heart_rate'].append(activity_for_day.heart_rate_avg if activity_for_day else 0)

    # Здесь возвращаем activity.html, если он есть, или используем profile.html с нужным табом
    return render_template(
        'profile.html',
        user=user,
        today_activity=today_activity,
        chart_data=chart_data,
        tab='activity'  # Указываем активный таб
    )

@app.route('/api/log_meal', methods=['POST', 'DELETE'])
def log_meal():
    if request.method == 'DELETE':
        data = request.get_json()
        user = User.query.filter_by(telegram_chat_id=str(data['chat_id'])).first_or_404()
        meal = MealLog.query.filter_by(
            user_id=user.id,
            date=date.today(),
            meal_type=data['meal_type']
        ).first_or_404()
        db.session.delete(meal)
        db.session.commit()
        return '', 200

    # POST
    data = request.get_json()
    user = User.query.filter_by(telegram_chat_id=str(data['chat_id'])).first_or_404()

    calories = data.get("calories")
    protein = data.get("protein")
    fat = data.get("fat")
    carbs = data.get("carbs")

    raw = data.get("analysis", "")

    if None in (calories, protein, fat, carbs):
        def ptn(p):
            m = re.search(p, raw, flags=re.IGNORECASE)
            return float(m.group(1)) if m else None

        calories = ptn(r'Калории[:\s]+(\d+)')
        protein = ptn(r'Белки[:\s]+([\d.]+)')
        fat = ptn(r'Жиры[:\s]+([\d.]+)')
        carbs = ptn(r'Углеводы[:\s]+([\d.]+)')

    if None in (calories, protein, fat, carbs):
        return jsonify({"error": "cannot parse BJU"}), 400

    meal = MealLog(
        user_id=user.id,
        date=date.today(),
        meal_type=data['meal_type'],
        calories=int(calories),
        protein=float(protein),
        fat=float(fat),
        carbs=float(carbs),
        analysis=raw or ""  # ИСПРАВЛЕНО: Защита от Null
    )

    try:
        db.session.add(meal)
        recalculate_streak(user)  # <-- Добавлено
        db.session.commit()
        return jsonify({"status": "ok"}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "exists"}), 409


# ЭТО ПРАВИЛЬНЫЙ КОД

@app.route('/analyze_meal_photo', methods=['POST'])
def analyze_meal_photo():
    # Поддержка вызова из Telegram: принимаем chat_id в форме или query
    chat_id = request.form.get('chat_id') or request.args.get('chat_id')
    user = None
    if chat_id:
        user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    else:
        user = get_current_user()

    if not user:
        return jsonify({"error": "unauthorized", "reason": "no_user"}), 401

    if not getattr(user, 'has_subscription', False):
        return jsonify({"error": "Эта функция доступна только по подписке.", "subscription_required": True}), 403

    file = request.files.get('file')
    if not file:
        return jsonify({"error": "Файл не найден"}), 400

    # ... (код сохранения файла) ...
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with open(filepath, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')

            # --- ИЗМЕНЕННЫЙ ПРОМПТ ---
        tmpl = PromptTemplate.query.filter_by(name='meal_photo', is_active=True) \
                .order_by(PromptTemplate.version.desc()).first()

        system_prompt = (tmpl.body if tmpl else
                             "Ты — профессиональный диетолог. Проанализируй фото еды. Определи:"
                             "\n- Каллорий должен быть максимально реалистичным, ..., 500. А числа в которые хочется верить что то вроде 370, 420.."
                             "\n- Название блюда (в поле 'name')."
                             "\n- Калорийность, Белки, Жиры, Углеводы (в полях 'calories', 'protein', 'fat', 'carbs')."
                             "\n- Дай подробный текстовый анализ блюда (в поле 'analysis')."
                             "\n- Список основных ингредиентов (в поле 'ingredients' как список строк)."
                             "\n- Сделай краткий вывод: насколько блюдо полезно или вредно для диеты (в поле 'verdict')."
                             '\nВерни JSON СТРОГО в формате: {"name": "...", "ingredients": ["..."], "calories": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "analysis": "...", "verdict": "..."}'
                             )

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Проанализируй блюдо на фото."}
                ]}
            ],
            max_tokens=500,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content.strip()
        data = json.loads(content)

        return jsonify(data)

    except Exception as e:
        return jsonify({"error": f"Ошибка анализа фото: {e}"}), 500

@app.route('/api/subscription/status')
def subscription_status():
    chat_id = request.args.get('chat_id')
    user = None
    if chat_id:
        user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    else:
        user = get_current_user()

    if not user:
        return jsonify({"ok": False, "reason": "no_user"}), 401

    return jsonify({"ok": True, "has_subscription": bool(getattr(user, 'has_subscription', False))})


@app.route('/api/trainings/<int:tid>/checkin', methods=['POST'])
@login_required
def checkin_training(tid):
    user = get_current_user()
    training = Training.query.get_or_404(tid)

    # Валидация времени: чекин возможен за 30 мин до и 1.5 часа после начала
    now = datetime.now()
    # Учитываем, что training.date и training.start_time хранятся без таймзоны (считаем серверное время)
    start_dt = datetime.combine(training.date, training.start_time)

    # Разница в часах
    time_diff = (now - start_dt).total_seconds() / 3600

    # Окно: [-0.5 ... +1.5] часа от начала
    if -0.5 <= time_diff <= 1.5:
        # Проверяем дубликаты (чтобы не накручивали за одну тренировку)
        existing = SquadScoreLog.query.filter_by(
            user_id=user.id,
            category='workout',
            description=f"Training {tid}"
        ).first()

        if not existing:
            points = award_squad_points(user, 'workout', 50, f"Training {tid}")
            db.session.commit()
            return jsonify({"ok": True, "points": points, "message": "Чекин успешен! +50 баллов"})
        else:
            return jsonify({"ok": True, "message": "Уже отмечено"})

    return jsonify({"ok": False, "error": "Чекин доступен только во время тренировки"}), 400

@app.route('/api/trainings/my')
def api_trainings_my():
    # Фильтрация по локальному времени Алматы, поддержка start_time как datetime *и* как time (+ отдельная дата)
    from zoneinfo import ZoneInfo
    from datetime import datetime, date, time as dt_time

    chat_id = request.args.get('chat_id')
    user = None
    if chat_id:
        user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    else:
        user = get_current_user()

    if not user:
        return jsonify({"ok": False, "reason": "no_user"}), 401

    tz_almaty = ZoneInfo("Asia/Almaty")
    tz_utc = ZoneInfo("UTC")
    now_local = datetime.now(tz_almaty)

    # Берём список тренировок пользователя без фильтра по времени (типы полей могут отличаться),
    # дальше фильтруем и сортируем в Python.
    q = (
        db.session.query(Training)
        .join(TrainingSignup, TrainingSignup.training_id == Training.id)
        .filter(TrainingSignup.user_id == user.id)
        .limit(200)
    )

    entries = []
    for t in q.all():
        # Поля времени/даты могут называться по-разному
        start_field = getattr(t, 'start_time', None)
        date_field = (
            getattr(t, 'start_date', None)
            or getattr(t, 'date', None)
            or getattr(t, 'day', None)
        )

        if not start_field:
            continue

        # Собираем локальный datetime Алматы
        local_dt = None
        if isinstance(start_field, datetime):
            local_dt = start_field if start_field.tzinfo else start_field.replace(tzinfo=tz_almaty)
        elif isinstance(start_field, dt_time):
            # Нужна дата: пробуем взять из date_field
            if date_field:
                if isinstance(date_field, datetime):
                    d = date_field.date()
                elif isinstance(date_field, date):
                    d = date_field
                else:
                    d = None
                if d is not None:
                    local_dt = datetime.combine(d, start_field).replace(tzinfo=tz_almaty)
        elif isinstance(start_field, date):
            # Редкий случай: есть только дата — считаем 00:00
            local_dt = datetime.combine(start_field, dt_time(0, 0)).replace(tzinfo=tz_almaty)

        if not local_dt:
            # Не смогли восстановить полный datetime — пропускаем
            continue

        # Отфильтруем прошедшие
        if local_dt < now_local:
            continue

        start_utc = local_dt.astimezone(tz_utc)

        entries.append({
            "id": t.id,
            "title": getattr(t, 'title', getattr(t, 'name', 'Тренировка')),
            "start_utc": start_utc,  # для сортировки
            "start_time": start_utc.isoformat().replace("+00:00", "Z"),
            "location": getattr(t, 'location', None),
        })

    # Сортируем по времени начала
    entries.sort(key=lambda x: (x["start_utc"], x["id"]))
    # Возвращаем первые 50
    items = [{k: v for k, v in e.items() if k != "start_utc"} for e in entries[:50]]

    return jsonify({"ok": True, "items": items})

@app.route('/api/meals/today/<int:chat_id>')
def get_today_meals_api(chat_id):
    # Находим пользователя по ID чата в телеграме
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()

    # Ищем все записи о приемах пищи для этого пользователя за сегодня
    logs = MealLog.query.filter_by(user_id=user.id, date=date.today()).order_by(MealLog.created_at).all()

    # Считаем итоговые калории
    total_calories = sum(m.calories for m in logs)

    # Формируем данные для ответа
    meal_data = [
        {
            'meal_type': m.meal_type,
            'name': m.name or "Без названия",
            'calories': m.calories,
            'protein': m.protein,
            'fat': m.fat,
            'carbs': m.carbs
        }
        for m in logs
    ]

    return jsonify({"meals": meal_data, "total_calories": total_calories}), 200




@app.route('/metrics')
@login_required
def metrics():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)
    latest_analysis = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()

    # 1) Суммарные калории по приёмам пищи за сегодня
    total_meals = db.session.query(func.sum(MealLog.calories)) \
                      .filter_by(user_id=user.id, date=date.today()) \
                      .scalar() or 0

    # Получаем список приёмов пищи
    today_meals = MealLog.query \
        .filter_by(user_id=user.id, date=date.today()) \
        .all()

    # 2) Базовый метаболизм из последнего замера
    metabolism = latest_analysis.metabolism if latest_analysis else 0

    # 3) Активная калорийность
    activity = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    active_kcal = activity.active_kcal if activity else None
    steps = activity.steps if activity else None
    distance_km = activity.distance_km if activity else None
    resting_kcal = activity.resting_kcal if activity else None

    # Проверяем данные
    missing_meals = (total_meals == 0)
    missing_activity = (active_kcal is None)

    # 4) Дефицит
    deficit = None
    if not missing_meals and not missing_activity and metabolism is not None:
        deficit = (metabolism + active_kcal) - total_meals

    return render_template(
        'profile.html',
        user=user,
        age=calculate_age(user.date_of_birth) if user.date_of_birth else None,
        # для табов профиля и активности
        diet=Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first(),
        today_activity=activity,
        latest_analysis=latest_analysis,
        previous_analysis=BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).offset(
            1).first(),
        chart_data=None,  # Отключаем для этой страницы, если не нужно

        # новые переменные для metrics
        total_meals=total_meals,
        today_meals=today_meals,
        metabolism=metabolism,
        active_kcal=active_kcal,
        steps=steps,
        distance_km=distance_km,
        resting_kcal=resting_kcal,
        deficit=deficit,
        missing_meals=missing_meals,
        missing_activity=missing_activity,
        tab='metrics'  # Указываем активный таб
    )


@app.route('/api/registered_chats')
def registered_chats():
    """Возвращает список всех телеграм‑chat_id, которые привязаны к пользователям."""
    chats = (
        db.session.query(User.telegram_chat_id)
        .filter(User.telegram_chat_id.isnot(None))
        .all()
    )
    # chats — список кортежей, поэтому разбираем
    chat_ids = [c[0] for c in chats]
    return jsonify({"chat_ids": chat_ids})


# ---------------- ADMIN PANEL ----------------

@app.route("/admin")
@admin_required  # Защита маршрута для админа
def admin_dashboard():
    users = User.query.order_by(User.id).all()  # Order by ID for stable display
    today = date.today()

    statuses = {}
    details = {}

    # Define metrics consistent with profile.html
    metrics_def = [
        ('Рост', 'height', '📏', 'см', True),
        ('Вес', 'weight', '⚖️', 'кг', False),
        ('Мышцы', 'muscle_mass', '💪', 'кг', True),
        ('Жир', 'fat_mass', '🧈', 'кг', False),
        ('Вода', 'body_water', '💧', '%', True),
        ('Метаболизм', 'metabolism', '⚡', 'ккал', True),
        ('Белок', 'protein_percentage', '🥚', '%', True),
        ('Висц. жир', 'visceral_fat_rating', '🔥', '', False),
        ('ИМТ', 'bmi', '📐', '', False),
    ]

    for u in users:
        # statuses
        has_meal = MealLog.query.filter_by(user_id=u.id, date=today).count() > 0
        has_activity = Activity.query.filter_by(user_id=u.id, date=today).count() > 0
        statuses[u.id] = {
            'meal': has_meal,
            'activity': has_activity,
            'subscription_active': u.has_subscription  # Проверяем наличие активной подписки
        }
        # meals
        meals = MealLog.query.filter_by(user_id=u.id, date=today).all()
        meals_data = [{
            'type': m.meal_type,
            'cal': m.calories,
            'prot': m.protein,
            'fat': m.fat,
            'carbs': m.carbs
        } for m in meals]

        # activity
        act = Activity.query.filter_by(user_id=u.id, date=today).first()
        activity_data = None
        if act:
            activity_data = {
                'steps': act.steps,
                'active_kcal': act.active_kcal,
                'resting_kcal': act.resting_kcal,
                'distance_km': act.distance_km,
                'hr_avg': act.heart_rate_avg
            }

        # body analysis
        last = BodyAnalysis.query.filter_by(user_id=u.id) \
            .order_by(BodyAnalysis.timestamp.desc()).first()
        prev = BodyAnalysis.query.filter_by(user_id=u.id) \
            .order_by(BodyAnalysis.timestamp.desc()).offset(1).first()

        # metrics with deltas
        metrics = []
        for label, field, icon, unit, good_up in metrics_def:
            cur = getattr(last, field, None)
            pr = getattr(prev, field, None)
            diff = pct = arrow = None
            is_good = None
            if cur is not None and pr is not None:
                diff = cur - pr
                if pr != 0:
                    pct = diff / pr * 100
                arrow = '↑' if diff > 0 else '↓' if diff < 0 else ''
                # Handle cases where diff is 0 for arrow display
                if diff == 0:
                    arrow = ''  # No arrow for no change
                    is_good = True  # Can consider no change as good/neutral
                else:
                    is_good = (diff > 0 and good_up) or (diff < 0 and not good_up)
            metrics.append({
                'label': label,
                'icon': icon,
                'unit': unit,
                'cur': cur,
                'diff': diff,
                'pct': pct,
                'arrow': arrow,
                'is_good': is_good
            })

        details[u.id] = {
            'meals': meals_data,
            'activity': activity_data,
            'metrics': metrics
        }

    return render_template(
        "admin_dashboard.html",
        users=users,
        statuses=statuses,
        details=details,
        today=today
    )


# ===== ADMIN: Заявки на подписку =====

@app.route("/admin/applications")
@admin_required
def admin_applications_list():
    """Показывает страницу со всеми заявками на подписку."""
    try:
        applications = SubscriptionApplication.query.order_by(
            SubscriptionApplication.status.asc(),
            SubscriptionApplication.created_at.desc()
        ).all()
    except Exception as e:
        flash(f"Ошибка загрузки заявок: {e}", "error")
        applications = []

    return render_template("admin_applications.html", applications=applications)


@app.route("/admin/applications/<int:app_id>/status", methods=["POST"])
@admin_required
def admin_update_application_status(app_id):
    """Обновляет статус заявки."""
    app_obj = db.session.get(SubscriptionApplication, app_id)
    if not app_obj:
        flash("Заявка не найдена", "error")
        return redirect(url_for("admin_applications_list"))

    new_status = request.form.get("status")
    # Добавлены новые статусы для логистики
    allowed_statuses = ('pending', 'processed', 'warehouse', 'in_transit', 'delivered')

    if new_status in allowed_statuses:
        try:
            old_status = app_obj.status
            app_obj.status = new_status
            db.session.commit()
            log_audit("app_status_change", "SubscriptionApplication", app_obj.id,
                      old={"status": old_status}, new={"status": new_status})

            # Опционально: отправить PUSH уведомление при смене статуса
            if new_status == 'warehouse':
                msg = "Ваш набор Sola собирается на складе 📦"
            elif new_status == 'in_transit':
                msg = "Ваш набор Sola уже в пути! 🚚"
            elif new_status == 'delivered':
                msg = "Ваш набор доставлен! 🎉"
            else:
                msg = None

            if msg:
                from notification_service import send_user_notification
                send_user_notification(user_id=app_obj.user_id, title="Статус доставки", body=msg, type="info")

            flash("Статус заявки обновлен.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Ошибка обновления: {e}", "error")
    else:
        flash("Некорректный статус.", "error")

    return redirect(url_for("admin_applications_list"))

# =======================================
@app.route("/admin/user/create", methods=["GET", "POST"])
@admin_required
def admin_create_user():
    errors = []
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        date_str = request.form.get('date_of_birth', '').strip()
        is_trainer = 'is_trainer' in request.form

        if not name:
            errors.append("Имя обязательно.")
        if not email:
            errors.append("Email обязателен.")
        if not password or len(password) < 6:
            errors.append("Пароль обязателен и должен содержать минимум 6 символов.")
        if User.query.filter_by(email=email).first():
            errors.append("Этот email уже зарегистрирован.")

        date_of_birth = None
        if date_str:
            try:
                date_of_birth = datetime.strptime(date_str, "%Y-%m-%d").date()
                if date_of_birth > date.today():
                    errors.append("Дата рождения не может быть в будущем.")
            except ValueError:
                errors.append("Некорректный формат даты рождения.")

        if errors:
            return render_template('admin_create_user.html', errors=errors, form_data=request.form)

        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            is_trainer=is_trainer
        )
        db.session.add(new_user)
        db.session.commit()
        flash(f"Пользователь '{new_user.name}' успешно создан!", "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_create_user.html", errors=errors, form_data={})


@app.route("/admin/user/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    # Fetch all historical data for the user
    meal_logs = MealLog.query.filter_by(user_id=user.id).order_by(MealLog.date.desc()).all()
    activities = Activity.query.filter_by(user_id=user.id).order_by(Activity.date.desc()).all()
    body_analyses = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).all()
    diets = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).all()

    # Determine current status for today
    today = date.today()
    has_meal_today = any(m.date == today for m in meal_logs)
    has_activity_today = any(a.date == today for a in activities)

    # For charts: last 30 days activity
    last_30_days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    activity_chart_labels = [d.strftime("%d.%m") for d in last_30_days]
    activity_steps_values = []
    activity_kcal_values = []

    activity_map = {a.date: a for a in activities if a.date in last_30_days}  # optimize lookup
    for d in last_30_days:
        activity_for_day = activity_map.get(d)
        activity_steps_values.append(activity_for_day.steps if activity_for_day else 0)
        activity_kcal_values.append(activity_for_day.active_kcal if activity_for_day else 0)

    # For charts: last 30 days diet (calories)
    diet_chart_labels = [d.strftime("%d.%m") for d in last_30_days]
    diet_kcal_values = []

    diet_map = {d.date: d for d in diets if d.date in last_30_days}  # optimize lookup
    for d in last_30_days:
        diet_for_day = diet_map.get(d)
        diet_kcal_values.append(diet_for_day.total_kcal if diet_for_day else 0)

    return render_template(
        "admin_user_detail.html",
        user=user,
        meal_logs=meal_logs,
        activities=activities,
        body_analyses=body_analyses,
        diets=diets,
        has_meal_today=has_meal_today,
        has_activity_today=has_activity_today,
        # Chart data
        activity_chart_labels=json.dumps(activity_chart_labels),
        activity_steps_values=json.dumps(activity_steps_values),
        activity_kcal_values=json.dumps(activity_kcal_values),
        diet_chart_labels=json.dumps(diet_chart_labels),
        diet_kcal_values=json.dumps(diet_kcal_values),
        today=today
    )


@app.route("/admin/user/<int:user_id>/edit", methods=["POST"])
@admin_required
def admin_user_edit(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    original_email = user.email  # Keep original email for unique check

    user.name = request.form["name"].strip()
    user.email = request.form["email"].strip()
    user.is_trainer = 'is_trainer' in request.form  # Update trainer status

    dob = request.form.get("date_of_birth")
    user.date_of_birth = datetime.strptime(dob, "%Y-%m-%d").date() if dob else None

    # Handle password change if provided
    new_password = request.form.get("password")
    if new_password:
        if len(new_password) < 6:
            flash("Новый пароль должен быть не менее 6 символов.", "error")
            return redirect(url_for("admin_user_detail", user_id=user.id))
        user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')

    # Check for duplicate email only if changed
    if user.email != original_email and User.query.filter_by(email=user.email).first():
        flash("Этот email уже занят другим пользователем.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    # Handle avatar upload
    if 'avatar' in request.files:
        file = request.files['avatar']
        if file.filename != '':
            filename = secure_filename(file.filename)
            # You might want to delete the old avatar file here if it exists
            # os.remove(os.path.join(app.config['UPLOAD_FOLDER'], user.avatar))
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            user.avatar = filename

    try:
        db.session.commit()
        flash("Данные пользователя обновлены", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Ошибка при обновлении пользователя. Возможно, email уже используется.", "error")

    return redirect(url_for("admin_user_detail", user_id=user.id))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        # === 0) Если у пользователя есть собственная группа — чистим всё, что к ней привязано
        if getattr(user, "own_group", None):
            gid = user.own_group.id

            # реакции к сообщениям группы
            msg_ids = [row[0] for row in db.session.query(GroupMessage.id).filter_by(group_id=gid).all()]
            if msg_ids:
                MessageReaction.query.filter(MessageReaction.message_id.in_(msg_ids))\
                                     .delete(synchronize_session=False)
            # сообщения группы
            GroupMessage.query.filter_by(group_id=gid).delete(synchronize_session=False)
            # задачи/объявления группы
            GroupTask.query.filter_by(group_id=gid).delete(synchronize_session=False)
            # участники группы
            GroupMember.query.filter_by(group_id=gid).delete(synchronize_session=False)
            # сама группа
            db.session.delete(user.own_group)

        # === 1) Членства пользователя в чужих группах
        GroupMember.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 2) Сообщения пользователя и реакции на них
        user_msg_ids = [row[0] for row in db.session.query(GroupMessage.id).filter_by(user_id=user.id).all()]
        if user_msg_ids:
            MessageReaction.query.filter(MessageReaction.message_id.in_(user_msg_ids))\
                                 .delete(synchronize_session=False)
        GroupMessage.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 3) Реакции, поставленные пользователем
        MessageReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 4) Тренировки, где он тренер, и записи на них
        trainer_tids = [row[0] for row in db.session.query(Training.id).filter_by(trainer_id=user.id).all()]
        if trainer_tids:
            TrainingSignup.query.filter(TrainingSignup.training_id.in_(trainer_tids))\
                                .delete(synchronize_session=False)
            Training.query.filter(Training.id.in_(trainer_tids)).delete(synchronize_session=False)

        # === 5) Записи пользователя на тренировки
        TrainingSignup.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 6) Пищевые логи / активность / анализы / диеты / логи напоминаний
        MealReminderLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        MealLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Activity.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        BodyAnalysis.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Diet.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 7) Подписки / заказы / настройки
        Subscription.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Order.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        UserSettings.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 8) Наконец, сам пользователь
        db.session.delete(user)
        db.session.commit()
        flash(f"Пользователь '{user.name}' и все связанные данные удалены.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка при удалении пользователя: {e}", "error")

    return redirect(url_for("admin_dashboard"))


@app.route('/groups')
@login_required
def groups_list():
    if not get_current_user().has_subscription:
        flash("Доступ к группам и сообществу открыт только по подписке.", "warning")
        return redirect(url_for('profile'))
    user = get_current_user()
    # если тренер — показываем его группу (или кнопку создания)
    if user.is_trainer:
        return render_template('groups_list.html', group=user.own_group)
    # обычный пользователь — список всех групп
    groups = Group.query.all()
    return render_template('groups_list.html', groups=groups)


@app.route('/groups/new', methods=['GET', 'POST'])
@login_required
def create_group():
    user = get_current_user()
    if not user.is_trainer:
        abort(403)
    if user.own_group:
        flash("Вы уже являетесь тренером группы. Вы можете создать только одну группу.", "warning")
        return redirect(url_for('group_detail', group_id=user.own_group.id))
    if request.method == 'POST':
        name = request.form['name']
        description = request.form.get('description', '').strip()
        if not name:
            flash("Название группы обязательно!", "error")
            return render_template('group_new.html')

        group = Group(name=name, description=description, trainer=user)
        db.session.add(group)
        db.session.commit()
        flash(f"Группа '{group.name}' успешно создана!", "success")
        return redirect(url_for('group_detail', group_id=group.id))
    return render_template('group_new.html')


@app.route('/groups/<int:group_id>')
@login_required

def group_detail(group_id):
    # Ваша проверка подписки здесь
    if not get_current_user().has_subscription:
        flash("Доступ к группам и сообществу открыт только по подписке.", "warning")
        # ИСПРАВЛЕНИЕ: Добавьте эту строку
        return redirect(url_for('profile'))

    group = Group.query.get_or_404(group_id)
    user = get_current_user()
    is_member = any(m.user_id == user.id for m in group.members)

    raw_messages = GroupMessage.query.filter_by(group_id=group.id).order_by(GroupMessage.timestamp.desc()).all()
    processed_messages = []
    last_sender_id = None
    for message in raw_messages:
        show_avatar = (message.user_id != last_sender_id)
        processed_messages.append({
            'id': message.id, 'group_id': message.group_id, 'user_id': message.user_id, 'user': message.user,
            'text': message.text, 'timestamp': message.timestamp, 'image_file': message.image_file,
            'reactions': message.reactions, 'show_avatar': show_avatar, 'is_current_user': (message.user_id == user.id)
        })
        last_sender_id = message.user_id
    all_posts = GroupTask.query.filter_by(group_id=group.id).order_by(GroupTask.created_at.desc()).all()

    group_member_stats = []
    if user.is_trainer and group.trainer_id == user.id:
        today = date.today()
        all_relevant_members = [m.user for m in group.members]
        if group.trainer not in all_relevant_members and group.trainer.email != ADMIN_EMAIL:
            all_relevant_members.append(group.trainer)

        for member_user in all_relevant_members:
            if member_user.email == ADMIN_EMAIL and not any(m.user_id == member_user.id for m in group.members):
                continue

            latest_analysis = BodyAnalysis.query.filter_by(user_id=member_user.id).order_by(
                BodyAnalysis.timestamp.desc()).first()

            fat_loss_progress = None
            if latest_analysis and member_user.fat_mass_goal and latest_analysis.fat_mass > member_user.fat_mass_goal:
                start_datetime = latest_analysis.timestamp

                meal_data = db.session.query(MealLog.date, func.sum(MealLog.calories)).filter(
                    MealLog.user_id == member_user.id, MealLog.date >= start_datetime.date()
                ).group_by(MealLog.date).all()
                meal_map = dict(meal_data)

                activity_data = db.session.query(Activity.date, Activity.active_kcal).filter(
                    Activity.user_id == member_user.id, Activity.date >= start_datetime.date()
                ).all()
                activity_map = dict(activity_data)

                member_metabolism = latest_analysis.metabolism or 0
                total_accumulated_deficit = 0
                delta_days = (today - start_datetime.date()).days

                if delta_days >= 0:
                    for i in range(delta_days + 1):
                        current_day = start_datetime.date() + timedelta(days=i)
                        consumed = meal_map.get(current_day, 0)
                        burned_active = activity_map.get(current_day, 0)

                        # --- ИЗМЕНЕНИЯ ЗДЕСЬ ---
                        if i == 0:  # Это день анализа
                            # Убираем калории, съеденные ДО замера
                            calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                                MealLog.user_id == member_user.id,
                                MealLog.date == current_day,
                                MealLog.created_at < start_datetime
                            ).scalar() or 0
                            consumed -= calories_before_analysis
                            # Игнорируем активность за день замера, т.к. нет точного времени
                            burned_active = 0
                        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

                        daily_deficit = (member_metabolism + burned_active) - consumed
                        if daily_deficit > 0:
                            total_accumulated_deficit += daily_deficit

                KCAL_PER_KG_FAT = 7700
                total_fat_to_lose_kg = latest_analysis.fat_mass - member_user.fat_mass_goal

                estimated_fat_burned_kg = min(total_accumulated_deficit / KCAL_PER_KG_FAT, total_fat_to_lose_kg)

                percentage = 0
                if total_fat_to_lose_kg > 0:
                    percentage = (estimated_fat_burned_kg / total_fat_to_lose_kg) * 100

                fat_loss_progress = {
                    'percentage': min(100, max(0, percentage)),
                    'initial_kg': latest_analysis.fat_mass,
                    'goal_kg': member_user.fat_mass_goal,
                    'current_kg': latest_analysis.fat_mass - estimated_fat_burned_kg
                }

                # --- ПРОВЕРКА АКТИВНОСТИ (НОВОЕ) ---
                # Ищем последнюю запись еды или активности
                last_meal = MealLog.query.filter_by(user_id=member_user.id).order_by(MealLog.date.desc()).first()
                last_act = Activity.query.filter_by(user_id=member_user.id).order_by(Activity.date.desc()).first()

                last_active_date = None
                if last_meal: last_active_date = last_meal.date
                if last_act and (not last_active_date or last_act.date > last_active_date):
                    last_active_date = last_act.date

                is_inactive = False
                days_inactive = 0

                if last_active_date:
                    days_inactive = (date.today() - last_active_date).days
                    if days_inactive >= 3:  # Если не было активности 3 дня
                        is_inactive = True
                elif not member_user.is_trainer:  # Если вообще нет записей и это не тренер
                    is_inactive = True
                    days_inactive = 999
                    # -----------------------------------

                group_member_stats.append({
                    'user': member_user,
                    'fat_loss_progress': fat_loss_progress,
                    'is_trainer_in_group': (member_user.id == group.trainer_id),
                    'is_inactive': is_inactive,  # Флаг для шаблона
                    'days_inactive': days_inactive
                })

                # Сортировка: Тренер -> Неактивные (чтобы были на виду в списке) -> Активные
            group_member_stats.sort(
                key=lambda x: (not x['is_trainer_in_group'], not x['is_inactive'], x['user'].name.lower()))

        # Получаем будущие тренировки группы
        upcoming_trainings = Training.query.filter(
            Training.group_id == group.id,
            Training.date >= date.today()
        ).order_by(Training.date, Training.start_time).all()

    return render_template('group_detail.html',
                               group=group,
                               is_member=is_member,
                               processed_messages=processed_messages,
                               group_member_stats=group_member_stats,
                               all_posts=all_posts,
                               upcoming_trainings=upcoming_trainings)  # Передаем тренировки

@app.route('/group_message/<int:message_id>/react', methods=['POST'])
@login_required
def react_to_message(message_id):
    message = GroupMessage.query.get_or_404(message_id)
    user = get_current_user()

    existing_reaction = MessageReaction.query.filter_by(
        message_id=message_id,
        user_id=user.id
    ).first()

    user_reacted = False
    if existing_reaction:
        db.session.delete(existing_reaction)
    else:
        reaction = MessageReaction(message=message, user=user, reaction_type='👍')
        db.session.add(reaction)
        user_reacted = True

    db.session.commit()

    new_like_count = MessageReaction.query.filter_by(message_id=message_id).count()

    return jsonify({
        "success": True,
        "new_like_count": new_like_count,
        "user_reacted": user_reacted
    })


@app.route('/api/groups/<int:group_id>/messages')
@login_required
def get_group_messages(group_id):
    # Убедимся, что группа существует
    Group.query.get_or_404(group_id)
    user_id = get_current_user().id

    messages = GroupMessage.query.filter_by(group_id=group_id).order_by(GroupMessage.timestamp.asc()).all()

    # Собираем данные в нужный формат
    results = []
    for msg in messages:
        reactions_data = []
        user_has_reacted = False
        for reaction in msg.reactions:
            reactions_data.append({'user_id': reaction.user_id})
            if reaction.user_id == user_id:
                user_has_reacted = True

        results.append({
            "id": msg.id,
            "text": msg.text,
            "image_url": url_for('serve_file', filename=msg.image_file) if msg.image_file else None,
            "user": {
                "name": msg.user.name,
                "avatar_url": url_for('serve_file', filename=msg.user.avatar.filename) if msg.user.avatar else url_for(
                    'static', filename='default-avatar.png')
            },
            "is_current_user": msg.user_id == user_id,
            "reactions_count": len(reactions_data),
            "current_user_reacted": user_has_reacted
        })
    return jsonify(results)

@app.route('/groups/<int:group_id>/tasks/new', methods=['POST'])
@login_required
def create_group_task(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    # Only the group's trainer can create tasks/announcements
    if not (user.is_trainer and group.trainer_id == user.id):
        abort(403)

    title = request.form['title'].strip()
    description = request.form.get('description', '').strip()
    is_announcement = 'is_announcement' in request.form
    due_date_str = request.form.get('due_date')

    if not title:
        flash("Заголовок обязателен.", "error")
        return redirect(url_for('group_detail', group_id=group_id))

    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash("Неверный формат даты. Используйте ГГГГ-ММ-ДД.", "error")
            return redirect(url_for('group_detail', group_id=group_id))

    task = GroupTask(
        group=group,
        trainer=user,
        title=title,
        description=description,
        is_announcement=is_announcement,
        due_date=due_date
    )
    db.session.add(task)
    db.session.commit()  # Сначала сохраняем задачу

    # --- НАЧАЛО НОВОГО КОДА ---
    try:
        # Собираем chat_id всех участников группы
        chat_ids = [member.user.telegram_chat_id for member in group.members if member.user.telegram_chat_id]

        if chat_ids:
            # Формируем сообщение
            task_type = "Объявление" if is_announcement else "Новая задача"
            message_text = f"🔔 **{task_type} от тренера {user.name}**\n\n**{title}**\n\n_{description}_"

            # URL вашего бота (нужно будет указать, когда бот будет на сервере)
            BOT_WEBHOOK_URL = os.getenv("BOT_WEBHOOK_URL")
            BOT_SECRET_TOKEN = os.getenv("BOT_SECRET_TOKEN")

            if BOT_WEBHOOK_URL and BOT_SECRET_TOKEN:
                payload = {
                    "chat_ids": chat_ids,
                    "message": message_text,
                    "secret": BOT_SECRET_TOKEN
                }
                # Отправляем запрос боту, не дожидаясь ответа
                print(f"INFO: Sending notification to bot at {BOT_WEBHOOK_URL} for {len(chat_ids)} users.")
                requests.post(BOT_WEBHOOK_URL, json=payload, timeout=2)
            else:
                print("WARNING: BOT_WEBHOOK_URL or BOT_SECRET_TOKEN not set in .env. Skipping notification.")

    except Exception as e:
        print(f"Failed to send notification to bot: {e}")
    # --- КОНЕЦ НОВОГО КОДА ---

    flash(f"{'Объявление' if is_announcement else 'Задача'} '{title}' успешно добавлено!", "success")
    return redirect(url_for('group_detail', group_id=group_id))


# Добавьте в app.py
@app.route('/api/user_progress/<int:chat_id>')
def get_user_progress(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()

    analyses = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).limit(2).all()

    if len(analyses) == 0:
        return jsonify({"error": "Нет данных для сравнения"}), 404

    latest = analyses[0]
    previous = analyses[1] if len(analyses) > 1 else None

    def serialize(analysis):
        if not analysis: return None
        return {
            "date": analysis.timestamp.strftime('%d.%m.%Y'),
            "weight": analysis.weight,
            "fat_mass": analysis.fat_mass,
            "muscle_mass": analysis.muscle_mass
        }

    return jsonify({
        "latest": serialize(latest),
        "previous": serialize(previous)
    })

# Добавьте в app.py

@app.route('/api/meal_history/<int:chat_id>')
def get_meal_history(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()
    page = request.args.get('page', 1, type=int)

    # Группируем приемы пищи по дням и считаем сумму калорий
    daily_meals = db.session.query(
        MealLog.date,
        func.sum(MealLog.calories).label('total_calories'),
        func.count(MealLog.id).label('meal_count')
    ).filter_by(user_id=user.id).group_by(MealLog.date).order_by(MealLog.date.desc()).paginate(page=page, per_page=5, error_out=False)

    return jsonify({
        "days": [
            {"date": d.date.strftime('%d.%m.%Y'), "total_calories": d.total_calories, "meal_count": d.meal_count}
            for d in daily_meals.items
        ],
        "has_next": daily_meals.has_next,
        "has_prev": daily_meals.has_prev,
        "page": page
    })

@app.route('/api/activity_history/<int:chat_id>')
def get_activity_history(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()
    page = request.args.get('page', 1, type=int)

    daily_activity = Activity.query.filter_by(user_id=user.id).order_by(Activity.date.desc()).paginate(page=page, per_page=5, error_out=False)

    return jsonify({
        "days": [
            {"date": a.date.strftime('%d.%m.%Y'), "steps": a.steps, "active_kcal": a.active_kcal}
            for a in daily_activity.items
        ],
        "has_next": daily_activity.has_next,
        "has_prev": daily_activity.has_prev,
        "page": page
    })

@app.route('/groups/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def delete_group_task(task_id):
    task = GroupTask.query.get_or_404(task_id)
    user = get_current_user()

    # Only the trainer who created it (or group's trainer) can delete
    if not (user.is_trainer and task.trainer_id == user.id):
        abort(403)

    db.session.delete(task)
    db.session.commit()
    flash(f"{'Объявление' if task.is_announcement else 'Задача'} '{task.title}' удалено.", "info")
    return redirect(url_for('group_detail', group_id=task.group_id))


# Route for handling image uploads with chat messages
@app.route('/groups/<int:group_id>/message/image', methods=['POST'])
@login_required
def post_group_image_message(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()
    is_member = any(m.user_id == user.id for m in group.members)

    if not (user.is_trainer and group.trainer_id == user.id or is_member):
        abort(403)

    text = request.form.get('text', '').strip()
    file = request.files.get('image')

    image_filename = None
    if file and file.filename != '':
        unique_filename = f"chat_{group_id}_{uuid.uuid4().hex}.png"
        image_data = file.read()

        output_buffer = BytesIO()
        with Image.open(BytesIO(image_data)) as img:
            img.thumbnail(CHAT_IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)
            img.save(output_buffer, format="PNG")
        resized_data = output_buffer.getvalue()

        new_file = UploadedFile(
            filename=unique_filename,
            content_type='image/png',
            data=resized_data,
            size=len(resized_data),
            user_id=user.id
        )
        db.session.add(new_file)
        image_filename = unique_filename

    if not text and not image_filename:
        return jsonify({"error": "Сообщение не может быть пустым"}), 400

    msg = GroupMessage(group=group, user=user, text=text, image_file=image_filename)
    db.session.add(msg)
    db.session.commit()

    # Вместо редиректа возвращаем JSON с данными нового сообщения
    return jsonify({
        "success": True,
        "message": {
            "id": msg.id,
            "text": msg.text,
            "image_url": url_for('serve_file', filename=msg.image_file) if msg.image_file else None,
            "user": {
                "name": user.name,
                "avatar_url": url_for('serve_file', filename=user.avatar.filename) if user.avatar else url_for('static',
                                                                                                               filename='default-avatar.png')
            },
            "is_current_user": True,
            "reactions": []
        }
    })

@app.route('/groups/<int:group_id>/join', methods=['POST'])
@login_required
def join_group(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    # Prevent joining if already a member
    if GroupMember.query.filter_by(group_id=group.id, user_id=user.id).first():
        flash("Вы уже состоите в этой группе.", "info")
        return redirect(url_for('group_detail', group_id=group.id))

    # Prevent trainer from joining another group as a member
    if user.is_trainer and user.own_group and user.own_group.id != group_id:
        flash("Как тренер, вы не можете присоединиться к другой группе.", "error")
        return redirect(url_for('groups_list'))

    member = GroupMember(group=group, user=user)
    db.session.add(member)
    db.session.commit()
    flash(f"Вы успешно присоединились к группе '{group.name}'!", "success")
    return redirect(url_for('group_detail', group_id=group.id))


@app.route('/groups/<int:group_id>/leave', methods=['POST'])
@login_required
def leave_group(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    member = GroupMember.query.filter_by(group_id=group.id, user_id=user.id).first()
    if not member:
        flash("Вы не состоите в этой группе.", "info")
        return redirect(url_for('group_detail', group_id=group_id))

    # Prevent trainers from leaving their own group if they are the trainer
    if user.is_trainer and group.trainer_id == user.id:
        flash("Как тренер, вы не можете покинуть свою собственную группу.", "error")
        return redirect(url_for('group_detail', group_id=group_id))

    db.session.delete(member)
    db.session.commit()
    flash(f"Вы покинули группу '{group.name}'.", "success")
    return redirect(url_for('groups_list'))


# --- Admin Group Management ---

@app.route("/admin/groups")
@admin_required
def admin_groups_list():
    groups = Group.query.all()
    return render_template("admin_groups_list.html", groups=groups)


@app.route("/admin/groups/<int:group_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_group(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        flash("Группа не найдена.", "error")
        return redirect(url_for("admin_groups_list"))

    trainers = User.query.filter_by(is_trainer=True).all()  # For assigning new trainer

    if request.method == "POST":
        group.name = request.form['name'].strip()
        group.description = request.form.get('description', '').strip()
        new_trainer_id = request.form.get('trainer_id')

        # Check for unique group name (if you want to enforce this)
        # existing_group = Group.query.filter(Group.name == group.name, Group.id != group_id).first()
        # if existing_group:
        #     flash("Группа с таким названием уже существует.", "error")
        #     return render_template("admin_edit_group.html", group=group, trainers=trainers)

        if new_trainer_id and int(new_trainer_id) != group.trainer_id:
            # Check if new trainer already owns a group
            potential_trainer = db.session.get(User, int(new_trainer_id))
            if potential_trainer and potential_trainer.own_group and potential_trainer.own_group.id != group_id:
                flash(f"Тренер {potential_trainer.name} уже руководит другой группой.", "error")
                return render_template("admin_edit_group.html", group=group, trainers=trainers)
            group.trainer_id = int(new_trainer_id)
            group.trainer.is_trainer = True  # Ensure new trainer is marked as trainer

        db.session.commit()
        flash("Группа успешно обновлена.", "success")
        return redirect(url_for("admin_groups_list"))

    return render_template("admin_edit_group.html", group=group, trainers=trainers)


@app.route("/admin/groups/<int:group_id>/delete", methods=["POST"])
@admin_required
def admin_delete_group(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        flash("Группа не найдена.", "error")
        return redirect(url_for("admin_groups_list"))

    try:
        db.session.delete(group)  # Cascade will delete members, messages, tasks
        db.session.commit()
        flash(f"Группа '{group.name}' и все связанные данные удалены.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка при удалении группы: {e}", "error")
    return redirect(url_for("admin_groups_list"))


@app.route("/admin/squads/distribution")
@admin_required
def admin_squads_distribution():
    # Ищем пользователей, подавших заявку (pending)
    # Можно также добавить тех, кто 'none', но имеет заполненные предпочтения, если нужно
    pending_users = User.query.filter(
        User.squad_status == 'pending'
    ).order_by(User.updated_at.desc()).all()

    groups = Group.query.order_by(Group.name).all()

    # Собираем статистику по группам (сколько мест занято)
    # Group.members - это relationship, можно использовать len()
    groups_data = []
    for g in groups:
        groups_data.append({
            "id": g.id,
            "name": g.name,
            "count": len(g.members),
            "trainer_name": g.trainer.name if g.trainer else "Нет тренера"
        })

    return render_template(
        "admin_squads_distribution.html",
        users=pending_users,
        groups=groups_data
    )


# --- SQUADS API ---

@app.route('/api/groups/my', methods=['GET'])
@login_required
def api_my_group():
    u = get_current_user()

    # Ищем группу, где юзер - участник
    member_record = GroupMember.query.filter_by(user_id=u.id).first()
    if not member_record:
        return jsonify({"ok": True, "group": None})

    g = member_record.group

    # Собираем участников
    members_data = []

    # Определяем начало текущей недели (понедельник)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())

    for m in g.members:
        # Считаем сумму баллов за текущую неделю
        weekly_score = db.session.query(func.sum(SquadScoreLog.points)).filter(
            SquadScoreLog.user_id == m.user.id,
            func.date(SquadScoreLog.created_at) >= start_of_week
        ).scalar() or 0

        members_data.append({
            "id": m.user.id,
            "name": m.user.name,
            "avatar_filename": m.user.avatar.filename if m.user.avatar else None,
            "is_me": (m.user.id == u.id),
            "score": int(weekly_score)  # Реальные баллы
        })

    # Сортируем по очкам
    members_data.sort(key=lambda x: x['score'], reverse=True)

    # --- Ищем ближайшую будущую тренировку группы ---
    next_training = Training.query.filter(
        Training.group_id == g.id,
        Training.date >= date.today()
    ).order_by(Training.date, Training.start_time).all()

    now = datetime.now()
    next_training_iso = None

    for t in next_training:
        # Собираем полный datetime
        dt = datetime.combine(t.date, t.start_time)
        if dt > now:
            next_training_iso = dt.isoformat()
            break
    # ------------------------------------------------

    group_data = {
        "id": g.id,
        "name": g.name,
        "next_training_iso": next_training_iso,
        "description": g.description,
        "trainer_name": g.trainer.name if g.trainer else "Тренер",
        "trainer_avatar": g.trainer.avatar.filename if g.trainer and g.trainer.avatar else None,
        "members": members_data,
        "is_trainer": (g.trainer_id == u.id)
    }

    return jsonify({"ok": True, "group": group_data})

# --- ADMIN ASSIGN UPDATE ---

@app.route("/admin/squads/assign", methods=["POST"])
@admin_required
def admin_assign_squad():
    user_id = request.form.get("user_id")
    group_id = request.form.get("group_id")

    if not user_id or not group_id:
        flash("Ошибка: не выбран пользователь или группа", "error")
        return redirect(url_for("admin_squads_distribution"))

    try:
        u = db.session.get(User, user_id)
        g = db.session.get(Group, group_id)

        if u and g:
            # 1. Проверяем, не состоит ли уже
            existing = GroupMember.query.filter_by(user_id=u.id, group_id=g.id).first()
            if not existing:
                member = GroupMember(group=g, user=u)
                db.session.add(member)

            # 2. Обновляем статус пользователя
            u.squad_status = 'active'
            # Ставим флаг, чтобы показать экран поздравления при следующем входе
            u.is_new_squad_member = True
            db.session.commit()

            # 3. Отправляем PUSH уведомление
            send_user_notification(
                user_id=u.id,
                title=f"Вы приняты в отряд {g.name}! 🔥",
                body="Тренер подтвердил заявку. Заходите знакомиться с командой.",
                type="success",
                data={"route": "/squad"}
            )

            flash(f"Пользователь {u.name} добавлен в {g.name}", "success")
        else:
            flash("Пользователь или группа не найдены", "error")

    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка: {e}", "error")

    return redirect(url_for("admin_squads_distribution"))

# Найдите и замените существующую функцию admin_grant_subscription

@app.route('/api/ack_squad_entry', methods=['POST'])
@login_required
def ack_squad_entry():
    """
    Сбрасывает флаг is_new_squad_member, когда пользователь увидел экран поздравления.
    """
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        user.is_new_squad_member = False
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/admin/user/<int:user_id>/subscribe", methods=["POST"])
@admin_required
def admin_grant_subscription(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    duration = request.form.get('duration')
    if not duration:
        flash("Не выбран период подписки.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    today = date.today()
    end_date = None

    # Определяем дату окончания на основе выбора
    if duration == '1m':
        end_date = today + timedelta(days=30)
        message = "Подписка на 1 месяц успешно выдана!"
    elif duration == '3m':
        end_date = today + timedelta(days=90)
        message = "Подписка на 3 месяца успешно выдана!"
    elif duration == '6m':
        end_date = today + timedelta(days=180)
        message = "Подписка на 6 месяцев успешно выдана!"
    elif duration == '12m':
        end_date = today + timedelta(days=365)
        message = "Подписка на 1 год успешно выдана!"
    elif duration == 'unlimited':
        end_date = None  # None означает безлимитную подписку
        message = "Безлимитная подписка успешно выдана!"
    else:
        flash("Некорректный период подписки.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    existing_subscription = Subscription.query.filter_by(user_id=user.id).first()

    if existing_subscription:
        # Если подписка уже есть, обновляем её
        existing_subscription.start_date = today
        existing_subscription.end_date = end_date
        existing_subscription.source = 'admin_update'
    else:
        # Если подписки нет, создаем новую
        new_subscription = Subscription(
            user_id=user.id,
            start_date=today,
            end_date=end_date,
            source='admin_grant'
        )
        db.session.add(new_subscription)

    db.session.commit()
    flash(message, "success")
    return redirect(url_for("admin_user_detail", user_id=user.id))



@app.route("/admin/user/<int:user_id>/manage_subscription", methods=["POST"])
@admin_required
def manage_subscription(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    action = request.form.get('action')
    sub = Subscription.query.filter_by(user_id=user_id).first()
    today = date.today()

    try:
        if action == 'grant':
            duration = request.form.get('duration')
            start_date_str = request.form.get('start_date')

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else today

            end_date = None
            if duration == 'unlimited':
                end_date = None
            else:  # 1m, 3m, 6m, 12m
                months = {'1m': 1, '3m': 3, '6m': 6, '12m': 12}
                # Рассчитываем дельту от даты старта
                end_date = start_date + timedelta(days=30 * months.get(duration, 0))

            if sub:
                sub.start_date = start_date
                sub.end_date = end_date
                sub.status = 'active'
                sub.remaining_days_on_freeze = None
                flash("Подписка успешно обновлена.", "success")
            else:
                sub = Subscription(user_id=user.id, start_date=start_date, end_date=end_date, source='admin_grant')
                db.session.add(sub)
                flash("Подписка успешно выдана.", "success")

                # --- ДОБАВЬТЕ ЭТУ СТРОКУ ---
                # Устанавливаем флаг, чтобы показать пользователю приветствие
            user.show_welcome_popup = True
        elif action == 'remove':
            if sub:
                db.session.delete(sub)
                flash("Подписка успешно удалена.", "success")
            else:
                flash("У пользователя нет подписки для удаления.", "warning")

        elif action == 'freeze':
            if sub and sub.status == 'active' and sub.end_date:
                remaining = (sub.end_date - today).days
                sub.remaining_days_on_freeze = max(0, remaining)  # Сохраняем оставшиеся дни
                sub.status = 'frozen'
                flash(f"Подписка заморожена. Оставалось дней: {sub.remaining_days_on_freeze}", "success")
            else:
                flash("Невозможно заморозить: подписка неактивна, безлимитная или уже заморожена.", "warning")

        elif action == 'unfreeze':
            if sub and sub.status == 'frozen':
                days_to_add = sub.remaining_days_on_freeze or 0
                sub.end_date = today + timedelta(days=days_to_add)  # Восстанавливаем срок
                sub.status = 'active'
                sub.remaining_days_on_freeze = None
                flash(f"Подписка разморожена. Новая дата окончания: {sub.end_date.strftime('%d.%m.%Y')}", "success")
            else:
                flash("Подписка не была заморожена.", "warning")

        else:
            flash("Неизвестное действие.", "error")

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        flash(f"Произошла ошибка: {e}", "error")

    return redirect(url_for("admin_user_detail", user_id=user.id))

@app.route('/api/dismiss_welcome_popup', methods=['POST'])
@login_required
def dismiss_welcome_popup():
    """API-маршрут, который вызывается, когда пользователь закрывает приветственное окно."""
    user = get_current_user()
    if user:
        user.show_welcome_popup = False
        db.session.commit()
        return jsonify({'status': 'ok'}), 200
    return jsonify({'status': 'error', 'message': 'User not found'}), 404


@app.route('/api/create_application', methods=['POST'])
@login_required
def create_application():
    u = get_current_user()
    if not u:
        return jsonify(success=False, message="Не авторизованы."), 401

    # 1. Проверяем, может у пользователя УЖЕ ЕСТЬ подписка
    if getattr(u, "subscription_status", None) == 'active':
        return jsonify(success=False, message="У вас уже есть действующая подписка."), 400

    # 2. Проверяем, нет ли у него УЖЕ ОТКРЫТОЙ ЗАЯВКИ
    existing_app = SubscriptionApplication.query.filter_by(user_id=u.id, status='pending').first()
    if existing_app:
        return jsonify(success=True, message="У вас уже есть активная заявка. Мы скоро с вами свяжемся.")

    data = request.json
    phone = data.get('phone')

    # 3. Валидация номера
    if not phone or len(phone) < 7:
        return jsonify(success=False, message="Пожалуйста, введите корректный номер телефона."), 400

    # 4. Все в порядке, создаем заявку
    try:
        new_app = SubscriptionApplication(
            user_id=u.id,
            phone_number=phone
        )
        db.session.add(new_app)
        db.session.commit()

        track_event('application_created', u.id)
        return jsonify(success=True, message="Ваша заявка принята, мы скоро с вами свяжемся.")

    except Exception as e:
        db.session.rollback()
        print(f"!!! Ошибка при создании заявки: {e}")
        return jsonify(success=False, message="Произошла ошибка на сервере. Попробуйте позже."), 500

@app.route('/subscription/manage', methods=['POST'])
@login_required
def manage_user_subscription():
    user = get_current_user()
    action = request.form.get('action')
    sub = user.subscription  # Получаем подписку текущего пользователя

    if not sub:
        flash("У вас нет активной подписки для управления.", "warning")
        return redirect(url_for('profile'))

    today = date.today()

    try:
        if action == 'freeze':
            if sub.status == 'active' and sub.end_date:
                remaining_days = (sub.end_date - today).days
                if remaining_days > 0:
                    sub.status = 'frozen'
                    sub.remaining_days_on_freeze = remaining_days
                    flash(f"Подписка успешно заморожена. Оставалось {remaining_days} дней.", "success")
                else:
                    flash("Срок действия подписки уже истёк, заморозка невозможна.", "warning")
            else:
                flash("Эту подписку невозможно заморозить.", "warning")

        elif action == 'unfreeze':
            if sub.status == 'frozen':
                days_to_add = sub.remaining_days_on_freeze or 0
                sub.end_date = today + timedelta(days=days_to_add)
                sub.status = 'active'
                sub.remaining_days_on_freeze = None
                flash(f"Подписка разморожена! Новая дата окончания: {sub.end_date.strftime('%d.%m.%Y')}", "success")
            else:
                flash("Подписка не была заморожена.", "warning")

        else:
            flash("Неизвестное действие.", "error")

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        flash(f"Произошла ошибка: {e}", "error")

    return redirect(url_for('profile'))


# ... другие маршруты

@app.route('/welcome-guide')
@login_required  # Только для залогиненных пользователей
def welcome_guide():
    # Убедимся, что у пользователя есть подписка, чтобы видеть эту страницу
    if not get_current_user().has_subscription:
        flash("Эта страница доступна только для пользователей с активной подпиской.", "warning")
        return redirect(url_for('profile'))

    return render_template('welcome_guide.html')



@app.route('/api/user/weekly_summary')
@login_required
def weekly_summary():
    if not get_current_user().has_subscription:
        return jsonify({"error": "Subscription required"}), 403

    user_id = session.get('user_id')
    today = date.today()
    week_ago = today - timedelta(days=6)

    labels = [(week_ago + timedelta(days=i)).strftime("%a") for i in range(7)]

    # 1. Данные по весу (здесь ошибки не было, код без изменений)
    from sqlalchemy import text  # у тебя уже импортирован

    weight_sql = text("""
        SELECT EXTRACT(DOW FROM timestamp) AS day_of_week, AVG(weight) AS avg_weight
        FROM body_analysis
        WHERE user_id = :user_id AND DATE(timestamp) BETWEEN :week_ago AND :today
        GROUP BY day_of_week
        ORDER BY day_of_week
    """)
    weight_data = db.session.execute(
        weight_sql, {"user_id": user_id, "week_ago": week_ago, "today": today}
    ).fetchall()

    # 2. Потребленные калории (сумма за каждый день)
    meals_sql = text("""
        SELECT date, SUM(calories) as total_calories FROM meal_logs
        WHERE user_id = :user_id AND date BETWEEN :week_ago AND :today
        GROUP BY date
    """)
    meal_logs = db.session.execute(meals_sql, {'user_id': user_id, 'week_ago': week_ago, 'today': today}).fetchall()

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Убираем .strftime(), так как row.date уже является строкой 'YYYY-MM-DD'
    meals_map = {row.date: row.total_calories for row in meal_logs}

    # 3. Сожженные активные калории
    activity_sql = text("""
        SELECT date, active_kcal FROM activity
        WHERE user_id = :user_id AND date BETWEEN :week_ago AND :today
    """)
    activities = db.session.execute(activity_sql, {'user_id': user_id, 'week_ago': week_ago, 'today': today}).fetchall()

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # То же самое: убираем .strftime()
    activity_map = {row.date: row.active_kcal for row in activities}

    # --- Сбор данных для уровней (High/Med/Low) ---

    # Считаем количество приемов пищи по дням (count distinct meal_type)
    meals_count_sql = text("""
            SELECT date, COUNT(DISTINCT meal_type) as cnt FROM meal_logs
            WHERE user_id = :user_id AND date BETWEEN :week_ago AND :today
            GROUP BY date
        """)
    meals_count_rows = db.session.execute(meals_count_sql,
                                          {'user_id': user_id, 'week_ago': week_ago, 'today': today}).fetchall()
    meals_count_map = {row.date: row.cnt for row in meals_count_rows}

    # Считаем шаги по дням
    steps_sql = text("""
            SELECT date, steps FROM activity
            WHERE user_id = :user_id AND date BETWEEN :week_ago AND :today
        """)
    steps_rows = db.session.execute(steps_sql, {'user_id': user_id, 'week_ago': week_ago, 'today': today}).fetchall()
    steps_map = {row.date: row.steps for row in steps_rows}

    user_step_goal = getattr(get_current_user(), 'step_goal', 10000) or 10000

    nutrition_levels = []
    activity_levels = []

    # Проходим по дням недели
    for i in range(7):
        d_obj = week_ago + timedelta(days=i)

        # 1. Уровень питания
        # 2 приема = Low (0), 3 приема = Medium (1), 4 приема = High (2)
        # (Если 0-1 прием, тоже считаем Low)
        m_count = meals_count_map.get(d_obj, 0)
        if m_count >= 4:
            n_level = 2  # High
        elif m_count == 3:
            n_level = 1  # Medium
        else:
            n_level = 0  # Low
        nutrition_levels.append(n_level)

        # 2. Уровень активности
        # 100% цели = High (2), >=50% = Medium (1), <50% = Low (0)
        steps = steps_map.get(d_obj, 0)
        pct = steps / user_step_goal
        if pct >= 1.0:
            a_level = 2  # High
        elif pct >= 0.5:
            a_level = 1  # Medium
        else:
            a_level = 0  # Low
        activity_levels.append(a_level)

    # Собираем данные в массивы по дням (существующая логика веса остается)
    weight_values = [
        next((w.avg_weight for w in weight_data if int(w.day_of_week) == (week_ago + timedelta(days=i)).weekday()),
             None) for i in range(7)]

    # Оставляем калории для совместимости, но добавляем уровни
    consumed_kcal_values = [meals_map.get((week_ago + timedelta(days=i)).strftime('%Y-%m-%d'), 0) for i in range(7)]
    burned_kcal_values = [activity_map.get((week_ago + timedelta(days=i)).strftime('%Y-%m-%d'), 0) for i in range(7)]

    return jsonify({
        "labels": labels,
        "datasets": {
            "weight": weight_values,
            "consumed_kcal": consumed_kcal_values,
            "burned_kcal": burned_kcal_values,
            "nutrition_levels": nutrition_levels,  # <-- Новые данные [0, 1, 2...]
            "activity_levels": activity_levels  # <-- Новые данные [0, 1, 2...]
        }
    })


@app.route('/api/user/deficit_history')
@login_required
def deficit_history():
    user = get_current_user()
    latest_analysis = user.latest_analysis

    # 1. Определяем точку старта
    if user.initial_body_analysis_id:
        start_point = db.session.get(BodyAnalysis, user.initial_body_analysis_id)
        # Если start_point найден, берем его дату, иначе fallback
        start_datetime = start_point.timestamp if start_point else datetime.now(UTC)
    elif latest_analysis:
        start_datetime = latest_analysis.timestamp
    else:
        return jsonify({"error": "Нет данных замеров"}), 404

    today = date.today()

    # 2. Запрашиваем данные
    meal_logs = MealLog.query.filter(
        MealLog.user_id == user.id,
        MealLog.date >= start_datetime.date()
    ).all()
    activity_logs = Activity.query.filter(
        Activity.user_id == user.id,
        Activity.date >= start_datetime.date()
    ).all()

    # Получаем замеры
    body_analyses = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        func.date(BodyAnalysis.timestamp) >= start_datetime.date()
    ).all()
    analysis_map = {b.timestamp.date(): b for b in body_analyses}

    # Словари
    meals_map = {}
    for log in meal_logs:
        if log.date not in meals_map:
            meals_map[log.date] = 0
        meals_map[log.date] += log.calories

    activity_map = {log.date: log.active_kcal for log in activity_logs}

    history_data = []
    # Метаболизм берем из последнего известного замера или дефолт
    metabolism = latest_analysis.metabolism if latest_analysis else 1600

    delta_days = (today - start_datetime.date()).days

    # 3. Собираем список (он получается от Старого -> к Новому)
    for i in range(delta_days + 1):
        current_day = start_datetime.date() + timedelta(days=i)
        consumed = meals_map.get(current_day, 0)
        burned_active = activity_map.get(current_day, 0)

        # Корректировка для первого дня (если нужно учитывать время замера)
        if i == 0:
            calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                MealLog.user_id == user.id,
                MealLog.date == current_day,
                MealLog.created_at < start_datetime
            ).scalar() or 0
            consumed -= calories_before_analysis
            burned_active = 0

        total_burned = metabolism + burned_active
        daily_deficit = total_burned - consumed
        if daily_deficit < 0:
            daily_deficit = 0  # (опционально: показывать 0 вместо отрицательного дефицита)

        day_analysis = analysis_map.get(current_day)

        item = {
            "date": current_day.strftime('%d.%m.%Y'),
            "consumed": consumed,
            "base_metabolism": metabolism,
            "burned_active": burned_active,
            "total_burned": total_burned,
            "deficit": daily_deficit,
            "is_measurement_day": day_analysis is not None,
            "weight": day_analysis.weight if day_analysis else None,
            "bmi": day_analysis.bmi if day_analysis else None
        }

        if day_analysis:
            # Считаем процент жира, если есть данные массы и веса
            fat_perc = 0.0
            if day_analysis.weight and day_analysis.weight > 0 and day_analysis.fat_mass:
                fat_perc = (day_analysis.fat_mass / day_analysis.weight) * 100

            # Добавляем детальные метрики для экрана BodyAnalysisDetailsPage
            item.update({
                "timestamp": day_analysis.timestamp.isoformat(),
                "body_age": day_analysis.body_age,
                "fat_mass_kg": day_analysis.fat_mass,
                "body_fat_percentage": fat_perc,
                "muscle_mass_kg": day_analysis.muscle_mass,
                "water_percentage": day_analysis.body_water,
                "visceral_fat_level": day_analysis.visceral_fat_rating,
                "bone_mass_kg": day_analysis.bone_mineral_percentage,
                "bmr_kcal": day_analysis.metabolism,
                "protein_percentage": day_analysis.protein_percentage
            })

        history_data.append(item)

    # --- ВАЖНОЕ ИЗМЕНЕНИЕ: Разворачиваем список, чтобы новые были сверху ---
    history_data.reverse()
    # -----------------------------------------------------------------------

    return jsonify(history_data)
@app.route("/purchase")
def purchase_page():
    user_id = session.get('user_id')
    if user_id:
        track_event('paywall_viewed', user_id)
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "kilograpptestbot")
    return render_template("purchase.html", bot_username=bot_username)


from sqlalchemy.exc import IntegrityError


@app.route('/api/trainings/<int:tid>/signup', methods=['POST'])
def signup_training(tid):
    u = get_current_user()
    if not u:
        abort(401)

    t = Training.query.get_or_404(tid)

    # Нельзя записываться на прошедшие
    now = datetime.now()
    if datetime.combine(t.date, t.end_time) <= now:
        abort(400, description="Тренировка уже прошла")

    # --- ИСПРАВЛЕНИЕ: Сначала определяем переменную already ---
    already = TrainingSignup.query.filter_by(training_id=t.id, user_id=u.id).first()

    # Проверка на лимит мест
    seats_taken = len(t.signups)
    capacity = t.capacity or 0

    # Проверяем лимит, ТОЛЬКО если capacity > 0. Если 0, то безлимит.
    if capacity > 0 and not already and seats_taken >= capacity:
        abort(409, description="Нет свободных мест")

    if already:
        # Идемпотентность — просто вернём текущий статус
        return jsonify({"ok": True, "data": t.to_dict(u.id)})

    s = TrainingSignup(training_id=t.id, user_id=u.id)
    db.session.add(s)
    try:
        # --- ПРОВЕРКА АЧИВОК ---
        check_all_achievements(u)
        # -----------------------
        db.session.commit()

        # [FIX] Обновляем объект t, чтобы подтянулся новый список signups
        db.session.refresh(t)

    except IntegrityError:
        db.session.rollback()
        # Если ошибка целостности, значит запись уже есть - тоже обновляем состояние
        db.session.refresh(t)
        return jsonify({"ok": True, "data": t.to_dict(u.id)})

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/api/trainings/<int:tid>/signup', methods=['DELETE'])
def cancel_signup(tid):
    u = get_current_user()
    if not u:
        abort(401)

    t = Training.query.get_or_404(tid)
    s = TrainingSignup.query.filter_by(training_id=t.id, user_id=u.id).first()
    if not s:
        abort(404, description="Запись не найдена")

    db.session.delete(s)
    db.session.commit()
    db.session.refresh(t)  # <--- ДОБАВЛЕНО: Обновляем объект t из БД

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/trainings-calendar')
def trainings_calendar_page():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    u = get_current_user()
    return render_template('trainings-calendar.html', me_id=(u.id if u else None))

@app.post("/api/dismiss_renewal_reminder")
@login_required
def dismiss_renewal_reminder():
    u = get_current_user()
    if not u:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    u.renewal_reminder_last_shown_on = date.today()
    db.session.commit()
    return jsonify({"ok": True})

@app.get("/api/me/telegram/status")
@login_required
def telegram_status():
    u = get_current_user()
    return jsonify({"linked": bool(u and u.telegram_chat_id)})

@app.route('/api/me/telegram/settings')
@login_required
def get_tg_settings():
    from models import db
    u = get_current_user()
    s = get_effective_user_settings(u)  # <-- синхронизация, если пусто

    payload = {
        "ok": True,
        "telegram_notify_enabled": bool(s.telegram_notify_enabled),
        "notify_trainings":        bool(s.notify_trainings),
        "notify_subscription":     bool(s.notify_subscription),
        "notify_meals":            bool(s.notify_meals),
        # алиас для старого фронта
        "notify_promos":           bool(s.notify_subscription),
    "meal_timezone":           s.meal_timezone or "Asia/Almaty",  # ← дефолт Алматы

    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp




# app.py
@app.route("/devices")
def devices():
    return render_template("devices.html")
bp = Blueprint("settings_api", __name__, url_prefix="/bp")

@bp.route("/api/me/telegram/settings", methods=["GET"])
def get_tg_settings():
    u = get_current_user()
    if not u:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    s = u.settings or UserSettings(user_id=u.id)
    if not u.settings:
        db.session.add(s); db.session.commit()
    return jsonify({
        "ok": True,
        "telegram_notify_enabled": bool(s.telegram_notify_enabled),
        "notify_trainings":        bool(s.notify_trainings),
        "notify_subscription":     bool(s.notify_subscription),
        # НОВОЕ
        "notify_meals":            bool(s.notify_meals),
        "meal_timezone": s.meal_timezone or "Asia/Almaty",
    })


@app.route('/api/me/telegram/settings', methods=['POST','PATCH'])
@login_required
def patch_tg_settings():
    u = get_current_user()
    s = get_effective_user_settings(u)

    data = request.get_json(silent=True) or request.form.to_dict(flat=True) or {}

    def to_bool(v):
        if isinstance(v, bool): return v
        if isinstance(v, (int, float)): return v != 0
        if v is None: return False
        return str(v).strip().lower() in ("1","true","yes","on","y")

    alias_map = {
        "telegram_notify_enabled":         "telegram_notify_enabled",
        "telegram_notifications_enabled":  "telegram_notify_enabled",  # алиас
        "notify_trainings":                "notify_trainings",
        "notify_subscription":             "notify_subscription",
        "notify_promos":                   "notify_subscription",       # алиас
        "notify_meals":                    "notify_meals",
    }

    touched = {}
    for incoming_key, model_attr in alias_map.items():
        if incoming_key in data:
            val = to_bool(data[incoming_key])
            setattr(s, model_attr, val)   # источник истины
            setattr(u, model_attr, val)   # для обратной совместимости
            touched[model_attr] = val

    if "meal_timezone" in data:
        tz = (data.get("meal_timezone") or "").strip()
        try:
            ZoneInfo(tz)  # валидация
        except Exception:
            return jsonify({"ok": False, "error": "invalid_timezone"}), 400
        s.meal_timezone = tz
        touched["meal_timezone"] = tz

    db.session.add_all([s, u])
    db.session.commit()

    resp = jsonify({
        "ok": True,
        "saved": touched,
        "telegram_notify_enabled": bool(s.telegram_notify_enabled),
        "notify_trainings":        bool(s.notify_trainings),
        "notify_subscription":     bool(s.notify_subscription),
        "notify_meals":            bool(s.notify_meals),
        "notify_promos":           bool(s.notify_subscription),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ===== ADMIN: AI Очередь (модерация MealLog) =====

@app.route("/admin/ai")
@admin_required
def admin_ai_queue():
    q = MealLog.query.order_by(MealLog.created_at.desc()).limit(200).all()
    return render_template("admin_ai_queue.html", logs=q)

@app.route("/admin/ai/<int:meal_id>/flag", methods=["POST"])
@admin_required
def admin_ai_flag(meal_id):
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)
    old = {"is_flagged": m.is_flagged}
    m.is_flagged = True
    db.session.commit()
    log_audit("ai_flag", "MealLog", meal_id, old=old, new={"is_flagged": True})
    flash("Помечено как требующее внимания", "success")
    return redirect(url_for("admin_ai_queue"))

@app.route("/admin/ai/<int:meal_id>/unflag", methods=["POST"])
@admin_required
def admin_ai_unflag(meal_id):
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)
    old = {"is_flagged": m.is_flagged}
    m.is_flagged = False
    db.session.commit()
    log_audit("ai_unflag", "MealLog", meal_id, old=old, new={"is_flagged": False})
    flash("Снята пометка", "success")
    return redirect(url_for("admin_ai_queue"))

@app.route("/admin/ai/<int:meal_id>/edit", methods=["POST"])
@admin_required
def admin_ai_edit(meal_id):
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)
    old = {"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
           "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs}
    m.name = request.form.get("name", m.name)
    m.verdict = request.form.get("verdict", m.verdict)
    m.analysis = request.form.get("analysis", m.analysis)
    m.calories = int(request.form.get("calories", m.calories) or m.calories)
    m.protein = float(request.form.get("protein", m.protein) or m.protein)
    m.fat = float(request.form.get("fat", m.fat) or m.fat)
    m.carbs = float(request.form.get("carbs", m.carbs) or m.carbs)
    db.session.commit()
    log_audit("ai_edit", "MealLog", meal_id, old=old,
              new={"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
                   "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs})
    flash("Сохранено", "success")
    return redirect(url_for("admin_ai_queue"))

@app.route("/admin/ai/<int:meal_id>/reanalyse", methods=["POST"])
@admin_required
def admin_ai_reanalyse(meal_id):
    """Перегенерировать анализ по загруженной здесь фотке (админом)."""
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)

    file = request.files.get('file')
    if not file:
        flash("Загрузите изображение для перегенерации", "error")
        return redirect(url_for("admin_ai_queue"))

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with open(filepath, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')

        tmpl = PromptTemplate.query.filter_by(name='meal_photo', is_active=True) \
            .order_by(PromptTemplate.version.desc()).first()
        system_prompt = (tmpl.body if tmpl else
            "Ты — профессиональный диетолог. Проанализируй фото еды. Определи: ...")

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Проанализируй блюдо на фото."}
                ]}
            ],
            max_tokens=500,
        )

        # парсинг ответа (как в твоём коде)
        content = response.choices[0].message.content
        data = json.loads(content)
        old = {"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
               "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs}

        m.name = data.get("name") or m.name
        m.verdict = data.get("verdict") or m.verdict
        m.analysis = data.get("analysis") or m.analysis
        m.calories = int(float(data.get("calories", m.calories)))
        m.protein = float(data.get("protein", m.protein))
        m.fat = float(data.get("fat", m.fat))
        m.carbs = float(data.get("carbs", m.carbs))
        m.image_path = filepath
        db.session.commit()

        log_audit("ai_reanalyse", "MealLog", meal_id, old=old,
                  new={"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
                       "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs,
                       "image_path": filepath})
        flash("Перегенерировано", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка перегенерации: {e}", "error")

    return redirect(url_for("admin_ai_queue"))


# ===== ADMIN: Планировщик (APScheduler) =====

@app.route("/admin/jobs")
@admin_required
def admin_jobs():
    sched = get_scheduler()
    jobs = []
    if sched:
        for j in sched.get_jobs():
            jobs.append({
                "id": j.id,
                "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
                "paused": getattr(j, "paused", False)
            })
    return render_template("admin_jobs.html", jobs=jobs)

@app.route("/admin/jobs/<job_id>/pause", methods=["POST"])
@admin_required
def admin_jobs_pause(job_id):
    pause_job(job_id)
    log_audit("job_pause", "Job", job_id)
    flash("Задача приостановлена", "success")
    return redirect(url_for("admin_jobs"))

@app.route("/admin/jobs/<job_id>/resume", methods=["POST"])
@admin_required
def admin_jobs_resume(job_id):
    resume_job(job_id)
    log_audit("job_resume", "Job", job_id)
    flash("Задача возобновлена", "success")
    return redirect(url_for("admin_jobs"))

@app.route("/admin/jobs/run_tick_now", methods=["POST"])
@admin_required
def admin_jobs_run_tick_now():
    run_tick_now(app)
    log_audit("job_run", "MealReminders", "tick_now")
    flash("Тик запущен", "success")
    return redirect(url_for("admin_jobs"))


# ===== ADMIN: Промпты =====

@app.route("/admin/prompts", methods=["GET", "POST"])
@admin_required
def admin_prompts():
    if request.method == "POST":
        name = request.form["name"].strip()
        version = int(request.form["version"])
        body = request.form["body"]
        p = PromptTemplate(name=name, version=version, body=body, is_active=False)
        db.session.add(p)
        db.session.commit()
        log_audit("prompt_create", "PromptTemplate", p.id, new={"name": name, "version": version})
        flash("Шаблон сохранён", "success")
        return redirect(url_for("admin_prompts"))

    prompts = PromptTemplate.query.order_by(PromptTemplate.name, PromptTemplate.version.desc()).all()
    return render_template("admin_prompts.html", prompts=prompts)

@app.route("/admin/prompts/<int:pid>/activate", methods=["POST"])
@admin_required
def admin_prompts_activate(pid):
    p = db.session.get(PromptTemplate, pid)
    if not p: abort(404)
    # деактивируем остальные с тем же name
    db.session.query(PromptTemplate).filter(
        PromptTemplate.name == p.name,
        PromptTemplate.id != p.id
    ).update({"is_active": False})
    p.is_active = True
    db.session.commit()
    log_audit("prompt_activate", "PromptTemplate", pid, new={"name": p.name, "version": p.version})
    flash("Активирован", "success")
    return redirect(url_for("admin_prompts"))


# ===== ADMIN: Рассылки в Telegram =====

@app.route("/admin/broadcast", methods=["GET", "POST"])
@admin_required
def admin_broadcast():
    if request.method == "POST":
        text = request.form["text"].strip()
        only_active = bool(request.form.get("only_active"))
        q = db.session.query(User.telegram_chat_id, User.id).filter(User.telegram_chat_id.isnot(None))
        if only_active:
            q = q.join(Subscription, Subscription.user_id == User.id).filter(Subscription.status == 'active')
        rows = q.all()
        sent = 0
        for chat_id, uid in rows:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=10
                )
                sent += 1
            except Exception:
                pass
        log_audit("broadcast_send", "Telegram", "bulk", new={"text": text, "only_active": only_active, "sent": sent})
        flash(f"Отправлено: {sent}", "success")
        return redirect(url_for("admin_broadcast"))
    return render_template("admin_broadcast.html")



@app.get("/admin/impersonate/<int:user_id>")
@admin_required
def admin_impersonate_user(user_id):
    target = db.session.get(User, user_id) or abort(404)
    admin_id = session.get("user_id")
    # сохраняем, чтобы можно было вернуться
    session["impersonator_id"] = admin_id
    session["user_id"] = target.id
    flash(f"Вы вошли как {target.name} (ID {target.id}).", "success")
    try:
        log_audit("impersonate_start", "User", target.id, old={"admin": admin_id})
    except Exception:
        pass
    return redirect(url_for("profile"))


@app.get("/admin/impersonate/stop")
def admin_stop_impersonation():
    impersonator = session.pop("impersonator_id", None)
    if impersonator:
        session["user_id"] = impersonator
        flash("Возвращён доступ администратора.", "success")
        try:
            log_audit("impersonate_stop", "User", impersonator)
        except Exception:
            pass
    else:
        flash("Режим имперсонации не активен.", "error")
    # возвращаемся в админку, если есть user_id, иначе на дашборд
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/users/<int:user_id>/telegram/test")
@admin_required
def admin_user_send_test_tg(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if not user.telegram_chat_id:
        flash("Telegram не привязан у пользователя.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    text = (request.form.get("text") or f"Привет, {user.name}! Это тестовое сообщение 💬").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        flash("TELEGRAM_BOT_TOKEN не задан в окружении.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": user.telegram_chat_id, "text": text},
            timeout=10
        )
        r.raise_for_status()
        flash("Тестовое сообщение отправлено.", "success")
        try:
            log_audit("telegram_test_sent", "User", user.id, new={"text_len": len(text)})
        except Exception:
            pass
    except Exception as e:
        flash(f"Ошибка отправки: {e}", "error")

    return redirect(url_for("admin_user_detail", user_id=user.id))
@app.post("/api/me/telegram/unlink")
@login_required
def user_unlink_telegram():
    """Снимает привязку Telegram и выключает уведомления (совместимо со старым UI)."""
    u = get_current_user()
    if not u:
        abort(401)

    already = not bool(getattr(u, "telegram_chat_id", None))
    # Снимаем chat_id
    u.telegram_chat_id = None

    # Выключаем уведомления и синхронизируем с UserSettings
    try:
        u.telegram_notify_enabled = False
    except Exception:
        pass

    try:
        if getattr(u, "settings", None):
            u.settings.telegram_notify_enabled = False
    except Exception:
        pass

    db.session.commit()
    return jsonify({"ok": True, "already": already})

# Запасной маршрут для старого фронта/кнопок (вы вызывали /unlink_telegram)
@app.post("/unlink_telegram")
@login_required
def unlink_telegram_alias():
    return user_unlink_telegram()

@app.post("/admin/users/<int:user_id>/telegram/unlink")
@admin_required
def admin_user_unlink_telegram(user_id):
    user = db.session.get(User, user_id) or abort(404)
    old = {"telegram_chat_id": user.telegram_chat_id}
    user.telegram_chat_id = None
    db.session.commit()
    flash("Telegram отвязан.", "success")
    try:
        log_audit("telegram_unlink", "User", user.id, old=old, new={"telegram_chat_id": None})
    except Exception:
        pass
    return redirect(url_for("admin_user_detail", user_id=user.id))



@app.route("/admin/user/<int:user_id>/reset_telegram", methods=["GET","POST"], endpoint="admin_reset_telegram")
@admin_required
def admin_reset_telegram(user_id):
    user = db.session.get(User, user_id) or abort(404)
    old = {
        "telegram_chat_id": getattr(user, "telegram_chat_id", None),
        "telegram_code": getattr(user, "telegram_code", None),
    }
    user.telegram_chat_id = None
    user.telegram_code = None
    if hasattr(user, "renewal_telegram_sent"):
        user.renewal_telegram_sent = False
    db.session.commit()
    try:
        log_audit("reset_telegram", "User", user.id, old=old, new={"telegram_chat_id": None, "telegram_code": None})
    except Exception:
        pass
    flash("Связка с Telegram сброшена.", "success")
    return redirect(url_for("admin_user_detail", user_id=user.id))


# генерация/отправка магической ссылки из админки
@app.route("/admin/user/<int:user_id>/send_magic_link", methods=["GET","POST"], endpoint="admin_send_magic_link")
@admin_required
def admin_send_magic_link(user_id):
    user = db.session.get(User, user_id) or abort(404)
    s = _magic_serializer()
    token = s.dumps(str(user.id))

    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    magic_url = (
        f"{base}{url_for('magic_login', token=token)}"
        if base else url_for("magic_login", token=token, _external=True)
    )

    sent = False
    if getattr(user, "telegram_chat_id", None):
        sent = _send_telegram(user.telegram_chat_id, f"🔑 Вход без пароля: {magic_url}")

    try:
        log_audit("magic_link", "User", user.id, new={"sent_to_telegram": bool(sent)})
    except Exception:
        pass

    msg = "Ссылка сгенерирована. " + ("Отправлена в Telegram. " if sent else "")
    flash(f"{msg}Скопируйте при необходимости: {magic_url}", "success")
    return redirect(url_for("admin_user_detail", user_id=user.id))

@app.route("/admin/users/<int:user_id>/export", methods=["GET","POST"], endpoint="admin_user_export")
@admin_required
def admin_user_export(user_id):
    user = db.session.get(User, user_id) or abort(404)
    fmt = (request.form.get("format") or request.args.get("format") or "json").lower()

    meals = MealLog.query.filter_by(user_id=user.id).order_by(MealLog.date.desc()).all()
    acts  = Activity.query.filter_by(user_id=user.id).order_by(Activity.date.desc()).all()
    diets = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).all()
    bodies = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).all()

    if fmt == "csv":
        import io, csv
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["date","meal_type","name","calories","protein","fat","carbs","verdict","analysis"])
        for m in meals:
            w.writerow([
                m.date.isoformat() if getattr(m, "date", None) else "",
                m.meal_type, m.name or "",
                m.calories, m.protein, m.fat, m.carbs,
                m.verdict or "", (m.analysis or "").replace("\n", " ")
            ])
        resp = make_response(sio.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="user_{user.id}_meals.csv"'
        try: log_audit("export_csv", "User", user.id, new={"rows": len(meals)})
        except Exception: pass
        return resp

    import json as _json
    data = {
        "user": {
            "id": user.id, "name": user.name, "email": user.email,
            "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
            "telegram_chat_id": getattr(user, "telegram_chat_id", None)
        },
        "meals": [{
            "id": m.id, "date": m.date.isoformat() if m.date else None,
            "meal_type": m.meal_type, "name": m.name,
            "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs,
            "verdict": m.verdict, "analysis": m.analysis,
            "image_path": getattr(m, "image_path", None)
        } for m in meals],
        "activities": [{
            "id": a.id, "date": a.date.isoformat() if a.date else None,
            "steps": a.steps, "active_kcal": a.active_kcal,
            "resting_kcal": getattr(a, "resting_kcal", None),
            "distance_km": getattr(a, "distance_km", None)
        } for a in acts],
        "diets": [{
            "id": d.id, "date": d.date.isoformat() if d.date else None,
            "total_kcal": d.total_kcal, "protein": d.protein, "fat": d.fat, "carbs": d.carbs,
            "breakfast": json.loads(d.breakfast or "[]"),
            "lunch": json.loads(d.lunch or "[]"),
            "dinner": json.loads(d.dinner or "[]"),
            "snack": json.loads(d.snack or "[]"),
        } for d in diets],
        "body_analyses": [{
            "id": b.id,
            "timestamp": b.timestamp.isoformat() if b.timestamp else None,
            "height": getattr(b, "height", None),
            "weight": getattr(b, "weight", None),
            "muscle_mass": getattr(b, "muscle_mass", None),
            "fat_mass": getattr(b, "fat_mass", None),
            "bmi": getattr(b, "bmi", None),
            "metabolism": getattr(b, "metabolism", None)
        } for b in bodies]
    }
    resp = make_response(_json.dumps(data, ensure_ascii=False, default=str))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="user_{user.id}.json"'
    try:
        log_audit("export_json", "User", user.id,
                  new={"meals": len(meals), "activities": len(acts), "diets": len(diets), "body_analyses": len(bodies)})
    except Exception:
        pass
    return resp
# --- ВИЗУАЛИЗАЦИЯ ТЕЛА -------------------------------------------------------

def _latest_analysis_for(user_id: int):
    return (BodyAnalysis.query
            .filter(BodyAnalysis.user_id == user_id)
            .order_by(BodyAnalysis.timestamp.desc())
            .first())

@app.get("/visualize", endpoint="visualize")
@login_required
def visualize_page():
    u = get_current_user()
    latest_analysis = _latest_analysis_for(u.id)

    fat_loss_progress = None
    # --- НАЧАЛО ИЗМЕНЕНИЙ: Новая логика расчета прогресса ---
    initial_analysis = db.session.get(BodyAnalysis, u.initial_body_analysis_id) if u.initial_body_analysis_id else None

    if initial_analysis and latest_analysis and latest_analysis.fat_mass and u.fat_mass_goal and initial_analysis.fat_mass > u.fat_mass_goal:
        initial_fat_mass = initial_analysis.fat_mass
        current_fat_mass = latest_analysis.fat_mass
        goal_fat_mass = u.fat_mass_goal

        total_fat_to_lose_kg = initial_fat_mass - goal_fat_mass
        fat_lost_so_far_kg = initial_fat_mass - current_fat_mass

        percentage = 0
        if total_fat_to_lose_kg > 0:
            percentage = (fat_lost_so_far_kg / total_fat_to_lose_kg) * 100
        percentage = min(100, max(0, percentage))
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        # --- НАЧАЛО ИЗМЕНЕНИЙ: Выбор мотивационного сообщения ---
        motivation_text = ""
        if percentage == 0:
            motivation_text = "Путь в тысячу ли начинается с первого шага. Начнем?"
        elif 0 < percentage < 10:
            motivation_text = "Отличное начало! Первые результаты уже есть."
        elif 10 <= percentage < 40:
            motivation_text = "Вы на верном пути! Продолжайте в том же духе."
        elif 40 <= percentage < 70:
            motivation_text = "Больше половины позади! Выглядит впечатляюще."
        elif 70 <= percentage < 100:
            motivation_text = "Финишная прямая! Цель совсем близко."
        elif percentage >= 100:
            motivation_text = "Поздравляю! Цель достигнута. Вы великолепны!"
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        fat_loss_progress = {
            'percentage': percentage,
            'burned_kg': fat_lost_so_far_kg,
            'total_to_lose_kg': total_fat_to_lose_kg,
            'initial_kg': initial_fat_mass,
            'goal_kg': goal_fat_mass,
            'current_kg': current_fat_mass,
            'motivation_text': motivation_text  # Добавляем сообщение в словарь
        }

    latest_visualization = BodyVisualization.query.filter_by(user_id=u.id).order_by(BodyVisualization.id.desc()).first()

    return render_template(
        'visualize.html',
        latest_analysis=latest_analysis,
        latest_visualization=latest_visualization,
        fat_loss_progress=fat_loss_progress
    )


@app.route('/visualize/run', methods=['POST'])
@login_required
def visualize_run():
    u = get_current_user()
    if not u:
        abort(401)

    if not getattr(u, 'face_consent', False):
        return jsonify({"success": False,
                        "error": "Чтобы сгенерировать визуализацию, нужно разрешить использование аватара (галочка в профиле)."}), 400

    latest = BodyAnalysis.query.filter_by(user_id=u.id).order_by(BodyAnalysis.timestamp.desc()).first()
    if not latest:
        return jsonify(
            {"success": False, "error": "Загрузите актуальный анализ тела — без него визуализация не строится."}), 400

    # --- ИЗМЕНЕНИЕ: Получаем байты фото (Приоритет: Полный рост -> Аватар -> Дефолт) ---
    avatar_bytes = None

    # 1. Проверяем наличие фото в полный рост
    if getattr(u, 'full_body_photo', None):
        avatar_bytes = u.full_body_photo.data

    # 2. Если нет, берем аватар (как запасной вариант)
    elif u.avatar:
        avatar_bytes = u.avatar.data

    if not avatar_bytes:
        # Если у пользователя нет ни фото тела, ни аватара, загружаем дефолтный из static
        try:
            with open(os.path.join(app.static_folder, 'i.webp'), 'rb') as f:
                avatar_bytes = f.read()
        except FileNotFoundError:
            app.logger.error("[visualize] Default avatar i.webp not found in static folder.")
            return jsonify({"success": False, "error": "Файл аватара по умолчанию не найден."}), 500

    # --- metrics_current ---
    current_weight = latest.weight or 0
    metrics_current = {
        "height_cm": latest.height,
        "weight_kg": current_weight,
        "fat_mass": latest.fat_mass,
        "muscle_mass": latest.muscle_mass,
        "metabolism": latest.metabolism,
        "fat_pct": _compute_pct(latest.fat_mass, current_weight),
        "muscle_pct": _compute_pct(latest.muscle_mass, current_weight),
        "sex": getattr(u, "sex", None),
    }

    # --- metrics_target (Полный расчет) ---
    metrics_target = metrics_current.copy()
    fat_mass_goal = getattr(u, "fat_mass_goal", None)
    muscle_mass_goal = getattr(u, "muscle_mass_goal", None)

    if fat_mass_goal is not None and muscle_mass_goal is not None:
        metrics_target["fat_mass"] = fat_mass_goal
        metrics_target["muscle_mass"] = muscle_mass_goal

        delta_fat = (metrics_current.get("fat_mass") or 0) - fat_mass_goal
        delta_muscle = muscle_mass_goal - (metrics_current.get("muscle_mass") or 0)
        target_weight = current_weight - delta_fat + delta_muscle
        metrics_target["weight_kg"] = target_weight

        metrics_target["fat_pct"] = _compute_pct(fat_mass_goal, target_weight)
        metrics_target["muscle_pct"] = _compute_pct(muscle_mass_goal, target_weight)

    try:
        # Вызываем обновленную функцию, передавая байты аватара
        current_image_filename, target_image_filename = generate_for_user(
            user=u,
            avatar_bytes=avatar_bytes,
            metrics_current=metrics_current,
            metrics_target=metrics_target
        )

        # Функция create_record теперь принимает имена файлов
        new_viz_record = create_record(
            user=u,
            curr_filename=current_image_filename,
            tgt_filename=target_image_filename,
            metrics_current=metrics_current,
            metrics_target=metrics_target
        )

        # Используем новый маршрут 'serve_file'

        # ANALYTICS: Body Visualization Generated
        try:
            amplitude.track(BaseEvent(
                event_type="Body Visualization Generated",
                user_id=str(u.id),  # <--- ИСПРАВЛЕНО: user -> u
                event_properties={
                    "current_weight": metrics_current.get("weight_kg"),
                    "target_weight": metrics_target.get("weight_kg"),
                    "sex": metrics_current.get("sex")
                }
            ))
        except Exception as e:
            print(f"Amplitude error: {e}")

        return jsonify({
            "success": True,
            "visualization": {
                "image_current_path": url_for('serve_file', filename=new_viz_record.image_current_path),
                "image_target_path": url_for('serve_file', filename=new_viz_record.image_target_path),
                "created_at": new_viz_record.created_at.strftime('%d.%m.%Y %H:%M')
            }
        })

    except Exception as e:
        app.logger.error("[visualize] generation failed: %s", e, exc_info=True)
        db.session.rollback()  # Откатываем транзакцию в случае ошибки
        return jsonify({"success": False, "error": f"Не удалось сгенерировать визуализацию: {e}"}), 500

# ===== ADMIN: Аудит =====

@app.route("/admin/audit")
@admin_required
def admin_audit():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template("admin_audit.html", logs=logs)


# ===== ADMIN: Жалобы (Reports) =====

@app.route("/admin/reports")
@admin_required
def admin_reports():
    # Загружаем жалобы с подгрузкой связанных данных
    reports = MessageReport.query.options(
        subqueryload(MessageReport.message).subqueryload(GroupMessage.user),
        subqueryload(MessageReport.reporter)
    ).order_by(MessageReport.created_at.desc()).all()

    data = []
    for r in reports:
        # Пропускаем, если сообщение уже удалено
        if not r.message:
            # Можно удалять "сироту" из базы
            db.session.delete(r)
            continue

        data.append({
            "id": r.id,
            "reason": r.reason,
            "created_at": r.created_at.strftime('%Y-%m-%d %H:%M'),
            "reporter": r.reporter.name if r.reporter else "Unknown",
            "sender": r.message.user.name if r.message.user else "Unknown",
            "sender_id": r.message.user_id if r.message.user else None,
            "text": r.message.text,
            "image": url_for('serve_file', filename=r.message.image_file) if r.message.image_file else None
        })

    # Коммитим удаление сирот, если были
    db.session.commit()

    return render_template("admin_reports.html", reports=data)


@app.route("/admin/reports/<int:rid>/resolve", methods=["POST"])
@admin_required
def admin_report_resolve(rid):
    r = db.session.get(MessageReport, rid)
    if not r: abort(404)

    action = request.form.get("action")  # 'delete_msg' | 'dismiss'

    if action == 'delete_msg':
        msg = r.message
        if msg:
            # Удаляем сообщение и саму жалобу
            db.session.delete(msg)
            db.session.delete(r)
            flash("Сообщение удалено, жалоба закрыта.", "success")
            log_audit("mod_delete_msg", "GroupMessage", msg.id, new={"reason": r.reason})
        else:
            db.session.delete(r)
            flash("Сообщение уже было удалено.", "warning")

    elif action == 'dismiss':
        # Удаляем только жалобу
        db.session.delete(r)
        flash("Жалоба отклонена.", "info")
        log_audit("mod_dismiss_report", "MessageReport", rid)

    db.session.commit()
    return redirect(url_for("admin_reports"))


# --- ANALYTICS DASHBOARD ---

@app.route("/admin/analytics")
@admin_required
def admin_analytics_page():
    # 1. Воронка Онбординга (Конверсия в уникальных пользователях)
    # Этапы: Регистрация -> Анализ весов -> Подтверждение анализа -> Визуализация -> Финиш
    funnel_steps_keys = [
        'signup_completed',
        'scales_analyzed',
        'analysis_confirmed',
        'visualization_generated',
        'onboarding_finished'
    ]
    funnel_labels = [
        'Регистрация',
        'Анализ весов',
        'Подтверждение данных',
        'Визуализация (AI)',
        'Завершение тура'
    ]

    funnel_counts = []
    for step in funnel_steps_keys:
        # Считаем уникальных юзеров, совершивших это действие
        count = db.session.query(func.count(func.distinct(AnalyticsEvent.user_id))) \
            .filter(AnalyticsEvent.event_type == step).scalar()
        funnel_counts.append(count or 0)

    # 2. Динамика регистраций (за последние 14 дней)
    today = date.today()
    dates_labels = []
    reg_values = []

    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        d_next = d + timedelta(days=1)

        # Считаем события 'signup_completed' за этот день
        cnt = db.session.query(func.count(AnalyticsEvent.id)).filter(
            AnalyticsEvent.event_type == 'signup_completed',
            AnalyticsEvent.created_at >= d,
            AnalyticsEvent.created_at < d_next
        ).scalar()

        dates_labels.append(d.strftime("%d.%m"))
        reg_values.append(cnt or 0)

    # 3. Общая статистика (KPI)
    # Просмотры пейволла
    paywall_hits = db.session.query(func.count(AnalyticsEvent.id)) \
                       .filter(AnalyticsEvent.event_type == 'paywall_viewed').scalar() or 0

    # Созданные заявки
    apps_created = db.session.query(func.count(AnalyticsEvent.id)) \
                       .filter(AnalyticsEvent.event_type == 'application_created').scalar() or 0

    return render_template(
        "admin_analytics.html",
        # Передаем данные как JSON строки для JS
        funnel_labels=json.dumps(funnel_labels),
        funnel_data=json.dumps(funnel_counts),
        dates_labels=json.dumps(dates_labels),
        reg_data=json.dumps(reg_values),
        paywall_hits=paywall_hits,
        apps_created=apps_created
    )


@app.route("/admin/analytics/events")
@admin_required
def admin_analytics_events_list():
    page = request.args.get('page', 1, type=int)
    user_id = request.args.get('user_id', type=str)
    event_type = request.args.get('event_type')

    query = AnalyticsEvent.query.options(subqueryload(AnalyticsEvent.user))

    # Фильтры
    if user_id and user_id.isdigit():
        query = query.filter(AnalyticsEvent.user_id == int(user_id))
    if event_type:
        query = query.filter(AnalyticsEvent.event_type.ilike(f"%{event_type}%"))

    # Сортировка: новые сверху + Пагинация (50 штук на страницу)
    pagination = query.order_by(AnalyticsEvent.created_at.desc()).paginate(page=page, per_page=50)

    return render_template(
        "admin_analytics_events.html",
        events=pagination.items,
        pagination=pagination,
        filter_user_id=user_id,
        filter_event_type=event_type
    )

# регистрация блюпринта (добавь после определения маршрутов)
app.register_blueprint(bp)
app.register_blueprint(shopping_bp, url_prefix="/shopping")
app.register_blueprint(assistant_bp) # <--- И ЭТУ СТРОКУ
app.register_blueprint(streak_bp)    # <--- Добавлено

from user_bp import user_bp # <--- ИМПОРТ НОВОГО BP
app.register_blueprint(user_bp) # <--- РЕГИСТРАЦИЯ

from support_bp import support_bp
app.register_blueprint(support_bp, url_prefix='/api/support')

@app.route('/files/<path:filename>')
def serve_file(filename):
    """Отдаёт загруженный файл из БД."""
    f = UploadedFile.query.filter_by(filename=filename).first_or_404()
    return send_file(BytesIO(f.data), mimetype=f.content_type)

@app.route('/ai-instructions')
@login_required
def ai_instructions_page():
    """Отображает страницу с инструкциями по работе с ИИ-ассистентом."""
    return render_template('ai_instructions.html')


@app.route('/profile/reset_goals', methods=['POST'])
@login_required
def reset_goals():
    """Сбрасывает цели пользователя и стартовую точку для нового отсчета."""
    user = get_current_user()
    if not user:
        # Это API-эндпоинт, возвращаем JSON-ошибку
        return jsonify({"success": False, "error": "User not found"}), 401

    user.fat_mass_goal = None
    user.muscle_mass_goal = None
    user.initial_body_analysis_id = None

    db.session.commit()

    # flash(...) # flash() бесполезен для API
    # return redirect(url_for('profile')) # <-- НЕПРАВИЛЬНО для API

    # ПРАВИЛЬНО: Возвращаем JSON
    return jsonify({"success": True, "message": "Progress reset successfully"})


@app.route('/api/app/calendar_data', methods=['GET'])
@login_required
def app_calendar_data():
    """
    Возвращает детальную статистику по дням для календаря:
    1. Процент заполнения кольца питания (0.25, 0.5, 0.75, 1.0).
    2. Процент заполнения кольца активности (steps / goal).
    """
    user = get_current_user()
    month_str = request.args.get('month')

    if not month_str:
        today = date.today()
        month_str = f"{today.year:04d}-{today.month:02d}"

    try:
        y, m = map(int, month_str.split('-'))
        start_date = date(y, m, 1)
        if m == 12:
            end_date = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(y, m + 1, 1) - timedelta(days=1)
    except:
        return jsonify({"ok": False, "error": "Invalid month format"}), 400

    # 1. Данные по еде: считаем количество уникальных meal_type за каждый день
    # Если 1 прием = 25%, 4 приема = 100%
    meals_query = db.session.query(
        MealLog.date,
        func.count(func.distinct(MealLog.meal_type))
    ).filter(
        MealLog.user_id == user.id,
        MealLog.date >= start_date,
        MealLog.date <= end_date
    ).group_by(MealLog.date).all()

    # 2. Данные по активности: шаги за каждый день
    activity_query = db.session.query(Activity).filter(
        Activity.user_id == user.id,
        Activity.date >= start_date,
        Activity.date <= end_date
    ).all()

    # Сборка данных
    daily_stats = {}
    step_goal = getattr(user, 'step_goal', 10000) or 10000

    # Заполняем еду
    for date_obj, count in meals_query:
        date_str = date_obj.strftime("%Y-%m-%d")
        if date_str not in daily_stats:
            daily_stats[date_str] = {"meal_percent": 0.0, "step_percent": 0.0}

        # Логика: 1 прием = 0.25, 4 приема = 1.0
        daily_stats[date_str]["meal_percent"] = min(1.0, count * 0.25)

    # Заполняем активность
    for act in activity_query:
        date_str = act.date.strftime("%Y-%m-%d")
        if date_str not in daily_stats:
            daily_stats[date_str] = {"meal_percent": 0.0, "step_percent": 0.0}

        steps = act.steps or 0
        if step_goal > 0:
            daily_stats[date_str]["step_percent"] = min(1.0, steps / step_goal)

    # Даты тренировок (оставляем список для точек)
    training_dates = db.session.query(Training.date).join(TrainingSignup).filter(
        TrainingSignup.user_id == user.id,
        Training.date >= start_date,
        Training.date <= end_date
    ).distinct().all()
    training_dates_list = [d[0].strftime("%Y-%m-%d") for d in training_dates]

    return jsonify({
        "ok": True,
        "current_streak": getattr(user, "current_streak", 0),
        "daily_stats": daily_stats,  # <-- Новый формат данных
        "training_dates": training_dates_list
    })

@app.route('/api/achievements', methods=['GET'])
@login_required
def get_achievements():
    user = get_current_user()
    unlocked = UserAchievement.query.filter_by(user_id=user.id).all()
    unlocked_slugs = {u.slug for u in unlocked}

    result = []
    for slug, meta in ACHIEVEMENTS_METADATA.items():
        result.append({
            "slug": slug,
            "title": meta["title"],
            "description": meta["description"],
            "icon": meta["icon"],
            "color": meta["color"],
            "is_unlocked": slug in unlocked_slugs
        })
    return jsonify({"ok": True, "achievements": result})


@app.route('/api/achievements/unseen', methods=['POST'])
@login_required
def get_unseen_achievements():
    user = get_current_user()
    unseen = UserAchievement.query.filter_by(user_id=user.id, seen=False).all()
    data = []
    for ua in unseen:
        meta = ACHIEVEMENTS_METADATA.get(ua.slug)
        if meta: data.append(meta)
        ua.seen = True
    db.session.commit()
    return jsonify({"ok": True, "new_achievements": data})

@app.route('/api/app/register_device', methods=['POST'])
@login_required
def register_device_token():
    user = get_current_user()
    data = request.get_json()
    token = data.get('fcm_token')

    if not token:
        return jsonify({"ok": False, "error": "TOKEN_REQUIRED"}), 400

    # Опционально: отвязываем этот токен от других юзеров, если он у них был
    User.query.filter(User.fcm_device_token == token, User.id != user.id).update({"fcm_device_token": None})

    user.fcm_device_token = token
    db.session.commit()
    return jsonify({"ok": True})


@app.route('/api/app/activity/log', methods=['POST'])
@login_required
def app_log_activity():
    """
    Сохраняет активность (шаги/калории) из мобильного приложения.
    Использует сессию (@login_required) и принимает дату.
    """
    user = get_current_user()
    data = request.get_json(force=True, silent=True) or {}

    # 1. Получаем данные
    steps = int(data.get('steps') or 0)
    active_kcal = int(data.get('active_kcal') or 0)
    source = data.get('source', 'app')
    date_str = data.get('date')  # 'YYYY-MM-DD'

    # 2. Определяем дату (важно для часовых поясов!)
    # По умолчанию берем текущую дату в Алматы, а не серверную UTC
    log_date = datetime.now(ZoneInfo("Asia/Almaty")).date()

    if date_str:
        try:
            log_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    # 3. Ищем запись за ЭТУ дату
    activity = Activity.query.filter_by(user_id=user.id, date=log_date).first()

    try:
        if activity:
            # Обновляем существующую (перезаписываем или суммируем - зависит от логики,
            # Health Connect обычно дает "итого за день", поэтому перезапись безопасна)
            activity.steps = steps
            activity.active_kcal = active_kcal
            activity.source = source
        else:
            # Создаем новую
            activity = Activity(
                user_id=user.id,
                date=log_date,
                steps=steps,
                active_kcal=active_kcal,
                source=source
            )
            db.session.add(activity)

        db.session.commit()

        return jsonify({'ok': True, 'message': 'Activity saved'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/auth/request_code', methods=['POST'])
def api_auth_request_code():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({"ok": False, "error": "EMAIL_REQUIRED"}), 400

    code = ''.join(random.choices(string.digits, k=6))
    expires = datetime.now() + timedelta(minutes=10)

    user = User.query.filter(func.lower(User.email) == email).first()
    if user:
        # Для существующего пользователя (сброс пароля)
        user.verification_code = code
        user.verification_code_expires_at = expires
    else:
        # Для нового пользователя (регистрация)
        ev = db.session.get(EmailVerification, email)
        if not ev:
            ev = EmailVerification(email=email)
            db.session.add(ev)
        ev.code = code
        ev.expires_at = expires

    db.session.commit()

    if send_email_code(email, code):
        return jsonify({"ok": True, "message": "Code sent"})
    else:
        return jsonify({"ok": False, "error": "SEND_EMAIL_FAILED"}), 500

@app.route('/api/auth/reset_password', methods=['POST'])
def api_auth_reset_password():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()
    new_password = (data.get('new_password') or '').strip()

    if not email or not code or not new_password:
        return jsonify({"ok": False, "error": "MISSING_DATA"}), 400

    user = User.query.filter(func.lower(User.email) == email).first()
    if not user:
        return jsonify({"ok": False, "error": "USER_NOT_FOUND"}), 404

    if not user.verification_code or user.verification_code != code:
        return jsonify({"ok": False, "error": "INVALID_CODE"}), 400

    if user.verification_code_expires_at < datetime.now():
        return jsonify({"ok": False, "error": "CODE_EXPIRED"}), 400

    # Меняем пароль
    user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')

    # Очищаем код и подтверждаем почту
    user.verification_code = None
    user.verification_code_expires_at = None
    user.is_verified = True

    db.session.commit()

    return jsonify({"ok": True, "message": "Password changed"})


@app.route('/api/auth/verify_email', methods=['POST'])
def api_auth_verify_email():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()

    if not email or not code:
        return jsonify({"ok": False, "error": "MISSING_DATA"}), 400

    user = User.query.filter(func.lower(User.email) == email).first()
    if user:
        # Проверка для существующего (редкий кейс для этого эндпоинта, но оставим)
        if user.verification_code == code and user.verification_code_expires_at > datetime.now():
            user.is_verified = True
            user.verification_code = None
            db.session.commit()
            return jsonify({"ok": True, "message": "Email verified"})
        return jsonify({"ok": False, "error": "INVALID_CODE"}), 400

    # Проверка для нового пользователя
    ev = db.session.get(EmailVerification, email)
    if not ev:
        return jsonify({"ok": False, "error": "CODE_NOT_REQUESTED"}), 404

    if ev.code == code and ev.expires_at > datetime.now():
        # Код верный. Удаляем запись, чтобы нельзя было использовать повторно,
        # или оставляем флаг. Для простоты - удаляем, клиент переходит к регистрации.
        db.session.delete(ev)
        db.session.commit()
        return jsonify({"ok": True, "message": "Email verified"})

    return jsonify({"ok": False, "error": "INVALID_CODE"}), 400


@app.route("/api/app/fcm_token", methods=["POST", "DELETE"])
@login_required
def api_app_fcm_token():
    """
    POST  -> сохраняет/обновляет FCM токен устройства для текущего пользователя
    DELETE -> удаляет токен (например, при logout на мобилке)
    """
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "UNAUTHORIZED"}), 401

    # Удаление токена
    if request.method == "DELETE":
        try:
            user.fcm_device_token = None
            user.updated_at = datetime.now(UTC)
            db.session.commit()
            return jsonify({"ok": True}), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({"ok": False, "error": f"SERVER_ERROR: {e}"}), 500

    # POST: сохранение токена
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or data.get("fcm_token") or "").strip()

    if not token:
        return jsonify({"ok": False, "error": "TOKEN_REQUIRED"}), 400

    # минимальная sanity-проверка (FCM токены обычно длинные)
    if len(token) < 20 or len(token) > 4096:
        return jsonify({"ok": False, "error": "TOKEN_INVALID"}), 400

    try:
        # Если токен уже висит на другом пользователе — отвязываем
        other = User.query.filter(
            User.fcm_device_token == token,
            User.id != user.id
        ).first()
        if other:
            other.fcm_device_token = None
            other.updated_at = datetime.now(UTC)

        # Сохраняем текущему
        user.fcm_device_token = token
        user.updated_at = datetime.now(UTC)

        db.session.commit()
        return jsonify({"ok": True}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"SERVER_ERROR: {e}"}), 500

@app.route('/api/auth/verify_reset_code', methods=['POST'])
def api_auth_verify_reset_code():
    """
    Проверяет валидность кода сброса пароля БЕЗ его использования (удаления).
    Нужен для перехода на экран ввода нового пароля.
    """
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()

    if not email or not code:
        return jsonify({"ok": False, "error": "MISSING_DATA"}), 400

    user = User.query.filter(func.lower(User.email) == email).first()
    if not user:
        return jsonify({"ok": False, "error": "USER_NOT_FOUND"}), 404

    # Проверяем совпадение кода
    if not user.verification_code or user.verification_code != code:
        return jsonify({"ok": False, "error": "INVALID_CODE"}), 400

    # Проверяем срок действия
    if user.verification_code_expires_at < datetime.now():
        return jsonify({"ok": False, "error": "CODE_EXPIRED"}), 400

    # ВАЖНО: Мы НЕ удаляем код здесь, так как он понадобится
    # для финального сброса пароля в /api/auth/reset_password
    return jsonify({"ok": True, "message": "Code is valid"})


@app.route('/api/squads/join', methods=['POST'])
@login_required
def join_squad_request():
    user = get_current_user()
    data = request.get_json(force=True, silent=True) or {}

    pref_time = data.get('preferred_time')
    fit_level = data.get('fitness_level')

    if not pref_time or not fit_level:
        return jsonify({"ok": False, "error": "Заполните все поля"}), 400

    try:
        user.squad_pref_time = pref_time
        user.squad_fitness_level = fit_level
        user.squad_status = 'pending'  # Статус "Ждет распределения"

        db.session.commit()

        # ANALYTICS: Squad Join Requested
        try:
            amplitude.track(BaseEvent(
                event_type="Squad Join Requested",
                user_id=str(user.id),
                event_properties={
                    "preferred_time": pref_time,
                    "fitness_level": fit_level
                }
            ))
        except Exception as e:
            print(f"Amplitude error: {e}")

        return jsonify({"ok": True, "message": "Заявка в Squad принята"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


# --- SQUAD FEED API ---

@app.route('/api/groups/<int:group_id>/feed')
@login_required
def get_squad_feed(group_id):
    u = get_current_user()
    # Проверка доступа (состоит ли в группе)
    if not u.is_trainer:
        if not GroupMember.query.filter_by(user_id=u.id, group_id=group_id).first():
            return jsonify({"ok": False, "error": "Access denied"}), 403

    # Получаем ТОЛЬКО родительские посты (где parent_id is NULL)
    # Сортируем: новые сверху
    posts = GroupMessage.query.filter_by(group_id=group_id, parent_id=None) \
        .order_by(GroupMessage.timestamp.desc()).limit(50).all()

    feed_data = []
    for p in posts:
        # Собираем комментарии к посту
        comments_data = []
        for c in p.replies:
            comments_data.append({
                "id": c.id,
                "user_id": c.user_id,
                "user_name": c.user.name,
                "avatar": c.user.avatar.filename if c.user.avatar else None,
                "text": c.text,
                "timestamp": c.timestamp.strftime('%d.%m %H:%M'),
                "is_me": (c.user_id == u.id)
            })

        # Сортируем комменты: старые сверху (хронология разговора)
        comments_data.sort(key=lambda x: x['timestamp'])  # Упрощенно, лучше по ID или real datetime

        feed_data.append({
            "id": p.id,
            "type": p.type,  # 'post' or 'system'
            "user_id": p.user_id,
            "user_name": p.user.name,
            "avatar": p.user.avatar.filename if p.user.avatar else None,
            "text": p.text,
            "image": p.image_file,
            "timestamp": p.timestamp.strftime('%d.%m %H:%M'),
            "comments": comments_data,
            "likes_count": len(p.reactions),
            "is_liked": any(r.user_id == u.id for r in p.reactions),
            "is_me": (p.user_id == u.id)
        })

    return jsonify({"ok": True, "feed": feed_data})


@app.route('/api/groups/<int:group_id>/post', methods=['POST'])
@login_required
def create_squad_post(group_id):
    """Создание поста (Только тренер или система)"""
    u = get_current_user()
    group = db.session.get(Group, group_id)

    # Проверка прав: постить может только тренер этой группы
    if group.trainer_id != u.id and not is_admin():
        return jsonify({"ok": False, "error": "Только тренер может писать посты"}), 403

    text = request.form.get('text', '').strip()
    msg_type = request.form.get('type', 'post')  # 'post'

    if not text:
        return jsonify({"ok": False, "error": "Текст не может быть пустым"}), 400

    # Обработка картинки (если есть)
    image_filename = None
    file = request.files.get('image')
    if file and file.filename:
        filename = secure_filename(file.filename)
        unique_filename = f"feed_{group_id}_{uuid.uuid4().hex}_{filename}"

        file_data = file.read()
        # Ресайз (опционально)
        output_buffer = BytesIO()
        try:
            with Image.open(BytesIO(file_data)) as img:
                img.thumbnail((800, 800))  # Для ленты можно побольше
                img.save(output_buffer, format=img.format or "JPEG")
            final_data = output_buffer.getvalue()
        except:
            final_data = file_data

        new_file = UploadedFile(
            filename=unique_filename,
            content_type=file.mimetype,
            data=final_data,
            size=len(final_data),
            user_id=u.id
        )
        db.session.add(new_file)
        db.session.flush()
        image_filename = unique_filename

    post = GroupMessage(
        group_id=group.id,
        user_id=u.id,
        text=text,
        type=msg_type,
        image_file=image_filename,
        parent_id=None
    )
    db.session.add(post)
    db.session.commit()

    # --- УВЕДОМЛЕНИЯ УЧАСТНИКАМ ---
    try:
        # 1. Формируем текст уведомления
        snippet = (text[:50] + '...') if len(text) > 50 else text
        if not snippet and image_filename:
            snippet = "Новое фото 📷"

        notif_title = f"Новое в {group.name} 📢"
        notif_body = f"{u.name}: {snippet}"

        # 2. Собираем ID получателей (все участники, кроме автора)
        # group.members - это список объектов GroupMember
        recipients_ids = [m.user_id for m in group.members if m.user_id != u.id]

        # Если автор не тренер (редкий кейс), то тренеру тоже отправляем
        if group.trainer_id != u.id and group.trainer_id not in recipients_ids:
            recipients_ids.append(group.trainer_id)

        # 3. Рассылаем
        for rid in recipients_ids:
            send_user_notification(
                user_id=rid,
                title=notif_title,
                body=notif_body,
                type="info",
                data={"route": "/squad"}  # При клике открываем вкладку Squads
            )


    except Exception as e:
        print(f"[PUSH ERROR] Failed to notify group: {e}")
        # ------------------------------

        # ANALYTICS: Squad Post Created
        try:
            amplitude.track(BaseEvent(
                event_type="Squad Post Created",
                user_id=str(u.id),
                event_properties={
                    "group_id": group.id,
                    "has_image": bool(image_filename),
                    "post_type": msg_type
                }
            ))
        except Exception as e:
            print(f"Amplitude error: {e}")

    return jsonify({"ok": True, "message": "Пост опубликован"})

@app.route('/api/groups/<int:group_id>/reply', methods=['POST'])
@login_required
def create_squad_comment(group_id):
    """Создание комментария (Любой участник)"""
    u = get_current_user()
    data = request.get_json(force=True, silent=True) or {}

    parent_id = data.get('parent_id')
    text = data.get('text', '').strip()

    if not parent_id or not text:
        return jsonify({"ok": False, "error": "Нет ID поста или текста"}), 400

    # Проверка членства
    if not GroupMember.query.filter_by(user_id=u.id, group_id=group_id).first() and not u.is_trainer:
        return jsonify({"ok": False, "error": "Вы не участник"}), 403

    comment = GroupMessage(
        group_id=group_id,
        user_id=u.id,
        text=text,
        type='comment',
        parent_id=parent_id
    )
    db.session.add(comment)
    db.session.commit()

    # --- УВЕДОМЛЕНИЕ АВТОРУ ПОСТА ---
    try:
        # Находим родительский пост
        parent_post = db.session.get(GroupMessage, parent_id)

        # Если родитель существует и его автор — не мы сами
        if parent_post and parent_post.user_id != u.id:
            snippet = (text[:40] + '...') if len(text) > 40 else text

            send_user_notification(
                user_id=parent_post.user_id,
                title="Новый комментарий 💬",
                body=f"{u.name} ответил: {snippet}",
                type="info",
                data={"route": "/squad"}
            )
    except Exception as e:
        print(f"[PUSH ERROR] Failed to notify comment author: {e}")
    # --------------------------------

    return jsonify({"ok": True, "comment": {
        "id": comment.id,
        "text": comment.text,
        "user_name": u.name,
        "avatar": u.avatar.filename if u.avatar else None,
        "is_me": True
    }})


@app.route('/groups/<int:group_id>/trainings/new', methods=['POST'])
@login_required
def create_group_training(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    # Только тренер группы может создавать тренировки
    if not (user.is_trainer and group.trainer_id == user.id):
        return jsonify({"ok": False, "error": "Только тренер может назначать тренировки"}), 403

    data = request.form
    try:
        dt = _parse_date_yyyy_mm_dd(data.get('date') or '')
        st = _parse_hh_mm(data.get('start_time') or '')
        et = _parse_hh_mm(data.get('end_time') or '')

        if et <= st:
            return jsonify({"ok": False, "error": "Конец раньше начала"}), 400

        t = Training(
            trainer_id=user.id,
            group_id=group.id,  # Привязываем к группе
            title=data.get('title') or "Групповая тренировка",
            description=data.get('description') or "",
            meeting_link=data.get('meeting_link') or "#",
            date=dt,
            start_time=st,
            end_time=et,
            capacity=100,  # Для своих безлимит или много
            is_public=False  # Приватная для группы
        )
        db.session.add(t)
        db.session.commit()

        # Можно отправить уведомление (код уведомления опущен для краткости)

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/groups/nudge/<int:user_id>', methods=['POST'])
@login_required
def nudge_member(user_id):
    """Отправляет напоминание пользователю от тренера."""
    target_user = db.session.get(User, user_id)
    if not target_user:
        return jsonify({"ok": False, "error": "User not found"}), 404

    currentUser = get_current_user()

    # Проверка прав (только тренер может пинать)
    # (Упрощенно: если у текущего юзера есть группа и этот юзер в ней состоит, или если админ)
    is_authorized = False
    if currentUser.is_trainer and currentUser.own_group:
        # Проверяем, состоит ли target_user в группе тренера
        member = GroupMember.query.filter_by(group_id=currentUser.own_group.id, user_id=user_id).first()
        if member:
            is_authorized = True

    if not is_authorized and not is_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    try:
        # Отправляем PUSH
        from notification_service import send_user_notification

        # Разные тексты в зависимости от времени отсутствия (можно усложнить)
        title = "Тренер ждет тебя! 👀"
        body = f"{currentUser.name}: Давно не видел твоих отчетов. Как дела? Возвращайся в строй!"

        send_user_notification(
            user_id=user_id,
            title=title,
            body=body,
            type='reminder',
            data={"route": "/squad"}
        )

        # Опционально: Можно записать это в лог или чат, что тренер напомнил
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/groups/messages/<int:message_id>/report', methods=['POST'])
@login_required
def report_message(message_id):
        """Пожаловаться на сообщение. Сохраняет в БД и уведомляет тренера."""
        msg = db.session.get(GroupMessage, message_id)
        if not msg:
            return jsonify({"ok": False, "error": "Message not found"}), 404

        reporter = get_current_user()

        # 1. Сохраняем в БД
        report = MessageReport(
            message_id=msg.id,
            reporter_id=reporter.id,
            reason=request.json.get('reason', 'other')
        )
        db.session.add(report)

        # 2. Уведомляем тренера группы
        group = msg.group
        if group.trainer_id and group.trainer_id != reporter.id:  # Не уведомляем, если тренер сам жалуется (странный кейс)
            from notification_service import send_user_notification
            send_user_notification(
                user_id=group.trainer_id,
                title="Жалоба на сообщение 🛡️",
                body=f"{reporter.name} пожаловался на сообщение в чате.",
                type='warning',
                data={"route": "/squad"}
            )

        db.session.commit()
        return jsonify({"ok": True, "message": "Жалоба отправлена"})

@app.route('/api/groups/<int:group_id>/weekly_stories', methods=['GET'])
@login_required
def get_weekly_stories(group_id):
        """Генерирует данные для Stories (итоги прошлой недели)."""
        group = db.session.get(Group, group_id)
        if not group:
            return jsonify({"ok": False, "error": "Group not found"}), 404

        # 1. Расчет дат (Прошлая неделя Пн-Вс)
        tz = ZoneInfo("Asia/Almaty")
        now = datetime.now(tz)
        today_date = now.date()

        start_of_current_week = today_date - timedelta(days=today_date.weekday())
        start_date = start_of_current_week - timedelta(days=7)
        end_date = start_of_current_week - timedelta(days=1)

        # 2. Топ по баллам
        scores = db.session.query(
            SquadScoreLog.user_id,
            func.sum(SquadScoreLog.points).label('total')
        ).filter(
            SquadScoreLog.group_id == group_id,
            func.date(SquadScoreLog.created_at) >= start_date,
            func.date(SquadScoreLog.created_at) <= end_date
        ).group_by(SquadScoreLog.user_id).order_by(text('total DESC')).limit(3).all()

        if not scores:
            return jsonify({"ok": True, "has_stories": False})

        top_3 = []
        for rank, (uid, total) in enumerate(scores):
            u = db.session.get(User, uid)
            if u:
                top_3.append({
                    "rank": rank + 1,
                    "name": u.name,
                    "avatar": u.avatar.filename if u.avatar else None,
                    "score": int(total)
                })

        # 3. MVP (1 место)
        mvp_data = top_3[0] if top_3 else None

        # Формируем JSON-сценарий сторис
        stories = []

        # Слайд 1: Интро
        stories.append({
            "type": "intro",
            "title": "Итоги недели",
            "subtitle": f"{start_date.strftime('%d.%m')} — {end_date.strftime('%d.%m')}",
            "bg_color": "0xFF4F46E5"
        })

        # Слайд 2: Лидерборд
        if top_3:
            stories.append({
                "type": "leaderboard",
                "title": "Лидеры гонки 🏆",
                "data": top_3,
                "bg_color": "0xFF0F172A"
            })

        # Слайд 3: MVP
        if mvp_data:
            stories.append({
                "type": "mvp",
                "title": "MVP Недели 🔥",
                "user": mvp_data,
                "bg_color": "0xFFFF5722"
            })

        return jsonify({
            "ok": True,
            "has_stories": True,
            "stories": stories
        })


# ------------------ WEIGHT GOAL API FOR FLUTTER ------------------

@app.route('/api/app/set_weight_goal', methods=['POST'])
@login_required
def api_set_weight_goal():
    """
    Устанавливает целевой вес пользователя и фиксирует Точку А (текущий вес).
    """
    user = get_current_user()
    data = request.get_json(force=True, silent=True) or {}

    try:
        new_goal = float(data.get('target_weight', 0))
        if new_goal <= 0 or new_goal > 300:
            return jsonify({"ok": False, "error": "Некорректный вес"}), 400

        # 1. Определяем текущий вес для фиксации Точки А
        current_weight = 0.0
        # Сначала ищем в логах веса
        last_log = WeightLog.query.filter_by(user_id=user.id).order_by(WeightLog.date.desc(),
                                                                       WeightLog.created_at.desc()).first()

        if last_log:
            current_weight = last_log.weight
        else:
            # Fallback: ищем в BodyAnalysis, если логов нет
            last_ana = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
            if last_ana and last_ana.weight:
                current_weight = last_ana.weight
            else:
                # Если совсем ничего нет, считаем целью текущий (фиктивно, чтобы не ломать логику)
                current_weight = new_goal

        # 2. Фиксируем Точку А и Точку Б
        user.start_weight = current_weight
        user.weight_goal = new_goal

        # 3. Создаем запись в WeightLog на сегодня, если её нет (чтобы график начинался красиво)
        today = date.today()
        if not WeightLog.query.filter_by(user_id=user.id, date=today).first():
            db.session.add(WeightLog(user_id=user.id, weight=current_weight, date=today))

        db.session.commit()

        # Аналитика
        try:
            from amplitude import BaseEvent
            amplitude.track(BaseEvent(
                event_type="Weight Goal Updated",
                user_id=str(user.id),
                event_properties={"new_goal": new_goal, "start_point": current_weight}
            ))
        except:
            pass

        return jsonify({"ok": True, "message": "Цель успешно обновлена", "weight_goal": new_goal})

    except ValueError:
        return jsonify({"ok": False, "error": "Значение должно быть числом"}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/app/weight_progress', methods=['GET'])
@login_required
def api_get_weight_progress():
    """
    Возвращает прогресс по весу на основе WeightLog и User.start_weight.
    Без сложного прогнозирования дефицита (только факты).
    """
    user = get_current_user()

    # 1. Проверяем наличие целей (Точка А и Точка Б)
    if user.start_weight is None or user.weight_goal is None:
        # Попытка миграции старых данных "на лету"
        if user.initial_body_analysis_id:
            init_a = db.session.get(BodyAnalysis, user.initial_body_analysis_id)
            if init_a and init_a.weight:
                user.start_weight = init_a.weight
                db.session.commit()
            else:
                return jsonify({"ok": True, "has_data": False, "message": "Цель не установлена"})
        else:
            return jsonify({"ok": True, "has_data": False, "message": "Цель не установлена"})

    try:
        # 2. Получаем текущий вес (последняя запись в логе)
        last_log = WeightLog.query.filter_by(user_id=user.id).order_by(WeightLog.date.desc(),
                                                                       WeightLog.created_at.desc()).first()

        current_weight = 0.0
        if last_log:
            current_weight = last_log.weight
        else:
            # Fallback на BodyAnalysis, если логов нет
            last_ana = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
            if last_ana:
                current_weight = last_ana.weight
            else:
                current_weight = user.start_weight

        # 3. Расчет прогресса (Линейный)
        start = user.start_weight
        goal = user.weight_goal
        current = current_weight

        total_diff = start - goal
        diff_done = start - current

        percentage = 0.0
        if abs(total_diff) > 0.1:
            percentage = (diff_done / total_diff) * 100

        percentage = min(100.0, max(0.0, percentage))

        return jsonify({
            "ok": True,
            "has_data": True,
            "data": {
                "percentage": round(percentage, 1),
                "current_kg": round(current, 1),
                "initial_kg": round(start, 1),  # В JSON поле называется initial_kg для совместимости с фронтом
                "goal_kg": round(goal, 1),
                "total_change_needed": round(total_diff, 1),
                "already_changed": round(diff_done, 1),
                "remaining": round(current - goal, 1)
            }
        })

    except Exception as e:
        print(f"Error in weight API: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/user/set_weight_goal', methods=['POST'])
@login_required
def set_weight_goal():
    user = get_current_user()
    data = request.get_json()
    new_goal = data.get('weight_goal')

    if new_goal is None:
        return jsonify({"ok": False, "error": "Цель не указана"}), 400

    # ЛОГИКА: Находим последний замер веса в базе
    last_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()

    if last_analysis:
        # Текущий последний вес становится Точкой А
        user.initial_body_analysis_id = last_analysis.id

    # Устанавливаем новую Точку Б
    user.weight_goal = float(new_goal)

    db.session.commit()
    return jsonify({"ok": True, "message": "Цель установлена, точка А обновлена"})


# ------------------ RECIPES MANAGEMENT API ------------------

# 1. API ДЛЯ ПРИЛОЖЕНИЯ (FLUTTER)
@app.route('/api/recipes/catalog', methods=['GET'])
@login_required
def get_recipes_catalog():
    """
    Возвращает список категорий с вложенными рецептами.
    Используется в приложении для отображения экрана питания.
    """
    categories = RecipeCategory.query.order_by(RecipeCategory.sort_order.asc()).all()
    result = []

    for cat in categories:
        # Берем только активные рецепты для этой категории
        recipes = Recipe.query.filter_by(category_id=cat.id, is_active=True).all()

        # Если в категории нет рецептов, можно пропускать или отправлять пустой список
        # В данном случае отправляем, даже если пусто, но можно добавить if recipes:

        recipes_data = []
        for r in recipes:
            recipes_data.append(r.to_dict())

        result.append({
            "category_name": cat.name,
            "category_slug": cat.slug,
            "category_color": cat.color_hex,
            "recipes": recipes_data
        })

    return jsonify({"ok": True, "catalog": result})


# 2. АДМИНКА: КАТЕГОРИИ

@app.route('/admin/recipes/categories', methods=['GET'])
@admin_required
def admin_list_categories():
    cats = RecipeCategory.query.order_by(RecipeCategory.sort_order.asc()).all()
    return jsonify({
        "ok": True,
        "categories": [{
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "color_hex": c.color_hex,
            "sort_order": c.sort_order
        } for c in cats]
    })


@app.route('/admin/recipes/categories', methods=['POST'])
@admin_required
def admin_create_category():
    data = request.get_json(force=True, silent=True) or {}

    name = data.get('name')
    slug = data.get('slug')

    if not name or not slug:
        return jsonify({"ok": False, "error": "Name and Slug are required"}), 400

    cat = RecipeCategory(
        name=name,
        slug=slug,
        color_hex=data.get('color_hex', '#FFFFFF'),
        sort_order=int(data.get('sort_order', 0))
    )

    try:
        db.session.add(cat)
        db.session.commit()
        return jsonify({"ok": True, "id": cat.id})
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "Slug already exists"}), 400


@app.route('/admin/recipes/categories/<int:cat_id>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_category(cat_id):
    cat = db.session.get(RecipeCategory, cat_id)
    if not cat:
        return jsonify({"ok": False, "error": "Category not found"}), 404

    if request.method == 'DELETE':
        # При удалении категории рецепты не удаляются, а получают category_id=NULL (из-за SET NULL в модели)
        db.session.delete(cat)
        db.session.commit()
        return jsonify({"ok": True, "message": "Category deleted"})

    # PUT (Update)
    data = request.get_json(force=True, silent=True) or {}

    if 'name' in data:
        cat.name = data['name']
    if 'slug' in data:
        cat.slug = data['slug']
    if 'color_hex' in data:
        cat.color_hex = data['color_hex']
    if 'sort_order' in data:
        cat.sort_order = int(data['sort_order'])

    try:
        db.session.commit()
        return jsonify({"ok": True, "message": "Category updated"})
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "Slug conflict"}), 400


# 3. АДМИНКА: РЕЦЕПТЫ

@app.route('/admin/recipes', methods=['GET'])
@admin_required
def admin_list_recipes():
    # Фильтрация по категории ?category_id=1
    cat_id = request.args.get('category_id')
    query = Recipe.query
    if cat_id:
        query = query.filter_by(category_id=int(cat_id))

    # Сортируем: сначала новые
    recipes = query.order_by(Recipe.created_at.desc()).all()
    return jsonify({
        "ok": True,
        "recipes": [r.to_dict() for r in recipes]
    })


@app.route('/admin/recipes', methods=['POST'])
@admin_required
def admin_create_recipe():
    """
    Создает рецепт.
    Ожидает multipart/form-data, чтобы можно было загрузить картинку.
    Сложные поля (ingredients, instructions) ожидаются в виде JSON-строк.
    """
    try:
        # 1. Текстовые поля и числа
        title = request.form.get('title')
        cat_id = request.form.get('category_id')

        if not title:
            return jsonify({"ok": False, "error": "Title required"}), 400

        calories = int(request.form.get('calories', 0))
        protein = float(request.form.get('protein', 0))
        fat = float(request.form.get('fat', 0))
        carbs = float(request.form.get('carbs', 0))
        prep_time_minutes = int(request.form.get('prep_time_minutes', 0))

        # 2. Обработка файла (Картинка блюда)
        image_url = None
        file = request.files.get('image')
        if file and file.filename:
            filename = secure_filename(file.filename)
            # Генерируем уникальное имя
            unique_filename = f"recipe_{uuid.uuid4().hex}_{filename}"

            file_data = file.read()
            new_file = UploadedFile(
                filename=unique_filename,
                content_type=file.mimetype,
                data=file_data,
                size=len(file_data),
                user_id=get_current_user().id  # <--- ИСПРАВЛЕНО ЗДЕСЬ
            )
            db.session.add(new_file)
            db.session.flush()  # Чтобы файл записался и был доступен

            # Генерируем ссылку для API
            image_url = url_for('serve_file', filename=unique_filename, _external=True)

        # 3. Парсинг JSON полей
        ingredients = []
        instructions = []

        # Получаем данные как строки и пытаемся распарсить
        raw_ingredients = request.form.get('ingredients')
        if raw_ingredients:
            try:
                ingredients = json.loads(raw_ingredients)
            except Exception as e:
                print(f"JSON Error ingredients: {e}")
                # Если ошибка, оставим пустым списком

        raw_instructions = request.form.get('instructions')
        if raw_instructions:
            try:
                instructions = json.loads(raw_instructions)
            except Exception as e:
                print(f"JSON Error instructions: {e}")

        # 4. Создание объекта
        recipe = Recipe(
            category_id=int(cat_id) if cat_id else None,
            title=title,
            image_url=image_url,
            calories=calories,
            protein=protein,
            fat=fat,
            carbs=carbs,
            prep_time_minutes=prep_time_minutes,
            ingredients=ingredients,
            instructions=instructions,
            is_active=True
        )

        db.session.add(recipe)
        db.session.commit()

        return jsonify({"ok": True, "recipe": recipe.to_dict()})

    except Exception as e:
        db.session.rollback()
        print(f"Error creating recipe: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/admin/recipes/<int:r_id>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_recipe(r_id):
    recipe = db.session.get(Recipe, r_id)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found"}), 404

    if request.method == 'DELETE':
        db.session.delete(recipe)
        db.session.commit()
        return jsonify({"ok": True, "message": "Recipe deleted"})

    # PUT (Редактирование)
    try:
        # Сценарий 1: Пришел JSON (обновление данных без картинки)
        if request.is_json:
            data = request.get_json()

            if 'title' in data:
                recipe.title = data['title']
            if 'category_id' in data:
                recipe.category_id = int(data['category_id']) if data['category_id'] else None

            if 'calories' in data:
                recipe.calories = int(data['calories'])
            if 'protein' in data:
                recipe.protein = float(data['protein'])
            if 'fat' in data:
                recipe.fat = float(data['fat'])
            if 'carbs' in data:
                recipe.carbs = float(data['carbs'])

            if 'prep_time_minutes' in data:
                recipe.prep_time_minutes = int(data['prep_time_minutes'])

            if 'ingredients' in data:
                # Если пришел уже список (от JS фронтенда), сохраняем как есть
                recipe.ingredients = data['ingredients']

            if 'instructions' in data:
                recipe.instructions = data['instructions']

            if 'is_active' in data:
                recipe.is_active = bool(data['is_active'])

            # Если прислали URL картинки строкой
            if 'image_url' in data:
                recipe.image_url = data['image_url']

        # Сценарий 2: Пришел Multipart Form (возможно с файлом)
        else:
            if request.form.get('title'):
                recipe.title = request.form.get('title')
            if request.form.get('category_id'):
                cid = request.form.get('category_id')
                recipe.category_id = int(cid) if cid and cid != 'null' else None

            if request.form.get('calories') is not None:
                recipe.calories = int(request.form.get('calories'))
            if request.form.get('protein') is not None:
                recipe.protein = float(request.form.get('protein'))
            if request.form.get('fat') is not None:
                recipe.fat = float(request.form.get('fat'))
            if request.form.get('carbs') is not None:
                recipe.carbs = float(request.form.get('carbs'))

            if request.form.get('prep_time_minutes') is not None:
                recipe.prep_time_minutes = int(request.form.get('prep_time_minutes'))

            if request.form.get('is_active') is not None:
                val = request.form.get('is_active').lower()
                recipe.is_active = (val in ['true', '1', 'on'])

            # JSON поля из формы (приходят строками)
            if request.form.get('ingredients'):
                try:
                    recipe.ingredients = json.loads(request.form.get('ingredients'))
                except:
                    pass

            if request.form.get('instructions'):
                try:
                    recipe.instructions = json.loads(request.form.get('instructions'))
                except:
                    pass

            # Картинка
            file = request.files.get('image')
            if file and file.filename:
                filename = secure_filename(file.filename)
                unique_filename = f"recipe_{uuid.uuid4().hex}_{filename}"

                file_data = file.read()
                new_file = UploadedFile(
                    filename=unique_filename,
                    content_type=file.mimetype,
                    data=file_data,
                    size=len(file_data),
                    user_id=get_current_user().id  # <--- ИСПРАВЛЕНО ЗДЕСЬ
                )
                db.session.add(new_file)
                db.session.flush()
                # Обновляем ссылку
                recipe.image_url = url_for('serve_file', filename=unique_filename, _external=True)

        db.session.commit()
        return jsonify({"ok": True, "recipe": recipe.to_dict()})

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/admin/support')
@admin_required
def admin_support_page():
    # Загружаем тикеты с данными пользователей
    tickets = db.session.query(SupportTicket, User).join(User, SupportTicket.user_id == User.id).order_by(
        SupportTicket.updated_at.desc()).all()

    tickets_data = []
    for t, u in tickets:
        # Собираем сообщения
        msgs = []
        sorted_msgs = sorted(t.messages, key=lambda x: x.created_at)
        for m in sorted_msgs:
            msgs.append({
                'sender': m.sender_type,  # 'user' or 'support'
                'text': m.text,
                'image_url': m.image_url,
                'created_at': m.created_at.strftime('%d.%m %H:%M')
            })

        tickets_data.append({
            'id': t.id,
            'user_id': u.id,
            'user_name': u.name or u.email,
            'user_email': u.email,
            'status': t.status,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': t.updated_at.strftime('%Y-%m-%d %H:%M'),
            'messages': msgs,
            'last_msg': msgs[-1]['text'] if msgs and msgs[-1]['text'] else ('Картинка' if msgs else 'Пусто')
        })

    return render_template('admin_support.html', tickets=tickets_data)


@app.route('/admin/support/reply', methods=['POST'])
@admin_required
def admin_support_reply():
    ticket_id = request.form.get('ticket_id')
    text = request.form.get('text')

    ticket = SupportTicket.query.get(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    msg = SupportMessage(
        ticket_id=ticket.id,
        sender_type='support',
        text=text
    )

    ticket.updated_at = datetime.utcnow()
    # Если ответили, можно менять статус или оставить как есть.
    # Обычно если админ ответил, тикет все еще open, ждет реакции юзера.

    db.session.add(msg)
    db.session.commit()

    return jsonify({'status': 'ok', 'time': msg.created_at.strftime('%d.%m %H:%M')})


@app.route('/admin/support/close/<int:ticket_id>', methods=['POST'])
@admin_required
def admin_support_close(ticket_id):
    ticket = SupportTicket.query.get(ticket_id)
    if ticket:
        ticket.status = 'closed' if ticket.status == 'open' else 'open'
        ticket.updated_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('admin_support_page'))

# В app.py добавьте этот роут:
@app.route("/admin/recipes/page")
@admin_required
def admin_recipes_page():
    return render_template("admin_recipes.html")
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)