import os
import json
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from extensions import db
from models import (
    User, Subscription, Diet, StagedDiet, DietPreference, BodyAnalysis, UserSettings
)

# === Алмата таймзона ===
ALMATY = ZoneInfo("Asia/Almaty")
_SCHED = None


# ==============================
#   УТИЛИТЫ
# ==============================

def _today_local() -> date:
    return datetime.now(ALMATY).date()

def _active_subscribers():
    """Пользователи с активной подпиской на сегодня."""
    today = _today_local()
    q = (
        db.session.query(User)
        .join(Subscription, Subscription.user_id == User.id)
        .filter(
            Subscription.status == 'active',
            Subscription.start_date <= today,
            (Subscription.end_date.is_(None)) | (Subscription.end_date >= today),
        )
    )
    return q.all()

def _ensure_preferences(user: User):
    """Создаём DietPreference при первом заходе (пол/цель/предпочтения — если известны)."""
    pref = DietPreference.query.filter_by(user_id=user.id).first()
    if pref:
        return pref

    pref = DietPreference(
        user_id=user.id,
        # если у вас пол/цель хранятся в других таблицах — подставьте оттуда
        sex=None,                      # не знаем — оставляем None; поле есть и сохранится
        goal=None,
        include_favorites=None,
        exclude_ingredients=None,
        kcal_target=None,
        protein_min=None,
        fat_max=None,
        carbs_max=None,
    )
    db.session.add(pref)
    db.session.flush()
    return pref

# === Реальная генерация: та же логика, что и в ручном /generate_diet ===
# Вынесена сюда как общая функция, чтобы не было дублирования.
# Если у вас уже есть функция, которая делает GPT-вызов — просто импортируйте её и используйте вместо этой.
from openai import OpenAI

def _gpt_client():
    key = os.getenv("OPENAI_API_KEY") or current_app.config.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=key)

def _analysis_json(ba: BodyAnalysis | None):
    if not ba:
        return {}
    return {
        "height": ba.height, "weight": ba.weight, "fat_mass": ba.fat_mass,
        "muscle_mass": ba.muscle_mass, "visceral_fat_rating": ba.visceral_fat_rating,
        "metabolism": ba.metabolism, "bmi": ba.bmi, "body_age": ba.body_age,
        "skeletal_muscle_mass": ba.skeletal_muscle_mass, "protein_percentage": ba.protein_percentage,
        "bone_mineral_percentage": ba.bone_mineral_percentage, "body_water": ba.body_water,
        "waist_hip_ratio": ba.waist_hip_ratio
    }

def _generate_diet_with_gpt(user: User, pref: DietPreference | None, target_date: date) -> dict:
    """
    Генерация в ТОМ ЖЕ формате, что и /generate_diet:
    breakfast/lunch/dinner/snack — СПИСКИ блюд вида {"name","grams","kcal","recipe"} + total_kcal/protein/fat/carbs.
    """
    latest = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
    payload = {
        "user": {"id": user.id, "name": user.name},
        "date": str(target_date),
        "prefs": {
            "sex": pref.sex if pref else None,
            "goal": pref.goal if pref else None,
            "include_favorites": pref.include_favorites if pref else None,
            "exclude_ingredients": pref.exclude_ingredients if pref else None,
            "kcal_target": pref.kcal_target if pref else None,
            "protein_min": pref.protein_min if pref else None,
            "fat_max": pref.fat_max if pref else None,
            "carbs_max": pref.carbs_max if pref else None,
        },
        "body_analysis": _analysis_json(latest)
    }

    client = _gpt_client()
    prompt = (
        "Сгенерируй рацион на 1 день (завтрак, обед, ужин, перекус) на основе метрик тела и предпочтений.\n"
        "ДЛЯ КАЖДОГО приёма верни МАССИВ объектов с ключами:\n"
        "  {\"name\":\"...\",\"grams\":0,\"kcal\":0,\"recipe\":\"...\"}\n"
        "Итог сверху:\n"
        "{\n"
        "  \"breakfast\": [ ... ],\n"
        "  \"lunch\": [ ... ],\n"
        "  \"dinner\": [ ... ],\n"
        "  \"snack\": [ ... ],\n"
        "  \"total_kcal\": 0,\n"
        "  \"protein\": 0,\n"
        "  \"fat\": 0,\n"
        "  \"carbs\": 0\n"
        "}\n"
        "Верни ЧИСТЫЙ JSON (без префиксов/бэктиков)."
    )

    msg = [
        {"role": "system", "content": "Ты профессиональный диетолог. Отвечай строго в формате JSON."},
        {"role": "user", "content": f"Входные данные JSON:\n{json.dumps(payload, ensure_ascii=False)}\n\n{prompt}"}
    ]
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=msg,
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    txt = resp.choices[0].message.content.strip()
    # На всякий случай уберём возможные ```json ... ``` (хотя response_format должен вернуть чистый JSON)
    if txt.startswith("```"):
        try:
            txt = txt.split("```json", 1)[1].split("```", 1)[0].strip()
        except Exception:
            pass
    data = json.loads(txt)

    # Нормализация и валидация
    def _ensure_items(v):
        if isinstance(v, list):
            out = []
            for it in v:
                if not isinstance(it, dict):
                    continue
                out.append({
                    "name": it.get("name") or "Блюдо",
                    "grams": int(it.get("grams") or 0),
                    "kcal": int(it.get("kcal") or 0),
                    "recipe": it.get("recipe") or ""
                })
            return out
        return []

    for meal in ("breakfast", "lunch", "dinner", "snack"):
        data[meal] = _ensure_items(data.get(meal, []))

    # Итоги
    data["total_kcal"] = int(data.get("total_kcal") or 0)
    data["protein"]    = float(data.get("protein") or 0)
    data["fat"]        = float(data.get("fat") or 0)
    data["carbs"]      = float(data.get("carbs") or 0)

    return data


