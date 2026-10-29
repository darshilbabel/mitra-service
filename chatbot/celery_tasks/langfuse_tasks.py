import json
from celery import shared_task
from chatbot.llm_models.llm_script import handle_bedrock_model
from chatbot.models.company_models import CompanyBot, CompanyStateMachine
from chatbot.services.core.prompt_builder import PromptBuilder
from chatbot.utils.chat_utils import get_ai_profile, get_guided_chat
from chatbot.utils.langfuse_client import get_langfuse_client
import traceback
import logging
from datetime import datetime
from langfuse import Evaluation
from asgiref.sync import sync_to_async

logger = logging.getLogger('django')

async def chatbot_response(companybot: CompanyBot, input: dict, stage: str | None, is_json_response: bool):
    try:
        messages = input.get("messages")

        if messages is None:
            raise ValueError("Messages is none")

        if stage and type(stage) is not str:
            raise ValueError("Stage should be of type string")

        state_machine = None
        if stage:
            state_machine = CompanyStateMachine.objects.filter(company_bot=companybot, stage=stage).first()

        chatbot_system_prompt = await sync_to_async(PromptBuilder.build_system_prompt)(company_bot=companybot, state_machine=state_machine)
        print(chatbot_system_prompt)

        chatbot_tools = None
        if chatbot_tools:
            chatbot_tools = json.loads(str(companybot.tool_context)).get("content_tool")

        chatbot_response = handle_bedrock_model(
            company_bot=companybot, system_prompt=chatbot_system_prompt,
            messages=messages,tools=chatbot_tools, is_json_response=is_json_response
        )

        return chatbot_response

    except Exception as e:
        logger.error("Error occured during execution: %s", e, exc_info=True)
        raise e


async def evaluate_dataset(output, expected_output, evaluator_bot: CompanyBot):
    try:
        evaluator_system_prompt = await sync_to_async(PromptBuilder.build_system_prompt)(company_bot=evaluator_bot)

        evaluator_user_message = '''
        Here's the actual output and the expected output
        <actual_output>
            {actual_output}
        </actual_output>

        <expected_output>
            {expect_output}
        </expected_output>
        '''

        evaluator_tools = None
        if evaluator_tools:
            evaluator_tools = json.loads(str(evaluator_bot.tool_context)).get("content_tool")

        formatted_evaluator_user_message = evaluator_user_message.format_map({
            "actual_output": output if type(output) is str else json.dumps(output),
            "expect_output": expected_output if type(expected_output) is str else json.dumps(expected_output)
        })

        ai_profile_id = await sync_to_async(get_ai_profile)()

        evaluator_messages = get_guided_chat(evaluator_bot, [{
            "receiver": ai_profile_id,
            "message": formatted_evaluator_user_message
        }])


        evaluator_output = handle_bedrock_model(
            company_bot=evaluator_bot, system_prompt=evaluator_system_prompt,
            messages=evaluator_messages,tools=evaluator_tools, is_json_response=True
        )
        evaluator_output_reason = evaluator_output.get("reason")
        evaluator_output_score = evaluator_output.get("score")

        print("Output")
        print("="*50)
        print(json.dumps(evaluator_output, indent=2))
        print("="*50)

        if not evaluator_output_reason or evaluator_output_score:
            raise ValueError("Invalid output by the evaluator")

        return Evaluation(
            name=evaluator_bot.get("name", "SampleExperimentName"),
            value=evaluator_output_score,
            comment=evaluator_output_reason
        )

    except Exception as e:
        logger.error("Error occurer during evaluation: %s", e, exc_info=True)
        raise e


@shared_task(bind=True)
def run_langfuse_experiment(self, dataset_id: str, dataset_name: str, config: dict):
    try:
        langfuse = get_langfuse_client()

        datasets = langfuse.get_dataset(name=dataset_name)

        evaluator_bot_route = config.get("evaluator_bot_route")
        chat_bot_route = config.get("chat_bot_route")
        is_json_response = config.get("is_json_response", False)

        # TODO: These data should be fetched inside the webhook view and a
        # relvant error should be returned then and there itself. Even checking if
        # the config is valid should be done insde the webhook view and not in
        # celery
        chatbot = CompanyBot.objects.get(route=chat_bot_route)
        evaluator_bot = CompanyBot.objects.get(route=evaluator_bot_route)


        if not evaluator_bot_route or not chat_bot_route:
            raise ValueError("Invalid config sent")

        def get_chatbot_output(*, item, **kwargs):
            return chatbot_response(companybot=chatbot, input=item.input, stage=config.get("stage"), is_json_response=is_json_response)

        def evaluate(*, input, output, expected_output, **kwargs):
            return evaluate_dataset(output=output, expected_output=expected_output, evaluator_bot=evaluator_bot)

        result = datasets.run_experiment(
            name = f'Experiment {datetime.now().strftime("%Y-%b-%d %H:%M")}',
            task=get_chatbot_output,
            evaluators=[evaluate],
            max_concurrency=1
        )

        logger.info(f"Evaluations completed {result.format()}")


    except Exception as e:
        traceback.print_exc()
        logger.error("Error occured in langfuse task: %s", e, exc_info=True)
