from abc import ABC, abstractmethod
import logging

from chatbot.utils.langfuse_client import get_langfuse_client

langfuse = get_langfuse_client()
logger = logging.getLogger('django')


class BasePostprocessor(ABC):
    """Base class for postprocessing operations"""

    @abstractmethod
    def postprocess(self, state_machine, llm_response, **kwargs):
        """Execute postprocessing logic"""
        pass


class SimplePostprocessor(BasePostprocessor):
    """Handles simple prompt-based postprocessing"""

    def postprocess(self, state_machine, llm_response, **kwargs):
        """Execute simple postprocessing using postprocess_prompt"""
        company_bot = kwargs.get('company_bot')
        # Span: one-shot LLM call analysing the main LLM response with the state's postprocess_prompt —
        # the actual generation (model, tokens, cost) is created deeper inside handle_bedrock_model / handle_openai_model.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="simple_postprocessor.postprocess",
            input={
                "state_machine": state_machine.name,
                "postprocess_prompt": state_machine.postprocess_prompt,
                "llm_response": llm_response,
                "provider": getattr(company_bot, 'provider', None),
                "model": getattr(company_bot, 'llm_model', None),
            },
        ) as postprocess_span:
            if not state_machine.postprocess_prompt:
                logger.warning(f"No postprocess_prompt defined for state {state_machine.name}")
                postprocess_span.update(output={"skipped": "no_postprocess_prompt"}, level="WARNING")
                return None

            # Get LLM handler
            from chatbot.llm_models.llm_script import handle_bedrock_model, handle_openai_model
            from chatbot.models import LLMProvider

            messages = kwargs.get('messages', [])

            # Add the LLM response to messages for context
            enhanced_messages = messages.copy()
            if llm_response:
                enhanced_messages.append({
                    'role': 'assistant',
                    'content': str(llm_response)
                })

            # Build simple prompt with LLM response context
            simple_prompt = self._build_simple_prompt(
                state_machine.postprocess_prompt, company_bot, llm_response
            )

            try:
                if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
                    response = handle_bedrock_model(
                        system_prompt=simple_prompt,
                        messages=enhanced_messages,
                        model_name=company_bot.llm_model,
                        temperature=company_bot.bot_temperature,
                        max_token=company_bot.max_token,
                        company_bot=company_bot
                    )
                elif company_bot.provider == LLMProvider.OPENAI:
                    response = handle_openai_model(
                        system_prompt=simple_prompt,
                        messages=enhanced_messages,
                        model_name=company_bot.llm_model,
                        temperature=company_bot.bot_temperature,
                        max_token=company_bot.max_token,
                        tools=None,
                        tool_choice='auto',
                        is_json_response=False
                    )
                else:
                    response = None

                logger.info(f"Simple postprocessing response for {state_machine.name}: {response}")
                postprocess_span.update(output=response)
                return response

            except Exception as e:
                logger.error(f"Error in simple postprocessing: {e}")
                postprocess_span.update(output={"error": str(e)}, level="ERROR")
                return None

    def _build_simple_prompt(self, postprocess_prompt, company_bot, llm_response):
        """Build prompt for simple postprocessing"""
        enhanced_prompt = f"{postprocess_prompt}\n\nLLM Response to analyze: {llm_response}"

        from chatbot.models import LLMProvider
        if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
            return [{'text': enhanced_prompt}]
        else:
            return [{'role': 'system', 'content': enhanced_prompt}]


class ComplexPostprocessor(BasePostprocessor):
    """Handles complex bot-based postprocessing"""

    def postprocess(self, state_machine, llm_response, **kwargs):
        """Execute complex postprocessing using postprocess_bot"""
        postprocess_bot = state_machine.postprocess_bot
        # Span: full LLM call analysing the main LLM response with the state's postprocess_bot —
        # the actual generation (model, tokens, cost) is created deeper inside handle_bedrock_model / handle_openai_model.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="complex_postprocessor.postprocess",
            input={
                "state_machine": state_machine.name,
                "postprocess_bot": getattr(postprocess_bot, 'name', None),
                "llm_response": llm_response,
                "provider": getattr(postprocess_bot, 'provider', None),
                "model": getattr(postprocess_bot, 'llm_model', None),
            },
        ) as postprocess_span:
            if not postprocess_bot:
                logger.warning(f"No postprocess_bot defined for state {state_machine.name}")
                postprocess_span.update(output={"skipped": "no_postprocess_bot"}, level="WARNING")
                return None

            try:
                # Use the postprocess_bot to analyze the LLM response
                from chatbot.services.core.prompt_builder import PromptBuilder
                from chatbot.llm_models.llm_script import handle_bedrock_model, handle_openai_model
                from chatbot.models import LLMProvider

                messages = kwargs.get('messages', [])

                # Add the LLM response to messages for context
                enhanced_messages = messages.copy()
                if llm_response:
                    from chatbot.models import LLMProvider
                    enhanced_messages.append({
                        'role': 'assistant' if postprocess_bot.provider == LLMProvider.OPENAI else 'assistant',
                        'content': str(llm_response)
                    })

                # Build prompt using the postprocess bot's context
                prompt_builder = PromptBuilder()

                system_prompt = prompt_builder.build_system_prompt(postprocess_bot, None)

                if postprocess_bot.provider == LLMProvider.BEDROCK_CONVERSE:
                    response = handle_bedrock_model(
                        system_prompt=system_prompt,
                        messages=enhanced_messages,
                        model_name=postprocess_bot.llm_model,
                        temperature=postprocess_bot.bot_temperature,
                        max_token=postprocess_bot.max_token,
                        company_bot=postprocess_bot
                    )
                elif postprocess_bot.provider == LLMProvider.OPENAI:
                    response = handle_openai_model(
                        system_prompt=system_prompt,
                        messages=enhanced_messages,
                        model_name=postprocess_bot.llm_model,
                        temperature=postprocess_bot.bot_temperature,
                        max_token=postprocess_bot.max_token,
                        tools=None,
                        tool_choice='auto',
                        is_json_response=False
                    )
                else:
                    response = None

                logger.info(f"Complex postprocessing response for {state_machine.name}: {response}")
                postprocess_span.update(output=response)
                return response

            except Exception as e:
                logger.error(f"Error in complex postprocessing: {e}")
                postprocess_span.update(output={"error": str(e)}, level="ERROR")
                return None
