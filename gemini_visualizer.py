import os
import time
import uuid
import json
from typing import Tuple, Dict

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Используем самую мощную модель для генерации (из твоего списка)
# Imagen 4 Ultra дает лучший фотореализм и текстуру кожи
IMAGE_MODEL_NAME = "imagen-4.0-ultra-generate-001"

# Модель для "рассуждений" (анализ лица и превращение цифр в описание тела)
REASONING_MODEL_NAME = "gemini-2.0-flash"


def _analyze_face_from_avatar(client, avatar_bytes: bytes) -> str:
    """
    1. Анализирует аватарку.
    2. Извлекает ключевые черты лица для сохранения идентичности.
    """
    try:
        prompt = """
        Analyze the face in this image to create a consistent character prompt. 
        Describe ONLY the face features strictly and concisely:
        1. Ethnicity/Skin tone (precise description).
        2. Exact hair style, texture, and color.
        3. Facial structure (jawline, cheekbones, eye shape).
        4. Facial hair (beard/stubble/mustache) if any.
        5. Age approximation.

        Output format example: "A Latino man, approx 30 years old, with short fade haircut, dark brown eyes, sharp jawline, and light stubble beard."
        Do NOT describe clothing, background, or body.
        """
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
            ]
        )
        return response.text.strip() if response.text else "A realistic person with neutral expression"
    except Exception as e:
        print(f"Error analyzing face: {e}")
        return "A realistic person with neutral expression"


def _generate_smart_fitness_description(client, sex: str, metrics: Dict[str, float]) -> str:
    """
    «Мозг» системы. Превращает сухие цифры в визуальное описание тела.
    """
    height = metrics.get("height", 175)
    weight = metrics.get("weight", 80)
    fat_mass = metrics.get("fat_mass")
    muscle_mass = metrics.get("muscle_mass")

    # Если есть только масса, считаем проценты для ИИ, чтобы ему было понятнее
    fat_pct = metrics.get("fat_pct")
    if fat_pct is None and weight > 0:
        fat_pct = (fat_mass / weight) * 100 if fat_mass else 20

    muscle_pct = metrics.get("muscle_pct")
    if muscle_pct is None and weight > 0:
        muscle_pct = (muscle_mass / weight) * 100 if muscle_mass else 40

    # Промпт для ИИ-диетолога
    prompt = f"""
    You are a professional fitness visualizer. 
    Convert these biometrics into a PHOTOGRAPHIC visual description of a human body for an AI image generator.

    Subject: {sex}
    Height: {height} cm
    Weight: {weight} kg
    Body Fat: {fat_pct:.1f}%
    Muscle Mass: aprox {muscle_pct:.1f}% (or relative to weight)

    Rules:
    1. Analyze the Fat/Muscle ratio. 
       - Low fat + High muscle = Vascular, striated, defined.
       - High fat + High muscle = "Bear mode", bulky, thick neck, undefined abs.
       - Low fat + Low muscle = Skinny, bony.
       - High fat + Low muscle = Soft, round, lack of definition ("Skinny fat").
    2. Describe specific areas: Abs visibility, arm vascularity, chest definition, waist width, face fullness (fat affects face shape!).
    3. Be brutally honest regarding the stats. If 30% fat, describe a soft belly. If 10%, describe distinct abs.

    Output ONLY the visual description string (max 50 words). 
    Example: "Athletic build with visible upper abs, defined deltoids, slight vascularity in forearms, lean face with sharp jawline."
    """

    try:
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[types.Part(text=prompt)]
        )
        return response.text.strip()
    except Exception as e:
        print(f"Error generating body desc: {e}")
        # Fallback на старую логику если ИИ сбоит
        if fat_pct < 15: return "extremely lean, shredded athletic physique"
        if fat_pct < 25: return "fit average physique, flat stomach"
        return "overweight physique, soft body, visible belly"


def _build_final_prompt(face_description: str, body_description: str, sex: str) -> str:
    """
    Собирает финальный технический промпт для Imagen 4 Ultra.
    """
    clothing = "simple black athletic shorts, shirtless" if sex == 'male' else "black sports bra and leggings"

    return f"""
    raw photo, 8k uhd, dslr, soft lighting.
    Subject: {face_description}.
    Body: {body_description}.
    Clothing: {clothing}.
    Pose: Standing straight, arms relaxed at sides, full body shot, neutral background.

    Details:
    - Photorealistic skin texture, pores, imperfections.
    - Anatomically correct muscle and fat distribution based on description.
    - Consistent lighting.
    - NO artistic filters, NO cartoon style.
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
    """
    Главная функция генерации.
    1. Анализирует лицо (1 раз).
    2. Генерирует описание тела для "Сейчас".
    3. Генерирует описание тела для "Цель".
    4. Рисует две картинки с одним лицом.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())

    # 1. Анализируем лицо (Константа для обоих изображений)
    face_description = _analyze_face_from_avatar(client, avatar_bytes)
    print(f"[Visualizer] Face Analysis: {face_description}")

    # 2. Генерируем описание тела CURRENT
    # Вычисляем проценты, если их нет
    if not metrics_current.get("fat_pct") and metrics_current.get("weight"):
        metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), metrics_current.get("weight"))

    # Добавляем рост в метрики, если его нет (берем из Target или дефолт)
    if not metrics_current.get("height"):
        metrics_current["height"] = metrics_target.get("height") or metrics_target.get("height_cm") or 175

    current_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_current)
    print(f"[Visualizer] Current Body: {current_body_desc}")

    # 3. Генерируем описание тела TARGET
    # Важно: Target weight может отличаться от Current
    if not metrics_target.get("fat_pct") and metrics_target.get("weight_kg"):
        metrics_target["fat_pct"] = _compute_pct(metrics_target.get("fat_mass", 0), metrics_target.get("weight_kg"))

    target_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_target)
    print(f"[Visualizer] Target Body: {target_body_desc}")

    # 4. Конфигурация Imagen
    # aspect_ratio="9:16" идеально для мобильных телефонов (Stories формат)
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio="9:16",
        output_mime_type="image/png",
    )

    # --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ CURRENT ---
    prompt_curr = _build_final_prompt(face_description, current_body_desc, user.sex or "male")
    try:
        response_curr = client.models.generate_images(
            model=IMAGE_MODEL_NAME,
            prompt=prompt_curr,
            config=config
        )
        if not response_curr.generated_images:
            raise RuntimeError("No image for Current state.")
        curr_png = response_curr.generated_images[0].image.image_bytes
    except Exception as e:
        raise RuntimeError(f"Error generating Current image: {str(e)}")

    # --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ TARGET ---
    prompt_tgt = _build_final_prompt(face_description, target_body_desc, user.sex or "male")
    try:
        response_tgt = client.models.generate_images(
            model=IMAGE_MODEL_NAME,
            prompt=prompt_tgt,
            config=config
        )
        if not response_tgt.generated_images:
            raise RuntimeError("No image for Target state.")
        tgt_png = response_tgt.generated_images[0].image.image_bytes
    except Exception as e:
        raise RuntimeError(f"Error generating Target image: {str(e)}")

    # 5. Сохранение в БД (используя существующую логику UploadedFile)
    curr_filename = _save_png_to_db(curr_png, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_png, user.id, f"{ts}_target")

    return curr_filename, tgt_filename


def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, float],
                  metrics_target: Dict[str, float]):
    """
    Сохраняет запись о визуализации в таблицу BodyVisualization
    """
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="imagen-4-ultra-smart"
    )
    db.session.add(vis)
    db.session.commit()
    return vis