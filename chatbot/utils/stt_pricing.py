import json
import logging

logger = logging.getLogger('django')


def _parse_other_params(other_params_raw):
    # """Same parsing as get_pricing_from_company_bot — other_params may be a dict or a JSON string."""
    if not other_params_raw:
        return None
    try:
        if isinstance(other_params_raw, str):
            return json.loads(other_params_raw)
        return other_params_raw
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing other_params: {e}")
        return None


def _get_model_pricing(other_params_raw, model_id):
    # """Look up model_pricing[model_id] from a single other_params source."""
    other_params = _parse_other_params(other_params_raw)
    if not isinstance(other_params, dict):
        return None
    pricing_data = other_params.get('model_pricing')
    if not isinstance(pricing_data, dict):
        return None
    model_pricing = pricing_data.get(model_id)
    if not isinstance(model_pricing, dict):
        return None
    return model_pricing


def _resolve_rate(voice_provider, company_bot, model_id):
    # """Resolve the per-1k-unit input rate for a provider, in priority order:
    # 1. voice_provider.other_params['model_pricing'][model_id]['input_cost_per_1k']
    # 2. company_bot.other_params['model_pricing'][model_id]['input_cost_per_1k']
    # 3. None (caller treats this as zero/unpriced)
    # """
    for source_name, other_params_raw in (
        ("voice_provider", getattr(voice_provider, 'other_params', None)),
        ("company_bot", getattr(company_bot, 'other_params', None)),
    ):
        pricing = _get_model_pricing(other_params_raw, model_id)
        if pricing and 'input_cost_per_1k' in pricing:
            try:
                rate_per_1k = float(pricing['input_cost_per_1k'])
                logger.info(f"💵 Using {source_name}.other_params pricing for '{model_id}': {rate_per_1k}/1k units")
                return rate_per_1k / 1000
            except (ValueError, TypeError):
                logger.error(f"Invalid input_cost_per_1k for '{model_id}' in {source_name}.other_params")

    logger.info(f"💵 No pricing configured for '{model_id}' in voice_provider or company_bot other_params — cost will be 0")
    return None


def compute_stt_usage_and_cost(model: str, duration_seconds: float, voice_provider=None, company_bot=None):
    # """Returns (usage_details, cost_details) for an STT generation."""
    duration_seconds = duration_seconds or 0
    usage_details = {"input": duration_seconds, "output": 0, "total": duration_seconds}

    rate = _resolve_rate(voice_provider, company_bot, model)
    if rate is None:
        return usage_details, None

    input_cost = duration_seconds * rate
    cost_details = {"input": input_cost, "output": 0, "total": input_cost}
    return usage_details, cost_details


def compute_translate_usage_and_cost(model: str, char_count: int, voice_provider=None, company_bot=None):
    # """Returns (usage_details, cost_details) for a translation/transliteration generation.
    # Note: "custom_llm" is intentionally never configured here — it routes through
    # handle_openai_model/handle_bedrock_model, which already report cost via their own generation."""
    char_count = char_count or 0
    usage_details = {"input": char_count, "output": 0, "total": char_count}

    rate = _resolve_rate(voice_provider, company_bot, model)
    if rate is None:
        return usage_details, None

    input_cost = char_count * rate
    cost_details = {"input": input_cost, "output": 0, "total": input_cost}
    return usage_details, cost_details