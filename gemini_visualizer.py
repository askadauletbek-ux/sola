import os
import time
import uuid
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# ИСПОЛЬЗУЕМ IMAGEN 3 ДЛЯ ГЕНЕРАЦИИ (High Fidelity)
# Убедитесь, что 'imagen-3.0-generate-001' включена в Google Cloud Console
MODEL_NAME = "imagen-3.0-generate-001"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    """
    Создает промпт для Imagen 3.
    Переводит численные метрики в визуальные описания и задает жесткий фрейминг.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 40)

    # --- 1. Перевод цифр в визуальные дескрипторы ---
    body_features = []

    # Жировая прослойка (Fat %)
    if fat_pct < 10:
        body_features.append("extremely shredded, visible vascularity, striated muscle, thin skin")
    elif fat_pct < 15:
        body_features.append("very lean, visible six-pack abs, sharp muscle separation")
    elif fat_pct < 20:
        body_features.append("athletic fit, flat stomach, visible muscle tone")
    elif fat_pct < 28:
        body_features.append("average build, healthy weight, smooth contours, soft definition")
    elif fat_pct < 35:
        body_features.append("soft body composition, rounded contours, visible subcutaneous fat, 'dad bod' or 'curvy'")
    else:
        body_features.append("heavy build, significant soft tissue, rounded abdomen")

    # Мышечная масса (Muscle %)
    if muscle_pct > 45:
        body_features.append("massive hypertrophied muscles, bodybuilder physique, wide shoulders, thick neck")
    elif muscle_pct > 38:
        body_features.append("strong athletic build, developed chest and arms, gym-goer physique")
    else:
        body_features.append("average musculature, slender frame, not bulky")

    body_desc_str = ", ".join(body_features)

    # --- 2. Одежда ---
    if sex == 'female':
        clothing = "wearing a simple black minimalist sports bra and tight black leggings"
    else:
        # Для мужчин: шорты и голый торс для лучшей видимости прогресса
        clothing = "wearing plain black athletic shorts, shirtless, bare torso"

    # --- 3. Сборка промпта с "Якорями" композиции ---
    # Фразы "Wide shot", "Show shoes" и "Space above head" критичны для одинакового зума
    return f"""
Full-body studio photograph of a {sex}, height {height}cm, weight {weight}kg.
Physique description: {body_desc_str}.
Clothing: {clothing}.
Pose: Standing in a neutral anatomical A-pose, arms relaxed at sides, looking directly at camera.
Background: Plain white studio background.

IMPORTANT COMPOSITION RULES:
- Wide angle lens full shot.
- The image MUST show the entire body from the top of the head to the bottom of the shoes.
- Visible feet and shoes are required.
- Include empty white space above the head and below the feet.
- Do NOT crop the head. Do NOT crop the feet.
- Camera at waist height, looking straight on.

Style: 8k resolution, photorealistic, cinematic lighting, highly detailed skin texture, raw photo style, unedited.
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
    """
    Генерирует изображения До и После используя Imagen 3.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())

    # --- 1. Подготовка метрик ---

    # Current
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    # Target
    tgt_data_for_prompt = {
        "height": metrics_target.get("height_cm"),
        "weight": metrics_target.get("weight_kg"),
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # --- 2. Конфигурация Генерации ---
    # aspect_ratio="9:16" - ГЛАВНОЕ исправление для "прыгающего" зума.
    # safety_filter_level можно настроить, если модель блокирует голый торс (зависит от настроек аккаунта)
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        output_mime_type="image/png",
        include_rai_reasoning=True
    )

    # --- 3. Генерация CURRENT ---
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
        # Логируем ошибку, чтобы понять, если промпт заблокирован safety-фильтрами
        raise RuntimeError(f"Error generating Current image: {str(e)}")

    # --- 4. Генерация TARGET ---
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
        raise RuntimeError(f"Error generating Target image: {str(e)}")

    # --- 5. Сохранение ---
    curr_filename = _save_png_to_db(curr_png, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_png, user.id, f"{ts}_target")

    return curr_filename, tgt_filename


def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, float],
                  metrics_target: Dict[str, float]):
    """
    Создает запись в базе данных о проведенной визуализации.
    """
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="imagen-3"
    )
    db.session.add(vis)
    db.session.commit()
    return vis