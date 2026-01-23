import os
import io
import time
from datetime import datetime
from typing import Tuple, Dict
import uuid

from PIL import Image
from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Убедитесь, что эта модель доступна (например, gemini-1.5-flash или gemini-2.0-flash-exp)
# Если 2.5 недоступна, используйте "gemini-1.5-flash"
MODEL_NAME = "gemini-2.0-flash-exp"

def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    """
    Промпт сфокусирован на сохранении исходного изображения (фон, одежда, поза)
    и изменении только морфологии тела под заданные метрики.
    """
    height = metrics.get("height")
    weight = metrics.get("weight")
    fat_pct = metrics.get("fat_pct")
    muscle_pct = metrics.get("muscle_pct")

    # Формируем описание задачи для модели
    # Мы убрали жесткое описание одежды и фона, заменив его на инструкции по сохранению.
    return f"""
# SYSTEM ROLE: EXPERT ANATOMICAL PHOTO EDITOR
Your task is to realistically modify the physique of the person in the provided input image to match specific biometric data, while STRICTLY PRESERVING the original context.

# PRESERVATION DIRECTIVES (DO NOT CHANGE)
1. **Background & Lighting:** Keep the background, shadows, and lighting EXACTLY as they are in the source image. DO NOT replace with a white studio background.
2. **Clothing:** Preserve the original clothing style, color, and texture. Only adjust the fit/drape of the fabric to realistically match the new body shape (e.g., tighter on muscles, looser if weight drops).
3. **Face & Identity:** The face must remain UNCHANGED.
4. **Pose:** Maintain the exact original pose.

# TARGET BODY CONFIGURATION ({variant_label.upper()})
- **Sex:** {sex}
- **Height:** {height} cm
- **Weight:** {weight} kg
- **Body Fat:** {fat_pct}% (Visual cue: {"Visible abs, vascularity, tight skin" if fat_pct < 15 else "Softer definition, smooth contours"}).
- **Muscle Mass:** {muscle_pct}% (Visual cue: {"Hypertrophy, dense muscle volume" if muscle_pct > 40 else "Average athletic tone"}).

# EXECUTION
Generate a photorealistic result that looks like the original photo was taken of the subject with this exact new body composition. No CGI artifacts.
""".strip()

def _extract_first_image_bytes(response) -> bytes:
    if not response or not getattr(response, "candidates", []):
        raise RuntimeError("No candidates returned by Gemini model.")

    for cand in response.candidates:
        if cand.content and cand.content.parts:
            for part in cand.content.parts:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data

    raise RuntimeError("No image data found in response parts.")

def _save_png_to_db(raw_bytes: bytes, user_id: int, base_name: str) -> str:
    unique_filename = f"viz_{user_id}_{base_name}_{uuid.uuid4().hex}.png"
    new_file = UploadedFile(
        filename=unique_filename,
        content_type='image/png',
        data=raw_bytes,
        size=len(raw_bytes),
        user_id=user_id
    )
    db.session.add(new_file)
    return unique_filename

def _compute_pct(value: float, weight: float) -> float:
    if not value or not weight or weight <= 0:
        return 0.0
    return round(100.0 * float(value) / float(weight), 2)

def generate_for_user(user, avatar_bytes: bytes, metrics_current: Dict[str, float], metrics_target: Dict[str, float]) -> Tuple[str, str]:
    """
    Генерирует изображения До и После, используя загруженное фото (avatar_bytes) как референс.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())

    # 1. Подготовка ТЕКУЩИХ метрик (Точка А)
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    # 2. Подготовка ЦЕЛЕВЫХ метрик (Точка Б)
    tgt_weight = metrics_target.get("weight_kg", 0)
    tgt_data_for_prompt = {
        "height": metrics_target.get("height_cm"),
        "weight": tgt_weight,
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # Генерация текущего состояния (реконструкция с уточненными цифрами)
    # Мы передаем avatar_bytes и просим применить текущие метрики (это может немного "подтянуть" фото под цифры)
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current")
    contents_curr = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_curr),
    ]
    # Используем config для контроля качества (если требуется)
    generate_config = types.GenerateContentConfig(
        temperature=0.4, # Понижаем температуру для большей точности следования референсу
        top_p=0.95,
        top_k=40,
        candidate_count=1
    )

    resp_curr = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents_curr,
        config=generate_config
    )
    curr_png = _extract_first_image_bytes(resp_curr)

    # Генерация целевого состояния
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target")
    contents_tgt = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)), # Используем оригинал как исходник
        types.Part(text=prompt_tgt),
    ]
    resp_tgt = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents_tgt,
        config=generate_config
    )
    tgt_png = _extract_first_image_bytes(resp_tgt)

    # Сохранение в БД
    curr_filename = _save_png_to_db(curr_png, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_png, user.id, f"{ts}_target")

    return curr_filename, tgt_filename

def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, float],
                  metrics_target: Dict[str, float]):
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="gemini"
    )
    db.session.add(vis)
    db.session.commit()
    return vis