import logging
from chatbot.models import LLMProvider

logger = logging.getLogger(__name__)


class PromptBuilder:
    """Centralized prompt building logic"""

    @staticmethod
    def build_system_prompt(company_bot, state_machine=None, other_data=None):
        """Build system prompt based on provider type"""

        system_parts = []

        system_parts.append(company_bot.context.strip())

        if company_bot.pre_context and company_bot.pre_context.strip():
            logger.info(f"[PromptBuilder] Pre-context found for bot '{company_bot.name}': {company_bot.pre_context[:100]}...")
            system_parts.append(company_bot.pre_context.strip())
        else:
            logger.debug(f"[PromptBuilder] No pre-context found for bot '{company_bot.name}'")

        if state_machine and state_machine.context:
            system_parts.append(state_machine.context.strip())

        if state_machine and state_machine.completion_criteria:
            system_parts.append(f"Completion Criteria:\n{state_machine.completion_criteria.strip()}")

        if other_data:
            system_parts.append(f"Other Data:\n{str(other_data).strip()}")

        tool_context = ""
        if (state_machine and
                hasattr(state_machine, 'tool_context') and
                state_machine.tool_context and
                state_machine.tool_context.strip()):
            tool_context = None
        elif company_bot.tool_context and company_bot.tool_context.strip():
            tool_context = company_bot.tool_context.strip()

        if company_bot.provider == LLMProvider.BEDROCK_CONVERSE:
            result = [{'text': system_parts[0]}]
            if len(system_parts) > 1:
                result.append({'text': "\n\n".join(system_parts[1:])})
            if tool_context:
                result.append({'text': tool_context})
            return result

        elif company_bot.provider == LLMProvider.OPENAI:
            return [{
                'role': 'system',
                'content': "\n\n".join(system_parts)
            }]

        return []
