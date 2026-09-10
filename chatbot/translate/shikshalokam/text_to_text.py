import logging
import os

import requests

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

logger = logging.getLogger("django")
langfuse = get_langfuse_client()

SL_LLM_BASE_URL = os.getenv("SL_LLM_BASE_URL", "")


def sl_translate(message_body: str, source_language: str, target_language: str, voice_provider) -> dict:
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="shikshalokam_translate",
        model="shikshalokam-translate",
        input={
            "message_preview": message_body[:200] if message_body else None,
            "source_language": source_language,
            "target_language": target_language,
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
                url=f"{SL_LLM_BASE_URL}/translate",
                json={
                    "text": message_body,
                    "source_lang": source_language,
                    "target_lang": target_language,
                },
                timeout=request_timeout,
            )
            response.raise_for_status()
            translated = response.json().get("translation", "")

            char_count = len(message_body) if message_body else 0
            # usage_details, cost_details = compute_translate_usage_and_cost("shikshalokam-translate", char_count)
            usage_details, cost_details = compute_translate_usage_and_cost(
                                           "shikshalokam-translate", char_count,
                                           voice_provider=voice_provider,
                                           company_bot=getattr(voice_provider, 'company_bot', None),
                                        )
            gen.update(
                output={"translated_preview": translated[:200]},
                usage_details=usage_details,
                cost_details=cost_details,
            )
            return {"status": 200, "content": translated}
        except Exception as e:
            logger.error("ShikshaLokam translate error: %s", e)
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {"status": 500, "content": str(e)}