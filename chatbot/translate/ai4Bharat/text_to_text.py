import os
import traceback
import requests
import logging

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

ai4bharat_api_key = os.getenv("BHASHANI_API_KEY")
ai4bharat_base_url = os.getenv("BHASHANI_BASE_URL")
ai4bharat_user_id = os.getenv("BHASHANI_USER_ID")
ai4bharat_authorization = os.getenv("BHASHANI_AUTHORIZATION")
logger = logging.getLogger('django')
langfuse = get_langfuse_client()


def call_ai4bharat_translation_api(voice_provider, source_language, target_language, message_body):
    other_params = voice_provider.other_params if voice_provider.other_params else {}
    service_id = other_params.get('serviceId', 'bhashini/iiith/nmt-all')

    with langfuse.start_as_current_observation(
        as_type="generation",
        name="ai4bharat_translate",
        model=service_id,
        input={
            "message_preview": message_body[:200] if message_body else None,
            "source_language": source_language,
            "target_language": target_language,
        },
    ) as gen:
        api_url = ai4bharat_base_url

        payload = {
            "pipelineTasks": [
                {
                    "taskType": "translation",
                    "config": {
                        "language": {
                            "sourceLanguage": source_language,
                            "targetLanguage": target_language,
                        },
                        "serviceId": service_id,
                    }
                }
            ],
            "inputData": {
                "input": [
                    {
                        "source": message_body
                    }
                ]
            }
        }

        headers = {
            'accept': '*/*',
            'content-type': 'application/json',
            'Authorization': ai4bharat_authorization,
        }

        char_count = len(message_body) if message_body else 0
        # usage_details, cost_details = compute_translate_usage_and_cost("ai4bharat", char_count)
        usage_details, cost_details = compute_translate_usage_and_cost(
                                         service_id, char_count,
                                          voice_provider=voice_provider,
                                          company_bot=getattr(voice_provider, 'company_bot', None),
                                      )
        try:
            request_timeout = other_params.get("request_timeout", 30)
            try:
                request_timeout = float(request_timeout)
            except Exception:
                request_timeout = 30

            response = requests.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=request_timeout
            )
            print("Response: ", response)
            print("Res text: ", response.json())
            logger.info(f"Response from AI4Bharat Text Translation {response}")
            logger.info(f"JSON Response from AI4Bharat Text Translation {response.json()}")

            if response.status_code == 200:
                translated_data = response.json()
                if isinstance(translated_data, dict) and 'pipelineResponse' in translated_data:
                    translated_message = translated_data['pipelineResponse'][0].get('output', [{}])[0].get('target', '')

                    print("translated_message: ", translated_message)
                    gen.update(
                        output={"translated_preview": translated_message[:200]},
                        usage_details=usage_details,
                        cost_details=cost_details,
                    )
                    return {
                        'status': 200,
                        'content': translated_message
                    }

            gen.update(
                output={"status": "fallback", "message": "no_pipelineResponse"},
                usage_details=usage_details,
                cost_details=cost_details,
            )
            return {
                'status': 200,
                'content': message_body
            }
        except Exception as e:
            logger.error('Error processing: %s', e, exc_info=True)
            print(f"Error during translation API call: {str(e)}")
            traceback.print_exc()
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {
                'status': 500,
                'content': f"Error during translation API call: {str(e)}"
            }