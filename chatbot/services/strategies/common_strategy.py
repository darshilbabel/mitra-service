from chatbot.services.strategies.base_strategy import BotStrategy
from chatbot.utils.langfuse_client import get_langfuse_client

langfuse = get_langfuse_client()


class CommonBotStrategy(BotStrategy):
    """Common Strategy for bot functionality"""

    def get_default_route(self):
        return ''

    def get_handler_type(self):
        return 'common'

    def process_session(self, session_data, **kwargs):
        with langfuse.start_as_current_observation(
            as_type="span",
            name="common_strategy.process_session",
            input={"current_step": session_data['chat_session'].current_step},
        ) as span:
            chat_session = session_data['chat_session']
            company_bot = session_data['company_bot']
            try:
                from chatbot.models.company_models import CompanyStateMachine
                state_machine = CompanyStateMachine.objects.filter(
                    company_bot=company_bot, step=chat_session.current_step
                ).first()
                span.update(output={"state_machine": state_machine.name if state_machine else None})
                return {'state_machine': state_machine}
            except Exception as e:
                span.update(output={"error": str(e)}, level="ERROR")
                return {'error': f"State machine error: {e}"}

    def get_response(self, **kwargs):
        return self.response_handler.handle_response(**kwargs)