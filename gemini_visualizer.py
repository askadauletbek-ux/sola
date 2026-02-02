import os
import time
import uuid
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# --- ИСПОЛЬЗУЕМ ТОПОВУЮ МОДЕЛЬ ИЗ ВАШЕГО СПИСКА (IMAGEN 4) ---
MODEL_NAME = "imagen-4.0-generate-001"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    """
    Промпт адаптирован для Imagen 4.
    Эта модель отлично понимает естественный язык и детали композиции.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 40)

    # 1. Описание телосложения
    body_features = []

    # Жировая прослойка
    if fat_pct < 10:
        body_features.append("extremely shredded, vascular, thin skin, visible muscle striations")
    elif fat_pct < 15:
        body_features.append("very lean, defined six-pack abs, athletic cut")
    elif fat_pct < 22:
        body_features.append("fit, flat stomach, healthy muscle definition")
    elif fat_pct < 30:
        body_features.append("soft body composition, smooth contours, average build")
    else:
        body_features.append("heavy build, rounded contours, visible soft tissue")

    # Мышечная масса
    if muscle_pct > 42:
        body_features.append("heavy musculature, bodybuilder physique, broad shoulders")
    elif muscle_pct > 36:
        body_features.append("athletic build, well-developed muscles")
    else:
        body_features.append("average musculature, not bulky")

    body_desc_str = ", ".join(body_features)

    # 2. Одежда
    if sex == 'female':
        clothing = "wearing a black minimalist sports bra and black leggings"
    else:
        clothing = "wearing black athletic shorts, shirtless, bare torso"

    # 3. Сборка промпта
    # Imagen 4 хорошо понимает инструкции по кадрированию.
    return f"""
Full-body studio photograph of a {sex}, height {height}cm, weight {weight}kg.
Physique details: {body_desc_str}.
Clothing: {clothing}.
Pose: Standing in a neutral anatomical A-pose, arms relaxed at sides, looking at camera.
Background: Plain white studio background.

COMPOSITION REQUIRED:
- Wide shot.
- Full body visible from head to toe.
- Feet and shoes MUST be visible.
- Leave empty white space above the head.
- Do NOT crop the head or feet.

Style: 8k, hyper-realistic, highly detailed skin texture, professional studio lighting.
""".strip()


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

    # --- Подготовка метрик ---
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    tgt_data_for_prompt = {
        "height": metrics_target.get("height_cm"),
        "weight": metrics_target.get("weight_kg"),
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # --- Конфигурация Генерации ---
    # aspect_ratio="9:16" - Важно для фото в полный рост
    # УБРАН include_rai_reasoning, чтобы избежать ошибки валидации
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        output_mime_type="image/png"
    )

    # --- Генерация Current ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current")
    try:
        response_curr = client.models.generate_images(
            model=MODEL_NAME,
            prompt=prompt_curr,
            config=config
        )
        if not response_curr.generated_images:
            raise RuntimeError("Imagen returned no images for Current state.")
        curr_png = response_curr.generated_images[0].image.image_bytes
    except Exception as e:
        # Логируем ошибку с именем модели
        raise RuntimeError(f"Error generating Current image ({MODEL_NAME}): {str(e)}")

    # --- Генерация Target ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target")
    try:
        response_tgt = client.models.generate_images(
            model=MODEL_NAME,
            prompt=prompt_tgt,
            config=config
        )
        if not response_tgt.generated_images:
            raise RuntimeError("Imagen returned no images for Target state.")
        tgt_png = response_tgt.generated_images[0].image.image_bytes
    except Exception as e:
        raise RuntimeError(f"Error generating Target image ({MODEL_NAME}): {str(e)}")

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
        provider="imagen-4"
    )
    db.session.add(vis)
    db.session.commit()
    return vis