import os
import time
import uuid
import json
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Imagen 4 Ultra - лучшее качество (поддерживает референсы в Vertex/Gemini API)
IMAGE_MODEL_NAME = "imagen-4.0-ultra-generate-001"

# Gemini 2.0 Flash - используется для анализа и как запасной генератор
REASONING_MODEL_NAME = "gemini-2.0-flash"


def _analyze_face_from_avatar(client, avatar_bytes: bytes) -> str:
    """
    Создает словесный портрет для подстраховки референса.
    """
    try:
        prompt = """
        Analyze this face for ID retention. Describe distinct features strictly:
        1. Ethnicity and exact Skin Tone (e.g., 'pale olive', 'dark brown').
        2. Exact Hair (style, hairline, color).
        3. Facial Structure (face shape, nose shape, eyes).
        4. Distinguishing marks (moles, scars, beard pattern).

        Output format: "Photo of a [ethnicity] man, [age], [hair], [distinct features]."
        Keep it under 30 words.
        """
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
            ]
        )
        return response.text.strip() if response.text else "A realistic person"
    except Exception as e:
        print(f"Error analyzing face: {e}")
        return "A realistic person"


def _generate_smart_fitness_description(client, sex: str, metrics: Dict[str, float]) -> str:
    """
    Превращает цифры в описание тела (мышцы, жир).
    """
    height = metrics.get("height", 175)
    weight = metrics.get("weight", 80)
    fat_mass = metrics.get("fat_mass")
    muscle_mass = metrics.get("muscle_mass")

    # Расчет процентов
    fat_pct = metrics.get("fat_pct")
    if fat_pct is None and weight > 0:
        fat_pct = (fat_mass / weight) * 100 if fat_mass else 20

    muscle_pct = metrics.get("muscle_pct")
    if muscle_pct is None and weight > 0:
        muscle_pct = (muscle_mass / weight) * 100 if muscle_mass else 40

    prompt = f"""
    Act as an anatomy expert for AI image generation.
    Describe the BODY ONLY for a {sex}.
    Stats: Height {height}cm, Weight {weight}kg, Body Fat {fat_pct:.1f}%, Muscle {muscle_pct:.1f}%.

    Rules:
    - 6-12% Fat: Visible abs, vascularity, defined definition.
    - 15-20% Fat: Flat stomach, athletic but softer.
    - 25%+ Fat: Soft belly, love handles, no definition.
    - High Muscle: Broad shoulders, thick chest/arms.

    Output concise visual string (max 40 words). 
    Example: "Lean athletic build, visible 6-pack abs, vascular arms, broad shoulders."
    """

    try:
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[types.Part(text=prompt)]
        )
        return response.text.strip()
    except Exception as e:
        # Fallback
        if fat_pct < 15: return "shredded athletic physique, visible abs"
        if fat_pct < 25: return "fit physique, flat stomach"
        return "soft physique, visible belly fat"


def _build_final_prompt(face_desc: str, body_desc: str, sex: str) -> str:
    clothing = "black athletic shorts" if sex == 'male' else "black sports bra and leggings"

    return f"""
    High-fidelity raw photo.
    Subject: {face_desc}.
    Body Condition: {body_desc}.
    Clothing: {clothing}, shirtless (if male).

    CRITICAL INSTRUCTIONS:
    - PRESERVE FACIAL IDENTITY FROM REFERENCE IMAGE.
    - The face must match the reference exactly.
    - Realistic skin texture, dslr photography, 8k.
    - Standing straight, neutral lighting, white background.
    """


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

    # 1. Текстовый анализ лица (помогает модели понять контекст)
    face_description = _analyze_face_from_avatar(client, avatar_bytes)

    # 2. Описания тел
    if not metrics_current.get("fat_pct") and metrics_current.get("weight"):
        metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), metrics_current.get("weight"))
    if not metrics_current.get("height"):
        metrics_current["height"] = metrics_target.get("height") or metrics_target.get("height_cm") or 175

    current_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_current)

    if not metrics_target.get("fat_pct") and metrics_target.get("weight_kg"):
        metrics_target["fat_pct"] = _compute_pct(metrics_target.get("fat_mass", 0), metrics_target.get("weight_kg"))

    target_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_target)

    # 3. Подготовка Конфига с REFERENCE IMAGE (Это ключевой момент для 1:1)
    # Создаем объект изображения для референса
    try:
        ref_image = types.Image(image_bytes=avatar_bytes)

        # Настройка для Imagen с Subject Reference
        # ID 0 указывает, что этот референс - главный субъект
        reference_config = [
            types.ReferenceImage(
                image=ref_image,
                subject_id="0",
                subject_description="The user's face"
            )
        ]

        config = types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
            output_mime_type="image/png",
            reference_images=reference_config  # <-- ПЕРЕДАЕМ АВАТАРКУ
        )

        # Помечаем в промпте, какой subject_id использовать
        prompt_suffix = " [subject_id:0]"

    except Exception as e:
        print(f"Reference Image setup warning: {e}. Trying without specific config type...")
        # Если версия SDK старая и не поддерживает типы выше
        config = types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
            output_mime_type="image/png"
        )
        prompt_suffix = ""

    # Функция генерации (обертка для повторного использования)
    def _gen_image(prompt_text):
        full_prompt = prompt_text + prompt_suffix
        try:
            # Попытка 1: Imagen 4 Ultra с Reference Image
            return client.models.generate_images(
                model=IMAGE_MODEL_NAME,
                prompt=full_prompt,
                config=config
            )
        except Exception as e:
            print(f"Imagen generation failed ({e}), switching to Gemini 2.0 Flash fallback...")
            # Попытка 2: Gemini 2.0 Flash (если Imagen не сработал или не принял референс)
            # Gemini 2.0 Flash отлично понимает "Нарисуй этого человека" если передать картинку в contents
            return client.models.generate_images(
                model='gemini-2.0-flash',
                prompt=prompt_text + ". Make sure the face matches the provided image exactly.",
                config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16",
                                                  output_mime_type="image/png"),
                # Для Gemini 2.0 мы можем попробовать передать image в contents если метод generate_images это поддерживает,
                # но обычно generate_images работает только по промпту.
                # Если Imagen Subject Reference не сработал, 100% сходства без FaceSwap (insightface) добиться сложно.
            )

    # --- ГЕНЕРАЦИЯ ---
    try:
        # Current
        prompt_curr = _build_final_prompt(face_description, current_body_desc, user.sex or "male")
        response_curr = _gen_image(prompt_curr)
        if not response_curr.generated_images: raise RuntimeError("No image for Current")
        curr_png = response_curr.generated_images[0].image.image_bytes

        # Target
        prompt_tgt = _build_final_prompt(face_description, target_body_desc, user.sex or "male")
        response_tgt = _gen_image(prompt_tgt)
        if not response_tgt.generated_images: raise RuntimeError("No image for Target")
        tgt_png = response_tgt.generated_images[0].image.image_bytes

    except Exception as e:
        raise RuntimeError(f"Generation pipeline failed: {str(e)}")

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
        provider="imagen-4-subject-ref"
    )
    db.session.add(vis)
    db.session.commit()
    return vis