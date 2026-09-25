import logging
import os

import jwt
from django.http import JsonResponse
from jwt import ExpiredSignatureError, InvalidTokenError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from chatbot.models import ChatSession, ChatStatus, Company, CompanyChat, Profile
from chatbot.models.company_models import CompanyBot, CompanyStateMachine
from chatbot.models.enums import OperationTypeChoices
from chatbot.utils.audio_provider_utils import text_translate_provider
from chatbot.utils.chat_utils import get_ai_profile, build_validation_response
from chatbot.utils.ptm_utils.chat_utils import save_question_answer_utils

JWT_PUBLIC_KEY = os.getenv("JWT_PUBLIC_KEY")

logger = logging.getLogger("django")


@api_view(['POST'])
def save_chats_view(request):
    body = request.data
    message = body.get('message')
    session = body.get('session')
    status = body.get('status', 'COMPLETED')
    role = body.get('role')
    chunks = body.get('chunks')
    user_profile = None
    if not message or not session:
        return Response({"error": "message and session are required."}, status=400)

    print("message: ", message)

    try:
        ai_user = get_ai_profile()
    except Profile.DoesNotExist:
        return Response({"error": "AI profile not found."}, status=400)

    try:
        chat_session = ChatSession.objects.get(session=session)
        if chat_session:
            user_profile = chat_session.profile
    except ChatSession.DoesNotExist:
        return Response({"error": "chat_session not found."}, status=400)


    if role == 'bot':
        sender = ai_user
        receiver = user_profile
    elif role == 'user':
        sender = user_profile
        receiver = ai_user
    else:
        return Response({"error": "Invalid role. Must be 'bot' or 'user'."}, status=400)

    CompanyChat.objects.create(
        message=message,
        session=session,
        status=status,
        sender=sender,
        receiver=receiver,
        chunks=chunks
    )


    return Response({
        'status': 'ok',
        'message': 'Message saved successfully!'
    }, status=200)


@api_view(['POST'])
def create_chatsession(request):
    body = request.data
    session = body.get('session')
    email = body.get('email')
    preferred_language =  body.get('preferred_language', {}).get('value')

    access_token = request.headers.get("X-auth-token")
    if not access_token:
        return JsonResponse({"message": "Access token missing"}, status=401)

    try:
        decoded = jwt.decode(
            access_token,
            JWT_PUBLIC_KEY,
            algorithms=["HS256"]
        )
        user_id = decoded.get("data", {}).get("id")
        first_name = decoded.get("data", {}).get("name")
        user_roles = decoded.get("roles", [])

    except ExpiredSignatureError:
        return JsonResponse({"message": "Access token expired"}, status=401)

    except InvalidTokenError:
        return JsonResponse({"message": "Invalid access token"}, status=401)


    if not session:
        return Response({"error": "session is required."}, status=400)

    if not email:
        return Response({"error": "Email is required."}, status=400)

    try:
        company = Company.objects.get(slug='shikshalokamstaging')
    except Exception as e:
        return Response({"error": f"{e}"}, status=400)

    profile, created = Profile.objects.get_or_create(
        userid = user_id,
        defaults={
            'first_name': first_name,
            'email': email,
            'password': 'grit@123',
            'preferred_route': preferred_language,
            'company': company,
            "designation": user_roles
        }
    )

    bot_route = body.get("bot_route")
    language = body.get("language", "en")

    c, created = (
        ChatSession.objects.select_related("company_bot")
        .only(
            "id",
            "session",
            "session_status",
            "current_step",
            "session_type",
            "profile_id",
            "company_bot__id",
        )
        .get_or_create(
            session=session,
            defaults={
                "session_status": ChatStatus.IN_PROGRESS,
                "profile": profile,
            },
        )
    )

    first_bot_question = None
    translated_bot_question = None
    current_step = c.current_step
    first_operation_type = None

    company_bot = c.company_bot
    if not company_bot and bot_route:
        company_bot = CompanyBot.objects.filter(route=bot_route).first()
        if company_bot and not c.company_bot:
            c.company_bot = company_bot
            c.save(update_fields=["company_bot"])

    first_bot_audio_s3_url = None
    first_validations = None

    if company_bot:
        step = c.current_step if c.current_step is not None else 0
        first_state = CompanyStateMachine.objects.filter(
            company_bot=company_bot, step=step
        ).first()

        if first_state:
            first_bot_question = first_state.bot_question
            first_operation_type = first_state.operation_type
            # Always check for cached audio (including English)
            cached = (first_state.translations or {}).get(language, {})
            first_bot_audio_s3_url = cached.get("audio_s3")
            if language and language != "en" and first_bot_question:
                translated_bot_question = cached.get("text")
                if not translated_bot_question:
                    try:
                        translation_result = text_translate_provider(
                            message_body=first_bot_question,
                            target_language=language,
                            source_language="en",
                            company_bot=company_bot,
                        )
                        if (
                            translation_result
                            and translation_result.get("status") == 200
                        ):
                            translated_bot_question = translation_result.get("content")
                    except Exception as e:
                        logger.info(f"Translation failed for first_bot_question: {e}")

            first_validations = build_validation_response({
                "validate_method": first_state.validate_method,
                "validation_type": first_state.validation_type,
                "render_as": first_state.render_as,
                "error_message": first_state.error_message,
                "validation_config": first_state.validation_config,
                "translations": first_state.translations,
                "min_choices": first_state.min_choices,
                "max_choices": first_state.max_choices,
            }, language=language)

    logger.info(
        f"create_chatsession: session={session}, created={created}, first_bot_question={bool(first_bot_question)}"
    )

    return Response(
        {
            "status": "ok",
            "message": "Chatsession created!"
            if created
            else "Chatsession already exists!",
            "chatsession": {
                "session": c.session,
                "session_status": c.session_status,
                "profile_id": profile.id,
            },
            # Backward-compatible keys
            "first_bot_question": first_bot_question,
            "translated_bot_question": translated_bot_question,
            "first_bot_audio_s3_url": first_bot_audio_s3_url,
            "current_step": current_step,
            "operation_type": first_operation_type,
            # Uniform keys matching non_llm_chat_view response shape
            "bot_message": first_bot_question,
            "translated_bot_message": translated_bot_question,
            "audio_s3_url": first_bot_audio_s3_url,
            "step": current_step,
            "is_new_session": created,
            "validations": first_validations,
        },
        status=200,
    )


