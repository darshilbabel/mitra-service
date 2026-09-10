import os
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from chatbot.models import CompanyBot, Voice, VoiceType
from chatbot.translate.ai4Bharat.text_lang_detect import (
    call_ai4bharat_text_lang_detect_api,
)
from chatbot.utils.audio_converter_utils import convert_s3_audio_to_wav_base64
from chatbot.utils.audio_provider_utils import (
    text_speech_provider,
    speech_text_provider,
    text_translate_provider,
)
from chatbot.utils.transliterate_utils import transliterate_text
from chatbot.utils.langfuse_client import get_langfuse_client

from langfuse import observe, propagate_attributes


logger = logging.getLogger("django")

langfuse = get_langfuse_client()

ai4bharat_api_key = os.getenv("BHASHANI_API_KEY")


@api_view(["POST"])
@observe(as_type="span", name="text_speech_view", capture_input=False, capture_output=False)
def text_speech_view(request):
    try:
        print("text_speech_view called")
        body = request.data

        text = body.get("text", "")
        source_language = body.get("source_language", "en")
        route = body.get("route")

        if not route:
            return Response(
                {
                    "status": "error",
                    "message": "route is a required field",
                },
                status=500,
            )

        langfuse.update_current_span(
            input={
                "text": text,
            },
            metadata={
                "route": str(route),
                "source_language": str(source_language),
            },
        )

        with propagate_attributes(
            tags=[f"bot_route:{route}"],
        ):
            company_bot = CompanyBot.objects.filter(route=route).first()

            response = text_speech_provider(
                company_bot=company_bot,
                text=text,
                source_language=source_language,
            )

            if response.get("status") == 200:
                langfuse.update_current_span(
                    output={
                        "status": "ok",
                    }
                )

                return Response(
                    {
                        "status": "ok",
                        "audio": response.get("content"),
                    },
                    status=200,
                )

            langfuse.update_current_span(
                output={
                    "status": "error",
                    "message": response.get("content"),
                }
            )

            return Response(
                {
                    "status": "error",
                    "message": response.get("content"),
                },
                status=response.get("status"),
            )

    except Exception as e:
        logger.error("Error in text_speech_view: %s", e, exc_info=True)

        langfuse.update_current_span(
            output={
                "status": "error",
                "error": str(e),
            }
        )

        return Response(
            {
                "status": "error",
                "message": str(e),
            },
            status=500,
        )


@api_view(["POST"])
@observe(as_type="span", name="speech_text", capture_input=False, capture_output=False)
def speech_text(request):
    try:
        body = request.data

        s3_url = body.get("s3Url")
        audio_format = body.get("audio_format", "wav")
        source_language = body.get("source_language", "en")
        route = body.get("route")

        if not route:
            return Response(
                {
                    "status": "error",
                    "message": "route is a required field",
                },
                status=500,
            )

        langfuse.update_current_span(
            input={
                "audio_format": audio_format,
                "source_language": source_language,
                "s3_url": s3_url,
            },
            metadata={
                "route": str(route),
                "audio_format": str(audio_format),
                "source_language": str(source_language),
            },
        )

        with propagate_attributes(
            tags=[f"bot_route:{route}"],
        ):
            company_bot = CompanyBot.objects.filter(route=route).first()

            if not company_bot:
                company_bot = CompanyBot.objects.filter(
                    route="/common_bot"
                ).first()

            encoded_audio = convert_s3_audio_to_wav_base64(
                s3_url=s3_url
            )

            response = speech_text_provider(
                company_bot=company_bot,
                base64=encoded_audio,
                audio_format=audio_format,
                source_language=source_language,
            )

            if response.get("status") == 200:
                langfuse.update_current_span(
                    output={
                        "status": "ok",
                        "transcript": response.get("content"),
                    }
                )

                return Response(
                    {
                        "status": "ok",
                        "transcript": response.get("content"),
                    },
                    status=200,
                )

            langfuse.update_current_span(
                output={
                    "status": "error",
                    "message": response.get("content"),
                }
            )

            return Response(
                {
                    "status": "error",
                    "message": response.get("content"),
                },
                status=response.get("status"),
            )

    except Exception as e:
        logger.error("Error in speech_text: %s", e, exc_info=True)

        langfuse.update_current_span(
            output={
                "status": "error",
                "error": str(e),
            }
        )

        return Response(
            {
                "status": "error",
                "message": str(e),
            },
            status=500,
        )


