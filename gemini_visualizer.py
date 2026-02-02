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

# Модель оставляем ту же, как вы просили
MODEL_NAME = "gemini-3-pro-image-preview"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str, scene_id: str) -> str:
    """
    Финальная версия промпта (без изменений).
    """
    height = metrics.get("height")
    weight = metrics.get("weight")
    fat_pct = metrics.get("fat_pct")
    muscle_pct = metrics.get("muscle_pct")

    if sex == 'female':
        clothing_description = "Plain black sports bra (top) and plain black athletic shorts. Simple, functional, no logos, no embellishments. Matte fabric."
    else:  # male
        clothing_description = "Plain black athletic shorts, bare torso. Simple, functional, no logos, no embellishments. Matte fabric."

    photo_style = "Hyper-realistic, clinical, high-fidelity studio photograph. Captured with a professional medium-format camera (e.g., Hasselblad H6D-100c) and a prime 120mm macro lens set to f/11 for maximum depth of field and sharpness across the entire body. RAW photo, unedited, unprocessed."

    return f"""
# PRIMARY OBJECTIVE: ABSOLUTE PHOTOREALISM & SCIENTIFIC DATA ACCURACY
The output MUST be a physically accurate, hyper-realistic photographic representation of the human body strictly based on the provided metrics. 

# SCENE SETUP
- **Scene ID:** {scene_id}
- **Style:** {photo_style}
- **Background:** Pure white (#FFFFFF) seamless studio cyclorama.
- **Camera:** Static, eye-level, full-height shot. 120mm focal length.
- **Lighting:** Ultra-flat, high-key diffused studio lighting. No shadows.

# COMPOSITION
- **Visibility:** The ENTIRE subject (head to feet) MUST be fully visible.
- **Margins:** 5-10% white margin above head and below feet.
- **NO CROPPING:** Do not crop hair, fingers, or toes.

# SUBJECT & IDENTITY
- **Pose:** Strict anatomical A-pose.
- **Identity:** The face MUST be distinct and realistic.
- **Clothing:** {clothing_description}

# BODY SPECIFICATION FOR "{variant_label}"
- **sex:** {sex}
- **height_cm:** {height}
- **weight_kg:** {weight}
- **fat_percent:** {fat_pct}% (LITERAL translation to subcutaneous fat. High = softer contours, Low = tight skin).
- **muscle_percent:** {muscle_pct}% (LITERAL translation to muscle volume and definition).

# REALISM DIRECTIVES
- **Skin Texture:** Microscopic detail: pores, natural variations, no airbrushing.
- **Gravity:** Realistic effects on soft tissues (fat) and muscles.
- **Proportions:** Anthropometrically correct for the specified height and sex.

The final output must be an authentic, unedited, high-resolution photograph.
""".strip()


def _extract_first_image_bytes(response) -> bytes:
    """
    Обновлено для обработки ответа от generate_images.
    """
    if not response:
        raise RuntimeError("No response returned by Gemini model.")

    # Для метода generate_images структура ответа отличается от generate_content
    # Обычно это response.generated_images[0].image.image_bytes
    if hasattr(response, "generated_images") and response.generated_images:
        first_img = response.generated_images[0]
        if hasattr(first_img, "image") and first_img.image:
            return first_img.image.image_bytes

    # На случай если вернется старый формат (хотя для Imagen это вряд ли)
    if hasattr(response, "candidates"):
        raise RuntimeError(
            "Received 'candidates' structure instead of 'generated_images'. Ensure you are using client.models.generate_images.")

    raise RuntimeError("No image data found in response.")


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
    Генерирует изображения До и После используя client.models.generate_images
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())
    scene_id = f"scene-{uuid.uuid4().hex}"

    # 1. Подготовка ТЕКУЩИХ метрик
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    # 2. Подготовка ЦЕЛЕВЫХ метрик
    tgt_weight = metrics_target.get("weight_kg", 0)
    tgt_data_for_prompt = {
        "height": metrics_target.get("height_cm"),
        "weight": tgt_weight,
        "fat_pct": metrics_target.get("fat_pct"),
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # Конфигурация для генерации (опционально, можно добавить aspect_ratio и т.д.)
    # person_generation="allow_adult" или другие фильтры могут понадобиться в зависимости от прав доступа
    gen_config = types.GenerateImagesConfig(
        number_of_images=1,
        include_rai_reason=True,
        output_mime_type="image/png"
    )

    # --- Генерация текущего состояния ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current", scene_id)

    # ВНИМАНИЕ: generate_images принимает строку prompt.
    # Передача картинки-референса (avatar_bytes) напрямую в промпт НЕ поддерживается в T2I эндпоинте Imagen.
    # Если вы хотите сохранить лицо, вам нужно использовать Face LoRA (если доступно) или просто детальное описание лица.
    # Для исправления ошибки 404 мы убираем avatar_bytes из запроса.

    print(f"Generating Current with prompt len: {len(prompt_curr)}")
    resp_curr = client.models.generate_images(
        model=MODEL_NAME,
        prompt=prompt_curr,
        config=gen_config
    )
    curr_png = _extract_first_image_bytes(resp_curr)

    # --- Генерация целевого состояния ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target", scene_id)

    print(f"Generating Target with prompt len: {len(prompt_tgt)}")
    resp_tgt = client.models.generate_images(
        model=MODEL_NAME,
        prompt=prompt_tgt,
        config=gen_config
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