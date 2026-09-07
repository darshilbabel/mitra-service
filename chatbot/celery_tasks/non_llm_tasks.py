import base64
import logging
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.db import transaction

S3_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
)

from chatbot.models import CompanyBot, Voice, VoiceType
from chatbot.models.bot_vernacular_model import BotVernacular
from chatbot.models.company_models import CompanyStateMachine
from chatbot.models.enums import OperationTypeChoices
from chatbot.utils.audio_provider_utils import text_speech_provider, text_translate_provider
from shikshalokam_mohini.celery_config import app

logger = logging.getLogger("django")

S3_MEDIA_URL = os.getenv("S3_MEDIA_URL", "")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
AWS_REGION = os.getenv("AWS_REGION", "")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")


def _upload_audio_to_s3(audio_bytes, company_bot_id, state_machine_id, lang, audio_format):
    """Upload audio bytes to S3, return full URL or None."""
    try:
        key = f"state_machine_audio/{company_bot_id}/{state_machine_id}/{lang}.{audio_format}"
        s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=S3_CLIENT_CONFIG,
        )
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=key,
            Body=audio_bytes,
            ContentType=f"audio/{audio_format}",
        )
        return f"{S3_MEDIA_URL}{key}"
    except Exception as e:
        logger.info(
            f"generate_translations: S3 upload failed for sm={state_machine_id} lang: {e}"
        )
        return None


