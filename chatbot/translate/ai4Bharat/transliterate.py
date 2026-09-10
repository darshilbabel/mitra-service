import os
import traceback
import requests
from chatbot.translate.ai4Bharat.base_translation import get_service_id
from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost
import logging

ai4bharat_api_key = os.getenv("BHASHANI_API_KEY")
ai4bharat_base_url = os.getenv("BHASHANI_BASE_URL")
ai4bharat_user_id = os.getenv("BHASHANI_USER_ID")
ai4bharat_authorization = os.getenv("BHASHANI_AUTHORIZATION")
logger = logging.getLogger('django')
langfuse = get_langfuse_client()


def call_ai4bharat_transliterate_api(source_language, target_language, message_body, is_sentence=False,voice_provider=None):
    logger.info(f"Trying to transliterate {message_body}.")
    api_url = ai4bharat_base_url
    service_id = None
    pipeline_response = get_service_id(
        task_type='transliteration', source_language=source_language, target_language=target_language
    )
    if pipeline_response and pipeline_response.get('success'):
        service_id = pipeline_response.get('service_id', '')
        print("service_id: ", service_id)

    with langfuse.start_as_current_observation(
        as_type="generation",
        name="ai4bharat_transliterate",
        model=service_id or "ai4bharat_transliteration_unknown",
        input={
            "message_preview": message_body[:200] if message_body else None,
            "source_language": source_language,
            "target_language": target_language,
            "is_sentence": is_sentence,
        },
    ) as gen:
        payload = {
            "pipelineTasks": [
                {
                    "taskType": "transliteration",
                    "config": {
                        "language": {
                            "sourceLanguage": source_language,
                            "targetLanguage": target_language,
                        },
                        "serviceId": service_id,
                        "isSentence": is_sentence,
                        "numSuggestions": 7
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
            'userID': ai4bharat_user_id,
            'ulcaApiKey': ai4bharat_api_key
        }

        char_count = len(message_body) if message_body else 0
        # Provider-name keyed pricing (service_id varies by language pair, billing entity doesn't)
        # usage_details, cost_details = compute_translate_usage_and_cost("ai4bharat", char_count)
        usage_details, cost_details = compute_translate_usage_and_cost(
                                         service_id, char_count,
                                          voice_provider=voice_provider,
                                          company_bot=getattr(voice_provider, 'company_bot', None),
                                      )
        try:
            response = requests.post(api_url, json=payload, headers=headers, timeout=10)
            print("Response: ", response)
            print("Res text: ", response.json())
            logger.info(f"Response from AI4Bharat Transliteration: {response}")
            logger.info(f"JSON Response from AI4Bharat Transliteration: {response.json()}")

            if response.status_code == 200:
                transliteration_message_data = response.json()
                if isinstance(transliteration_message_data, dict) and 'pipelineResponse' in transliteration_message_data:
                    transliteration_message = transliteration_message_data['pipelineResponse'][0].get('output', [{}])[0].get('target', '')
                    print("transliteration: ", transliteration_message)
                    gen.update(
                        output={"transliterated_preview": transliteration_message[:200]},
                        usage_details=usage_details,
                        cost_details=cost_details,
                    )
                    return {
                        'status': 200,
                        'content': transliteration_message
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
            print(f"Error during transliteration API call: {str(e)}")
            logger.error(f"Error during transliteration API call: {str(e)}")
            traceback.print_exc()
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {
                'status': 500,
                'content': message_body
            }