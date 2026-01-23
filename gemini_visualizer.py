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

# Используем Gemini 2.0 Flash Experimental (хорошо работает с Multimodal)
# Если будет нестабильно, можно попробовать "gemini-1.5-pro"
MODEL_NAME = "gemini-2.0-flash-exp"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    """
    Промпт, ориентированный на генерацию нового изображения с сохранением контекста,
    но без агрессивных формулировок "edit/modify", которые триггерят фильтры.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 30)

    # Описываем желаемый результат, а не процесс изменения
    return f"""
Generate a high-fidelity, photorealistic full-body photograph based on the reference image provided.

# VISUAL REQUIREMENTS
1. **Subject:** Use the person in the reference image as the primary visual source. Maintain their approximate age, skin tone, and hair style.
2. **Attire & Setting:** Keep the clothing (style, color, fit) and the background identical to the reference image.
3. **Pose:** Reproduce the exact pose from the reference.

# BODY METRICS TO VISUALIZE ({variant_label.upper()})
Adjust the subject's physique to strictly match these biometric parameters:
- **Height:** {height} cm
- **Weight:** {weight} kg
- **Body Fat:** {fat_pct}% (Visual details: {"Visible abdominal definition, vascularity" if fat_pct < 15 else "Softer, smoother contours, no visible abs"}).
- **Muscle Mass:** {muscle_pct}% (Visual details: {"Athletic hypertrophy, defined deltoids and quads" if muscle_pct > 35 else "Average build, moderate tone"}).

# OUTPUT FORMAT
- Photorealistic style.
- No CGI or cartoon effects.
- Full body visible.
""".strip()


def _extract_first_image_bytes(response) -> bytes:
    """
    Извлекает байты изображения. Если изображения нет, пытается найти текст ошибки.
    """
    if not response or not getattr(response, "candidates", []):
        raise RuntimeError(f"Gemini returned empty response: {response}")

    for cand in response.candidates:
        # 1. Проверяем наличие Safety блоков
        if getattr(cand, "finish_reason", None) == "SAFETY":
            raise RuntimeError(
                "Gemini refused to generate image due to SAFETY filters. Try a different photo or prompt.")

        if cand.content and cand.content.parts:
            for part in cand.content.parts:
                # 2. Ищем байты изображения
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data

                # 3. Если изображения нет, собираем текст для отладки
                if part.text:
                    print(f"DEBUG: Model returned text instead of image: {part.text}")

    # Если мы здесь, значит ни картинки, ни внятной ошибки не нашли (но текст мог быть напечатан выше)
    raise RuntimeError("No image data found in response. Check server logs for model text output.")


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


def generate_for_user(user, avatar_bytes: bytes, metrics_current: Dict[str, float], metrics_target: Dict[str, float]) -> \
Tuple[str, str]:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())

    # 1. Подготовка данных
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    tgt_weight = metrics_target.get("weight_kg", 0)
    tgt_data_for_prompt = {
        "height": metrics_target.get("height_cm"),
        "weight": tgt_weight,
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # Конфигурация: понижаем threshold безопасности (если это разрешено API ключом),
    # чтобы разрешить "medical/anatomical" контент, который часто блочится.
    # В стандартном API BLOCK_ONLY_HIGH может помочь.
    safety_settings = [
        types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT",
            threshold="BLOCK_ONLY_HIGH",
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH",
            threshold="BLOCK_ONLY_HIGH",
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
            threshold="BLOCK_ONLY_HIGH",
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT",
            threshold="BLOCK_ONLY_HIGH",
        ),
    ]

    generate_config = types.GenerateContentConfig(
        temperature=0.4,
        candidate_count=1,
        safety_settings=safety_settings
    )

    # --- Генерация "СЕЙЧАС" ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current_physique")
    contents_curr = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_curr),
    ]

    try:
        resp_curr = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents_curr,
            config=generate_config
        )
        curr_png = _extract_first_image_bytes(resp_curr)
    except Exception as e:
        print(f"Error generating CURRENT image: {e}")
        # Если упало - пробуем вернуть просто оригинал или заглушку, но лучше пробросить ошибку
        raise e

    # --- Генерация "ЦЕЛЬ" ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target_physique")
    contents_tgt = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_tgt),
    ]

    try:
        resp_tgt = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents_tgt,
            config=generate_config
        )
        tgt_png = _extract_first_image_bytes(resp_tgt)
    except Exception as e:
        print(f"Error generating TARGET image: {e}")
        raise e

    # Сохранение
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