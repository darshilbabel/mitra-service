import json

from django.core.cache import cache

from chatbot.constants.cache import CacheKeyEnum
from chatbot.models import CompanyChat, LLMProvider, Profile


def format_message_as_per_openai_format(chats, intro=None):
    ai_user = Profile.objects.values("id").get(id=1)
    if intro:
        messages = [
            {
                'role': 'user',
                'content': "Hello"
            }, {
                'role': 'assistant',
                'content': intro
            }
        ]
    else:
        messages = []
    for chat in chats:
        chat_receiver = None
        chat_message = None
        chat_translated_message = None

        # variable instialisation
        if isinstance(chat, CompanyChat):
            chat_receiver = getattr(chat.receiver, 'id', None)
            chat_message = getattr(chat, 'message', None)
            chat_translated_message = getattr(chat, 'translated_message', None)

        elif isinstance(chat, dict):
            chat_receiver = chat.get("receiver")
            chat_message = chat.get("message")
            chat_translated_message = chat.get("translated_message")

        if chat_receiver == ai_user.get("id"):
            user_message = chat_message
            if chat_translated_message is not None and chat_translated_message != '':
                user_message = chat_translated_message
            messages.append({
                'role': 'user',
                'content': user_message
            })
        else:
            messages.append({
                'role': 'assistant',
                "content": chat_message
            })
    return messages


def format_message_as_per_bedrock_format(chats, intro=None, other_info=None):
    ai_user = Profile.objects.values("id").get(id=1)
    if intro:
        if other_info:
            user_name = other_info.get('first_name', None)
            user_location = other_info.get('user_location', None)
            if user_name:
                initial_msg = f"Hello my name is {user_name}."
                if user_location:
                    initial_msg += f" I am from {user_location}"
            else:
                initial_msg = "Hello"
        else:
            initial_msg = "Hello"
        messages = [
            {
                'role': 'user',
                'content': [{'text': initial_msg}]
            }, {
                'role': 'assistant',
                'content': [{'text': intro}]
            }
        ]
    else:
        messages = []
    for chat in chats:
        chat_receiver = None
        chat_message = None
        chat_translated_message = None

        # variable instialisation
        if isinstance(chat, CompanyChat):
            chat_receiver = getattr(chat.receiver, 'id', None)
            chat_message = getattr(chat, 'message', None)
            chat_translated_message = getattr(chat, 'translated_message', None)

        elif isinstance(chat, dict):
            chat_receiver = chat.get("receiver")
            chat_message = chat.get("message")
            chat_translated_message = chat.get("translated_message")

        if chat_receiver == ai_user.get("id"):
            user_message = chat_message
            if chat_translated_message is not None and chat_translated_message != '':
                user_message = chat_translated_message
            messages.append({
                'role': 'user',
                'content': [{'text': user_message}]
            })
        else:
            messages.append({
                'role': 'assistant',
                "content": [{'text': chat_message}]
            })

    if not messages or messages[0].get('role') != 'user':
        messages.insert(0, {
            'role': 'user',
            'content': [{'text': 'Hello'}]
        })

    return messages


def get_guided_chat(company_bot, company_chats, intro=None, other_info=None):
    messages = []
    if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
        messages = format_message_as_per_bedrock_format(chats=company_chats, intro=intro, other_info=other_info)
    elif company_bot.provider == LLMProvider.OPENAI:
        messages = format_message_as_per_openai_format(chats=company_chats, intro=intro)

    return messages


