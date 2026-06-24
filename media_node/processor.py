import sys
import gc
import logging
import warnings
import os
import threading
from contextlib import contextmanager
from PIL import Image

logger = logging.getLogger(__name__)

# CRITICAL: Set CUDA allocator config BEFORE importing torch
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

# Lazy imports handling for production environments where ML libs might be missing
try:
    from transformers import AutoProcessor, AutoModelForCausalLM
    import torch
except ImportError:
    logger.error("Error: transformers/torch not installed")
    sys.exit(1)

try:
    import whisper
except ImportError:
    logger.error("Error: openai-whisper not installed")
    sys.exit(1)

warnings.filterwarnings("ignore")
_INFERENCE_LOCK = threading.Lock()
_INFERENCE_LOCK_ENABLED = os.getenv("MEDIA_INFERENCE_LOCK", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@contextmanager
def inference_lock():
    if not _INFERENCE_LOCK_ENABLED:
        yield
        return
    with _INFERENCE_LOCK:
        yield


class ModelHandler:
    """
    Model manager with smart memory optimization.
    """
    def __init__(self, device):
        self.device = device
        self.vlm_model = None
        self.vlm_processor = None
        self.audio_model = None
        self._logged_audio_device = False

    def _log_audio_device_profile(self):
        if self._logged_audio_device or self.device != "cuda":
            return

        props = torch.cuda.get_device_properties(0)
        major, minor = torch.cuda.get_device_capability(0)
        tensor_core_note = "likely" if major >= 7 and "GTX" not in props.name else "unlikely"
        logger.info(
            "Whisper device: %s | capability: %s.%s | VRAM: %.1f GB | fast FP16/Tensor Cores: %s",
            props.name,
            major,
            minor,
            props.total_memory / 1024**3,
            tensor_core_note,
        )
        if tensor_core_note == "unlikely":
            logger.info(
                "GTX-class GPUs can report high CUDA utilization while running Whisper medium "
                "slower than expected because FP16 lacks Tensor Core acceleration."
            )
        self._logged_audio_device = True

    def load_vlm(self):
        """Load Florence-2, unload Whisper if present"""
        if self.vlm_model is not None: 
            return

        if self.audio_model is not None:
            logger.info("Unloading Whisper to free VRAM...")
            del self.audio_model
            self.audio_model = None
            gc.collect()
            torch.cuda.empty_cache()

        logger.info("Loading Florence-2...")
        model_id = "microsoft/Florence-2-large"
        self.vlm_processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.vlm_model = AutoModelForCausalLM.from_pretrained(
            model_id, trust_remote_code=True, torch_dtype=dtype
        ).to(self.device)

    def load_audio(self):
        """Load Whisper, unload Florence-2 if present"""
        if self.audio_model is not None:
            return

        self._log_audio_device_profile()

        if self.vlm_model is not None:
            logger.info("Unloading Florence-2 to free VRAM...")
            del self.vlm_model
            del self.vlm_processor
            self.vlm_model = None
            self.vlm_processor = None
            gc.collect()
            torch.cuda.empty_cache()

        # Aggressive memory cleanup before loading Whisper
        if self.device == "cuda":
            logger.info(f"Pre-load GPU memory: {torch.cuda.memory_allocated(0) / 1024**3:.2f} GB allocated")
            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            logger.info(f"After cleanup: {torch.cuda.memory_allocated(0) / 1024**3:.2f} GB allocated")

        logger.info("Loading Whisper...")
        self.audio_model = whisper.load_model("medium", device=self.device)

def run_florence_ocr(image_path, handler):
    """Run Florence-2 OCR"""
    try:
        image = Image.open(image_path).convert("RGB")
        task = "<OCR>"
        inputs = handler.vlm_processor(text=task, images=image, return_tensors="pt")
        inputs = {k: v.to(handler.device) for k, v in inputs.items()}

        if handler.device == "cuda" and handler.vlm_model.dtype == torch.float16:
            inputs["pixel_values"] = inputs["pixel_values"].to(torch.float16)

        generated = handler.vlm_model.generate(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"],
            max_new_tokens=1024,
            num_beams=3
        )

        text = handler.vlm_processor.batch_decode(generated, skip_special_tokens=False)[0]
        parsed = handler.vlm_processor.post_process_generation(
            text, task=task, image_size=(image.width, image.height)
        )
        return parsed.get("<OCR>", "")
    except Exception as e:
        logger.error(f"OCR error on {image_path}: {e}")
        return ""

def run_whisper_audio(audio_path, handler):
    """Run Whisper transcription"""
    try:
        result = handler.audio_model.transcribe(audio_path)
        return result["text"].strip()
    except Exception as e:
        logger.warning(f"Audio error on {audio_path}: {e}")
        return ""
