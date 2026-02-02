import os
import time
from typing import Tuple, Dict, Any, Optional
import uuid

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

MODEL_NAME = "gemini-3-pro-image-preview"


# --- НОВАЯ ЗАЩИТНАЯ ФУНКЦИЯ ---
def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Безопасно конвертирует value в float.
    Обрабатывает None, пустые строки и ошибки формата.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _compute_pct(value: Any, weight: Any) -> float:
    v = safe_float(value)
    w = safe_float(weight)
    if w <= 0:
        return 0.0
    return round(100.0 * v / w, 2)


# -----------------------------


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str) -> str:
    # Используем safe_float, чтобы в промпт не попало "None"
    height = safe_float(metrics.get("height"))
    weight = safe_float(metrics.get("weight"))
    fat_pct = safe_float(metrics.get("fat_pct"))
    muscle_pct = safe_float(metrics.get("muscle_pct"))

    if sex == 'female':
        clothing = "Plain black sports bra and shorts"
    else:
        clothing = "Plain black athletic shorts, shirtless"

    return f"""
    Generate a hyper-realistic, high-fidelity studio photograph of the person provided in the reference image, but modify their body composition according to the metrics below.

    CRITICAL INSTRUCTIONS:
    1. **FACE IDENTITY:** You MUST PRESERVE the face of the person from the input image exactly. It should look like the same person 1-in-1.
    2. **BODY METRICS:**
       - Height: {height}cm
       - Weight: {weight}kg
       - Body Fat: {fat_pct}% (Visual appearance: {'defined abs, vascularity' if fat_pct < 12 else 'soft outlines, no definition'}).
       - Muscle Mass: {muscle_pct}% (Visual appearance: {'muscular, broad' if muscle_pct > 40 else 'average build'}).
    3. **CLOTHING:** {clothing}.
    4. **SETTING:** Pure white studio background, professional lighting, 8k resolution, raw photo style.
    5. **POSE:** Standing straight, full body visible (head to toe).

    Output ONLY the generated image.
    """


def _extract_image_from_content(response) -> bytes:
    if not response or not response.candidates:
        raise RuntimeError("No candidates returned by Gemini.")

    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data

    text_content = response.text if response.text else "No text explanation"
    raise RuntimeError(f"No image generated. Model response: {text_content}")


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

    # 1. Расчет и очистка ТЕКУЩИХ метрик
    # safe_float спасет, даже если в словаре лежит {"weight": None}
    curr_weight = safe_float(metrics_current.get("weight"))
    curr_fat_mass = safe_float(metrics_current.get("fat_mass"))
    curr_muscle_mass = safe_float(metrics_current.get("muscle_mass"))

    # Обновляем словарь чистыми данными, чтобы не упало дальше
    metrics_current["weight"] = curr_weight

    # Считаем проценты, только если они не переданы
    if not metrics_current.get("fat_pct"):
        metrics_current["fat_pct"] = _compute_pct(curr_fat_mass, curr_weight)

    if not metrics_current.get("muscle_pct"):
        # Эвристика: если мышц нет в данных, берем 40% от веса как дефолт для мужчины
        curr_muscle_safe = curr_muscle_mass if curr_muscle_mass > 0 else (curr_weight * 0.4)
        metrics_current["muscle_pct"] = _compute_pct(curr_muscle_safe, curr_weight)

    # 2. Подготовка ЦЕЛЕВЫХ метрик
    tgt_weight = safe_float(metrics_target.get("weight_kg"))

    metrics_target_prepared = {
        "height": safe_float(metrics_target.get("height_cm") or metrics_target.get("height")),
        "weight": tgt_weight,
        "fat_pct": safe_float(metrics_target.get("fat_pct")),
        "muscle_pct": safe_float(metrics_target.get("muscle_pct"))
    }

    gen_config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        temperature=0.4
    )

    # --- Генерация Current ---
    print(f"Generating Current (Weight: {curr_weight})...")
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current")

    contents_curr = [
        types.Part(text=prompt_curr),
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
    ]

    try:
        resp_curr = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents_curr,
            config=gen_config
        )
        curr_png = _extract_image_from_content(resp_curr)
    except Exception as e:
        print(f"Error generating current: {e}")
        raise

    # --- Генерация Target ---
    print(f"Generating Target (Weight: {tgt_weight})...")
    prompt_tgt = _build_prompt(user.sex or "male", metrics_target_prepared, "target")

    contents_tgt = [
        types.Part(text=prompt_tgt),
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes))
    ]

    try:
        resp_tgt = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents_tgt,
            config=gen_config
        )
        tgt_png = _extract_image_from_content(resp_tgt)
    except Exception as e:
        print(f"Error generating target: {e}")
        raise

    # Сохранение
    curr_filename = _save_png_to_db(curr_png, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_png, user.id, f"{ts}_target")

    return curr_filename, tgt_filename


# create_record оставляем без изменений
def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, float],
                  metrics_target: Dict[str, float]):
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="gemini-3-pro"
    )
    db.session.add(vis)
    db.session.commit()
    return vis