import os
import time
from typing import Tuple, Dict
import uuid

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Модель оставляем, как есть
MODEL_NAME = "gemini-3-pro-image-preview"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    height = metrics.get("height")
    weight = metrics.get("weight")
    fat_pct = metrics.get("fat_pct")
    muscle_pct = metrics.get("muscle_pct")

    if sex == 'female':
        clothing = "Plain black sports bra and shorts"
    else:
        clothing = "Plain black athletic shorts, shirtless"

    return f"""
    Generate a hyper-realistic, high-fidelity studio photograph of the person provided in the reference image, but modify their body composition according to the metrics below.

    CRITICAL INSTRUCTIONS:
    1. **FACE IDENTITY:** You MUST PRESERVE the face of the person from the input image exactly. It should look like the same person 1-in-1.
    2. **BODY METRICS:**
       - Height: {height}cm
       - Weight: {weight}kg
       - Body Fat: {fat_pct}% (Visual appearance: {'defined abs, vascularity' if fat_pct < 12 else 'soft outlines, no definition'}).
       - Muscle Mass: {muscle_pct}% (Visual appearance: {'muscular, broad' if muscle_pct > 40 else 'average build'}).
    3. **CLOTHING:** {clothing}.
    4. **SETTING:** Pure white studio background, professional lighting, 8k resolution, raw photo style.
    5. **POSE:** Standing straight, full body visible (head to toe).

    Output ONLY the generated image.
    """


def _extract_image_from_content(response) -> bytes:
    """
    Извлекает байты изображения из ответа generate_content.
    """
    if not response or not response.candidates:
        raise RuntimeError("No candidates returned by Gemini.")

    # Ищем часть с картинкой
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data

    # Если модель вернула текст (например, отказ)
    text_content = response.text if response.text else "No text explanation"
    raise RuntimeError(f"No image generated. Model response: {text_content}")


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

    # 1. Расчет метрик
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    tgt_weight = metrics_target.get("weight_kg", 0)
    metrics_target_prepared = {
        "height": metrics_target.get("height_cm"),
        "weight": tgt_weight,
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # Конфигурация: просим вернуть только картинку
    gen_config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        temperature=0.4
    )

    # --- Генерация Current ---
    print("Generating Current with Face Identity...")
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current")

    # ИСПРАВЛЕНИЕ ЗДЕСЬ: Создаем объекты Part напрямую через конструктор
    contents_curr = [
        types.Part(text=prompt_curr),
        types.Part(
            inline_data=types.Blob(
                mime_type="image/jpeg",
                data=avatar_bytes
            )
        )
    ]

    try:
        resp_curr = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents_curr,
            config=gen_config
        )
        curr_png = _extract_image_from_content(resp_curr)
    except Exception as e:
        print(f"Error generating current: {e}")
        raise

    # --- Генерация Target ---
    print("Generating Target with Face Identity...")
    prompt_tgt = _build_prompt(user.sex or "male", metrics_target_prepared, "target")

    contents_tgt = [
        types.Part(text=prompt_tgt),
        types.Part(
            inline_data=types.Blob(
                mime_type="image/jpeg",
                data=avatar_bytes
            )
        )
    ]

    try:
        resp_tgt = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents_tgt,
            config=gen_config
        )
        tgt_png = _extract_image_from_content(resp_tgt)
    except Exception as e:
        print(f"Error generating target: {e}")
        raise

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
        provider="gemini-3-pro"
    )
    db.session.add(vis)
    db.session.commit()
    return vis