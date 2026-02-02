import os
import time
import uuid
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Основная модель для генерации (Лучшее качество)
MODEL_NAME = "imagen-4.0-generate-001"
# Вспомогательная модель для считывания лица с аватарки (Быстрая)
VISION_MODEL_NAME = "gemini-2.0-flash"


def _analyze_face_from_avatar(client, avatar_bytes: bytes) -> str:
    """
    Imagen 4 не видит картинки, поэтому мы используем Gemini,
    чтобы описать лицо пользователя текстом и передать это описание в Imagen.
    """
    try:
        prompt = """
        Analyze the face in this image. Describe ONLY the physical facial features to recreate this person.
        Include: ethnicity, skin tone, exact hair style and color, facial hair (beard/mustache details), eye color, and apparent age.
        Be concise (max 30 words). Do NOT describe clothing or body.
        Example output: "Latino male, short buzz cut dark hair, thick stubble beard, tan skin, brown eyes, approx 30 years old."
        """
        response = client.models.generate_content(
            model=VISION_MODEL_NAME,
            contents=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
            ]
        )
        return response.text.strip() if response.text else "A realistic human face"
    except Exception as e:
        print(f"Error analyzing face: {e}")
        return "A realistic human face"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str, face_description: str) -> str:
    """
    Промпт с исправленной логикой жира и добавлением описания лица.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 40)

    # --- 1. ЖЕСТКАЯ ЛОГИКА ТЕЛОСЛОЖЕНИЯ (REALISM FIX) ---
    body_features = []

    # Жировая прослойка (Fat %)
    # Исправлено: 25-30% жира теперь реально выглядят как лишний вес, а не как мышцы.
    if fat_pct < 10:
        body_features.append("extremely shredded, visible veins, thin skin, striated muscle")
    elif fat_pct < 15:
        body_features.append("athletic lean, visible six-pack abs, sharp definition")
    elif fat_pct < 20:
        body_features.append("fit, flat stomach, faint muscle outline, no love handles")
    elif fat_pct < 25:
        body_features.append("average build, soft stomach, no visible abs, smooth skin")
    elif fat_pct < 30:
        # Твой случай (27.7кг жира): Мягкое тело, животик
        body_features.append(
            "overweight body type, soft belly, visible love handles, round face, soft arms, no muscle definition")
    elif fat_pct < 35:
        body_features.append("heavyset build, protruding belly, thick waist, soft body composition")
    else:
        body_features.append("obese build, significant body fat, very round shape")

    # Мышечная масса (Muscle %)
    # Если жира много (>25%), мы приглушаем описание мышц, чтобы не было конфликта
    if muscle_pct > 42 and fat_pct < 25:
        body_features.append("massive bodybuilder musculature, broad shoulders")
    elif muscle_pct > 36 and fat_pct < 30:
        body_features.append("broad frame, naturally strong build under fat")  # Мышцы под жиром
    elif muscle_pct > 36:
        body_features.append("athletic musculature")
    else:
        body_features.append("average musculature")

    body_desc_str = ", ".join(body_features)

    # --- 2. Одежда ---
    if sex == 'female':
        clothing = "wearing a black minimalist sports bra and black leggings"
    else:
        clothing = "wearing black athletic shorts, shirtless, bare torso"

    # --- 3. Сборка промпта ---
    return f"""
Full-body studio photograph of a {sex}, {face_description}.
Height: {height}cm, Weight: {weight}kg.
Body Condition: {body_desc_str}.
Clothing: {clothing}.
Pose: Standing in a neutral anatomical A-pose, arms relaxed at sides, looking at camera.
Background: Plain white studio background.

COMPOSITION RULES:
- Wide shot (Full Body).
- Head, feet, and shoes MUST be fully visible.
- Leave white space above the head.
- Do NOT crop.

Style: 8k, hyper-realistic, raw photograph, highly detailed skin texture.
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

    # 1. Считываем лицо с аватарки (Один раз для обоих фото)
    face_description = _analyze_face_from_avatar(client, avatar_bytes)
    # Можно добавить "same person as described" для усиления

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
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        output_mime_type="image/png"
    )

    # --- Генерация Current ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current", face_description)
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
        raise RuntimeError(f"Error generating Current image: {str(e)}")

    # --- Генерация Target ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target", face_description)
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
        provider="imagen-4-face-aware"
    )
    db.session.add(vis)
    db.session.commit()
    return vis