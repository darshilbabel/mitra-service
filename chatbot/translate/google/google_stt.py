import base64
import concurrent.futures
import contextvars
import logging
import traceback
from typing import List

from google.api_core.client_options import ClientOptions
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech

from chatbot.translate.base.speech_to_text import (
    get_wav_duration,
    split_audio,
)
from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_stt_usage_and_cost

logger = logging.getLogger("django")
langfuse = get_langfuse_client()


def transcribe_chunk(
    client,
    project_id,
    location,
    config,
    chunk_number,
    chunk,
    chunk_duration,
    voice_provider=None,
):
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="google_stt_chunk",
        model="google-speech-v2",
        input={
            "chunk_number": chunk_number,
            "location": location,
        },
    ) as gen:
        request = cloud_speech.RecognizeRequest(
            recognizer=(
                f"projects/{project_id}/locations/"
                f"{location}/recognizers/_"
            ),
            config=config,
            content=chunk,
        )

        try:
            response = client.recognize(request=request)

            transcript = ""

            if response and not isinstance(response, str):
                for res_result in response.results:
                    if res_result and res_result.alternatives:
                        transcript += (
                            res_result.alternatives[0].transcript + " "
                        )

            # Use the actual duration of this individual chunk.
            usage_details, cost_details = compute_stt_usage_and_cost(
                "google-speech-v2",
                chunk_duration,
                voice_provider=voice_provider,
                company_bot=getattr(
                    voice_provider,
                    "company_bot",
                    None,
                ),
            )

            gen.update(
                output={
                    "transcript": transcript.strip(),
                },
                usage_details=usage_details,
                cost_details=cost_details,
            )

            return chunk_number, transcript.strip()

        except Exception as e:
            logger.error(
                "Error during API request for chunk %s : %s | "
                "location=%s recognizer=%s",
                chunk_number,
                e,
                location,
                request.recognizer,
                exc_info=True,
            )

            traceback.print_exc()

            gen.update(
                output=None,
                level="ERROR",
                status_message=str(e),
            )

            return chunk_number, ""


def transcribe_multiple_languages_v2(
    project_id: str,
    language_codes: List[str],
    audio_file: str,
    voice_provider: any,
) -> dict:

    with langfuse.start_as_current_observation(
        as_type="span",
        name="google_stt_batch",
        input={
            "language_codes": language_codes,
            "voice_provider_id": getattr(
                voice_provider,
                "id",
                None,
            ),
        },
    ) as stt_span:

        try:
            other_params = voice_provider.other_params or {}

            location = other_params.get(
                "location",
                "global",
            )

            client_options = None

            if location != "global":
                client_options = ClientOptions(
                    api_endpoint=f"{location}-speech.googleapis.com"
                )

            client = SpeechClient(
                client_options=client_options
            )

            config_kwargs = {
                "auto_decoding_config": (
                    cloud_speech.AutoDetectDecodingConfig()
                ),
                "language_codes": language_codes,
                "model": other_params.get(
                    "model",
                    "latest_long",
                ),
            }

            features_kwargs = {}

            feature_params = [
                "enable_automatic_punctuation",
                "enable_spoken_punctuation",
                "enable_spoken_emojis",
                "enable_word_time_offsets",
                "profanity_filter",
                "max_alternatives",
            ]

            for param in feature_params:
                if param in other_params:
                    features_kwargs[param] = other_params[param]

            if features_kwargs:
                config_kwargs["features"] = (
                    cloud_speech.RecognitionFeatures(
                        **features_kwargs
                    )
                )

            if other_params.get("boost_words"):
                phrases = []

                for item in other_params["boost_words"]:
                    if isinstance(item, dict):
                        word = item.get("word")
                    else:
                        word = item

                    if word:
                        phrases.append(
                            {
                                "value": word,
                            }
                        )

                config_kwargs["adaptation"] = {
                    "phrase_sets": [
                        {
                            "inline_phrase_set": {
                                "phrases": phrases
                            }
                        }
                    ]
                }

            config = cloud_speech.RecognitionConfig(
                **config_kwargs
            )

            audio_bytes = base64.b64decode(audio_file)

            chunk_duration = int(
                other_params.get(
                    "chunk_duration",
                    10,
                )
            )

            chunks = split_audio(
                audio_bytes,
                chunk_duration=chunk_duration,
            )

            # Each chunk can have a different actual duration.
            # For example:
            #   10 sec audio -> 10 sec
            #   10 sec audio + 3 sec -> 10 sec + 3 sec
            #
            # Do NOT calculate duration from `chunks` itself because
            # `chunks` is a list of (chunk_number, chunk_bytes) tuples.

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_to_chunk = {}

                for chunk_number, chunk in chunks:
                    actual_chunk_duration = get_wav_duration(
                        chunk
                    )

                    # Each task gets its own copy_context().
                    # A single Context cannot be entered by multiple
                    # threads concurrently.
                    ctx = contextvars.copy_context()

                    future = executor.submit(
                        ctx.run,
                        transcribe_chunk,
                        client,
                        project_id,
                        location,
                        config,
                        chunk_number,
                        chunk,
                        actual_chunk_duration,
                        voice_provider,
                    )

                    future_to_chunk[future] = chunk_number

                results = []

                for future in concurrent.futures.as_completed(
                    future_to_chunk
                ):
                    chunk_number, transcript = future.result()

                    results.append(
                        (
                            chunk_number,
                            transcript,
                        )
                    )

            results.sort()

            full_transcript = " ".join(
                transcript
                for _, transcript in results
            )

            stt_span.update(
                output={
                    "transcript_preview": full_transcript[:300],
                    "chunk_count": len(chunks),
                }
            )

            return {
                "status": 200,
                "content": full_transcript,
            }

        except Exception as e:
            logger.error(
                "Error processing file: %s",
                e,
                exc_info=True,
            )

            traceback.print_exc()

            stt_span.update(
                output=None,
                level="ERROR",
                status_message=str(e),
            )

            return {
                "status": 500,
                "content": f"Error processing file: {e}",
            }