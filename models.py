import json  # <-- Добавлен импорт для работы с JSON в to_dict()
from datetime import datetime, date, timedelta, time as dt_time
from sqlalchemy import UniqueConstraint, event
from sqlalchemy.sql import expression
from extensions import db


# ------------------ USERS / SUBSCRIPTION ------------------

class User(db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    date_of_birth = db.Column(db.Date)
    renewal_reminder_last_shown_on = db.Column(db.Date)
    renewal_telegram_sent = db.Column(db.Boolean, default=False, server_default=expression.false())
    fcm_device_token = db.Column(db.String(255), nullable=True, unique=True, index=True)
    # Глобальные флаги уведомлений (держим для обратной совместимости)
    telegram_notify_enabled = db.Column(db.Boolean, default=True, server_default=expression.true())
    notify_trainings = db.Column(db.Boolean, default=True, server_default=expression.true())
    notify_subscription = db.Column(db.Boolean, default=True, server_default=expression.true())

    # В models.py внутри class User:
    full_body_photo_id = db.Column(db.Integer, db.ForeignKey('uploaded_files.id'), nullable=True)
    full_body_photo = db.relationship('UploadedFile', foreign_keys=[full_body_photo_id])

    # Цели
    weight_goal = db.Column(db.Float, nullable=True)  # Точка Б (целевой вес)
    fat_mass_goal = db.Column(db.Float, nullable=True)
    muscle_mass_goal = db.Column(db.Float, nullable=True)
    # Новое поле:
    step_goal = db.Column(db.Integer, default=10000, server_default='10000', nullable=False)

    initial_body_analysis_id = db.Column(db.Integer, db.ForeignKey('body_analysis.id'), nullable=True)
    last_measurement_reminder_sent_at = db.Column(db.DateTime, nullable=True)

    is_trainer = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())

    # Новые поля для визуализации тела
    sex = db.Column(db.String(10), nullable=False, server_default='male', default='male')  # 'male' | 'female'
    face_consent = db.Column(db.Boolean, nullable=False, server_default=expression.false(), default=False)

    analysis_comment = db.Column(db.Text)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
    telegram_code = db.Column(db.String(10), nullable=True)
    show_welcome_popup = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    onboarding_complete = db.Column(db.Boolean, default=False, nullable=False)

    # Верификация почты и сброс пароля
    verification_code = db.Column(db.String(6), nullable=True)
    verification_code_expires_at = db.Column(db.DateTime, nullable=True)
    is_verified = db.Column(db.Boolean, default=False, server_default=expression.false())

    # --- SQUADS (Групповые челленджи) ---
    squad_status = db.Column(db.String(20), default='none')  # 'none', 'pending', 'active'
    squad_pref_time = db.Column(db.String(20), nullable=True)  # 'morning', 'day', 'evening'
    squad_fitness_level = db.Column(db.String(20), nullable=True)  # 'newbie', 'pro'
    # ------------------------------------

    # Кэш стрика (пересчитывается на основе реальных записей MealLog)
    current_streak = db.Column(db.Integer, default=0, server_default='0', nullable=False)
    streak_nutrition = db.Column(db.Integer, default=0, server_default='0', nullable=False)
    streak_activity = db.Column(db.Integer, default=0, server_default='0', nullable=False)
    # отношения
    subscription = db.relationship(
        'Subscription',
        backref=db.backref('user', uselist=False),
        uselist=False
    )

    avatar_file_id = db.Column(db.Integer, db.ForeignKey('uploaded_files.id'), nullable=True)
    # Убрали lazy='joined', теперь данные файла не будут грузиться автоматически при каждом запросе User
    avatar = db.relationship('UploadedFile', foreign_keys=[avatar_file_id])

    @property
    def has_subscription(self):
        return self.is_trainer or (self.subscription and self.subscription.is_active)

    # ----- Доступ к последнему анализу (динамические свойства) -----
    def _get_latest_analysis(self):
        if not hasattr(self, '_cached_latest_analysis'):
            self._cached_latest_analysis = BodyAnalysis.query.filter_by(user_id=self.id) \
                .order_by(BodyAnalysis.timestamp.desc()).first()
        return self._cached_latest_analysis

    @property
    def latest_analysis(self):
        return self._get_latest_analysis()

    @property
    def height(self):
        a = self._get_latest_analysis()
        return a.height if a else None

    @property
    def weight(self):
        a = self._get_latest_analysis()
        return a.weight if a else None

    @property
    def muscle_mass(self):
        a = self._get_latest_analysis()
        return a.muscle_mass if a else None

    @property
    def muscle_percentage(self):
        a = self._get_latest_analysis()
        return a.muscle_percentage if a else None

    @property
    def body_water(self):
        a = self._get_latest_analysis()
        return a.body_water if a else None

    @property
    def protein_percentage(self):
        a = self._get_latest_analysis()
        return a.protein_percentage if a else None

    @property
    def bone_mineral_percentage(self):
        a = self._get_latest_analysis()
        return a.bone_mineral_percentage if a else None

    @property
    def skeletal_muscle_mass(self):
        a = self._get_latest_analysis()
        return a.skeletal_muscle_mass if a else None

    @property
    def visceral_fat_rating(self):
        a = self._get_latest_analysis()
        return a.visceral_fat_rating if a else None

    @property
    def metabolism(self):
        a = self._get_latest_analysis()
        return a.metabolism if a else None

    @property
    def waist_hip_ratio(self):
        a = self._get_latest_analysis()
        return a.waist_hip_ratio if a else None

    @property
    def body_age(self):
        a = self._get_latest_analysis()
        return a.body_age if a else None

    @property
    def fat_mass(self):
        a = self._get_latest_analysis()
        return a.fat_mass if a else None

    @property
    def bmi(self):
        a = self._get_latest_analysis()
        return a.bmi if a else None

    @property
    def fat_free_body_weight(self):
        a = self._get_latest_analysis()
        return a.fat_free_body_weight if a else None


class SubscriptionApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # ID пользователя, который оставил заявку
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # Номер телефона, который он ввел
    phone_number = db.Column(db.String(20), nullable=False)
    # Дата создания
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Статус: 'pending' (в ожидании), 'processed' (обработана)
    status = db.Column(db.String(50), default='pending')

    # Связь, чтобы можно было легко получить пользователя
    user = db.relationship('User', backref=db.backref('applications', lazy=True))

    def __repr__(self):
        return f'<SubscriptionApplication {self.id} от User {self.user_id}>'


class Subscription(db.Model):
    __tablename__ = "subscription"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    start_date = db.Column(db.Date, default=date.today)
    end_date = db.Column(db.Date, nullable=True)
    source = db.Column(db.String(50))
    status = db.Column(db.String(20), nullable=False, default='active',
                       server_default='active')  # active, frozen, cancelled
    remaining_days_on_freeze = db.Column(db.Integer, nullable=True)

    @property
    def is_active(self):
        today = date.today()
        return (self.status == 'active'
                and self.start_date <= today
                and (self.end_date is None or self.end_date >= today))


class Order(db.Model):
    __tablename__ = "order"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    order_id = db.Column(db.String(36), unique=True, nullable=False)
    kaspi_invoice_id = db.Column(db.String(100), nullable=True)
    subscription_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', server_default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('orders', lazy=True))


# ------------------ GROUPS / CHAT ------------------

class Group(db.Model):
    __tablename__ = "group"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    trainer = db.relationship('User', backref=db.backref('own_group', uselist=False))
    members = db.relationship('GroupMember', back_populates='group', cascade='all, delete-orphan')
    messages = db.relationship('GroupMessage', back_populates='group', cascade='all, delete-orphan')
    tasks = db.relationship('GroupTask', backref=db.backref('group'), cascade='all, delete-orphan', lazy='dynamic')


class GroupMember(db.Model):
    __tablename__ = "group_member"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='uq_group_user'),)

    group = db.relationship('Group', back_populates='members')
    user = db.relationship('User', backref=db.backref('groups', lazy='dynamic'))


class GroupMessage(db.Model):
    __tablename__ = "group_message"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    image_file = db.Column(db.String(200), nullable=True)

    # --- НОВЫЕ ПОЛЯ ---
    # Тип сообщения: 'post' (тренер), 'system' (достижение), 'comment' (ответ)
    type = db.Column(db.String(20), default='post')
    # Ссылка на родительский пост (если это комментарий)
    parent_id = db.Column(db.Integer, db.ForeignKey('group_message.id'), nullable=True)
    # ------------------

    group = db.relationship('Group', back_populates='messages')
    user = db.relationship('User')

    # Связь с родительским сообщением и дочерними комментариями
    replies = db.relationship('GroupMessage',
                              backref=db.backref('parent', remote_side=[id]),
                              cascade='all, delete-orphan')

    reactions = db.relationship('MessageReaction', back_populates='message', cascade='all, delete-orphan')

