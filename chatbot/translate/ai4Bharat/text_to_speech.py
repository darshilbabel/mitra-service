import os
import traceback
import requests
from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

langfuse = get_langfuse_client()

ai4bharat_api_key = os.getenv("BHASHANI_API_KEY")
ai4bharat_base_url = os.getenv("BHASHANI_BASE_URL")
ai4bharat_user_id = os.getenv("BHASHANI_USER_ID")
ai4bharat_authorization = os.getenv("BHASHANI_AUTHORIZATION")


def ai4bharat_text_speech(voice_provider, text, gender, source_language):
    other_params = voice_provider.other_params if voice_provider.other_params else {}
    service_id = other_params.get('serviceId', 'Bhashini/IITM/TTS')

    with langfuse.start_as_current_observation(
        as_type="generation",
        name="ai4bharat_tts",
        model=service_id,
        input={
            "text": text,
            "gender": gender,
            "source_language": source_language,
        },
        model_parameters={
            "sampling_rate": other_params.get('samplingRate', 22050),
        },
    ) as gen:
        try:
            api_url = ai4bharat_base_url
            print("gender: ", gender)
            print("source_language: ", source_language)
            print("original text: ", text)

            payload = {
                "pipelineTasks": [
                    {
                        "taskType": "tts",
                        "config": {
                            "language": {
                                "sourceLanguage": source_language,
                            },
                            "gender": gender.lower(),
                            "serviceId": service_id,
                            "samplingRate": other_params.get('samplingRate', 22050),
                        }
                    }
                ],
                "inputData": {
                    "input": [
                        {
                            "source": text
                        }
                    ]
                }
            }

            headers = {
                'accept': '*/*',
                'content-type': 'application/json',
                'Authorization': ai4bharat_authorization,
            }
            request_timeout = other_params.get("request_timeout", 10)
            print("REQUEST TIMEOUT VALUE:", request_timeout)

            try:
                request_timeout = float(request_timeout)
            except Exception:
                request_timeout = 10

            response = requests.post(api_url, json=payload, headers=headers, timeout=request_timeout)
            print("AI4Bharat API Response Status Code:", response.status_code, response)

            char_count = len(text) if text else 0
            usage_details, cost_details = compute_translate_usage_and_cost(
                                               service_id, char_count,
                                                voice_provider=voice_provider,
                                                company_bot=getattr(voice_provider, 'company_bot', None),
                                            )

            if response.status_code == 200:
                audio_data = response.json()
                if isinstance(audio_data, dict) and 'pipelineResponse' in audio_data:
                    audio_content = audio_data['pipelineResponse'][0].get('audio', [{}])[0].get('audioContent', '')
                    gen.update(
                        output={"status": "ok", "audio_length_chars": len(audio_content) if audio_content else 0},
                        usage_details=usage_details,
                        cost_details=cost_details,
                    )
                    return {
                        'status': 200,
                        'content': audio_content
                    }
                else:
                    gen.update(
                        output={"status": "error", "message": "Unexpected response format"},
                        usage_details={"input": char_count, "output": 0, "total": char_count},
                        level="ERROR",
                    )
                    return {
                        'status': 500,
                        'content': 'Unexpected response format from AI4Bharat API'
                    }
            else:
                gen.update(
                    output={"status": "error", "status_code": response.status_code},
                    usage_details={"input": char_count, "output": 0, "total": char_count},
                    level="ERROR",
                    status_message=f"AI4Bharat returned {response.status_code}",
                )
                return {
                    'status': response.status_code,
                    'content': 'Failed to fetch audio from AI4Bharat API'
                }

        except Exception as e:
            traceback.print_exc()
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {
                'status': 500,
                'content': str(e)
            }