@api_view(['POST'])
def save_ptm_chats(request):
    body = request.data
    session = body.get('session')
    status = body.get('status', 'COMPLETED')
    flow = body.get('flow')
    profile_id = body.get('profile_id')
    question_id = body.get('id')
    answer_id = body.get('answer_id')
    sequence = body.get('sequence')
    question = body.get('question')
    translated_message = body.get('translated_question')
    answer = body.get('answer')
    language = body.get('language')
    sent_at = body.get('sent_at')
    audio_file = body.get('audio_url')
    service = body.get('service')
    # should_transliterate = body.get('should_transliterate', False)

    if not question or not session or not answer:
        return Response({"error": "question, answer and session are required."}, status=400)

    res = save_question_answer_utils(
        profile_id=profile_id, flow=flow, session=session, sequence=sequence, status=status,
        language=language, question_id=question_id, sent_at=sent_at, question=question,
        translated_message=translated_message, answer=answer,
        audio_file=audio_file, answer_id=answer_id, service=service
        # should_transliterate=should_transliterate,
    )

    if res.get("status") != 200:
        return Response(res, status=res.get("status"))

    # if status == "COMPLETED":
    #     create_ptm_report.delay(
    #         profile_id=profile_id,
    #         session=session,
    #         flow=flow,
    #         language=language
    #     )

    return Response(
        {"status": "ok", "message": res.get("message", "Message saved successfully!")},
        status=200,
    )


