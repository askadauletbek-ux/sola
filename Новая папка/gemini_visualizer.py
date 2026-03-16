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

# Убедитесь, что эта модель доступна в вашем регионе/аккаунте
MODEL_NAME = "gemini-2.5-flash-image"

def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str, scene_id: str) -> str:
    """
    Финальная версия промпта со строгим контролем масштаба, фона и ИМТ.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 40)
    bmi = metrics.get("bmi", 22.0)

    if sex == 'female':
        clothing_description = "Plain black sports bra (top) and plain black athletic shorts. Simple, functional, no logos, no embellishments. Matte fabric."
    else:  # male
        clothing_description = "Plain black athletic shorts, bare torso. Simple, functional, no logos, no embellishments. Matte fabric."

    return f"""
# PRIMARY OBJECTIVE
You are an expert clinical AI visualizer. Your task is to perform a highly accurate image-to-image translation. 
You must modify the body composition of the person in the provided reference image STRICTLY based on the physical metrics below.

# SCENE SETUP
- **Scene ID:** {scene_id}

# STRICT CONSISTENCY RULES (CRITICAL)
1. **Scale & Framing:** The subject MUST remain the EXACT SAME SIZE and at the EXACT SAME DISTANCE from the camera as in the reference image. DO NOT zoom in or out. Head and feet MUST remain exactly where they are in the original.
2. **Background & Environment:** PRESERVE the original background, room, shadows, and lighting completely. Do NOT generate a white studio background unless the original photo is already white. Modifying the background is strictly prohibited.
3. **Pose:** The pose MUST remain absolutely identical to the original image.
4. **Clothing:** {clothing_description}
5. **Identity:** The face MUST be an EXACT, UNALTERED match to the provided avatar image.

# BODY SPECIFICATION FOR "{variant_label.upper()}" STATE
- **Sex:** {sex}
- **Height:** {height} cm
- **Weight:** {weight} kg
- **BMI (Body Mass Index):** {bmi} (CRUCIAL: strictly reflect this BMI in the body volume and width)
- **Body Fat:** {fat_pct}% (Determines softness, curves, and subcutaneous fat visibility)
- **Muscle Mass:** {muscle_pct}% (Determines muscle volume and definition)

# REALISM & ANATOMY (DO NOT BEAUTIFY)
- This is a clinical visualization. DO NOT artificially beautify, slim down, or add unrealistic muscle definition unless the Body Fat % is very low.
- If BMI is high, accurately and realistically depict the excess body mass and volume.
- If this is the "CURRENT" state with high fat, it MUST look softer and heavier than the "TARGET" state.
- Skin Texture: Microscopic detail, pores, natural variations, no airbrushing.
- Gravity: Realistic effects on soft tissues (fat) and muscles.

Output MUST be an authentic, unedited, high-resolution synthesized photograph matching the input dimensions and framing perfectly.
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

def _compute_bmi(weight: float, height: float) -> float:
    """Рассчитывает ИМТ для передачи в промпт ИИ."""
    if not weight or not height or height <= 0:
        return 0.0
    return round(weight / ((height / 100.0) ** 2), 1)


def generate_for_user(user, avatar_bytes: bytes, metrics_current: Dict[str, float], metrics_target: Dict[str, float]) -> \
Tuple[str, str]:
    """
    Генерирует изображения До и После, нормализуя входные данные для промпта.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())
    scene_id = f"scene-{uuid.uuid4().hex}"

    # 1. Подготовка ТЕКУЩИХ метрик (Точка А)
    curr_height = metrics_current.get("height") or getattr(user, "height", 170)
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)

    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    # Считаем точный ИМТ для Точки А
    metrics_current["bmi"] = _compute_bmi(curr_weight, curr_height)

    # 2. Подготовка ЦЕЛЕВЫХ метрик (Точка Б)
    tgt_height = metrics_target.get("height_cm") or curr_height
    tgt_weight = metrics_target.get("weight_kg", 0)
    tgt_data_for_prompt = {
        "height": tgt_height,
        "weight": tgt_weight,
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct"),
        # Считаем точный ИМТ для Точки Б
        "bmi": _compute_bmi(tgt_weight, tgt_height)
    }

    # Жесткая консистентность генерации (минимум рандома)
    generation_config = types.GenerateContentConfig(
        temperature=0.0,
    )

    # Генерация текущего состояния
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current", scene_id)
    contents_curr = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_curr),
    ]
    resp_curr = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents_curr,
        config=generation_config
    )
    curr_png = _extract_first_image_bytes(resp_curr)

    # Генерация целевого состояния
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target", scene_id)
    contents_tgt = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_tgt),
    ]
    resp_tgt = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents_tgt,
        config=generation_config
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