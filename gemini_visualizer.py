import os
import time
import uuid
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Используем Imagen 4, так как она лучше всего понимает сложные инструкции.
# Но мы заставим её быть "честной", а не "художественной".
MODEL_NAME = "imagen-4.0-generate-001"
# Модель для "чтения" лица с аватарки
VISION_MODEL_NAME = "gemini-2.0-flash"


def _analyze_face_from_avatar(client, avatar_bytes: bytes) -> str:
    """
    Создает "Словесный портрет" пользователя.
    """
    try:
        # Просим описать только лицо, максимально точно
        prompt = """
        Describe the face of the person in this image for a police sketch or medical record.
        Focus on: exact skin tone, ethnicity, face shape, hair color/style/receding hairline, facial hair details, age.
        Do NOT describe expression or body. Be purely descriptive and factual.
        """
        response = client.models.generate_content(
            model=VISION_MODEL_NAME,
            contents=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
            ]
        )
        return response.text.strip() if response.text else "Male, average features"
    except Exception as e:
        print(f"Error analyzing face: {e}")
        return "Male, realistic face"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str, face_description: str) -> str:
    """
    Промпт в стиле "Medical/Documentary" для максимального реализма.
    """
    height = metrics.get("height", 170)
    weight = metrics.get("weight", 70)
    fat_pct = metrics.get("fat_pct", 20)
    muscle_pct = metrics.get("muscle_pct", 40)

    # --- ЛОГИКА ТЕЛОСЛОЖЕНИЯ (REALISM) ---
    body_details = ""

    # Жир (Fat %)
    if fat_pct < 12:
        body_details = "Bodybuilder definition. Extremely low body fat. Visible striations. Vascularity. Thin skin."
    elif fat_pct < 18:
        body_details = "Athletic build. Visible abs. Defined muscles. Taut skin."
    elif fat_pct < 24:
        body_details = "Average build. Healthy weight. Soft midsection covering muscles. No visible abs."
    elif fat_pct < 30:
        # Твой случай (27-30%): "Skinny Fat" или просто лишний вес
        body_details = "Overweight physique. Soft body composition. Visible belly protrusion (paunch). Love handles. Soft arms. No muscle definition. Smooth soft skin texture. Untrained look."
    elif fat_pct < 35:
        body_details = "Heavyset physique. Large protruding belly. Thick waist. Heavy soft limbs. High body fat percentage look."
    else:
        body_details = "Obese physique. Very round torso. Significant excess fat. Round soft features."

    # Мышцы (Muscle %)
    # Если жир высокий (>25%), мышцы скрыты. Прямо говорим модели НЕ рисовать их.
    if fat_pct > 25:
        muscle_instruction = "Musculature is completely hidden by subcutaneous fat. NOT muscular looking. Soft contours."
    elif muscle_pct > 40:
        muscle_instruction = "Hypertrophied underlying muscle mass."
    else:
        muscle_instruction = "Average muscle mass."

    # --- ОДЕЖДА ---
    # Для реализма лучше "домашний" стиль, а не "спортзал"
    if sex == 'female':
        clothing = "black basic underwear (bra and panties)"
    else:
        if fat_pct > 25:
            # Шорты под животом - маркер реализма
            clothing = "plain black boxer briefs, shirtless"
        else:
            clothing = "black athletic shorts, shirtless"

    # --- СБОРКА ПРОМПТА ---
    # Используем стиль "Clinical Body Reference" (Клинический референс тела)
    # Это переключает модель из режима "Красота" в режим "Анатомия"
    return f"""
Clinical full-body reference photo of a {sex}.
Face details: {face_description}.
Biometrics: Height {height}cm, Weight {weight}kg.

PHYSICAL CONDITION (Strictly adhere to this):
{body_details}
{muscle_instruction}

Clothing: {clothing}.
Pose: Standing neutral A-pose, arms at sides, facing forward.
Background: Neutral white medical wall.

PHOTOGRAPHY STYLE:
- Documentary style, raw, unflattering, realistic.
- NOT an artistic studio shoot. NOT a fitness magazine photo.
- Harsh realistic lighting.
- Focus on accurate representation of body volume and texture.
- Full body shot (Head to Shoes visible).
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

    # 1. Анализ лица (Один раз)
    face_desc = _analyze_face_from_avatar(client, avatar_bytes)

    # 2. Метрики
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

    # 3. Конфиг (Imagen 4)
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        output_mime_type="image/png"
    )

    # --- Генерация Current ---
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current", face_desc)

    # Добавляем жесткое негативное описание в сам промпт (так как API конфиг может не поддерживать negative_prompt)
    prompt_curr += "\nAVOID: bodybuilding, fitness model, six pack, perfect lighting, retouching."

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

    # --- Генерация Target ---
    prompt_tgt = _build_prompt(user.sex or "male", tgt_data_for_prompt, "target", face_desc)
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
        provider="imagen-4-clinical"
    )
    db.session.add(vis)
    db.session.commit()
    return vis