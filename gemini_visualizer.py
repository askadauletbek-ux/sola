import os
import time
import uuid
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Imagen 4 Ultra - идеален для работы с референсами
IMAGE_MODEL_NAME = "imagen-4.0-ultra-generate-001"
# Gemini 2.0 Flash - используем только для математики тела, лицо не трогаем
REASONING_MODEL_NAME = "gemini-2.0-flash"


def _generate_smart_fitness_description(client, sex: str, metrics: Dict[str, float]) -> str:
    """
    Генерирует описание ТОЛЬКО ТЕЛА (от шеи и ниже).
    Лицо описывать запрещено, чтобы не сбить Subject Reference.
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
    Act as a fitness anatomy expert.
    Describe the NECK DOWN body physique for a {sex}.
    Stats: Height {height}cm, Weight {weight}kg, Body Fat {fat_pct:.1f}%, Muscle {muscle_pct:.1f}%.

    Rules:
    - Focus on muscle definition, vascularity, and fat distribution.
    - DO NOT mention face, hair, or eyes.
    - 10-14% Fat: Defined abs, athletic cut.
    - 20%+ Fat: Soft belly, smooth torso.

    Output concise visual string (max 30 words).
    """

    try:
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[types.Part(text=prompt)]
        )
        return response.text.strip()
    except Exception as e:
        if fat_pct < 15: return "shredded athletic physique, visible abs"
        if fat_pct < 25: return "fit athletic build"
        return "soft physique, heavy build"


def _build_final_prompt(body_desc: str, sex: str) -> str:
    """
    Строит промпт, который игнорирует текстовое описание лица
    и ссылается строго на [subject_id:0].
    """
    clothing = "black athletic shorts" if sex == 'male' else "black sports bra and leggings"

    # ГЛАВНЫЙ СЕКРЕТ 1 В 1:
    # Мы пишем "Photo of [subject_id:0]". Мы НЕ пишем "Photo of a man".
    # Это заставляет модель брать пиксели из референса.
    return f"""
    Raw studio photo of [subject_id:0].
    The subject has a {body_desc}.
    Wearing {clothing}.

    Details:
    - Full body shot, standing straight.
    - 8k resolution, photorealistic, raw style.
    - Neutral lighting, white background.
    - Head and face must explicitly match the reference [subject_id:0].
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

    # 1. Подготовка метрик
    if not metrics_current.get("fat_pct") and metrics_current.get("weight"):
        metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), metrics_current.get("weight"))
    if not metrics_current.get("height"):
        metrics_current["height"] = metrics_target.get("height") or metrics_target.get("height_cm") or 175

    if not metrics_target.get("fat_pct") and metrics_target.get("weight_kg"):
        metrics_target["fat_pct"] = _compute_pct(metrics_target.get("fat_mass", 0), metrics_target.get("weight_kg"))

    # 2. Описываем ТОЛЬКО ТЕЛО (теперь без анализа лица)
    current_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_current)
    target_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_target)

    # 3. Настройка Референса (Ключевой этап)
    try:
        ref_image = types.Image(image_bytes=avatar_bytes)

        reference_config = [
            types.ReferenceImage(
                image=ref_image,
                subject_id="0",
                # ВАЖНО: Описание должно быть максимально абстрактным.
                # Если написать "Asian man", модель начнет фантазировать.
                # Если написать "Person's face", она будет смотреть на картинку.
                subject_description="A portrait of the person's face"
            )
        ]

        config = types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
            output_mime_type="image/png",
            reference_images=reference_config
        )
    except Exception as e:
        # Fallback для старых версий SDK
        print(f"Config Error: {e}")
        config = types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16", output_mime_type="image/png")

    # Функция генерации
    def _gen_image(body_desc):
        # Строим промпт только вокруг subject_id
        prompt_text = _build_final_prompt(body_desc, user.sex or "male")

        try:
            return client.models.generate_images(
                model=IMAGE_MODEL_NAME,
                prompt=prompt_text,
                config=config
            )
        except Exception as e:
            # Fallback на Gemini Flash, если Imagen упал
            # Тут мы пытаемся выкрутиться через текстовый промпт, так как Flash не умеет Subject ID
            print(f"Imagen failed ({e}), using fallback...")
            return client.models.generate_images(
                model='gemini-2.0-flash',
                prompt=prompt_text + ". Ensure facial identity matches the reference provided.",
                config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16", output_mime_type="image/png")
            )

    # --- ГЕНЕРАЦИЯ ---
    try:
        # Current
        response_curr = _gen_image(current_body_desc)
        if not response_curr.generated_images: raise RuntimeError("No image for Current")
        curr_png = response_curr.generated_images[0].image.image_bytes

        # Target
        response_tgt = _gen_image(target_body_desc)
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
        provider="imagen-4-subject-ref-v2"
    )
    db.session.add(vis)
    db.session.commit()
    return vis