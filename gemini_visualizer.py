import os
import time
import uuid
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Используем Imagen 4 (или 3, если 4 недоступна) для лучшего понимания промптов
MODEL_NAME = "imagen-4.0-generate-001"
# Вспомогательная модель для анализа лица
VISION_MODEL_NAME = "gemini-2.0-flash"


def _analyze_face_from_avatar(client, avatar_bytes: bytes) -> str:
    """
    Создает точное текстовое описание лица на основе фото.
    Это позволяет сохранить узнаваемость без использования FaceSwap.
    """
    try:
        # Промпт для vision-модели: опиши только лицо фактами
        prompt = """
        Describe the face of the person in this image efficiently for a character prompt.
        Focus on:
        1. Ethnicity and skin tone.
        2. Exact hair style and color.
        3. Facial hair (beard/mustache) details.
        4. Age approximation.
        5. Distinctive facial features (shape, eyes).
        Output format: "A [ethnicity] man, [age] years old, with [hair] and [beard], [skin tone] skin."
        Do NOT describe clothing or body.
        """
        response = client.models.generate_content(
            model=VISION_MODEL_NAME,
            contents=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
            ]
        )
        return response.text.strip() if response.text else "A realistic man"
    except Exception as e:
        print(f"Error analyzing face: {e}")
        return "A realistic man"


def _get_body_description(fat_pct: float, muscle_pct: float) -> str:
    """
    Возвращает честное описание тела на основе процента жира.
    Без прикрас и "героических" пропорций.
    """
    # 1. Описание жировой прослойки (СУХИЕ ФАКТЫ)
    if fat_pct < 10:
        fat_desc = "extremely low body fat, visible veins, shredded muscle definition, thin skin"
    elif fat_pct < 15:
        fat_desc = "athletic lean build, visible abs, defined muscles"
    elif fat_pct < 20:
        fat_desc = "fit build, flat stomach, healthy weight, slight muscle definition"
    elif fat_pct < 25:
        # 20-25%: Обычный мужчина, не толстый, но и не атлет
        fat_desc = "average physique, soft midsection, no visible abs, normal build"
    elif fat_pct < 30:
        # 25-30%: (Твой случай 27кг/92кг = ~29%). Появляется живот.
        fat_desc = "overweight physique, soft body, visible belly protrusion, love handles, soft arms, carrying extra weight"
    elif fat_pct < 35:
        fat_desc = "heavyset physique, large belly, thick waist, high body fat, round torso"
    else:
        fat_desc = "obese build, significant excess weight, very round body shape"

    # 2. Описание мышц (Корректируем в зависимости от жира)
    # Если жира много (>25%), мышцы под ним не видны, даже если их много.
    if fat_pct > 25:
        muscle_desc = "muscles hidden by fat"
    elif muscle_pct > 42:
        muscle_desc = "heavy muscle mass, broad build"
    elif muscle_pct > 38:
        muscle_desc = "athletic musculature"
    else:
        muscle_desc = "average muscle mass"

    return f"{fat_desc}, {muscle_desc}"


def _build_prompt(sex: str, metrics: Dict[str, float], face_description: str) -> str:
    """
    Собирает промпт, ориентированный на реализм.
    """
    height = metrics.get("height", 175)
    weight = metrics.get("weight", 80)
    fat_pct = metrics.get("fat_pct", 25)
    muscle_pct = metrics.get("muscle_pct", 40)

    # Получаем текстовое описание тела
    body_visuals = _get_body_description(fat_pct, muscle_pct)

    # Одежда: для честного прогресса лучше всего простые шорты
    clothing = "plain black boxer briefs" if sex == 'male' else "black sports bra and panties"

    # Промпт:
    # 1. Лицо (из аватара)
    # 2. Тело (из метрик)
    # 3. Стиль (реализм, документальное фото)
    return f"""
Full-body raw photo of {face_description}.
Height: {height}cm, Weight: {weight}kg.
BODY COMPOSITION: {body_visuals}.
Clothing: {clothing}, shirtless.
Pose: Standing neutral A-pose, arms at sides, facing camera.
Background: Neutral white studio wall.

STYLE:
- Documentary body reference photography.
- Realistic skin texture, natural lighting.
- NOT an artistic render, NOT a fitness model photoshoot.
- Accurate representation of body fat and shape described above.
- Full body visible from head to toe.
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

    # 1. Анализируем лицо с аватара (Один раз для обоих фото)
    # Это гарантирует, что на обоих фото будет один и тот же человек
    face_description = _analyze_face_from_avatar(client, avatar_bytes)

    # 2. Подготовка метрик CURRENT
    curr_weight = metrics_current.get("weight", 0)
    metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), curr_weight)

    # Если мышечная масса не передана, считаем грубо
    curr_muscle = metrics_current.get("muscle_mass") or (curr_weight * 0.4)
    metrics_current["muscle_pct"] = _compute_pct(curr_muscle, curr_weight)

    # 3. Подготовка метрик TARGET
    # (Бэк может прислать разные ключи, унифицируем)
    tgt_weight = metrics_target.get("weight_kg") or metrics_target.get("weight")
    tgt_fat_mass = metrics_target.get("fat_mass")

    # Если fat_pct нет, считаем его из массы
    tgt_fat_pct = metrics_target.get("fat_pct")
    if tgt_fat_pct is None and tgt_weight and tgt_fat_mass:
        tgt_fat_pct = _compute_pct(tgt_fat_mass, tgt_weight)

    tgt_data_processed = {
        "height": metrics_target.get("height_cm") or metrics_target.get("height"),
        "weight": tgt_weight,
        "fat_pct": tgt_fat_pct,
        "muscle_pct": metrics_target.get("muscle_pct")
    }

    # 4. Конфигурация
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        output_mime_type="image/png"
    )

    # --- ГЕНЕРАЦИЯ CURRENT ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, face_description)
    try:
        response_curr = client.models.generate_images(
            model=MODEL_NAME,
            prompt=prompt_curr,
            config=config
        )
        if not response_curr.generated_images:
            raise RuntimeError("No image for Current state.")
        curr_png = response_curr.generated_images[0].image.image_bytes
    except Exception as e:
        raise RuntimeError(f"Error generating Current image: {str(e)}")

    # --- ГЕНЕРАЦИЯ TARGET ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_processed, face_description)
    try:
        response_tgt = client.models.generate_images(
            model=MODEL_NAME,
            prompt=prompt_tgt,
            config=config
        )
        if not response_tgt.generated_images:
            raise RuntimeError("No image for Target state.")
        tgt_png = response_tgt.generated_images[0].image.image_bytes
    except Exception as e:
        raise RuntimeError(f"Error generating Target image: {str(e)}")

    # 5. Сохранение
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
        provider="imagen-face-aware"
    )
    db.session.add(vis)
    db.session.commit()
    return vis