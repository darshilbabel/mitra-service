import traceback
import threading
from google.cloud import translate
import logging
from google.oauth2 import service_account
from django.conf import settings

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

logger = logging.getLogger('django')
langfuse = get_langfuse_client()

_client_lock = threading.Lock()
_translation_client = None


def _get_client():
    global _translation_client
    if _translation_client is None:
        with _client_lock:
            if _translation_client is None:
                credentials = service_account.Credentials.from_service_account_file(settings.SECRETS_JSON_PATH)
                _translation_client = translate.TranslationServiceClient(credentials=credentials)
    return _translation_client


def translate_text(
    text: str,
    project_id: str,
    source_language_code: str,
    target_language_code: str,
    voice_provider: any,
):
    """Translating Text."""
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="google_translate",
        model="google-translate",
        input={
            "text_preview": text[:200] if text else None,
            "source_language_code": source_language_code,
            "target_language_code": target_language_code,
        },
    ) as gen:
        try:
            other_params = voice_provider.other_params if voice_provider.other_params else {}

            client = _get_client()

            location = other_params.get("location", "global")
            parent = f"projects/{project_id}/locations/{location}"
            glossary_id = other_params.get("glossary_id")

            request = {
                "parent": parent,
                "contents": [text],
                "mime_type": "text/plain",
                "source_language_code": source_language_code,
                "target_language_code": target_language_code,
            }

            if glossary_id:
                glossary = client.glossary_path(project_id, location, glossary_id)
                glossary_config = translate.TranslateTextGlossaryConfig(glossary=glossary)
                request["glossary_config"] = glossary_config

            response = client.translate_text(request=request)
            logger.info(f"Response from Google Text Translate: {response}")
            translations = response.glossary_translations if glossary_id else response.translations

            char_count = len(text) if text else 0
            # usage_details, cost_details = compute_translate_usage_and_cost("google-translate", char_count)
            usage_details, cost_details = compute_translate_usage_and_cost(
                                         "google-translate", char_count,
                                          voice_provider=voice_provider,
                                          company_bot=getattr(voice_provider, 'company_bot', None),
                                      )

            for translation in translations:
                gen.update(
                    output={"translated_preview": translation.translated_text[:200]},
                    usage_details=usage_details,
                    cost_details=cost_details,
                )
                return {
                    'status': 200,
                    'content': translation.translated_text
                }

            gen.update(output={"status": "no_translations_returned"}, usage_details=usage_details, cost_details=cost_details)

        except Exception as e:
            logger.error(f'Error processing Google Text Translate:{e}')
            print(f"Error during translation API call: {str(e)}")
            traceback.print_exc()
            gen.update(output=None, level="ERROR", status_message=str(e))
            return {
                'status': 500,
                'content': f"Error during translation API call: {str(e)}"
            }