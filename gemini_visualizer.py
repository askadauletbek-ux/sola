import os
import time
import uuid
import json
import logging
from typing import Tuple, Dict, Optional

# Библиотеки для обработки изображений и Face Swap
import cv2
import numpy as np
import insightface
from insightface.app import FaceAnalysis

from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

# --- КОНФИГУРАЦИЯ ---
IMAGE_MODEL_NAME = "imagen-4.0-ultra-generate-001"
REASONING_MODEL_NAME = "gemini-2.0-flash"
FACE_SWAP_MODEL_PATH = "./inswapper_128.onnx"  # Убедитесь, что файл лежит здесь

logger = logging.getLogger(__name__)


class FaceSwapper:
    """
    Класс для жесткого переноса лица (Deepfake style) для сохранения 100% сходства.
    """

    def __init__(self):
        self.swapper = None
        self.app = None
        self._load_models()

    def _load_models(self):
        if not os.path.exists(FACE_SWAP_MODEL_PATH):
            logger.warning(f"FaceSwap model not found at {FACE_SWAP_MODEL_PATH}. 1:1 match mode disabled.")
            return

        # Инициализация анализатора лиц
        self.app = FaceAnalysis(name='buffalo_l')
        self.app.prepare(ctx_id=0, det_size=(640, 640))

        # Инициализация сваппера
        self.swapper = insightface.model_zoo.get_model(FACE_SWAP_MODEL_PATH, download=False, download_zip=False)

    def process_image(self, source_bytes: bytes, target_bytes: bytes) -> bytes:
        """
        Берет лицо из source_bytes и накладывает на target_bytes.
        """
        if not self.swapper or not self.app:
            return target_bytes  # Возвращаем оригинал, если модели нет

        # Конвертация bytes -> numpy
        source_np = cv2.imdecode(np.frombuffer(source_bytes, np.uint8), cv2.IMREAD_COLOR)
        target_np = cv2.imdecode(np.frombuffer(target_bytes, np.uint8), cv2.IMREAD_COLOR)

        # Поиск лиц
        source_faces = self.app.get(source_np)
        target_faces = self.app.get(target_np)

        if not source_faces:
            logger.warning("No face detected in Avatar/Source.")
            return target_bytes

        if not target_faces:
            logger.warning("No face detected in Generated Body.")
            return target_bytes

        # Берем самое крупное лицо (главный герой)
        source_face = sorted(source_faces, key=lambda x: x.bbox[2] * x.bbox[3])[-1]
        target_face = sorted(target_faces, key=lambda x: x.bbox[2] * x.bbox[3])[-1]

        # Свап
        res_img = self.swapper.get(target_np, target_face, source_face, paste_back=True)

        # Конвертация обратно в bytes
        _, encoded_img = cv2.imencode('.png', res_img)
        return encoded_img.tobytes()


# Глобальный экземпляр сваппера (чтобы не грузить модель каждый раз)
face_swapper_service = FaceSwapper()


def _generate_smart_fitness_description(client, sex: str, metrics: Dict[str, float]) -> str:
    """
    Генерирует описание ТОЛЬКО тела, без упоминания лица.
    """
    height = metrics.get("height", 175)
    weight = metrics.get("weight", 80)
    fat_mass = metrics.get("fat_mass")
    muscle_mass = metrics.get("muscle_mass")

    fat_pct = metrics.get("fat_pct")
    if fat_pct is None and weight > 0:
        fat_pct = (fat_mass / weight) * 100 if fat_mass else 20

    muscle_pct = metrics.get("muscle_pct")
    if muscle_pct is None and weight > 0:
        muscle_pct = (muscle_mass / weight) * 100 if muscle_mass else 40

    prompt = f"""
    Act as a fitness anatomy expert.
    Describe the NECK DOWN body shape for a {sex}.
    Stats: Height {height}cm, Weight {weight}kg, Body Fat {fat_pct:.1f}%, Muscle {muscle_pct:.1f}%.

    Rules:
    - Low Fat (<12%): Vascularity, muscle separation, 6-pack abs.
    - Medium Fat (15-20%): Athletic but smooth, flat stomach.
    - High Fat (25%+): Soft belly, love handles, thicker waist.

    Output strictly visual body traits. DO NOT describe the face.
    Example: "Lean athletic torso, defined abs, vascular arms, broad shoulders, fitted athletic build."
    """

    try:
        response = client.models.generate_content(
            model=REASONING_MODEL_NAME,
            contents=[types.Part(text=prompt)]
        )
        return response.text.strip()
    except Exception as e:
        if fat_pct < 15: return "shredded athletic physique, visible abs, muscular arms"
        if fat_pct < 25: return "fit physique, flat stomach, broad shoulders"
        return "soft physique, visible belly fat, heavy build"


