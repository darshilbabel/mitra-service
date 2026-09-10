import base64
import os
import tempfile
import traceback
import concurrent.futures
import contextvars
from chatbot.translate.google.google_stt import split_audio
from sarvamai import SarvamAI
import logging

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_stt_usage_and_cost

sarvam_api_key = os.getenv("SARVAM_API_KEY")
logger = logging.getLogger('django')
langfuse = get_langfuse_client()


def transcribe_single_chunk(
        client, chunk_number, chunk, audio_format, source_language, model, mode, chunk_duration,voice_provider=None
):
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="sarvam_stt_chunk",
        model="sarvam-stt",
        input={"chunk_number": chunk_number, "source_language": source_language, "mode": mode},
    ) as gen:
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=True) as tmp_file:
                tmp_file.write(chunk)
                tmp_file.flush()

                with open(tmp_file.name, "rb") as f:
                    params = {
                        "file": f,
                        "model": model,
                        "language_code": source_language,
                    }

                    if mode:
                        params["mode"] = mode

                    response = client.speech_to_text.transcribe(**params)
            print("response: ", response)

            # usage_details, cost_details = compute_stt_usage_and_cost("sarvam-stt", chunk_duration)
            usage_details, cost_details = compute_stt_usage_and_cost(
                                           "sarvam-stt", chunk_duration,
                                           voice_provider=voice_provider,
                                           company_bot=getattr(voice_provider, 'company_bot', None),
                                        )
            if hasattr(response, "transcript"):
                gen.update(output={"transcript": response.transcript}, usage_details=usage_details, cost_details=cost_details)
                return (chunk_number, response.transcript)

            gen.update(output={"transcript": ""}, usage_details=usage_details, cost_details=cost_details)
            return (chunk_number, "")

        except Exception as e:
            traceback.print_exc()
            gen.update(output=None, level="ERROR", status_message=str(e))
            return (chunk_number, "")


def transcribe_sarvam_multiple_chunks(
        voice_provider,
        base64_audio_file,
        source_language,
        audio_format="wav",
):
    other_params = voice_provider.other_params or {}
    duration = int(other_params.get("chunk_duration", 10))
    model = other_params.get("model", "saaras:v3")
    mode = other_params.get("mode", "transcribe")

    with langfuse.start_as_current_observation(
        as_type="span",
        name="sarvam_stt_batch",
        input={
            "source_language": source_language,
            "audio_format": audio_format,
            "model": model,
            "voice_provider_id": getattr(voice_provider, 'id', None),
        },
    ) as s:
        try:
            audio_bytes = base64.b64decode(base64_audio_file)
            chunks = split_audio(audio_bytes, chunk_duration=duration)
            client = SarvamAI(api_subscription_key=sarvam_api_key)

            # Each task gets its OWN copy_context() call, so each submitted task runs
            # in a distinct Context object — a single Context cannot be entered via
            # .run() from more than one thread concurrently (raises RuntimeError).
            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = [
                    executor.submit(
                        contextvars.copy_context().run,
                        transcribe_single_chunk, client, chunk_number, chunk, audio_format,
                        source_language, model, mode, duration, voice_provider
                    )
                    for chunk_number, chunk in chunks
                ]

                transcripts = [
                    future.result()
                    for future in concurrent.futures.as_completed(futures)
                ]

            transcripts.sort()
            transcript = " ".join(content for _, content in transcripts)

            s.update(
                output={"transcript_preview": transcript[:300], "chunk_count": len(chunks)},
            )

            return {"status": 200, "content": transcript}

        except Exception as e:
            logger.error("Error processing: %s", e, exc_info=True)
            traceback.print_exc()
            s.update(output=None, level="ERROR", status_message=str(e))
            return {"status": 500, "content": str(e)}