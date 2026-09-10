import base64
import io
import logging
import os

from openai import OpenAI

from chatbot.translate.base.speech_to_text import (
    get_wav_duration,
    split_audio,
)
from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_stt_usage_and_cost


logger = logging.getLogger("django")
langfuse = get_langfuse_client()


def transcribe_audio(
    base64_audio: str,
    audio_format: str,
    source_language: str,
    voice_provider: any,
) -> dict:

    other_params = voice_provider.other_params or {}

    model = other_params.get(
        "model",
        "whisper-1",
    )

    with langfuse.start_as_current_observation(
        as_type="span",
        name="openai_whisper_batch",
        input={
            "audio_format": audio_format,
            "source_language": source_language,
            "model": model,
            "voice_provider_id": getattr(
                voice_provider,
                "id",
                None,
            ),
        },
    ) as batch_span:

        try:
            client_api_key = os.getenv("OPENAI_API_KEY")

            client = OpenAI(
                api_key=client_api_key
            )

            response_format = other_params.get(
                "response_format",
                "text",
            )

            temperature = other_params.get(
                "temperature",
                0,
            )

            chunk_duration = int(
                other_params.get(
                    "chunk_duration",
                    300,
                )
            )

            dictionary = other_params.get(
                "dictionary",
                [],
            )

            dictionary_prompt = None

            if dictionary and len(dictionary) > 0:
                dictionary_prompt = (
                    "Vocabulary: "
                    + ", ".join(dictionary)
                )

            audio_bytes = base64.b64decode(
                base64_audio
            )

            logger.info(
                "Audio size: %s bytes",
                len(audio_bytes),
            )

            chunks = split_audio(
                audio_bytes,
                chunk_duration=chunk_duration,
            )

            logger.info(
                "Number of chunks: %s",
                len(chunks),
            )

            transcripts = []

            for chunk_number, chunk in chunks:

                logger.info(
                    "Sending chunk: %s, size: %s bytes",
                    chunk_number,
                    len(chunk),
                )

                # Calculate the actual duration of this
                # particular chunk.
                #
                # This is important for the last chunk,
                # which can be shorter than chunk_duration.
                actual_chunk_duration = get_wav_duration(
                    chunk
                )

                logger.info(
                    "Chunk %s actual duration: %.2f seconds",
                    chunk_number,
                    actual_chunk_duration,
                )

                with langfuse.start_as_current_observation(
                    as_type="generation",
                    name="openai_whisper_chunk",
                    model=model,
                    input={
                        "chunk_number": chunk_number,
                        "chunk_bytes": len(chunk),
                        "chunk_duration": actual_chunk_duration,
                    },
                ) as gen:

                    try:
                        audio_file = io.BytesIO(
                            chunk
                        )

                        audio_file.name = (
                            f"audio.{audio_format}"
                        )

                        params = {
                            "model": model,
                            "file": audio_file,
                            "response_format": response_format,
                            "temperature": temperature,
                        }

                        if source_language:
                            params["language"] = (
                                source_language
                            )

                        if dictionary_prompt:
                            params["prompt"] = (
                                dictionary_prompt
                            )

                        transcription = (
                            client.audio.transcriptions.create(
                                **params
                            )
                        )

                        logger.info(
                            "Transcription received for chunk %s",
                            chunk_number,
                        )

                        # Use the ACTUAL duration of this
                        # chunk instead of the configured
                        # maximum chunk duration.
                        usage_details, cost_details = (
                            compute_stt_usage_and_cost(
                                model,
                                actual_chunk_duration,
                                voice_provider=voice_provider,
                                company_bot=getattr(
                                    voice_provider,
                                    "company_bot",
                                    None,
                                ),
                            )
                        )

                        if isinstance(
                            transcription,
                            str,
                        ):
                            transcript = transcription
                        else:
                            transcript = str(
                                transcription
                            )

                        transcripts.append(
                            transcript
                        )

                        gen.update(
                            output={
                                "transcript": transcript
                            },
                            usage_details=usage_details,
                            cost_details=cost_details,
                        )

                    except Exception as chunk_err:

                        logger.error(
                            "Error processing OpenAI "
                            "chunk %s: %s",
                            chunk_number,
                            chunk_err,
                            exc_info=True,
                        )

                        gen.update(
                            output=None,
                            level="ERROR",
                            status_message=str(
                                chunk_err
                            ),
                        )

                        raise

            full_transcript = " ".join(
                transcripts
            )

            batch_span.update(
                output={
                    "transcript_preview": (
                        full_transcript[:300]
                    ),
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

            batch_span.update(
                output=None,
                level="ERROR",
                status_message=str(e),
            )

            return {
                "status": 500,
                "content": (
                    f"Error during API request: {e}"
                ),
            }