def _build_final_prompt(body_desc: str, sex: str) -> str:
    """
    Промпт больше не описывает лицо словами, а ссылается на subject_id.
    """
    clothing = "black athletic shorts" if sex == 'male' else "black sports bra and yoga leggings"

    # Ключевое слово: [subject_id:0] заставляет модель смотреть на референс
    return f"""
    Raw DSLR photo of [subject_id:0].
    The person has a {body_desc}.
    Wearing {clothing}.

    Setting: Minimalist white photography studio, soft lighting.
    Quality: 8k, realistic skin texture, photorealistic, anatomical accuracy.
    Pose: Standing straight, facing camera, hands by sides.
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

    # 1. Готовим метрики
    if not metrics_current.get("fat_pct") and metrics_current.get("weight"):
        metrics_current["fat_pct"] = _compute_pct(metrics_current.get("fat_mass", 0), metrics_current.get("weight"))
    if not metrics_current.get("height"):
        metrics_current["height"] = metrics_target.get("height") or metrics_target.get("height_cm") or 175

    if not metrics_target.get("fat_pct") and metrics_target.get("weight_kg"):
        metrics_target["fat_pct"] = _compute_pct(metrics_target.get("fat_mass", 0), metrics_target.get("weight_kg"))

    # 2. Получаем текстовое описание ТЕЛА (лицо не трогаем)
    current_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_current)
    target_body_desc = _generate_smart_fitness_description(client, user.sex or "male", metrics_target)

    # 3. Настройка Imagen с жестким референсом
    try:
        ref_image = types.Image(image_bytes=avatar_bytes)

        # Настройка "Subject Reference"
        reference_config = [
            types.ReferenceImage(
                image=ref_image,
                subject_id="0",
                subject_description="A person"  # Максимально абстрактно, чтобы не сбивать
            )
        ]

        config = types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
            output_mime_type="image/png",
            reference_images=reference_config
        )
    except Exception as e:
        print(f"Config setup error: {e}")
        raise RuntimeError("Failed to setup reference image config")

    def _gen_and_swap(body_prompt):
        # Шаг А: Генерация тела через Imagen
        try:
            response = client.models.generate_images(
                model=IMAGE_MODEL_NAME,
                prompt=body_prompt,
                config=config
            )
            if not response.generated_images:
                raise ValueError("No images generated")

            gen_bytes = response.generated_images[0].image.image_bytes

            # Шаг Б: Face Swap (Магия 1-в-1)
            # Если файл модели есть, лицо заменится на оригинальное пиксель-в-пиксель
            final_bytes = face_swapper_service.process_image(source_bytes=avatar_bytes, target_bytes=gen_bytes)

            return final_bytes

        except Exception as e:
            print(f"Generation error: {e}")
            raise e

    # --- ЗАПУСК КОНВЕЙЕРА ---
    try:
        # Current
        prompt_curr = _build_final_prompt(current_body_desc, user.sex or "male")
        curr_final_bytes = _gen_and_swap(prompt_curr)

        # Target
        prompt_tgt = _build_final_prompt(target_body_desc, user.sex or "male")
        tgt_final_bytes = _gen_and_swap(prompt_tgt)

    except Exception as e:
        raise RuntimeError(f"Pipeline failed: {str(e)}")

    # 5. Сохранение
    curr_filename = _save_png_to_db(curr_final_bytes, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_final_bytes, user.id, f"{ts}_target")

    return curr_filename, tgt_filename


# Функция create_record остается без изменений
def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, float],
                  metrics_target: Dict[str, float]):
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="imagen-4-plus-faceswap"
    )
    db.session.add(vis)
    db.session.commit()
    return vis