def _upsert_staged(user: User, d: dict, day: date):
    sd = StagedDiet.query.filter_by(user_id=user.id, date=day).first()
    if not sd:
        sd = StagedDiet(user_id=user.id, date=day)
    # ⬇️ сохраняем массивы блюд как валидный JSON (строки), чтобы потом json.loads(...) работал без ошибок
    sd.breakfast = json.dumps(d["breakfast"], ensure_ascii=False)
    sd.lunch     = json.dumps(d["lunch"], ensure_ascii=False)
    sd.dinner    = json.dumps(d["dinner"], ensure_ascii=False)
    sd.snack     = json.dumps(d["snack"], ensure_ascii=False)
    sd.total_kcal = int(d["total_kcal"])
    sd.protein    = float(d["protein"])
    sd.fat        = float(d["fat"])
    sd.carbs      = float(d["carbs"])
    db.session.add(sd)


def _promote_staged_to_final(user: User, day: date):
    sd = StagedDiet.query.filter_by(user_id=user.id, date=day).first()
    if not sd:
        return False
    fin = Diet.query.filter_by(user_id=user.id, date=day).first()
    if not fin:
        fin = Diet(user_id=user.id, date=day)
    fin.breakfast, fin.lunch, fin.dinner, fin.snack = sd.breakfast, sd.lunch, sd.dinner, sd.snack
    fin.total_kcal, fin.protein, fin.fat, fin.carbs = sd.total_kcal, sd.protein, sd.fat, sd.carbs
    db.session.add(fin)
    db.session.delete(sd)
    return True

def _send_tg(token: str, chat_id: str | int, text: str, url_button: str | None = None):
    import requests
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if url_button:
        body["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть диету", "url": url_button}]]}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=12)
        return r.ok
    except Exception:
        return False


# ==============================
#   ДЖОБЫ
# ==============================

def _job_stage_generate():
    """05:00 — запускаем ЧАНКОВУЮ реальную GPT-генерацию в staged_diet для всех подписчиков."""
    today = _today_local()
    users = _active_subscribers()
    print(f"[diet_autogen] stage: {len(users)} active subscribers for {today}")
    if not users:
        return

    batch_size = int(os.getenv("DIET_AUTOGEN_BATCH", "20"))
    pause_sec = int(os.getenv("DIET_AUTOGEN_PAUSE_SEC", "2"))

    for i in range(0, len(users), batch_size):
        chunk = users[i:i+batch_size]
        print(f"[diet_autogen] stage: processing users {i+1}-{i+len(chunk)} / {len(users)}")
        for u in chunk:
            try:
                pref = _ensure_preferences(u)
                data = _generate_diet_with_gpt(u, pref, today)
                _upsert_staged(u, data, today)
                db.session.commit()
                print(f"[diet_autogen] stage: OK user_id={u.id}")
            except Exception as e:
                db.session.rollback()
                print(f"[diet_autogen] stage: FAIL user_id={u.id} error={e}")
        if i + batch_size < len(users):
            time.sleep(pause_sec)


