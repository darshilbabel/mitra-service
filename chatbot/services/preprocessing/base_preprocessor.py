from abc import ABC, abstractmethod
import logging

from chatbot.models import LLMProvider
from chatbot.utils.langfuse_client import get_langfuse_client

langfuse = get_langfuse_client()
logger = logging.getLogger('django')


class BasePreprocessor(ABC):
    """Base class for preprocessing operations"""

    @abstractmethod
    def preprocess(self, state_machine, **kwargs):
        """Execute preprocessing logic"""
        pass


class SimplePreprocessor(BasePreprocessor):
    """Handles simple prompt-based preprocessing"""

    def preprocess(self, state_machine, **kwargs):
        """Execute simple preprocessing using preprocess_prompt"""
        company_bot = kwargs.get('company_bot')
        # Span: one-shot LLM call with the state's preprocess_prompt — the actual generation
        # (model, tokens, cost) is created deeper inside handle_bedrock_model / handle_openai_model.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="simple_preprocessor.preprocess",
            input={
                "state_machine": state_machine.name,
                "preprocess_prompt": state_machine.preprocess_prompt,
                "provider": getattr(company_bot, 'provider', None),
                "model": getattr(company_bot, 'llm_model', None),
            },
        ) as preprocess_span:
            if not state_machine.preprocess_prompt:
                logger.warning(f"No preprocess_prompt defined for state {state_machine.name}")
                preprocess_span.update(output={"skipped": "no_preprocess_prompt"}, level="WARNING")
                return None

            # Get LLM handler
            from chatbot.llm_models.llm_script import handle_bedrock_model, handle_openai_model
            from chatbot.models import LLMProvider

            messages = kwargs.get('messages', [])

            # Build simple prompt
            simple_prompt = self._build_simple_prompt(state_machine.preprocess_prompt, company_bot)

            try:
                if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
                    response = handle_bedrock_model(
                        system_prompt=simple_prompt,
                        messages=messages,
                        model_name=company_bot.llm_model,
                        temperature=company_bot.bot_temperature,
                        max_token=company_bot.max_token,
                        company_bot=company_bot
                    )
                elif company_bot.provider == LLMProvider.OPENAI:
                    response = handle_openai_model(
                        system_prompt=simple_prompt,
                        messages=messages,
                        model_name=company_bot.llm_model,
                        temperature=company_bot.bot_temperature,
                        max_token=company_bot.max_token,
                        tools=None,
                        tool_choice='auto',
                        is_json_response=False
                    )
                else:
                    response = None

                logger.info(f"Simple preprocessing response for {state_machine.name}: {response}")
                preprocess_span.update(output=response)
                return response

            except Exception as e:
                logger.error(f"Error in simple preprocessing: {e}")
                preprocess_span.update(output={"error": str(e)}, level="ERROR")
                return None

    def _build_simple_prompt(self, preprocess_prompt, company_bot):
        """Build prompt for simple preprocessing"""
        if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
            return [{'text': preprocess_prompt}]
        else:
            return [{'role': 'system', 'content': preprocess_prompt}]


class ComplexPreprocessor(BasePreprocessor):
    """Handles complex bot-based preprocessing"""

    def preprocess(self, state_machine, **kwargs):
        """Execute complex preprocessing using preprocess_bot"""
        preprocess_bot = state_machine.preprocess_bot
        # Span: full LLM call driven by the state's preprocess_bot — the actual generation
        # (model, tokens, cost) is created deeper inside handle_bedrock_model / handle_openai_model.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="complex_preprocessor.preprocess",
            input={
                "state_machine": state_machine.name,
                "preprocess_bot": getattr(preprocess_bot, 'name', None),
                "provider": getattr(preprocess_bot, 'provider', None),
                "model": getattr(preprocess_bot, 'llm_model', None),
            },
        ) as preprocess_span:
            if not preprocess_bot:
                logger.warning(f"No preprocess_bot defined for state {state_machine.name}")
                preprocess_span.update(output={"skipped": "no_preprocess_bot"}, level="WARNING")
                return None

            try:
                # Use the preprocess_bot to generate response like normal flow
                from chatbot.services.core.prompt_builder import PromptBuilder
                from chatbot.llm_models.llm_script import handle_bedrock_model, handle_openai_model
                from chatbot.models import LLMProvider

                messages = kwargs.get('messages', [])

                # Build prompt using the preprocess bot's context
                prompt_builder = PromptBuilder()

                system_prompt = prompt_builder.build_system_prompt(preprocess_bot, None)

                if preprocess_bot.provider == LLMProvider.BEDROCK_CONVERSE:
                    response = handle_bedrock_model(
                        system_prompt=system_prompt,
                        messages=messages,
                        model_name=preprocess_bot.llm_model,
                        temperature=preprocess_bot.bot_temperature,
                        max_token=preprocess_bot.max_token,
                        company_bot=preprocess_bot
                    )
                elif preprocess_bot.provider == LLMProvider.OPENAI:
                    response = handle_openai_model(
                        system_prompt=system_prompt,
                        messages=messages,
                        model_name=preprocess_bot.llm_model,
                        temperature=preprocess_bot.bot_temperature,
                        max_token=preprocess_bot.max_token,
                        tools=None,
                        tool_choice='auto',
                        is_json_response=False
                    )
                else:
                    response = None

                logger.info(f"Complex preprocessing response for {state_machine.name}: {response}")
                preprocess_span.update(output=response)
                return response

            except Exception as e:
                logger.error(f"Error in complex preprocessing: {e}")
                preprocess_span.update(output={"error": str(e)}, level="ERROR")
                return None
