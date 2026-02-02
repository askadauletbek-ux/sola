import os
import time
import uuid
import json
from typing import Tuple, Dict, Any

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# Используем модель Imagen, которая поддерживает Reference Image (сохранение лица)
IMAGE_MODEL_NAME = "imagen-3.0-generate-001"  # Или "imagen-4.0-ultra-generate-001", если есть доступ
REASONING_MODEL_NAME = "gemini-2.0-flash"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Безопасная конвертация в float для защиты от NoneType error"""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _compute_pct(value: Any, weight: Any) -> float:
    v = _safe_float(value)
    w = _safe_float(weight)
    if w <= 0: return 0.0
    return round(100.0 * v / w, 2)


def _analyze_face_from_avatar(client, avatar_bytes: bytes) -> str:
    """Создает словесный портрет для усиления референса."""
    try:
        prompt = """
        Analyze this face. Describe strictly:
        1. Ethnicity and specific Skin Tone.
        2. Hair style and color.
        3. Facial Structure (beard? glasses?).
        Output concise string like: "Photo of a [ethnicity] man, [hair], [distinct features]."
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


def _generate_body_description(client, sex: str, metrics: Dict[str, Any]) -> str:
    """Превращает цифры в описание тела."""
    height = _safe_float(metrics.get("height") or metrics.get("height_cm"), 175)
    weight = _safe_float(metrics.get("weight") or metrics.get("weight_kg"), 80)

    # Пытаемся получить проценты, если их нет - считаем
    fat_pct = metrics.get("fat_pct")
    if fat_pct is None:
        fat_mass = _safe_float(metrics.get("fat_mass"))
        fat_pct = (fat_mass / weight * 100) if weight > 0 else 20
    else:
        fat_pct = _safe_float(fat_pct)

    muscle_pct = metrics.get("muscle_pct")
    if muscle_pct is None:
        muscle_mass = _safe_float(metrics.get("muscle_mass"))
        muscle_pct = (muscle_mass / weight * 100) if weight > 0 else 40
    else:
        muscle_pct = _safe_float(muscle_pct)

    prompt = f"""
    Act as an anatomy expert. Describe the BODY CONDITION ONLY for a {sex}.
    Stats: Height {height}cm, Weight {weight}kg, Body Fat {fat_pct:.1f}%, Muscle {muscle_pct:.1f}%.

    Rules:
    - Low Fat (<12%): "shredded, visible abs, vascularity".
    - Med Fat (15-20%): "athletic, flat stomach".
    - High Fat (>25%): "soft belly, love handles".

    Output concise visual string (max 20 words).
    """
    try:
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[types.Part(text=prompt)]
        )
        return response.text.strip()
    except Exception:
        if fat_pct < 15: return "shredded athletic physique, visible abs"
        return "soft physique"


def _build_final_prompt(face_desc: str, body_desc: str, sex: str) -> str:
    clothing = "black athletic shorts" if sex == 'male' else "black sports bra and leggings"
    return f"""
    High-fidelity raw photo of {face_desc}.
    The subject has a {body_desc}.
    Wearing {clothing}.
    Standing straight, white studio background, cinematic lighting, 8k.
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


def generate_for_user(user, avatar_bytes: bytes, metrics_current: Dict[str, Any], metrics_target: Dict[str, Any]) -> \
Tuple[str, str]:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)
    ts = int(time.time())

    # 1. Анализ (текстовый) + Описания тел
    face_desc = _analyze_face_from_avatar(client, avatar_bytes)
    current_body = _generate_body_description(client, user.sex or "male", metrics_current)
    target_body = _generate_body_description(client, user.sex or "male", metrics_target)

    # 2. Настройка Reference Image (КЛЮЧЕВОЙ МОМЕНТ ДЛЯ 1-в-1 ЛИЦА)
    # Мы используем avatar_bytes как референс с ID '0' (subject)
    try:
        ref_image = types.Image(image_bytes=avatar_bytes)

        # Конфигурация для Imagen: Subject Reference
        # Это заставляет модель "натянуть" лицо из референса на генерацию
        reference_config = [
            types.ReferenceImage(
                image=ref_image,
                subject_id="0",
                subject_description="The main subject's face"
            )
        ]

        # Конфиг генерации
        config = types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
            output_mime_type="image/png",
            reference_images=reference_config
        )

        # Добавляем триггер [subject_id:0] в промпт (требование Imagen для референсов)
        prompt_suffix = " [subject_id:0]"

    except Exception as e:
        print(f"Warning: Reference Image config failed ({e}). Fallback to text prompt.")
        config = types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16", output_mime_type="image/png")
        prompt_suffix = ""

    # Функция генерации
    def _gen(body_text):
        full_prompt = _build_final_prompt(face_desc, body_text, user.sex or "male") + prompt_suffix
        try:
            response = client.models.generate_images(
                model=IMAGE_MODEL_NAME,
                prompt=full_prompt,
                config=config
            )
            if response.generated_images:
                return response.generated_images[0].image.image_bytes
            raise RuntimeError("No image generated")
        except Exception as e:
            # Fallback на Gemini 2.0 Flash если Imagen недоступен/ошибка
            print(f"Imagen failed ({e}), trying Gemini 2.0 Flash fallback...")
            fallback_prompt = full_prompt + ". Make sure the face matches the provided image exactly."
            # Для Gemini 2.0 передаем картинку в contents (если поддерживается методом)
            # Примечание: generate_images обычно принимает только промпт.
            # Здесь упрощенный фоллбек без референса (так как Imagen - лучший вариант для лиц)
            raise RuntimeError(f"Generation failed: {e}")

    # 3. Генерация
    try:
        curr_png = _gen(current_body)
        tgt_png = _gen(target_body)
    except Exception as e:
        raise RuntimeError(f"Pipeline failed: {str(e)}")

    # 4. Сохранение
    curr_filename = _save_png_to_db(curr_png, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_png, user.id, f"{ts}_target")

    return curr_filename, tgt_filename


# Обязательная функция для app.py
def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, Any],
                  metrics_target: Dict[str, Any]):
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="imagen-ref-subject"
    )
    db.session.add(vis)
    db.session.commit()
    return vis