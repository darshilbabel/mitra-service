import os
import traceback
import requests
import re

from chatbot.models import LanguageMapping
from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

langfuse = get_langfuse_client()

def sarvam_text_to_speech(message, source_language, voice_provider):
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="sarvam_tts",
        model="sarvam-bulbul-tts",
        input={
            "text": message,
            "language_code": source_language,
            "voice_name": getattr(voice_provider, 'name', None),
            "gender": getattr(voice_provider, 'gender', None),
            "voice_speed": getattr(voice_provider, 'voice_speed', None),
        },
    ) as gen:
        try:
            api_key = os.getenv("SARVAM_API_KEY")
            if not api_key:
                gen.update(output={"status": "error", "message": "missing_api_key"}, level="ERROR")
                return {
                    'status': 500,
                    'content': 'SARVAM_API_KEY is not configured'
                }

            other = voice_provider.other_params if voice_provider and voice_provider.other_params else {}
            request_timeout = other.get("request_timeout", 60)

            try:
                request_timeout = float(request_timeout)
            except Exception:
                request_timeout = 60

            requested_speaker = other.get("speaker")

            if not requested_speaker and voice_provider and voice_provider.name:
                name_value = voice_provider.name.strip().lower()
                if not re.fullmatch(r"[a-z]{2,3}-[a-z]{2}", name_value):
                    requested_speaker = name_value

            payload = {
                "text": message,
                "target_language_code": LanguageMapping.get_sarvam_language(source_language),
                "model": other.get("model", "bulbul:v3"),
                "speaker": requested_speaker or "shubh",
                "speech_sample_rate": other.get("speech_sample_rate", 24000),
                "output_audio_codec": other.get("output_audio_codec", "wav"),
                "pace": other.get("pace", 1.0),
                "temperature": other.get("temperature", 0.6),
            }

            response = requests.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={
                    "api-subscription-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=request_timeout,
            )

            if response.status_code != 200:
                gen.update(
                    output={"status": "error", "status_code": response.status_code},
                    level="ERROR",
                    status_message=f"Sarvam returned {response.status_code}",
                )
                return {
                    'status': response.status_code,
                    'content': response.text
                }

            body = response.json()
            audios = body.get("audios", [])
            if not audios:
                gen.update(output={"status": "error", "message": "no_audio_returned"}, level="ERROR")
                return {
                    'status': 500,
                    'content': 'No audio returned from Sarvam TTS'
                }

            char_count = len(message) if message else 0
            # gen.update(
            #     output={"status": "ok", "audio_bytes": len(audios[0])},
            #     usage_details={"input": char_count, "output": 0, "total": char_count},
            # )
            usage_details, cost_details = compute_translate_usage_and_cost(
                                              "sarvam-bulbul-tts", char_count,
                                               voice_provider=voice_provider,
                                              company_bot=getattr(voice_provider, 'company_bot', None),
                                            )
            gen.update(
                        output={"status": "ok", "audio_bytes": len(audios[0])},
                        usage_details=usage_details,
                        cost_details=cost_details,
                      )

            return {
                'status': 200,
                'content': audios[0]
            }

        except Exception as e:
            traceback.print_exc()
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {
                'status': 500,
                'content': str(e)
            }