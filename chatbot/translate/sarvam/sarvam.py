import os
import re
import logging
import contextvars
from concurrent.futures import ThreadPoolExecutor, as_completed
from sarvamai import SarvamAI

from chatbot.utils.langfuse_client import get_langfuse_client
from chatbot.utils.stt_pricing import compute_translate_usage_and_cost

logger = logging.getLogger("django")
langfuse = get_langfuse_client()


class SarvamLanguageService:
    def __init__(self, api_key=None, max_workers=5):
        self.api_key = api_key or os.getenv("SARVAM_API_KEY")
        self.client = SarvamAI(api_subscription_key=self.api_key)
        self.max_workers = max_workers

    @staticmethod
    def split_text_into_chunks(text, max_chars=990):
        chunks = []
        sentence_end_pattern = re.compile(r'(?<=[.!?])\s+')
        sentences = sentence_end_pattern.split(text)

        current_chunk = ""
        for sentence in sentences:
            if not sentence.strip():
                continue

            if len(current_chunk) + len(sentence) + 1 <= max_chars:
                current_chunk += (" " if current_chunk else "") + sentence
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                if len(sentence) <= max_chars:
                    current_chunk = sentence
                else:
                    words = sentence.split()
                    word_chunk = ""
                    for word in words:
                        if len(word_chunk) + len(word) + 1 <= max_chars:
                            word_chunk += (" " if word_chunk else "") + word
                        else:
                            chunks.append(word_chunk.strip())
                            word_chunk = word
                    if word_chunk:
                        current_chunk = word_chunk
                    else:
                        current_chunk = ""
        if current_chunk:
            chunks.append(current_chunk.strip())

        logger.info(f"[Chunking] Total Chunks Created: {len(chunks)}")
        return chunks

    def _process_in_parallel(self, chunks, worker_func):
        results = [None] * len(chunks)
        current_ctx = contextvars.copy_context()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(current_ctx.run, worker_func, chunks[i]): i
                for i in range(len(chunks))
            }

            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()

        return " ".join(results)

    def _execute_text_task(
            self, method_name, response_attr, chunks, base_kwargs_builder, extra_kwargs=None,
            pricing_key="sarvam-translate",voice_provider=None
    ):
        try:
            extra_kwargs = extra_kwargs or {}

            def worker(chunk):
                with langfuse.start_as_current_observation(
                    as_type="generation",
                    name=f"sarvam_{method_name}_chunk",
                    model=pricing_key,
                    input={"chunk_preview": chunk[:200], "chunk_chars": len(chunk)},
                ) as gen:
                    try:
                        base_kwargs = base_kwargs_builder(chunk)

                        def normalize_value(v):
                            if isinstance(v, str) and v.lower() in ("true", "false"):
                                return v.lower() == "true"
                            return v

                        kwargs = {
                            **base_kwargs,
                            **{
                                k: normalize_value(v)
                                for k, v in extra_kwargs.items()
                                if v is not None
                            },
                        }

                        method = getattr(self.client.text, method_name)
                        response = method(**kwargs)
                        logger.info(f"Response {response}")

                        result_text = getattr(response, response_attr, chunk)

                        # usage_details, cost_details = compute_translate_usage_and_cost(pricing_key, len(chunk))
                        usage_details, cost_details = compute_translate_usage_and_cost(
                                                               pricing_key, len(chunk),
                                                               voice_provider=voice_provider,
                                                               company_bot=getattr(voice_provider, 'company_bot', None),
                                                        )
                        gen.update(
                            output={"result_preview": str(result_text)[:200]},
                            usage_details=usage_details,
                            cost_details=cost_details,
                        )
                        return result_text

                    except Exception as e:
                        logger.error(f"{method_name} error")
                        gen.update(output=None, level="ERROR", status_message=str(e))
                        return chunk

            return self._process_in_parallel(chunks, worker)

        except Exception:
            logger.error(f"{method_name} failed")
            raise

    def transliterate(
            self, input_text, source_lang, target_lang, max_chars=990, voice_provider=None,
    ):
        chunks = self.split_text_into_chunks(input_text, max_chars)

        other = getattr(voice_provider, "other_params", {}) or {}

        def base_kwargs_builder(chunk):
            return {
                "input": chunk,
                "source_language_code": source_lang,
                "target_language_code": target_lang,
            }

        with langfuse.start_as_current_observation(
            as_type="span",
            name="sarvam_transliterate_batch",
            input={"source_lang": source_lang, "target_lang": target_lang, "total_chars": len(input_text)},
        ) as s:
            result = self._execute_text_task(
                method_name="transliterate",
                response_attr="transliterated_text",
                chunks=chunks,
                base_kwargs_builder=base_kwargs_builder,
                extra_kwargs={
                    "numerals_format": other.get("numerals_format"),
                    "spoken_form": other.get("spoken_form"),
                    "spoken_form_numerals_language": other.get("spoken_form_numerals_language"),
                },
                pricing_key="sarvam-transliterate",
                voice_provider=voice_provider
            )
            s.update(output={"result_preview": result[:200], "chunk_count": len(chunks)})

        return {
            "status": 200,
            "content": result,
        }

    def translate(self, input_text, source_lang, target_lang, max_chars=990, voice_provider=None):
        chunks = self.split_text_into_chunks(input_text, max_chars)

        other = getattr(voice_provider, "other_params", {}) or {}
        gender = getattr(voice_provider, "gender", None)

        def base_kwargs_builder(chunk):
            return {
                "input": chunk,
                "source_language_code": source_lang,
                "target_language_code": target_lang,
                "speaker_gender": gender,
            }

        with langfuse.start_as_current_observation(
            as_type="span",
            name="sarvam_translate_batch",
            input={"source_lang": source_lang, "target_lang": target_lang, "total_chars": len(input_text)},
        ) as s:
            result = self._execute_text_task(
                method_name="translate",
                response_attr="translated_text",
                chunks=chunks,
                base_kwargs_builder=base_kwargs_builder,
                extra_kwargs={
                    "model": other.get("model"),
                    "mode": other.get("mode"),
                    "output_script": other.get("output_script"),
                    "numerals_format": other.get("numerals_format"),
                },
                pricing_key="sarvam-translate",
                voice_provider=voice_provider
            )
            s.update(output={"result_preview": result[:200], "chunk_count": len(chunks)})

        return {
            "status": 200,
            "content": result,
        }