def _job_finalize_and_notify():
    """06:00 — переносим из staged в diet и рассылаем уведомления в Telegram."""
    app = current_app._get_current_object()
    token = app.config.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    base_url = (app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")

    today = _today_local()
    users = _active_subscribers()
    if not users:
        return

    for u in users:
        changed = False
        try:
            changed = _promote_staged_to_final(u, today)
            db.session.commit()
        except Exception:
            db.session.rollback()
            changed = False

        if not changed:
            continue

        # уважаем пользовательские настройки уведомлений
        can_notify = True
        s: UserSettings | None = getattr(u, "settings", None)
        if s:
            can_notify = s.telegram_notify_enabled
        else:
            can_notify = u.telegram_notify_enabled

        if token and can_notify and u.telegram_chat_id:
            # Соберём текст точно как в /generate_diet
            try:
                fin = Diet.query.filter_by(user_id=u.id, date=today).first()
                if not fin:
                    raise RuntimeError("no final diet")

                b = json.loads(fin.breakfast or "[]")
                l = json.loads(fin.lunch or "[]")
                d = json.loads(fin.dinner or "[]")
                s_ = json.loads(fin.snack or "[]")

                def fmt(title, items):
                    lines = [f"🍱 {title}:"]
                    for it in items:
                        name  = it.get("name","Блюдо")
                        grams = it.get("grams",0)
                        kcal  = it.get("kcal",0)
                        lines.append(f"- {name} ({grams} г, {kcal} ккал)")
                    return "\n".join(lines)

                msg = "🍽️ Ваша диета на сегодня:\n\n"
                msg += fmt("Завтрак", b) + "\n\n"
                msg += fmt("Обед",     l) + "\n\n"
                msg += fmt("Ужин",     d) + "\n\n"
                msg += fmt("Перекус",  s_) + "\n\n"
                if fin.total_kcal is not None: msg += f"🔥 Калории: {int(fin.total_kcal)} ккал\n"
                if fin.protein    is not None: msg += f"🍗 Белки: {float(fin.protein)} г\n"
                if fin.fat        is not None: msg += f"🥑 Жиры: {float(fin.fat)} г\n"
                if fin.carbs      is not None: msg += f"🥔 Углеводы: {float(fin.carbs)} г"

                link = f"{base_url}/profile" if base_url else None
                _send_tg(token, u.telegram_chat_id, msg, link)
            except Exception:
                # если вдруг парсинг не удался — шлём короткое уведомление
                link = f"{base_url}/profile" if base_url else None
                _send_tg(token, u.telegram_chat_id, "🥗 Ваша диета на сегодня готова.", link)



# ==============================
#   СТАРТ СКЕДУЛЕРА
# ==============================

def start_diet_autogen_scheduler(app):
    global _SCHED
    if _SCHED:
        return _SCHED

    _SCHED = BackgroundScheduler(timezone="Asia/Almaty")

    def _stage():
        with app.app_context():
            print(f"[diet_autogen] stage START {datetime.now(ALMATY).isoformat(timespec='seconds')}")
            _job_stage_generate()
            print(f"[diet_autogen] stage DONE  {datetime.now(ALMATY).isoformat(timespec='seconds')}")

    def _finalize():
        with app.app_context():
            print(f"[diet_autogen] finalize START {datetime.now(ALMATY).isoformat(timespec='seconds')}")
            _job_finalize_and_notify()
            print(f"[diet_autogen] finalize DONE  {datetime.now(ALMATY).isoformat(timespec='seconds')}")

    # 05:00 — генерация
    _SCHED.add_job(_stage, 'cron', hour=5, minute=00, id='diet-autogen-stage')
    # 06:00 — промоут + уведомление
    _SCHED.add_job(_finalize, 'cron', hour=6, minute=00, id='diet-autogen-finalize')

    print("[diet_autogen] cron jobs registered: 05:00 stage, 06:00 finalize (Asia/Almaty)")
    _SCHED.start()
    print("[diet_autogen] BackgroundScheduler started")
    return _SCHED

