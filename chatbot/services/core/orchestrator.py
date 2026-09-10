import traceback
import logging
from langfuse import get_client
from .base_service import BaseChatService
from .prompt_builder import PromptBuilder
from .message_handler import MessageHandler
from chatbot.celery_tasks.handle_message import translate_and_send_message

logger = logging.getLogger('django')
langfuse = get_client()


class ChatOrchestrator:
    def __init__(self, bot_strategy):
        self.bot_strategy = bot_strategy
        self.base_service = BaseChatService()
        self.prompt_builder = PromptBuilder()
        self.message_handler = MessageHandler()

    def process_chat_request(self, channel_name, session_id, profile_id, language):
        try:
            # Root span for the whole chat-request lifecycle — every span below nests under this one.
            with langfuse.start_as_current_observation(
                as_type="span",
                name="process_chat_request",
                input={"session_id": session_id, "profile_id": profile_id, "route": self.bot_strategy.get_route()},
            ) as request_span:

                # Span: fetching chat session, company bot, profile, and chat history for this request.
                with langfuse.start_as_current_observation(
                    as_type="span", name="get_session_data", input={"session_id": session_id, "profile_id": profile_id}
                ) as session_data_span:
                    session_data = self.base_service.get_session_data(
                        session_id=session_id, profile_id=profile_id, bot_route=self.bot_strategy.get_route()
                    )
                    session_data_span.update(output={
                        "chat_session_id": getattr(session_data['chat_session'], 'id', None),
                        "company_bot_id": getattr(session_data['company_bot'], 'id', None),
                        "profile_found": session_data['profile'] is not None,
                        "company_chats_count": session_data['company_chats'].count(),
                    })

                # Span: resolving the bot's vernacular config and intro message for this profile.
                with langfuse.start_as_current_observation(as_type="span", name="get_bot_vernacular_and_intro") as vernacular_span:
                    bot_vernacular, intro_mssg = self.base_service.get_bot_vernacular_and_intro(
                        company_bot=session_data['company_bot'], profile=session_data['profile']
                    )
                    vernacular_span.update(output={"has_vernacular": bot_vernacular is not None, "intro_mssg": intro_mssg})

                other_info = self.base_service.get_user_profile_info(profile=session_data['profile'])

                messages = self.message_handler.prepare_messages(
                    company_bot=session_data['company_bot'], company_chats=session_data['company_chats'],
                    intro_mssg=intro_mssg, other_info=other_info
                )

                # Span: strategy-specific session processing (e.g. state machine lookup).
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="bot_strategy.process_session", input={"session_id": session_id}
                ) as process_session_span:
                    session_result = self.bot_strategy.process_session(
                        session_data, intro_mssg=intro_mssg, other_info=other_info, messages=messages
                    )
                    process_session_span.update(output={k: v for k, v in session_result.items() if k != 'messages'})

                if session_result.get('error'):
                    result = self._handle_error_response(
                        error_msg=session_result['error'], channel_name=channel_name, language=language,
                        chat_session=session_data['chat_session'], company_bot=session_data['company_bot']
                    )
                    request_span.update(output=result)
                    return result

                state_machine = session_result.get('state_machine', None)

                # Span: filtering prior chats down to the current state machine stage's relevant history.
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="get_filtered_chats",
                    input={"session_id": session_id, "state_machine": getattr(state_machine, 'name', None)},
                ) as filtered_chats_span:
                    temp_company_chats = self.message_handler.get_filtered_chats(
                        session_id=session_id, state_machine=state_machine,
                        company_chats=session_data['company_chats']
                    )
                    filtered_chats_span.update(output={"filtered_count": len(temp_company_chats) if hasattr(temp_company_chats, '__len__') else None})

                temp_messages = self.message_handler.prepare_messages(
                    company_bot=session_data['company_bot'], company_chats=temp_company_chats,
                    intro_mssg=intro_mssg, other_info=other_info
                )

                prompt_to_use = self.prompt_builder.build_system_prompt(
                    company_bot=session_data['company_bot'], state_machine=state_machine
                )

                response_params = {
                    'system_prompt': prompt_to_use,
                    'messages': messages,
                    'company_bot': session_data['company_bot'],
                    'session_id': session_id,
                    'channel_name': channel_name,
                    'language': language,
                    'profile_id': profile_id,
                    'temp_messages': temp_messages,
                    'intro_mssg': intro_mssg,
                }
                if hasattr(self.bot_strategy, 'get_route') and 'oneshot' in self.bot_strategy.get_route():
                    response_params['remaining_stages'] = session_result.get('remaining_stages', [])

                # Span: boundary around the strategy's LLM response — the actual model call/generation
                # is created deeper inside (llm_script.py), this just times/contains that whole step.
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="bot_strategy.get_response",
                    input={
                        "system_prompt": prompt_to_use,
                        "messages": messages,
                        "state_machine": getattr(state_machine, 'name', None),
                    },
                ) as get_response_span:
                    response = self.bot_strategy.get_response(**response_params)
                    get_response_span.update(output=response)

                request_span.update(output=response)
                logger.info('Bot response: %s', response)
                return response

        except Exception as e:
            logger.error('Error in chat processing: %s', e, exc_info=True)
            traceback.print_exc()
            raise

    def _handle_error_response(self, error_msg, channel_name, language, chat_session, company_bot):
        logger.info(f"Sending error message: {error_msg}")
        return translate_and_send_message(
            accumulated_message=error_msg, current_channel_name=channel_name, finish_reason="stop",
            current_step_number=chat_session.current_step, route=language, company_bot=company_bot
        )