class MessageReaction(db.Model):
    __tablename__ = "message_reaction"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('group_message.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reaction_type = db.Column(db.String(20), nullable=False, default='👍', server_default='👍')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='uq_message_user_reaction'),)

    message = db.relationship('GroupMessage', back_populates='reactions')
    user = db.relationship('User')


class GroupTask(db.Model):
    __tablename__ = "group_task"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    trainer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_announcement = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.Date, nullable=True)

    trainer = db.relationship('User')


class SquadScoreLog(db.Model):
    __tablename__ = 'squad_score_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    # Группа, в которой состоял юзер на момент получения (для истории)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True, index=True)

    points = db.Column(db.Integer, nullable=False)
    # Категории: 'food_log', 'workout', 'healthy_progress'
    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User', backref=db.backref('score_logs', lazy='dynamic', cascade='all, delete-orphan'))
    group = db.relationship('Group', backref=db.backref('score_logs', lazy='dynamic'))

# ------------------ DIET / ACTIVITY ------------------

class MealLog(db.Model):
    __tablename__ = 'meal_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    meal_type = db.Column(db.String(20), nullable=False)  # 'breakfast','lunch','dinner','snack'
    name = db.Column(db.String(100), nullable=True)
    verdict = db.Column(db.String(200), nullable=True)
    calories = db.Column(db.Integer, nullable=False)
    protein = db.Column(db.Float, nullable=False)
    fat = db.Column(db.Float, nullable=False)
    carbs = db.Column(db.Float, nullable=False)
    analysis = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Новое:
    image_path = db.Column(db.String(255), nullable=True)
    is_flagged = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())

    # Каскад на стороне ORM: удаляем логи при удалении пользователя
    user = db.relationship(
        'User',
        backref=db.backref('meals', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    )
    __table_args__ = (UniqueConstraint('user_id', 'date', 'meal_type', name='uq_user_date_meal'),)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.BigInteger, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    entity = db.Column(db.String(100), nullable=False)
    entity_id = db.Column(db.String(100), nullable=False)
    old_data = db.Column(db.JSON, nullable=True)
    new_data = db.Column(db.JSON, nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PromptTemplate(db.Model):
    __tablename__ = "prompt_templates"
    __table_args__ = (db.UniqueConstraint('name', 'version', name='uq_prompt_name_version'),)

    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    body = db.Column(db.Text, nullable=False)
    params = db.Column(db.JSON, nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Activity(db.Model):
    __tablename__ = "activity"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.Date, default=date.today, index=True)
    steps = db.Column(db.Integer)
    active_kcal = db.Column(db.Integer)
    resting_kcal = db.Column(db.Integer)
    distance_km = db.Column(db.Float)
    heart_rate_avg = db.Column(db.Integer)
    source = db.Column(db.String(50))

    user = db.relationship(
        "User",
        backref=db.backref("activities", lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    )


class Diet(db.Model):
    __tablename__ = "diet"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    date = db.Column(db.Date, default=date.today, index=True)
    breakfast = db.Column(db.Text)
    lunch = db.Column(db.Text)
    dinner = db.Column(db.Text)
    snack = db.Column(db.Text)
    total_kcal = db.Column(db.Integer)
    protein = db.Column(db.Float)
    fat = db.Column(db.Float)
    carbs = db.Column(db.Float)

    user = db.relationship(
        'User',
        backref=db.backref('diets', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    )


# ------------------ TRAININGS ------------------

class Training(db.Model):
    __tablename__ = 'trainings'

    id = db.Column(db.Integer, primary_key=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    # Добавлено поле group_id (nullable=True, так как бывают общие тренировки)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True, index=True)

    meeting_link = db.Column(db.String(255), nullable=False)

    title = db.Column(db.String(120), nullable=False, default="Онлайн-тренировка")
    description = db.Column(db.Text, default="")
    date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    location = db.Column(db.String(120))
    capacity = db.Column(db.Integer, default=10)
    is_public = db.Column(db.Boolean, default=True, server_default=expression.true())

    # Флаги для групповых уведомлений (чтобы не спамить)
    group_notified_1h = db.Column(db.Boolean, default=False, server_default=expression.false())
    group_notified_start = db.Column(db.Boolean, default=False, server_default=expression.false())

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    trainer = db.relationship('User', backref=db.backref('trainings', lazy=True))
    signups = db.relationship('TrainingSignup', backref='training', cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint('trainer_id', 'date', 'start_time', name='uq_trainer_date_start'),)

    # UI helper
    def to_dict(self, me_id=None):
        mine = (me_id is not None and self.trainer_id == me_id)

        now = datetime.now()
        start_dt = datetime.combine(self.date, self.start_time)
        end_dt = datetime.combine(self.date, self.end_time)
        is_past = now >= end_dt
        link_visible_at = start_dt - timedelta(minutes=10)

        joined = False
        if me_id:
            joined = any(s.user_id == me_id for s in self.signups)

        seats_taken = len(self.signups)
        spots_left = max(0, (self.capacity or 0) - seats_taken)

        can_open_link = False
        if mine:
            can_open_link = True
        elif joined and (now >= link_visible_at) and not is_past:
            can_open_link = True

        payload = {
            "id": self.id,
            "trainer_id": self.trainer_id,
            "trainer_name": (self.trainer.name if self.trainer and getattr(self.trainer, "name", None) else "Тренер"),
            "title": self.title or "Онлайн-тренировка",
            "date": self.date.strftime("%Y-%m-%d"),
            "start_time": self.start_time.strftime("%H:%M"),
            "end_time": self.end_time.strftime("%H:%M"),
            "mine": mine,
            "joined": joined,
            "is_past": is_past,
            "spots_left": spots_left,
            "link_visible_at": link_visible_at.isoformat(timespec="minutes"),
            "can_open_link": can_open_link
        }
        if can_open_link:
            payload["meeting_link"] = self.meeting_link
        return payload


class TrainingSignup(db.Model):
    __tablename__ = 'training_signups'

    id = db.Column(db.Integer, primary_key=True)
    training_id = db.Column(db.Integer, db.ForeignKey('trainings.id', ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete="CASCADE"), nullable=False, index=True)
    notified_1h = db.Column(db.Boolean, default=False, server_default=expression.false())
    notified_start = db.Column(db.Boolean, default=False, server_default=expression.false())
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('training_id', 'user_id', name='uq_training_user'),)


# ------------------ BODY ANALYSIS ------------------

class BodyAnalysis(db.Model):
    __tablename__ = "body_analysis"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    height = db.Column(db.Integer)
    weight = db.Column(db.Float)
    muscle_mass = db.Column(db.Float)
    muscle_percentage = db.Column(db.Float)
    body_water = db.Column(db.Float)
    protein_percentage = db.Column(db.Float)
    bone_mineral_percentage = db.Column(db.Float)
    skeletal_muscle_mass = db.Column(db.Float)
    visceral_fat_rating = db.Column(db.Float)
    metabolism = db.Column(db.Integer)
    waist_hip_ratio = db.Column(db.Float)
    body_age = db.Column(db.Integer)
    fat_mass = db.Column(db.Float)
    bmi = db.Column(db.Float)
    fat_free_body_weight = db.Column(db.Float)
    ai_comment = db.Column(db.Text, nullable=True)

    user = db.relationship(
        'User',
        foreign_keys=[user_id],  # <--- ДОБАВЬТЕ ЭТУ СТРОКУ
        backref=db.backref('analyses', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    )


# ------------------ SETTINGS / REMINDERS ------------------

class UserSettings(db.Model):
    __tablename__ = "user_settings"

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    telegram_notify_enabled = db.Column(
        db.Boolean, default=True, server_default=expression.true(), nullable=False
    )
    notify_trainings = db.Column(
        db.Boolean, default=True, server_default=expression.true(), nullable=False
    )
    notify_subscription = db.Column(
        db.Boolean, default=True, server_default=expression.true(), nullable=False
    )
    notify_meals = db.Column(
        db.Boolean, default=True, server_default=expression.true(), nullable=False
    )
    meal_timezone = db.Column(
        db.String(64), default="Asia/Almaty", server_default="Asia/Almaty", nullable=False
    )

    user = db.relationship("User", backref=db.backref("settings", uselist=False))


class MealReminderLog(db.Model):
    __tablename__ = "meal_reminder_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    meal_type = db.Column(db.String(16), nullable=False)  # breakfast|lunch|dinner
    date_sent = db.Column(db.Date, nullable=False, default=date.today, index=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "meal_type", "date_sent", name="u_meal_reminder_once_per_day"),
    )


# ------------------ DIET AUTOGEN PREFS / STAGING ------------------

class DietPreference(db.Model):
    __tablename__ = "diet_preference"

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    # При первичной генерации фиксируем долговременные настройки
    sex = db.Column(db.String(16), nullable=True)  # 'male' | 'female' | None
    goal = db.Column(db.String(32), nullable=True)  # 'fat_loss' | 'muscle_gain' | 'recomp' | ...
    include_favorites = db.Column(db.Text, nullable=True)  # запоминаем вкусы
    exclude_ingredients = db.Column(db.Text, nullable=True)
    kcal_target = db.Column(db.Integer, nullable=True)
    protein_min = db.Column(db.Float, nullable=True)
    fat_max = db.Column(db.Float, nullable=True)
    carbs_max = db.Column(db.Float, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("diet_preference", uselist=False))


class StagedDiet(db.Model):
    __tablename__ = "staged_diet"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    date = db.Column(db.Date, index=True, nullable=False, default=date.today)
    breakfast = db.Column(db.Text)
    lunch = db.Column(db.Text)
    dinner = db.Column(db.Text)
    snack = db.Column(db.Text)
    total_kcal = db.Column(db.Integer)
    protein = db.Column(db.Float)
    fat = db.Column(db.Float)
    carbs = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'date', name='uq_staged_user_date'),)

    user = db.relationship("User", backref=db.backref("staged_diets", lazy=True))


# === NEW: файлы в БД ===
class UploadedFile(db.Model):
    __tablename__ = 'uploaded_files'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    content_type = db.Column(db.String(120))
    data = db.Column(db.LargeBinary, nullable=False)
    size = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# === Shopping cart (NEW) ===
class ShoppingCart(db.Model):
    __tablename__ = "shopping_cart"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    diet_id = db.Column(db.Integer, db.ForeignKey("diet.id"), index=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("shopping_carts", lazy=True))
    diet = db.relationship("Diet", backref=db.backref("shopping_cart", uselist=False))


class ShoppingCartItem(db.Model):
    __tablename__ = "shopping_cart_item"

    id = db.Column(db.Integer, primary_key=True)
    cart_id = db.Column(db.Integer, db.ForeignKey("shopping_cart.id"), nullable=False, index=True)
    meal_type = db.Column(db.String(20), nullable=False)  # breakfast/lunch/dinner/snack

    product_name = db.Column(db.String(255), nullable=False)
    kaspi_query = db.Column(db.String(255))
    kaspi_url = db.Column(db.String(1024))

    total_grams = db.Column(db.Float)  # суммарно по позиции (если имеет смысл)
    pack_grams = db.Column(db.Float)  # предложенная фасовка
    quantity_packs = db.Column(db.Integer, default=1)

    cart = db.relationship("ShoppingCart", backref=db.backref("items", lazy=True))


class BodyVisualization(db.Model):
    __tablename__ = "body_visualization"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # исходные метрики и целевые (для восстановления промптов)
    metrics_current = db.Column(db.JSON, nullable=False)
    metrics_target = db.Column(db.JSON, nullable=False)

    image_current_path = db.Column(db.String(300), nullable=False)
    image_target_path = db.Column(db.String(300), nullable=False)

    provider = db.Column(db.String(50), nullable=False, default="gemini")  # 'gemini'
    provider_job_id = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="done")  # 'done'|'error'
    error = db.Column(db.Text, nullable=True)

    user = db.relationship("User", backref=db.backref("visualizations", lazy=True,
                                                      order_by="desc(BodyVisualization.created_at)"))


class UserAchievement(db.Model):
    __tablename__ = 'user_achievements'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    slug = db.Column(db.String(50), nullable=False)  # Уникальный код ачивки: 'streak_5', 'fat_loss_5kg'
    earned_at = db.Column(db.DateTime, default=datetime.utcnow)
    seen = db.Column(db.Boolean, default=False, nullable=False)  # Флаг: видел ли юзер анимацию

    __table_args__ = (db.UniqueConstraint('user_id', 'slug', name='uq_user_achievement'),)


class EmailVerification(db.Model):
    __tablename__ = "email_verification"
    email = db.Column(db.String(120), primary_key=True)
    code = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)