def _delete_audio_from_s3(url):
    """Delete an S3 object given its full audio_s3 URL. Returns True if deleted or already absent."""
    if not S3_MEDIA_URL:
        logger.info("revoke_audio: S3_MEDIA_URL is not configured, skipping delete")
        return False
        
    if not url or not url.startswith(S3_MEDIA_URL):
        logger.info(f"revoke_audio: url does not match S3_MEDIA_URL prefix, skipping delete: {url}")
        return False
    key = url[len(S3_MEDIA_URL):]
    try:
        s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=S3_CLIENT_CONFIG,
        )
        s3.delete_object(Bucket=S3_BUCKET_NAME, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return True
        logger.info(f"revoke_audio: S3 delete failed for key={key}: {e}")
        return False
    except Exception as e:
        logger.info(f"revoke_audio: S3 delete failed for key={key}: {e}")
        return False


def revoke_state_machine_audio(state_machine_id):
    """
    Deletes every cached `audio_s3` file for a CompanyStateMachine row from S3, then strips
    `audio_s3` from `translations` for each language where the S3 delete succeeded (or the
    object was already gone). Languages whose S3 delete fails keep their `audio_s3` entry
    untouched so the revoke can be retried. A language left with no other keys after the
    strip is dropped from `translations` entirely.

    S3 deletes run outside any DB lock (network calls can be slow); the row is locked only
    to snapshot URLs up front and again to apply the diff. If another writer changed a
    language's audio_s3 in between (e.g. a fresh generate), that language's stored URL no
    longer matches the snapshot and is left alone even if the snapshot's URL was deleted.

    Returns (removed_langs, failed_langs).
    """
    with transaction.atomic():
        sm = CompanyStateMachine.objects.select_for_update().get(pk=state_machine_id)
        snapshot = {
            lang: lang_data["audio_s3"]
            for lang, lang_data in (sm.translations or {}).items()
            if isinstance(lang_data, dict) and lang_data.get("audio_s3")
        }

    if not snapshot:
        return [], []

    removed_langs = []
    failed_langs = []
    for lang, url in snapshot.items():
        if _delete_audio_from_s3(url):
            removed_langs.append(lang)
        else:
            failed_langs.append(lang)

    if not removed_langs:
        return removed_langs, failed_langs

    with transaction.atomic():
        sm = CompanyStateMachine.objects.select_for_update().get(pk=state_machine_id)
        cached = dict(sm.translations or {})
        for lang in removed_langs:
            lang_data = cached.get(lang)
            if not isinstance(lang_data, dict) or lang_data.get("audio_s3") != snapshot[lang]:
                continue
            lang_data = dict(lang_data)
            del lang_data["audio_s3"]
            if lang_data:
                cached[lang] = lang_data
            else:
                del cached[lang]
        sm.translations = cached or None
        sm.save(update_fields=["translations"])
        return removed_langs, failed_langs


def _resolve_generation_scope(company_bot_id, state_machine_id=None, language=None):
    """
    Shared setup for both translation and audio generation tasks: resolves the CompanyBot,
    the CompanyStateMachine rows in scope (single row / bulk with LLM-skip rule / language-filtered),
    the text-translation language list, and the TTS voice map.
    Returns None if the CompanyBot doesn't exist.
    """
    try:
        company_bot = CompanyBot.objects.get(id=company_bot_id)
    except CompanyBot.DoesNotExist:
        return None

    ttt_voices = Voice.objects.filter(company_bot=company_bot, type=VoiceType.TextToText)
    tts_voices = Voice.objects.filter(company_bot=company_bot, type=VoiceType.TextToSpeech)

    if language:
        ttt_languages = [language]
        tts_voices = tts_voices.filter(language=language)
    else:
        ttt_languages = list(ttt_voices.values_list("language", flat=True))

    tts_voice_map = {v.language: v for v in tts_voices}

    state_machines = CompanyStateMachine.objects.filter(company_bot=company_bot)
    op_type_by_step = None
    if state_machine_id:
        state_machines = state_machines.filter(pk=state_machine_id)
    else:
        state_machines = state_machines.order_by("step")
        op_type_by_step = dict(
            CompanyStateMachine.objects.filter(company_bot=company_bot).values_list("step", "operation_type")
        )

    return company_bot, state_machines, ttt_languages, tts_voice_map, op_type_by_step


def _skip_state_machine(sm, op_type_by_step):
    """LLM-skip rule: in bulk mode, a step whose predecessor isn't NON_LLM is left untouched."""
    if not sm.bot_question and sm.step != 1:
        return True
    if op_type_by_step is not None and sm.operation_type == OperationTypeChoices.LLM:
        prev_op_type = op_type_by_step.get(sm.step - 1)
        if prev_op_type != OperationTypeChoices.NON_LLM:
            return True
    return False


def _load_vernacular_map(company_bot):
    bot_vernaculars = list(
        BotVernacular.objects.values("language", "alt_introductory_message").filter(company_bot=company_bot)
    )
    vernacular_map = {}
    vernacular_lang_counts = {}
    for v in bot_vernaculars:
        vernacular_lang_counts[v["language"]] = vernacular_lang_counts.get(v["language"], 0) + 1
        if v["language"] not in vernacular_map and v["alt_introductory_message"]:
            vernacular_map[v["language"]] = v["alt_introductory_message"]
    for lang, count in vernacular_lang_counts.items():
        if count > 1:
            logger.info(
                f"generate_audio: multiple BotVernacular rows for company_bot={company_bot.id} lang={lang}, using first"
            )
    return vernacular_map


def _merge_translations_field(state_machine_id, field, lang_values):
    """Lock the row, merge `field` -> value for each lang into its cached translations, save."""
    if not lang_values:
        return
    with transaction.atomic():
        sm = CompanyStateMachine.objects.select_for_update().get(pk=state_machine_id)
        cached = dict(sm.translations or {})
        for lang, value in lang_values.items():
            lang_data = dict(cached.get(lang, {}))
            lang_data[field] = value
            cached[lang] = lang_data
        sm.translations = cached
        sm.save(update_fields=["translations"])


@app.task
def generate_state_machine_translations(company_bot_id, state_machine_id=None, language=None, generate_audio=False):
    """
    Generate cached text translations for CompanyStateMachines on a bot.
    If state_machine_id is provided, only process that single row (used by the per-row admin button).
    If language is provided, only process that language (used for new-language auto-trigger).
    If generate_audio is True, also chains generate_state_machine_audio for the same scope.
    """
    scope = _resolve_generation_scope(company_bot_id, state_machine_id, language)
    if scope is None:
        logger.info(f"generate_translations: CompanyBot id={company_bot_id} not found")
        return
    company_bot, state_machines, languages, _tts_voice_map, op_type_by_step = scope

    for sm in state_machines:
        if sm.step == 1:
            continue
        if _skip_state_machine(sm, op_type_by_step):
            continue

        lang_texts = {}
        for lang in languages:
            if lang == "en":
                continue
            try:
                result = text_translate_provider(
                    message_body=sm.bot_question,
                    target_language=lang,
                    source_language="en",
                    company_bot=company_bot,
                )
                if result and result.get("status") == 200:
                    lang_texts[lang] = result["content"]
            except Exception as e:
                logger.info(f"generate_translations: text translation failed sm={sm.id} lang={lang}: {e}")

        if not lang_texts:
            continue

        _merge_translations_field(sm.pk, "text", lang_texts)
        logger.info(f"generate_translations: updated sm={sm.id} languages={list(lang_texts.keys())}")

    if generate_audio:
        generate_state_machine_audio.delay(company_bot_id, state_machine_id=state_machine_id, language=language)


@app.task
def generate_state_machine_audio(company_bot_id, state_machine_id=None, language=None):
    """
    Generate cached TTS audio for CompanyStateMachines on a bot, stored under the `audio_s3`
    key in the translations column. For step 1, audio comes from BotVernacular's
    alt_introductory_message. For other steps, English audio comes from bot_question directly;
    other languages are only generated if a cached translation already exists for them.
    If state_machine_id is provided, only process that single row.
    If language is provided, only process that language.
    """
    scope = _resolve_generation_scope(company_bot_id, state_machine_id, language)
    if scope is None:
        logger.info(f"generate_audio: CompanyBot id={company_bot_id} not found")
        return
    company_bot, state_machines, _languages, tts_voice_map, op_type_by_step = scope

    if not tts_voice_map:
        return

    vernacular_map = None

    for sm in state_machines:
        if _skip_state_machine(sm, op_type_by_step):
            continue

        cached = dict(sm.translations or {})

        if sm.step == 1:
            if vernacular_map is None:
                vernacular_map = _load_vernacular_map(company_bot)
            lang_texts = {
                lang: vernacular_map[lang] for lang in tts_voice_map if lang in vernacular_map
            }
        else:
            lang_texts = {}
            for lang in tts_voice_map:
                if lang == "en":
                    lang_texts[lang] = sm.bot_question
                else:
                    lang_data = cached.get(lang)
                    cached_text = lang_data.get("text") if isinstance(lang_data, dict) else None
                    if cached_text:
                        lang_texts[lang] = cached_text

        if not lang_texts:
            continue

        lang_audio = {}
        for lang, text in lang_texts.items():
            tts_voice = tts_voice_map.get(lang)
            try:
                tts_result = text_speech_provider(
                    company_bot=company_bot,
                    text=text,
                    source_language=lang,
                )
                if tts_result and tts_result.get("status") == 200:
                    audio_b64 = tts_result["content"]
                    if ";base64," in audio_b64:
                        audio_b64 = audio_b64.split(";base64,", 1)[1]
                    audio_bytes = base64.b64decode(audio_b64)
                    audio_format = "wav"
                    if tts_voice and tts_voice.other_params:
                        audio_format = tts_voice.other_params.get("output_audio_codec", "wav")
                    url = _upload_audio_to_s3(audio_bytes, company_bot_id, sm.id, lang, audio_format)
                    if url:
                        lang_audio[lang] = url
            except Exception as e:
                logger.info(f"generate_audio: TTS failed sm={sm.id} lang={lang}: {e}")

        if not lang_audio:
            continue

        _merge_translations_field(sm.pk, "audio_s3", lang_audio)
        logger.info(f"generate_audio: updated sm={sm.id} languages={list(lang_audio.keys())}")
