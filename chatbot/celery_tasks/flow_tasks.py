import json
import logging

from celery import shared_task

from chatbot.models import CompanyChat
from chatbot.services.core.bot_service_factory import BotServiceFactory
from chatbot.services.core.orchestrator import ChatOrchestrator
from chatbot.utils.langfuse_client import get_langfuse_client

from langfuse import observe, propagate_attributes
logger = logging.getLogger('django')
langfuse = get_langfuse_client()


def get_last_user_message(session_id):
    """Fetch the most recent user message for this session — used as a clean, human-readable trace input."""
    chat = CompanyChat.objects.filter(
        session=session_id, source='User'
    ).order_by('-created_at').first()
    return chat.message if chat else None


def extract_reply_text(response):
    if isinstance(response, dict):
        return (
            response.get('accumulated_message')
            or response.get('message')
            or response.get('text')
            or json.dumps(response)[:500]
        )
    return str(response)


@shared_task
@observe(as_type="span", name="get_flow_response",capture_output=False,)
def get_flow_response(channel_name, session_id, profile_id, route, bot_type, bot_route, text_data=None):
    print(f"last_user_message is {text_data}")
    
    # Update the current observation created by @observe with custom input and metadata
    langfuse.update_current_span(
     input={"user_message": text_data},
     metadata={
        "session_id": str(session_id),
        "profile_id": str(profile_id) if profile_id else "",
        "route": str(route),
        "bot_type": str(bot_type),
        "bot_route": str(bot_route),
      },
    )

    # Propagate attributes to all child spans (ensures metrics and costs aggregate properly)
    with propagate_attributes(
        session_id=session_id,
        user_id=str(profile_id) if profile_id else None,
        tags=[f"bot_route:{bot_route}"],
    ):
        print(f"bot_type is {bot_type} and bot_route is {bot_route}")
        
        bot_strategy = BotServiceFactory.create_bot_service(bot_type=bot_type, route=bot_route)
        orchestrator = ChatOrchestrator(bot_strategy=bot_strategy)
        
        response = orchestrator.process_chat_request(
            channel_name=channel_name, session_id=session_id, profile_id=profile_id, language=route
        )

        reply_text = extract_reply_text(response)
        
        # Explicitly set the output for the UI, overriding the default full return object
        langfuse.update_current_span(output={"bot_reply": reply_text})
        
        return response