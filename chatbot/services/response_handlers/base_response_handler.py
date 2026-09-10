from abc import ABC, abstractmethod
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db import transaction
from chatbot.celery_tasks.common_chat_tasks import save_in_company_db
from chatbot.celery_tasks.handle_message import translate_and_send_message
from chatbot.llm_models.llm_script import handle_bedrock_model, handle_openai_response_api
from chatbot.models import BotVernacular, ChatSession, ChatStatus, LLMProvider, CompanyBotTypeChoices
from chatbot.models.company_models import CompanyStateMachine
from chatbot.models.enums import OperationTypeChoices, PreProcessOutputMode
from chatbot.services.postprocessing.postprocessing_service import PostprocessingService
from chatbot.services.preprocessing.preprocessing_service import PreprocessingService
from chatbot.utils.langfuse_client import get_langfuse_client
import logging

logger = logging.getLogger('django')
channel_layer = get_channel_layer()
langfuse = get_langfuse_client()


class BaseResponseHandler(ABC):
    """Base class for handling LLM responses with common functionality"""

    TURN_RESPONSE_TOOL_NAME = "turn_response"
    DEFAULT_MAX_PROBES = 1

    def __init__(self):
        self.default_error_message = 'I am sorry, I could not understood completely. Could you rephrase this please?'
        self.preprocessing_service = PreprocessingService()
        self.postprocessing_service = PostprocessingService()
        self.min_word_count = 3
        self.max_retry_attempts = 2

    def _is_response_too_short(self, response):
        try:
            if not response or response == '':
                return False
            if isinstance(response, dict):
                response_text = response.get('response', '')
                if not response_text:
                    return False
            else:
                response_text = str(response)
            response_text = response_text.strip()
            if not response_text:
                return False
            word_count = len(response_text.split())
            logger.info(f"Response word count: {word_count}, text: '{response_text[:100]}...'")
            if word_count < self.min_word_count:
                logger.info(f"Response too short: {word_count} words (minimum: {self.min_word_count})")
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking response length: {e}")
            return False

    def is_non_llm_state(self, state_machine):
        return (
                state_machine
                and hasattr(state_machine, 'operation_type')
                and state_machine.operation_type == OperationTypeChoices.NON_LLM
        )

    def apply_turn_response_guard(self, response, chat_session, state_machine):
        if not (
                isinstance(response, dict)
                and 'toolUseId' in response
                and response.get('name') == self.TURN_RESPONSE_TOOL_NAME
                and isinstance(response.get('input'), dict)
        ):
            return response

        tool_input = response['input']
        stage_name = state_machine.name if state_machine else None

        with transaction.atomic():
            locked_session = ChatSession.objects.select_for_update().get(pk=chat_session.pk)
            other_params = dict(locked_session.other_params or {})
            prior = other_params.get('probe_guard') or {}
            probe_count = prior.get('probe_count', 0) if prior.get('stage') == stage_name else 0

            max_probes = tool_input.get('max_probes')
            if not isinstance(max_probes, int) or isinstance(max_probes, bool) or max_probes < 0:
                max_probes = self.DEFAULT_MAX_PROBES

            message_kind = tool_input.get('message_kind')
            if message_kind not in ('clarification', 'none'):
                probe_count += 1
                if probe_count > max_probes:
                    logger.info(
                        f"Probe guard override: stage={stage_name} probe_count={probe_count} "
                        f"max_probes={max_probes} - forcing state transition"
                    )
                    tool_input['response'] = ''
                    tool_input['should_function_call'] = True

            other_params['probe_guard'] = {
                'stage': stage_name,
                'probe_count': probe_count,
                'max_probes': max_probes,
            }
            locked_session.other_params = other_params
            locked_session.save(update_fields=['other_params'])

        chat_session.other_params = other_params
        return response

    def build_non_llm_function_call(self, state_machine):
        return {
            "toolUseId": "non_llm_auto",
            "name": "get_state_information",
            "input": {
                "next_state_name": state_machine.name,
                "reason": "NON_LLM auto transition"
            }
        }

    def handle_response(self, **kwargs):
        session_id = kwargs['session_id']
        # Root span for this response cycle — every span below nests under it.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="handle_response",
            input={"session_id": session_id, "route": kwargs.get('language')},
        ) as handle_response_span:
            chat_session = ChatSession.objects.get(session=session_id)
            chunks = []
            is_function_call = False

            # Span: checking for a bot-specific early-return path before any LLM call.
            with langfuse.start_as_current_observation(as_type="span", name="check_early_return") as early_return_span:
                early_return = self.check_early_return(chat_session, **kwargs)
                early_return_span.update(output={"early_return_type": type(early_return).__name__ if early_return is not None else None})

            if early_return is not None:
                if isinstance(early_return, str):
                    handle_response_span.update(output={"path": "early_return_str"})
                    return early_return
                elif isinstance(early_return, dict):
                    if early_return.get('skip_llm', False):
                        kwargs['skip_llm'] = True
                    else:
                        is_function_call = self.is_function_call(response=early_return)
                else:
                    handle_response_span.update(output={"path": "early_return_other"})
                    return early_return

            company_bot = kwargs.get('company_bot')
            try:
                state_machine = CompanyStateMachine.objects.filter(
                    company_bot=company_bot, step=chat_session.current_step
                ).first()
            except Exception as e:
                logger.error(f"Error getting state machine: {e}")
                state_machine = None

            if self.is_non_llm_state(state_machine):
                from chatbot.models import CompanyChat
                user_messages_for_state = CompanyChat.objects.filter(
                    session=session_id, stage=state_machine.name
                ).exclude(message=state_machine.bot_question).exists()

                kwargs['skip_llm'] = True
                kwargs['skip_reason'] = 'non_llm_operation_type'

                if not user_messages_for_state:
                    kwargs['send_bot_question'] = True
                    kwargs['bot_question_from_db'] = state_machine.bot_question or None
                else:
                    kwargs['send_bot_question'] = False
                    kwargs['force_function_call'] = True

            original_prompt = kwargs.get('system_prompt', [])
            preprocessing_result = {'action': 'continue', 'prompt': original_prompt}

            if state_machine and state_machine.preprocess_output_mode not in [
                PreProcessOutputMode.NONE, PreProcessOutputMode.SKIP, PreProcessOutputMode.MODIFY_QUESTION
            ]:
                # Span: running the configured preprocessing step for this state before the LLM call.
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="preprocessing.execute_preprocessing",
                    input={"state_machine": state_machine.name},
                ) as preprocessing_span:
                    preprocessing_result = self.preprocessing_service.execute_preprocessing(
                        state_machine, original_prompt, **kwargs
                    )
                    preprocessing_span.update(output={"action": preprocessing_result.get('action')})

                if preprocessing_result['action'] == 'skip':
                    kwargs['skip_llm'] = True
                    kwargs['skip_reason'] = 'preprocessing'
                elif preprocessing_result['action'] == 'modify_question':
                    kwargs['modified_bot_question'] = preprocessing_result.get('modified_bot_question')
                    kwargs['system_prompt'] = preprocessing_result.get('prompt', original_prompt)
                elif preprocessing_result['action'] == 'continue':
                    kwargs['system_prompt'] = preprocessing_result.get('prompt', original_prompt)

            response = None
            streaming_completed = False
            if not is_function_call and not kwargs.get('skip_llm', False):
                # Span (not generation) around the LLM call boundary — the real generation
                # (model, tokens, cost) is created deeper inside handle_bedrock_model /
                # handle_openai_response_api in llm_script.py; this just times the whole call.
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="get_llm_response",
                    input={
                        "system_prompt": kwargs.get('system_prompt'),
                        "provider": company_bot.provider,
                    },
                ) as llm_response_span:
                    result = self.get_llm_response(**kwargs)

                    if isinstance(result, tuple):
                        response, extra_content, finish_reason = result
                        if extra_content:
                            kwargs['llm_extra_content'] = extra_content
                    else:
                        response = result
                        finish_reason = None

                    llm_response_span.update(
                        output=str(response)[:500] if response else None,
                        metadata={"finish_reason": finish_reason},
                    )

                use_streaming = self.should_use_streaming(company_bot)
                streaming_completed = finish_reason == "stop" and use_streaming

                if response is None:
                    bot_vernacular = BotVernacular.objects.filter(
                        company_bot=company_bot, language=kwargs['language']
                    ).first()
                    error_message = bot_vernacular.error_message if (
                            bot_vernacular and bot_vernacular.error_message
                    ) else self.default_error_message
                    translated_message = self.translate_message(
                        message=error_message, channel_name=kwargs['channel_name'],
                        step_number=chat_session.current_step, language=kwargs['language'], company_bot=company_bot
                    )
                    self.save_message(
                        session_id=session_id, profile_id=kwargs['profile_id'], message=error_message,
                        chunks=chunks, status=ChatStatus.IN_PROGRESS, translated_message=translated_message,
                        stage=state_machine.name if state_machine else None
                    )
                    handle_response_span.update(output={"path": "llm_error"})
                    return error_message

            if response is not None and company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
                # Span: enforcing the follow-up probe budget for Bedrock's turn_response tool calls.
                with langfuse.start_as_current_observation(as_type="span", name="apply_turn_response_guard") as turn_guard_span:
                    response = self.apply_turn_response_guard(response, chat_session, state_machine)
                    turn_guard_span.update(output={"response_preview": str(response)[:300]})

            if is_function_call and response is None:
                response = early_return
            if kwargs.get('force_function_call') and state_machine:
                is_function_call = True
                response = self.build_non_llm_function_call(state_machine)

            if not is_function_call:
                is_function_call = self.is_function_call(response=response) if state_machine else False

            if is_function_call and state_machine and response:
                # Span: running postprocessing to decide the next state-machine stage/skip logic.
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="postprocessing.execute_postprocessing",
                    input={"state_machine": state_machine.name},
                ) as postprocessing_span:
                    postprocessing_result = self.postprocessing_service.execute_postprocessing(
                        state_machine, response, **kwargs
                    )
                    postprocessing_span.update(output=postprocessing_result)

                if postprocessing_result.get('skip_next_stage', False):
                    kwargs['skip_next_stage'] = True
                    kwargs['target_stage'] = state_machine.skip_to_step
                    next_stage_number = kwargs['target_stage']
                    try:
                        next_state_machine = CompanyStateMachine.objects.get(
                            company_bot=company_bot, step=next_stage_number
                        )
                        next_stage_preprocessing_result = self.preprocessing_service.execute_preprocessing(
                            next_state_machine, kwargs.get('system_prompt', []), **kwargs
                        )
                        if next_stage_preprocessing_result['action'] == 'skip':
                            kwargs['skip_next_stage_preprocessing'] = True
                        elif next_stage_preprocessing_result['action'] == 'modify_question':
                            kwargs['modified_bot_question'] = next_stage_preprocessing_result.get('modified_bot_question')
                            kwargs['system_prompt'] = next_stage_preprocessing_result.get('prompt', original_prompt)
                    except CompanyStateMachine.DoesNotExist:
                        logger.error(f"Next state machine {next_stage_number} not found for preprocessing")
                else:
                    next_stage_number = chat_session.current_step + 1
                    try:
                        next_state_machine = CompanyStateMachine.objects.get(
                            company_bot=company_bot, step=next_stage_number
                        )
                        next_stage_preprocessing_result = self.preprocessing_service.execute_preprocessing(
                            next_state_machine, kwargs.get('system_prompt', []), **kwargs
                        )
                        if next_stage_preprocessing_result['action'] == 'skip':
                            kwargs['skip_next_stage_preprocessing'] = True
                        elif next_stage_preprocessing_result['action'] == 'modify_question':
                            kwargs['modified_bot_question'] = next_stage_preprocessing_result.get('modified_bot_question')
                            kwargs['system_prompt'] = next_stage_preprocessing_result.get('prompt', original_prompt)
                    except CompanyStateMachine.DoesNotExist:
                        logger.info(f"Next state machine {next_stage_number} not found, likely at end of flow")

            result = self.process_response(
                response, chat_session, chunks, streaming_completed=streaming_completed, **kwargs
            )
            handle_response_span.update(output={"result_preview": str(result)[:300] if result else None})
            return result

    def analyze_response_for_postprocessing(self, response):
        return self.is_function_call(response)

    def should_use_streaming(self, company_bot):
        try:
            if hasattr(company_bot, 'stream'):
                return bool(company_bot.stream)
            return False
        except Exception as e:
            logger.error(f"Error determining streaming mode: {e}")
            return False

    def get_llm_response(self, **kwargs):
        """Dispatches to the actual provider call. The generation (model/tokens) is created
        inside handle_bedrock_model / handle_openai_response_api, not here."""
        company_bot = kwargs['company_bot']
        system_prompt = kwargs['system_prompt']
        response = None
        message_to_send = self.get_messages_for_llm(**kwargs)
        print("message_to_send: ", message_to_send)
        session_id = kwargs['session_id']
        profile_id = kwargs.get('profile_id')
        channel_name = kwargs['channel_name']
        chat_session = ChatSession.objects.get(session=session_id)
        state_machine = None
        try:
            state_machine = CompanyStateMachine.objects.get(
                company_bot=company_bot, step=chat_session.current_step
            )
        except Exception as e:
            logger.error(f"Error getting state machine for tools: {e}")

        tools = None
        has_state_machine_tool_context = (
                state_machine
                and hasattr(state_machine, 'tool_context')
                and state_machine.tool_context
                and state_machine.tool_context.strip()
        )
        has_company_bot_tool_context = (
                company_bot
                and hasattr(company_bot, 'tool_context')
                and company_bot.tool_context
                and company_bot.tool_context.strip()
        )

        if has_state_machine_tool_context or (
                company_bot.bot_type == CompanyBotTypeChoices.SIMPLE and has_company_bot_tool_context
        ):
            tool_context = (
                state_machine.tool_context.strip()
                if has_state_machine_tool_context
                else company_bot.tool_context.strip()
            )
            try:
                import json_repair
                tools = json_repair.repair_json(tool_context, return_objects=True)
                logger.info("Using state machine tool_context")
            except Exception as e:
                logger.error(f"Failed to parse state machine tool_context: {e}")
                tools = None

        if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
            try:
                # Actual Bedrock call — the generation (model/tokens/cost) is created inside this function.
                response = handle_bedrock_model(
                    system_prompt=system_prompt,
                    messages=message_to_send,
                    model_name=company_bot.llm_model,
                    temperature=company_bot.bot_temperature,
                    max_token=company_bot.max_token,
                    company_bot=company_bot,
                    tools=tools
                )
            except Exception as e:
                logger.error(f"Bedrock Error: %s", e)
                response = None

        elif company_bot.provider == LLMProvider.OPENAI:
            use_streaming = self.should_use_streaming(company_bot)
            logger.info(f"Using OpenAI {'streaming' if use_streaming else 'non-streaming'} for session {session_id}")

            result = self._handle_openai_response(
                system_prompt=system_prompt,
                messages=message_to_send,
                company_bot=company_bot,
                channel_name=channel_name,
                session_id=session_id,
                profile_id=profile_id,
                stream=use_streaming
            )

            if result is None or (isinstance(result, tuple) and result[0] is None):
                logger.error("OpenAI response returned None - error occurred")
                response = None
            elif isinstance(result, tuple):
                response, extra_content, finish_reason = result
                if extra_content:
                    print("Setting extra_content to ", extra_content)
                    kwargs['llm_extra_content'] = extra_content
                return response, extra_content, finish_reason
            else:
                response = result
        return response, None, None

    def _handle_openai_response(self, system_prompt, messages, company_bot,
                                channel_name, session_id, profile_id, stream=False):
        final_extra_content = None
        function_call_result = None
        try:
            logger.info(f"Processing free-flow for session {session_id}, channel {channel_name}")
            tools = None
            tool_choice = None
            try:
                import json_repair
                tool_context = json_repair.repair_json(company_bot.tool_context, return_objects=True)
                if tool_context:
                    if isinstance(tool_context, list):
                        tools = tool_context
                        tool_choice = "auto"
                    elif isinstance(tool_context, dict):
                        tools = tool_context.get("tool")
                        tool_choice = tool_context.get("tool_choice", "auto")
                logger.info("Using state machine tool_context")
            except Exception as e:
                logger.error(f"Failed to parse state machine tool_context: {e}", exc_info=True)
                print(f"❌ Error parsing tool_context: {e}")

            accumulated_response = ""
            finish_reason = None

            # Actual OpenAI Responses API call — generation is created inside this generator function.
            for chunk_data in handle_openai_response_api(
                    messages=messages,
                    system_prompt=system_prompt,
                    max_token=company_bot.max_token if company_bot.max_token else 2048,
                    temperature=company_bot.bot_temperature if company_bot.bot_temperature is not None else 0.0,
                    company_bot=company_bot,
                    top_p=company_bot.filter_score if company_bot.filter_score else None,
                    tool_choice=tool_choice,
                    tools=tools,
                    stream=stream
            ):
                content = chunk_data.get('content', '')
                finish_reason = chunk_data.get('finish_reason')
                error = chunk_data.get('error')
                extra_content = chunk_data.get('extra_content')
                function_call = chunk_data.get('function_call')

                if error:
                    logger.error(f'OpenAI error: {error}')
                    if stream:
                        self._send_error_chunk(channel_name, "Error processing your request")
                    return None

                if function_call:
                    function_call_result = function_call
                    logger.info(f"Function call received: {function_call['name']}")
                    logger.info(f"Function arguments: {function_call.get('arguments', {})}")

                if content:
                    accumulated_response += content
                if extra_content:
                    print("got extra_content: ", extra_content)
                    final_extra_content = extra_content

                print(f"Finish reason: {finish_reason}")

                if stream:
                    if content:
                        self._send_chunk(channel_name, content, None, None)
                    if finish_reason == "stop":
                        self._send_chunk(channel_name, "", "stop", final_extra_content)

            if function_call_result:
                logger.info(f"Returning function call result: {function_call_result}")
                response_dict = {
                    'function_call': function_call_result,
                    'finish_reason': 'function_call',
                    'extra_content': final_extra_content
                }
                return response_dict, final_extra_content, 'function_call'

            if accumulated_response:
                save_in_company_db(
                    session_id=session_id, profile_id=profile_id, initiated_by='AI',
                    message=accumulated_response, chunks=None, status=ChatStatus.IN_PROGRESS, stage=None
                )
                logger.info(f'Completed OpenAI response, length: {len(accumulated_response)} chars')

            return accumulated_response, final_extra_content, finish_reason

        except Exception as e:
            logger.error(f'Error in OpenAI response handling: {e}', exc_info=True)
            if stream:
                self._send_error_chunk(channel_name, "An error occurred processing your message")
            return None, None, None

    def _send_chunk(self, channel_name, content, finish_reason, extra_content=None):
        try:
            message_data = {
                "type": "chat.message",
                "text": {
                    "msg": content, "source": "bot", "type": "chunk", "finish_reason": finish_reason
                },
            }
            if extra_content:
                message_data["text"]["extra_content"] = extra_content
            async_to_sync(channel_layer.send)(channel_name, message_data)
        except Exception as e:
            logger.error(f"Failed to send chunk to channel {channel_name}: {e}", exc_info=True)

    def _send_error_chunk(self, channel_name, error_msg):
        try:
            async_to_sync(channel_layer.send)(
                channel_name,
                {
                    "type": "chat.message",
                    "text": {"msg": error_msg, "source": "bot", "type": "error", "finish_reason": "error"},
                },
            )
        except Exception as e:
            logger.error(f"Failed to send error to channel {channel_name}: {e}")

    def get_default_tools_config(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_state_information",
                    "description": "Get the information of the state you want to be in.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "state_name": {
                                "type": "string",
                                "description": "Name of the next state provided in the context."
                            }
                        },
                        "required": ["state_name"]
                    }
                }
            }
        ]

    def get_tools_config(self):
        logger.info("get_tools_config() is deprecated, use get_default_tools_config() instead")
        return self.get_default_tools_config()

    def is_function_call(self, response):
        if isinstance(response, dict):
            if 'toolUseId' in response and 'name' in response:
                return response.get('name') == 'get_state_information'
            elif 'name' in response and 'parameters' in response:
                return response.get('name') == 'get_state_information'
            elif 'function_call' in response:
                function_call = response.get('function_call', {})
                return function_call.get('name') == 'get_state_information'
            elif 'tool_calls' in response:
                tool_calls = response.get('tool_calls', [])
                for tool_call in tool_calls:
                    if 'function' in tool_call:
                        function = tool_call.get('function', {})
                        if function.get('name') == 'get_state_information':
                            return True
                return False
            elif 'output' in response and 'message' in response.get('output', {}):
                content = response['output']['message'].get('content', [])
                for item in content:
                    if 'toolUse' in item:
                        tool_use = item.get('toolUse', {})
                        if tool_use.get('name') == 'get_state_information':
                            return True
                return False
            elif 'parameters' in response or 'input' in response:
                nested_data = response.get('parameters') or response.get('input')
                if isinstance(nested_data, dict):
                    if 'next_state_name' in nested_data:
                        return True
                    elif 'response' in nested_data:
                        return False
                    else:
                        return 'get_state_information' in str(nested_data)
                return 'get_state_information' in str(nested_data)
            elif any(key in response for key in ['toolUseId', 'tool_calls', 'function_call']):
                return 'get_state_information' in str(response)
            return False
        elif isinstance(response, str):
            return 'get_state_information' in response
        return False

    def save_message(self, session_id, profile_id, message, chunks,
                     status, translated_message, stage=None, other_params=None):
        save_in_company_db(
            session_id=session_id, profile_id=profile_id, initiated_by='AI',
            message=message, chunks=chunks, status=status, translated_message=translated_message,
            stage=stage, other_params=other_params
        )

    def translate_message(self, message, channel_name, step_number, language, company_bot, extra_content=None, state_machine=None):
        """Translate and send message"""
        return translate_and_send_message(
            accumulated_message=message,
            current_channel_name=channel_name,
            current_step_number=step_number,
            finish_reason="stop",
            route=language,
            company_bot=company_bot,
            extra_content=extra_content,
            state_machine=state_machine
        )

    def get_chat_status(self, state_machine, company_bot):
        last_state = CompanyStateMachine.objects.filter(company_bot=company_bot).order_by('step').last()
        max_step = last_state.step if last_state else None
        if state_machine.step == max_step:
            return ChatStatus.COMPLETED
        else:
            return ChatStatus.IN_PROGRESS

    @abstractmethod
    def check_early_return(self, chat_session, **kwargs):
        pass

    @abstractmethod
    def get_messages_for_llm(self, **kwargs):
        pass

    @abstractmethod
    def process_response(self, response, chat_session, chunks, **kwargs):
        pass