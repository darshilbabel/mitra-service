from chatbot.models import PostProcessType
from chatbot.services.postprocessing.base_postprocessor import SimplePostprocessor, ComplexPostprocessor
from chatbot.services.postprocessing.postprocess_output_handlers import PostprocessOutputHandler
from chatbot.utils.langfuse_client import get_langfuse_client
import logging

langfuse = get_langfuse_client()
logger = logging.getLogger('django')


class PostprocessingService:
    """Main service for handling postprocessing operations"""

    def __init__(self):
        self.postprocessors = {
            PostProcessType.SIMPLE: SimplePostprocessor(),
            PostProcessType.COMPLEX: ComplexPostprocessor()
        }

    def execute_postprocessing(self, state_machine, llm_response, **kwargs):
        """Execute postprocessing based on state machine configuration"""
        # Span: dispatching to the configured postprocessor and applying its output mode.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="postprocessing_service.execute_postprocessing",
            input={
                "state_machine": state_machine.name,
                "step": state_machine.step,
                "postprocess_type": state_machine.postprocess_type,
                "postprocess_output_mode": state_machine.postprocess_output_mode,
                "llm_response": llm_response,
            },
        ) as postprocessing_span:
            # Skip postprocessing if not configured
            if state_machine.postprocess_type == PostProcessType.NONE:
                result = {'action': 'continue', 'skip_next_stage': False}
                postprocessing_span.update(output={"path": "not_configured", **result})
                return result

            # Get appropriate postprocessor
            postprocessor = self.postprocessors.get(state_machine.postprocess_type)
            if not postprocessor:
                logger.warning(f"No postprocessor found for type: {state_machine.postprocess_type}")
                result = {'action': 'continue', 'skip_next_stage': False}
                postprocessing_span.update(output={"path": "no_postprocessor", **result}, level="WARNING")
                return result

            # Execute postprocessing
            logger.info(f"Executing {state_machine.postprocess_type} postprocessing for state: {state_machine.name}")
            postprocess_response = postprocessor.postprocess(state_machine, llm_response, **kwargs)

            # Span: mapping the postprocessor's raw response to an action (continue/skip_next_stage).
            with langfuse.start_as_current_observation(
                as_type="span",
                name="postprocess_output_handler.handle_output",
                input={
                    "postprocess_output_mode": state_machine.postprocess_output_mode,
                    "postprocess_response": postprocess_response,
                },
            ) as output_handler_span:
                result = PostprocessOutputHandler.handle_output(
                    state_machine.postprocess_output_mode,
                    postprocess_response,
                    llm_response,
                    **kwargs
                )
                output_handler_span.update(output=result)

            postprocessing_span.update(output={"path": "executed", **result})
            return result

    def register_postprocessor(self, postprocess_type, postprocessor):
        """Register a new postprocessor"""
        self.postprocessors[postprocess_type] = postprocessor
