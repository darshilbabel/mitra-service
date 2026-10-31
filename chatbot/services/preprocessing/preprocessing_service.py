from chatbot.models import PreProcessType
from chatbot.services.preprocessing.base_preprocessor import SimplePreprocessor, ComplexPreprocessor
from chatbot.services.preprocessing.output_handlers import PreprocessOutputHandler
from chatbot.utils.langfuse_client import get_langfuse_client
import logging

langfuse = get_langfuse_client()
logger = logging.getLogger('django')


class PreprocessingService:
    """Main service for handling preprocessing operations"""

    def __init__(self):
        self.preprocessors = {
            PreProcessType.SIMPLE: SimplePreprocessor(),
            PreProcessType.COMPLEX: ComplexPreprocessor()
        }

    def execute_preprocessing(self, state_machine, original_prompt, **kwargs):
        """Execute preprocessing based on state machine configuration"""
        # Span: dispatching to the configured preprocessor and applying its output mode.
        with langfuse.start_as_current_observation(
            as_type="span",
            name="preprocessing_service.execute_preprocessing",
            input={
                "state_machine": state_machine.name,
                "step": state_machine.step,
                "preprocess_type": state_machine.preprocess_type,
                "preprocess_output_mode": state_machine.preprocess_output_mode,
            },
        ) as preprocessing_span:
            # Skip preprocessing if not configured
            if state_machine.preprocess_type == PreProcessType.NONE:
                result = {'action': 'continue', 'prompt': original_prompt}
                preprocessing_span.update(output={"path": "not_configured", "action": result['action']})
                return result

            # Get appropriate preprocessor
            preprocessor = self.preprocessors.get(state_machine.preprocess_type)
            if not preprocessor:
                logger.info(f"No preprocessor found for type: {state_machine.preprocess_type}")
                result = {'action': 'continue', 'prompt': original_prompt}
                preprocessing_span.update(
                    output={"path": "no_preprocessor", "action": result['action']}, level="WARNING"
                )
                return result

            # Execute preprocessing
            logger.info(f"Executing {state_machine.preprocess_type} preprocessing for state: {state_machine.name}")
            preprocess_response = preprocessor.preprocess(state_machine, **kwargs)

            # Span: mapping the preprocessor's raw response to an action (continue/skip/modify_question).
            with langfuse.start_as_current_observation(
                as_type="span",
                name="preprocess_output_handler.handle_output",
                input={
                    "preprocess_output_mode": state_machine.preprocess_output_mode,
                    "preprocess_response": preprocess_response,
                },
            ) as output_handler_span:
                result = PreprocessOutputHandler.handle_output(
                    state_machine.preprocess_output_mode, preprocess_response, original_prompt, **kwargs
                )
                output_handler_span.update(output={k: v for k, v in result.items() if k != 'prompt'})

            preprocessing_span.update(output={
                "path": "executed",
                "action": result.get('action'),
                "modified_bot_question": result.get('modified_bot_question'),
            })
            return result

    def register_preprocessor(self, preprocess_type, preprocessor):
        """Register a new preprocessor"""
        self.preprocessors[preprocess_type] = preprocessor