# ------------------ NOTIFICATIONS ------------------

class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=True)

    # Тип уведомления: 'info', 'warning', 'success', 'reminder'
    type = db.Column(db.String(50), default='info')

    # Статус прочтения
    is_read = db.Column(db.Boolean, default=False)

    # JSON-данные для навигации (например, {"route": "/diet", "args": "..."})
    data_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "type": self.type,
            "is_read": self.is_read,
            "data": json.loads(self.data_json) if self.data_json else {},
            "created_at": self.created_at.isoformat()
        }


class MessageReport(db.Model):
    __tablename__ = "message_reports"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('group_message.id'), nullable=False)
    reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reason = db.Column(db.String(50), default='other')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- ДОБАВЛЕННЫЕ СВЯЗИ ---
    # Теперь можно делать report.message
    message = db.relationship('GroupMessage', backref=db.backref('reports', lazy=True))

    # Рекомендую также добавить это, чтобы можно было делать report.reporter (узнать, кто пожаловался)
    reporter = db.relationship('User', foreign_keys=[reporter_id])


class AnalyticsEvent(db.Model):
    __tablename__ = "analytics_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    event_type = db.Column(db.String(50), nullable=False, index=True)  # например: 'signup', 'onboarding_step_1'
    event_data = db.Column(db.JSON, nullable=True)  # Доп. данные (например, success: true/false)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User', backref=db.backref('analytics_events', lazy=True))