def convert_llama_to_openai_tool(llama_tool_call):
    try:
        tool_spec = llama_tool_call.get("toolConfig", {}).get("tools", [])[0].get("toolSpec", {})

        if not tool_spec:
            raise ValueError("Invalid toolSpec structure in the provided Llama tool call.")

        parameters = tool_spec.get("inputSchema", {}).get("json", {})

        if "properties" in parameters:
            for key, value in parameters["properties"].items():
                if value.get("type") == "array" and "items" not in value:
                    value["items"] = {"type": "string"}

        openai_tool = [
            {
                "type": "function",
                "function": {
                    "name": tool_spec.get("name"),
                    "description": tool_spec.get("description"),
                    "parameters": parameters
                }
            }
        ]

        print("Converted OpenAI tool:", json.dumps(openai_tool, indent=4))
        return openai_tool

    except Exception as e:
        print("Error converting tool:", str(e))
        return None


def get_ai_profile():
    """Return AI Profile (id=1), from cache if present, else DB."""
    cached_profile = cache.get(CacheKeyEnum.AI_PROFILE)
    if cached_profile:
        print("Cache hit")
        return cached_profile
    profile = Profile.objects.get(id=1)
    cache.set(CacheKeyEnum.AI_PROFILE, profile, timeout=1000)
    return profile
    # return Profile.objects.get(id=1)


def _get_field(state_data, field):
    """Read a field from either a model instance or a .values() dict."""
    if isinstance(state_data, dict):
        return state_data.get(field)
    return getattr(state_data, field, None)


def build_validation_response(state_data, language="en"):
    """
    Build the validations dict for the FE API response.

    Accepts either a CompanyStateMachine model instance or a .values() dict
    with the relevant fields. All translations (choices, errors) are read
    from the sm.translations JSON field. English source text comes from
    validation_config and error_message model fields.

    Returns dict matching FE validations spec, or None if no validation configured.
    """
    from chatbot.models.enums import ValidateMethodChoices

    validate_method = _get_field(state_data, "validate_method")
    if not validate_method or validate_method == ValidateMethodChoices.NONE:
        return None

    translations = _get_field(state_data, "translations") or {}
    lang_translations = translations.get(language, {})
    en_translations = translations.get("en", {})

    # ── Error messages ──
    error_message_list = _get_field(state_data, "error_message") or []
    translated_errors = lang_translations.get("errors", {})
    en_errors = en_translations.get("errors", {})

    error_messages = []
    for entry in error_message_list:
        key = entry.get("key")
        if not key:
            continue
        en_text = (entry.get("labels", {}).get("en") or {}).get("text", "")

        if language != "en" and key in translated_errors:
            text = translated_errors[key].get("text", en_text)
            audio = translated_errors[key].get("audio_s3")
        else:
            text = en_text
            audio = en_errors.get(key, {}).get("audio_s3")

        error_messages.append({"key": key, "text": text, "audio_s3_url": audio})

    # Add default entry if not explicitly present
    if error_messages and not any(e["key"] == "default" for e in error_messages):
        first = error_messages[0]
        error_messages.append({
            "key": "default",
            "text": first["text"],
            "audio_s3_url": first["audio_s3_url"],
        })

    # ── Choices ──
    validation_config = _get_field(state_data, "validation_config") or {}
    translated_choices = lang_translations.get("choices", {})
    en_choices = en_translations.get("choices", {})

    choices = []
    for choice in validation_config.get("choices", []):
        key = choice.get("key")
        label = choice.get("label")
        if not key:
            continue

        if language != "en" and key in translated_choices:
            display_label = translated_choices[key].get("text") or label
            audio = translated_choices[key].get("audio_s3")
        else:
            display_label = label
            audio = en_choices.get(key, {}).get("audio_s3")

        choices.append({
            "key": key,
            "text": display_label,
            "audio_s3_url": audio,
        })

    config = {}
    if choices:
        config["choices"] = choices
    min_choices = _get_field(state_data, "min_choices")
    max_choices = _get_field(state_data, "max_choices")
    if min_choices is not None:
        config["min_choices"] = min_choices
    if max_choices is not None:
        config["max_choices"] = max_choices

    return {
        "method": validate_method,
        "type": _get_field(state_data, "validation_type"),
        "render_as": _get_field(state_data, "render_as"),
        "error_message": error_messages,
        "config": config,
    }
