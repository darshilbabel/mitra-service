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
        logger.warning(
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


def _resolve_generation_scope(company_bot_id, state_machine_id=None, language=None):
    """
    Shared setup for translation and audio generation tasks: resolves the CompanyBot,
    the CompanyStateMachine rows in scope, the text-translation language list, and the TTS voice map.
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
    """
    Decides whether to skip a state machine step for translation/audio generation.
    Step 1 is never skipped (it may carry intro content from BotVernacular).
    Other steps are skipped if they have no bot_question, no validation config,
    and no error messages. In bulk mode, also skips LLM steps whose predecessor
    isn't NON_LLM.
    """
    if sm.step == 1:
        return False
    has_content = bool(sm.bot_question) or bool(sm.validation_config) or bool(sm.error_message)
    if not has_content:
        return True
    if op_type_by_step is not None and sm.operation_type == OperationTypeChoices.LLM:
        prev_op_type = op_type_by_step.get(sm.step - 1)
        if prev_op_type != OperationTypeChoices.NON_LLM:
            return True
    return False


_NESTED_FIELDS = {"choices", "errors"}


def _merge_translations(state_machine_id, lang_updates):
    """
    Lock the row and merge per-language updates into sm.translations.
    lang_updates: dict of {lang: {field: value, ...}} to merge.
    For 'choices' and 'errors' fields, deep-merges per-key entries to
    preserve existing fields (e.g. audio_s3 when updating text).
    """
    if not lang_updates:
        return
    with transaction.atomic():
        sm = CompanyStateMachine.objects.select_for_update().get(pk=state_machine_id)
        cached = dict(sm.translations or {})
        for lang, updates in lang_updates.items():
            lang_data = dict(cached.get(lang, {}))
            for field, value in updates.items():
                if field in _NESTED_FIELDS and isinstance(value, dict):
                    existing = lang_data.get(field, {})
                    merged = dict(existing)
                    for k, v in value.items():
                        merged[k] = {**merged.get(k, {}), **v}
                    lang_data[field] = merged
                else:
                    lang_data[field] = value
            cached[lang] = lang_data
        sm.translations = cached
        sm.save(update_fields=["translations"])


def _translate_text(company_bot, text, lang):
    """Translate a single text string from English to target language. Returns translated text or None."""
    try:
        result = text_translate_provider(
            message_body=text,
            target_language=lang,
            source_language="en",
            company_bot=company_bot,
        )
        if result and result.get("status") == 200:
            return result["content"]
    except Exception as e:
        logger.warning(f"generate_translations: translation failed lang={lang}: {e}")
    return None


def _generate_tts_audio(company_bot, company_bot_id, text, lang, tts_voice_map, s3_key_suffix):
    """Generate TTS audio for a single text, upload to S3, return URL or None."""
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
            return _upload_audio_to_s3(audio_bytes, company_bot_id, s3_key_suffix, lang, audio_format)
    except Exception as e:
        logger.warning(f"generate_audio: TTS failed suffix={s3_key_suffix} lang={lang}: {e}")
    return None


# ── Translation generation ──

@app.task
def generate_state_machine_translations(company_bot_id, state_machine_id=None, language=None, generate_audio=False):
    """
    Generate all cached text translations for CompanyStateMachines on a bot.
    Translates bot_question, validation choice labels, and error message texts.
    All translations are stored in the single sm.translations JSON field.
    """
    scope = _resolve_generation_scope(company_bot_id, state_machine_id, language)
    if scope is None:
        logger.info(f"generate_translations: CompanyBot id={company_bot_id} not found")
        return
    company_bot, state_machines, languages, _tts_voice_map, op_type_by_step = scope

    for sm in state_machines:
        if _skip_state_machine(sm, op_type_by_step):
            continue

        lang_updates = {}

        for lang in languages:
            if lang == "en":
                continue

            updates = {}

            # Translate bot_question
            if sm.bot_question:
                translated = _translate_text(company_bot, sm.bot_question, lang)
                if translated:
                    updates["text"] = translated

            # Translate choice labels
            choices = (sm.validation_config or {}).get("choices", [])
            if choices:
                choice_translations = {}
                for choice in choices:
                    label = choice.get("label")
                    key = choice.get("key")
                    if not label or not key:
                        continue
                    translated = _translate_text(company_bot, label, lang)
                    if translated:
                        choice_translations[key] = {"text": translated}
                if choice_translations:
                    updates["choices"] = choice_translations

            # Translate error messages
            error_entries = sm.error_message or []
            if error_entries:
                error_translations = {}
                for entry in error_entries:
                    key = entry.get("key")
                    en_text = (entry.get("labels", {}).get("en") or {}).get("text")
                    if not key or not en_text:
                        continue
                    translated = _translate_text(company_bot, en_text, lang)
                    if translated:
                        error_translations[key] = {"text": translated}
                if error_translations:
                    updates["errors"] = error_translations

            if updates:
                lang_updates[lang] = updates

        if lang_updates:
            _merge_translations(sm.pk, lang_updates)
            logger.info(f"generate_translations: updated sm={sm.id} languages={list(lang_updates.keys())}")

    if generate_audio:
        generate_state_machine_audio.delay(company_bot_id, state_machine_id=state_machine_id, language=language)


# ── Audio generation ──

@app.task
def generate_state_machine_audio(company_bot_id, state_machine_id=None, language=None):
    """
    Generate all cached TTS audio for CompanyStateMachines on a bot.
    Generates audio for bot_question, validation choice labels, and error message texts.
    All audio URLs are stored in the single sm.translations JSON field.
    """
    scope = _resolve_generation_scope(company_bot_id, state_machine_id, language)
    if scope is None:
        logger.info(f"generate_audio: CompanyBot id={company_bot_id} not found")
        return
    company_bot, state_machines, _languages, tts_voice_map, op_type_by_step = scope

    if not tts_voice_map:
        return

    for sm in state_machines:
        if _skip_state_machine(sm, op_type_by_step):
            continue

        # Re-fetch to get latest translations (may have just been generated)
        sm.refresh_from_db()
        cached = dict(sm.translations or {})

        lang_updates = {}

        for lang in tts_voice_map:
            updates = {}

            # Audio for bot_question
            if sm.bot_question:
                if lang == "en":
                    text = sm.bot_question
                else:
                    text = (cached.get(lang) or {}).get("text")
                if text:
                    url = _generate_tts_audio(company_bot, company_bot_id, text, lang, tts_voice_map, sm.id)
                    if url:
                        updates["audio_s3"] = url

            # Audio for choice labels
            choices = (sm.validation_config or {}).get("choices", [])
            if choices:
                existing_choices = (cached.get(lang) or {}).get("choices", {})
                choice_audio = {}
                for choice in choices:
                    key = choice.get("key")
                    if not key:
                        continue
                    existing = existing_choices.get(key, {})
                    if lang == "en":
                        text = choice.get("label")
                    else:
                        text = existing.get("text")
                    if not text:
                        continue
                    s3_suffix = f"{sm.id}/choice_{key}_{lang}"
                    url = _generate_tts_audio(company_bot, company_bot_id, text, lang, tts_voice_map, s3_suffix)
                    if url:
                        choice_audio[key] = {**existing, "audio_s3": url}
                if choice_audio:
                    merged = dict(existing_choices)
                    for k, v in choice_audio.items():
                        merged[k] = {**merged.get(k, {}), **v}
                    updates["choices"] = merged

            # Audio for error messages
            error_entries = sm.error_message or []
            if error_entries:
                existing_errors = (cached.get(lang) or {}).get("errors", {})
                error_audio = {}
                for entry in error_entries:
                    key = entry.get("key")
                    if not key:
                        continue
                    existing = existing_errors.get(key, {})
                    if lang == "en":
                        text = (entry.get("labels", {}).get("en") or {}).get("text")
                    else:
                        text = existing.get("text")
                    if not text:
                        continue
                    s3_suffix = f"{sm.id}/error_{key}_{lang}"
                    url = _generate_tts_audio(company_bot, company_bot_id, text, lang, tts_voice_map, s3_suffix)
                    if url:
                        error_audio[key] = {**existing, "audio_s3": url}
                if error_audio:
                    merged = dict(existing_errors)
                    for k, v in error_audio.items():
                        merged[k] = {**merged.get(k, {}), **v}
                    updates["errors"] = merged

            if updates:
                lang_updates[lang] = updates

        if lang_updates:
            _merge_translations(sm.pk, lang_updates)
            logger.info(f"generate_audio: updated sm={sm.id} languages={list(lang_updates.keys())}")


# ── Audio revocation ──

def revoke_state_machine_audio(state_machine_id):
    """
    Deletes all cached audio from a CompanyStateMachine row (bot_question,
    choice labels, error messages) from S3 and from the translations JSON.

    Concurrency-safe: snapshots URLs under a lock, deletes from S3 outside
    the lock, then re-locks and only strips URLs that still match the snapshot
    (so a concurrent generate_audio won't have its fresh URLs wiped).

    Returns (removed_langs, failed_langs) for bot_question audio.
    """
    # ── 1. Snapshot all audio URLs under lock ──
    with transaction.atomic():
        sm = CompanyStateMachine.objects.select_for_update().get(pk=state_machine_id)
        translations = sm.translations
        if not translations:
            return [], []

        # snapshot: {lang: {"bot": url, "choices": {key: url}, "errors": {key: url}}}
        snapshot = {}
        for lang, lang_data in translations.items():
            if not isinstance(lang_data, dict):
                continue
            lang_snap = {}
            if lang_data.get("audio_s3"):
                lang_snap["bot"] = lang_data["audio_s3"]
            choice_urls = {}
            for key, choice_data in lang_data.get("choices", {}).items():
                if isinstance(choice_data, dict) and choice_data.get("audio_s3"):
                    choice_urls[key] = choice_data["audio_s3"]
            if choice_urls:
                lang_snap["choices"] = choice_urls
            error_urls = {}
            for key, error_data in lang_data.get("errors", {}).items():
                if isinstance(error_data, dict) and error_data.get("audio_s3"):
                    error_urls[key] = error_data["audio_s3"]
            if error_urls:
                lang_snap["errors"] = error_urls
            if lang_snap:
                snapshot[lang] = lang_snap

    if not snapshot:
        return [], []

    # ── 2. Delete from S3 outside transaction (network calls) ──
    deleted_urls = set()
    for lang, lang_snap in snapshot.items():
        if "bot" in lang_snap and _delete_audio_from_s3(lang_snap["bot"]):
            deleted_urls.add(lang_snap["bot"])
        for url in lang_snap.get("choices", {}).values():
            if _delete_audio_from_s3(url):
                deleted_urls.add(url)
        for url in lang_snap.get("errors", {}).values():
            if _delete_audio_from_s3(url):
                deleted_urls.add(url)

    if not deleted_urls:
        return [], list(snapshot.keys())

    # ── 3. Re-lock and strip only URLs that still match snapshot ──
    removed_langs = set()
    failed_langs = set()

    with transaction.atomic():
        sm = CompanyStateMachine.objects.select_for_update().get(pk=state_machine_id)
        cached = dict(sm.translations or {})

        for lang, lang_snap in snapshot.items():
            lang_data = cached.get(lang)
            if not isinstance(lang_data, dict):
                continue
            lang_data = dict(lang_data)

            # Strip bot audio only if URL unchanged since snapshot
            snap_bot = lang_snap.get("bot")
            if snap_bot:
                if lang_data.get("audio_s3") == snap_bot and snap_bot in deleted_urls:
                    del lang_data["audio_s3"]
                    removed_langs.add(lang)
                elif snap_bot not in deleted_urls:
                    failed_langs.add(lang)

            # Strip choice audio
            snap_choices = lang_snap.get("choices", {})
            choices = lang_data.get("choices")
            if isinstance(choices, dict) and snap_choices:
                choices = dict(choices)
                for key, snap_url in snap_choices.items():
                    entry = choices.get(key)
                    if isinstance(entry, dict) and entry.get("audio_s3") == snap_url and snap_url in deleted_urls:
                        choices[key] = {k: v for k, v in entry.items() if k != "audio_s3"}
                        removed_langs.add(lang)
                    elif snap_url not in deleted_urls:
                        failed_langs.add(lang)
                lang_data["choices"] = {k: v for k, v in choices.items() if v}
                if not lang_data["choices"]:
                    del lang_data["choices"]

            # Strip error audio
            snap_errors = lang_snap.get("errors", {})
            errors = lang_data.get("errors")
            if isinstance(errors, dict) and snap_errors:
                errors = dict(errors)
                for key, snap_url in snap_errors.items():
                    entry = errors.get(key)
                    if isinstance(entry, dict) and entry.get("audio_s3") == snap_url and snap_url in deleted_urls:
                        errors[key] = {k: v for k, v in entry.items() if k != "audio_s3"}
                        removed_langs.add(lang)
                    elif snap_url not in deleted_urls:
                        failed_langs.add(lang)
                lang_data["errors"] = {k: v for k, v in errors.items() if v}
                if not lang_data["errors"]:
                    del lang_data["errors"]

            if lang_data:
                cached[lang] = lang_data
            else:
                del cached[lang]

        sm.translations = cached or None
        sm.save(update_fields=["translations"])

    return list(removed_langs), list(failed_langs - removed_langs)