@api_view(["POST"])
@observe(as_type="span", name="text_translation_view", capture_input=False, capture_output=False)
def text_translation_view(request):
    try:
        body = request.data

        source_language = body.get("source_language", "en")
        target_language = body.get("target_language", "en")
        message_body = body.get("message_body")
        route = body.get("route")

        if not route:
            return Response(
                {
                    "status": "error",
                    "message": "route is a required field",
                },
                status=500,
            )

        langfuse.update_current_span(
            input={
                "message_body": message_body,
            },
            metadata={
                "route": str(route),
                "source_language": str(source_language),
                "target_language": str(target_language),
            },
        )

        with propagate_attributes(
            tags=[f"bot_route:{route}"],
        ):
            company_bot = CompanyBot.objects.filter(
                route=route
            ).first()

            response = text_translate_provider(
                company_bot=company_bot,
                message_body=message_body,
                target_language=target_language,
                source_language=source_language,
            )

            if response.get("status") == 200:
                langfuse.update_current_span(
                    output={
                        "status": "ok",
                        "transcript": response.get("content"),
                    }
                )

                return Response(
                    {
                        "status": "ok",
                        "transcript": response.get("content"),
                    },
                    status=200,
                )

            langfuse.update_current_span(
                output={
                    "status": "error",
                    "message": response.get("content"),
                }
            )

            return Response(
                {
                    "status": "error",
                    "message": response.get("content"),
                },
                status=response.get("status"),
            )

    except Exception as e:
        logger.error(
            "Error in text_translation_view: %s",
            e,
            exc_info=True,
        )

        langfuse.update_current_span(
            output={
                "status": "error",
                "error": str(e),
            }
        )

        return Response(
            {
                "status": "error",
                "message": str(e),
            },
            status=500,
        )


@api_view(["POST"])
@observe(as_type="span", name="text_transliterate_view", capture_input=False, capture_output=False)
def text_transliterate_view(request):
    try:
        body = request.data

        source_language = body.get("source_language", "en")
        target_language = body.get("target_language", "en")
        message_body = body.get("message_body")
        detect_language = body.get("detect_language", False)
        route = body.get("route")

        if not route:
            return Response(
                {
                    "status": "error",
                    "message": "route is a required field",
                },
                status=500,
            )

        langfuse.update_current_span(
            input={
                "message_body": message_body,
                "detect_language": detect_language,
            },
            metadata={
                "route": str(route),
                "source_language": str(source_language),
                "target_language": str(target_language),
                "detect_language": str(detect_language),
            },
        )

        with propagate_attributes(
            tags=[f"bot_route:{route}"],
        ):
            company_bot = CompanyBot.objects.filter(
                route=route
            ).first()

            if detect_language:
                detected_body = call_ai4bharat_text_lang_detect_api(
                    message_body=message_body
                )

                if detected_body and detected_body.get("content"):
                    source_language = detected_body.get("content")

                logger.info(
                    "detected_body: %s",
                    detected_body,
                )

            logger.info(
                "setting source_language: %s",
                source_language,
            )

            response = transliterate_text(
                company_bot=company_bot,
                message_body=message_body,
                target_language=target_language,
                source_language=source_language,
            )

            if response:
                langfuse.update_current_span(
                    output={
                        "status": "ok",
                        "transcript": response,
                    }
                )

                return Response(
                    {
                        "status": "ok",
                        "transcript": response,
                    },
                    status=200,
                )

            langfuse.update_current_span(
                output={
                    "status": "error",
                    "message": response,
                }
            )

            return Response(
                {
                    "status": "error",
                    "message": response,
                },
                status=500,
            )

    except Exception as e:
        logger.error(
            "Error in text_transliterate_view: %s",
            e,
            exc_info=True,
        )

        langfuse.update_current_span(
            output={
                "status": "error",
                "error": str(e),
            }
        )

        return Response(
            {
                "status": "error",
                "message": str(e),
            },
            status=500,
        )