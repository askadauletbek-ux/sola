import os
import io
import time
import uuid
from typing import Tuple, Dict, Optional

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Используем специализированную модель для изображений
# Проверьте доступность 'imagen-3.0-generate-001' в вашем списке моделей
MODEL_NAME = "imagen-3.0-generate-001"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    """
    Промпт оптимизирован для Imagen 3:
    1. Убраны лишние технические данные (Scene ID), которые модель игнорирует.
    2. Добавлены жесткие требования к композиции (Framing).
    3. Добавлен блок описания телосложения на основе метрик.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 40)

    # Динамическое описание тела для визуальной точности
    body_desc = []
    if fat_pct < 12:
        body_desc.append("extremely defined abs, visible vascularity, thin skin")
    elif fat_pct < 20:
        body_desc.append("athletic tone, visible muscle separation, flat stomach")
    elif fat_pct < 28:
        body_desc.append("soft definition, healthy weight, smooth contours")
    else:
        body_desc.append("softer body composition, rounded contours, visible subcutaneous fat")

    if muscle_pct > 45:
        body_desc.append("hypertrophied muscles, broad shoulders, powerful build")
    elif muscle_pct > 35:
        body_desc.append("athletic build, firm musculature")
    else:
        body_desc.append("slender build, low muscle mass")

    visual_body_text = ", ".join(body_desc)

    # Одежда
    if sex == 'female':
        clothing = "wearing black minimalist sports bra and tight black leggings"
    else:
        clothing = "wearing black athletic shorts, shirtless"

    return f"""
photorealistic full-body shot of a {sex}, {height}cm tall, weighing {weight}kg.
Body composition details: {visual_body_text}.
The subject is {clothing}.
Standing in a neutral A-pose, looking directly at the camera.
Background: clean white studio background.

IMPORTANT COMPOSITION RULES:
- Wide angle lens shot.
- Full body visible from head to toe. 
- MUST show shoes/feet at the bottom.
- MUST show empty space above the head.
- Do not crop the head or feet.
- Maintain a consistent distance from the camera.

Style: Raw 8k photograph, cinematic lighting, sharp focus, highly detailed skin texture.
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

    # --- Подготовка данных ---
    curr_weight = metrics_current.get("weight", 0)
    # Расчет процентов для Current
    metrics_current_processed = {
        "height": metrics_current.get("height"),
        "weight": curr_weight,
        "fat_pct": _compute_pct(metrics_current.get("fat_mass", 0), curr_weight),
        "muscle_pct": _compute_pct(metrics_current.get("muscle_mass", 0) or (curr_weight * 0.4), curr_weight)
    }

    # Расчет процентов для Target (данные приходят уже готовые или требуют маппинга)
    tgt_data_processed = {
        "height": metrics_target.get("height_cm"),
        "weight": metrics_target.get("weight_kg"),
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # Фиксируем seed, чтобы композиция кадров "До" и "После" была максимально похожей
    # (Imagen поддерживает seed, если нет - параметр будет проигнорирован, но это best practice)
    # Примечание: В текущем SDK config может отличаться, проверяйте актуальную доку Imagen.
    common_seed = int(time.time())

    # Конфигурация генерации (Imagen 3 Specific)
    # aspect_ratio="9:16" критически важен для фото в полный рост
    # safety_filter_level="block_only_high" позволяет генерировать shirtless мужчин/женщин в топах без цензуры
    generate_config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        include_rai_reasoning=True,
        output_mime_type="image/png"
        # seed=common_seed # Раскомментировать, если поддерживается версией API
    )

    # --- Генерация CURRENT ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current_processed, "current")

    # Imagen 3 обычно не принимает reference image в prompt так же легко, как Gemini.
    # Если модель поддерживает редактирование, endpoint будет другой (imagen-3.0-capability-editing).
    # Для генерации с нуля используем prompt:

    # Вариант А: Чистая генерация по тексту (более стабильный результат композиции)
    response_curr = client.models.generate_images(
        model=MODEL_NAME,
        prompt=prompt_curr,
        config=generate_config
    )

    if not response_curr.generated_images:
        raise RuntimeError("Failed to generate Current image")

    curr_image_bytes = response_curr.generated_images[0].image.image_bytes

    # --- Генерация TARGET ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_processed, "target")

    response_tgt = client.models.generate_images(
        model=MODEL_NAME,
        prompt=prompt_tgt,
        config=generate_config
    )

    if not response_tgt.generated_images:
        raise RuntimeError("Failed to generate Target image")

    tgt_image_bytes = response_tgt.generated_images[0].image.image_bytes

    # --- Сохранение ---
    curr_filename = _save_png_to_db(curr_image_bytes, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_image_bytes, user.id, f"{ts}_target")

    return curr_filename, tgt_filename