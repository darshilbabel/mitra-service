import base64
import logging
import os

import requests

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_stt_usage_and_cost

logger = logging.getLogger("django")
langfuse = get_langfuse_client()

SL_LLM_BASE_URL = os.getenv("SL_LLM_BASE_URL", "")


def sl_speech_to_text(base64_audio_file: str, source_language: str, audio_format: str, voice_provider) -> dict:
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="shikshalokam_stt",
        model="shikshalokam-stt",
        input={"source_language": source_language, "audio_format": audio_format},
    ) as gen:
        try:
            audio_bytes = base64.b64decode(base64_audio_file)
            filename = f"audio.{audio_format}"
            mime_type = f"audio/{audio_format}"

            other = voice_provider.other_params if voice_provider and voice_provider.other_params else {}
            request_timeout = other.get("request_timeout", 60)
            try:
                request_timeout = float(request_timeout)
            except Exception:
                request_timeout = 60

            response = requests.post(
                url=f"{SL_LLM_BASE_URL}/transcribe",
                files={"audio": (filename, audio_bytes, mime_type)},
                data={"source_lang": source_language},
                timeout=request_timeout,
            )
            response.raise_for_status()
            transcript = response.json().get("text", "")

            # No reliable duration available here — cost stays 0 (self-hosted, unpriced)
            # usage_details, cost_details = compute_stt_usage_and_cost("shikshalokam-stt", 0)
            usage_details, cost_details = compute_stt_usage_and_cost(
                                           "shikshalokam-stt", 0,
                                           voice_provider=voice_provider,
                                           company_bot=getattr(voice_provider, 'company_bot', None),
                                        )
            
            gen.update(output={"transcript": transcript}, usage_details=usage_details, cost_details=cost_details)
            return {"status": 200, "content": transcript}
        except Exception as e:
            logger.error("ShikshaLokam STT error: %s", e)
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {"status": 500, "content": str(e)}