# ------------------ RECIPES (ADMIN MANAGED) ------------------

class RecipeCategory(db.Model):
    __tablename__ = 'recipe_categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)       # Название: "Завтрак", "Обед"
    slug = db.Column(db.String(50), unique=True, nullable=False) # "breakfast", "lunch"
    color_hex = db.Column(db.String(10), default='#FFFFFF') # Цвет карточки: "#FFE2D3"
    icon_url = db.Column(db.Text, nullable=True)           # Иконка категории
    sort_order = db.Column(db.Integer, default=0)          # Для сортировки в списке

    # Связь с рецептами
    recipes = db.relationship('Recipe', backref='category', lazy=True)

    def __repr__(self):
        return f'<RecipeCategory {self.name}>'


class Recipe(db.Model):
    __tablename__ = 'recipes'

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('recipe_categories.id', ondelete='SET NULL'), nullable=True)

    # Основная информация
    title = db.Column(db.String(255), nullable=False)
    image_url = db.Column(db.Text, nullable=True)          # Ссылка на фото

    # КБЖУ
    calories = db.Column(db.Integer, nullable=False)
    protein = db.Column(db.Float, default=0.0)
    fat = db.Column(db.Float, default=0.0)
    carbs = db.Column(db.Float, default=0.0)

    # Детали
    prep_time_minutes = db.Column(db.Integer, nullable=True)

    # Структурированные данные (JSON)
    # Пример: [{"name": "Овсянка", "amount": "50 г"}, ...]
    ingredients = db.Column(db.JSON, default=list, nullable=True)

    # Пример: [{"step": 1, "text": "Залить водой", "image_url": "..."}]
    instructions = db.Column(db.JSON, default=list, nullable=True)

    is_active = db.Column(db.Boolean, default=True, server_default=expression.true())
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "category_name": self.category.name if self.category else None,
            "category_color": self.category.color_hex if self.category else "#FFFFFF",
            "title": self.title,
            "image_url": self.image_url,
            "calories": self.calories,
            "protein": self.protein,
            "fat": self.fat,
            "carbs": self.carbs,
            "prep_time_minutes": self.prep_time_minutes,
            "ingredients": self.ingredients or [],
            "instructions": self.instructions or []
        }
    
@event.listens_for(User, "after_insert")
def create_default_settings(mapper, connection, target):
    """
    Гарантируем наличие строки user_settings для каждого пользователя.
    Значения выставятся серверными дефолтами:
    - все уведомления ВКЛ
    - таймзона: Asia/Almaty
    """
    connection.execute(
        UserSettings.__table__.insert().values(user_id=target.id)
    )