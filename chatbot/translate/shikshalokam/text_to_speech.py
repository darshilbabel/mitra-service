import base64
import logging
import os

import requests

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

logger = logging.getLogger("django")
langfuse = get_langfuse_client()

SL_LLM_BASE_URL = os.getenv("SL_LLM_BASE_URL", "")


def sl_text_to_speech(text: str, source_language: str, voice_provider) -> dict:
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="shikshalokam_tts",
        model="shikshalokam-tts",
        input={
            "text": text,
            "target_lang": source_language,
            "voice_name": getattr(voice_provider, 'name', None),
            "gender": getattr(voice_provider, 'gender', None),
        },
    ) as gen:
        try:
            other = voice_provider.other_params if voice_provider and voice_provider.other_params else {}
            request_timeout = other.get("request_timeout", 60)
            try:
                request_timeout = float(request_timeout)
            except Exception:
                request_timeout = 60

            response = requests.post(
                url=f"{SL_LLM_BASE_URL}/tts",
                json={"text": text, "target_lang": source_language},
                timeout=request_timeout,
            )
            response.raise_for_status()
            audio_base64 = base64.b64encode(response.content).decode("utf-8")

            char_count = len(text) if text else 0
            # gen.update(
            #     output={"status": "ok", "audio_bytes": len(response.content)},
            #     usage_details={"input": char_count, "output": 0, "total": char_count},
            # )    
            usage_details, cost_details = compute_translate_usage_and_cost(
                                                   "shikshalokam-tts", char_count,
                                                    voice_provider=voice_provider,
                                                    company_bot=getattr(voice_provider, 'company_bot', None),
                                                 )
            gen.update(
                         output={"status": "ok", "audio_bytes": len(response.content)},
                         usage_details=usage_details,
                         cost_details=cost_details,
                      )

            return {"status": 200, "content": audio_base64}
        except Exception as e:
            logger.error("ShikshaLokam TTS error: %s", e, exc_info=True)
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {"status": 500, "content": str(e)}