@api_view(["POST"])
def non_llm_chat_view(request):
    """
    Guest HTTP endpoint for NON_LLM state machine steps.
    No auth required. Saves user message, advances step, returns next bot question.
    """

    body = request.data
    session = body.get("session")
    profile_id = body.get("profile_id")
    message = body.get("message")
    asr_audio = body.get("asr_audio")
    language = body.get("language", "en")
    flow_name = body.get("flow_name", None)
    company_bot = body.get("company_bot", None)

    missing = [
        f
        for f, v in [
            ("session", session),
            ("message", message),
        ]
        if not v
    ]
    if missing:
        return Response({"error": f"{', '.join(missing)} are required."}, status=400)

    is_new_session = False
    try:
        chat_session = (
            ChatSession.objects
            .only(
                "id",
                "session",
                "session_status",
                "current_step",
                "session_type",
                "profile_id",
                "company_bot_id",
            )
            .get(session=session)
        )
    except ChatSession.DoesNotExist:
        resolved_company_bot = None
        if company_bot:
            resolved_company_bot = CompanyBot.objects.filter(id=company_bot).first()
            if not resolved_company_bot:
                return Response({"error": "Invalid company_bot."}, status=400)

        chat_session = ChatSession(
            profile_id=profile_id,
            current_step=1,
            language=language,
            company_bot=resolved_company_bot,
            session_status=ChatStatus.IN_PROGRESS,
            session_type=flow_name,
            session=session,
        )
        chat_session.save()
        is_new_session = True

    if chat_session.session_status == ChatStatus.COMPLETED:
        return Response({ "error": "Chat session is already marked as completed" }, status=400)

    company_bot_id = chat_session.company_bot_id

    if not company_bot_id:
        return Response({"error": "No bot configured for this session."}, status=400)

    current_step = (
        chat_session.current_step if chat_session.current_step is not None else 0
    )

    next_step = current_step + 1
    next_to_next_step = current_step + 2
    state_machines = CompanyStateMachine.objects.filter(
        company_bot_id=company_bot_id, step__in=(current_step, next_step, next_to_next_step)
    ).values(
        "step", "name", "operation_type", "bot_question", "translations",
        "validate_method", "validation_type", "render_as", "error_message",
        "validation_config", "min_choices", "max_choices",
    )

    states = {}
    for state in state_machines:
        states[state["step"]] = state

    state_machine = states.get(current_step)
    if not state_machine:
        return Response(
            {"error": f"No state machine found for step {current_step}."}, status=400
        )

    if state_machine["operation_type"] != OperationTypeChoices.NON_LLM:
        return Response({"error": "Current step is not a NON_LLM step."}, status=400)

    sender_id: int | None = None
    if profile_id:
        try:
            sender_id = Profile.objects.values_list("id", flat=True).get(id=profile_id)
        except Profile.DoesNotExist:
            sender_id = chat_session.profile_id
    else:
        sender_id = chat_session.profile_id

    if sender_id is None:
        return Response({"error": "No profile resolved for this session."}, status=400)

    try:
        ai_profile = get_ai_profile()
    except Profile.DoesNotExist:
        return Response({"error": "AI profile not found."}, status=400)

    chat_session.current_step = next_step
    next_state = states.get(next_step)
    next_to_next_state = states.get(next_to_next_step)

    if not next_state:
        return Response({
            "error": "Chat session is already marked as completed"
        }, status=400)

    translated_user_message = None
    if language and language != "en" and message:
        try:
            translation_result = text_translate_provider(
                message_body=message,
                target_language="en",
                source_language=language,
                company_bot=company_bot_id,
            )
            if translation_result and translation_result.get("status") == 200:
                translated_user_message = translation_result.get("content")
        except Exception as e:
            logger.info(
                f"non_llm_chat_view: translation failed for user answer session={session}: {e}"
            )

    company_chat = CompanyChat.objects.create(
        message=message,
        translated_message=translated_user_message,
        session=session,
        status=ChatStatus.COMPLETED,
        sender_id=sender_id,
        receiver=ai_profile,
        stage=state_machine["name"],
        file_url=asr_audio,
    )

    logger.info(
        f"non_llm_chat_view: saved CompanyChat id={company_chat.id} for session={session} step={current_step}"
    )

    update_fields = ["current_step"]
    if not next_to_next_state:
        chat_session.session_status = ChatStatus.COMPLETED
        update_fields.append("session_status")
        logger.info(
            f"non_llm_chat_view: session={session} completed at step={next_step}"
        )

    chat_session.save(update_fields=update_fields)

    bot_message = next_state["bot_question"]
    translated_bot_message = None
    audio_s3_url = None

    cached = (next_state["translations"] or {}).get(language, {})
    audio_s3_url = cached.get("audio_s3")
    if language and language != "en" and bot_message:
        translated_bot_message = cached.get("text")
        if not translated_bot_message:
            try:
                translation_result = text_translate_provider(
                    message_body=bot_message,
                    target_language=language,
                    source_language="en",
                    company_bot=company_bot,
                )
                if translation_result and translation_result.get("status") == 200:
                    translated_bot_message = translation_result.get("content")
            except Exception as e:
                logger.info(
                    f"non_llm_chat_view: translation failed for step {next_step}: {e}"
                )

    CompanyChat.objects.create(
        message=bot_message,
        translated_message=translated_bot_message,
        session=chat_session.session,
        status=chat_session.session_status,
        sender=ai_profile,
        receiver=chat_session.profile,
        stage=next_state["name"],
    )

    logger.info(
        f"non_llm_chat_view: session={session} advancing to step={next_step} operation_type={next_state['operation_type']}"
    )

    validations = build_validation_response(next_state, language=language)

    return Response(
        {
            "is_complete": False if next_to_next_state is not None else True,
            "step": next_step,
            "bot_message": bot_message,
            "translated_bot_message": translated_bot_message,
            "audio_s3_url": audio_s3_url,
            "operation_type": next_state["operation_type"],
            "is_new_session": is_new_session,
            "validations": validations,
        },
        status=200,
    )
