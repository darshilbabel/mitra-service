"""
Run every Langfuse dataset in the ``report/guest-discussion`` folder through the
``/chaupal-story`` CompanyBot (Bedrock Converse) and print the outputs.

Usage
-----
    python manage.py run_chaupal_tests
    python manage.py run_chaupal_tests --folder report/guest-discussion --route /chaupal-story

Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL.
"""

import copy
import json
import logging

from django.core.management.base import BaseCommand, CommandError

from chatbot.llm_models.llm_script import handle_bedrock_model
from chatbot.models import CompanyBot
from chatbot.utils.langfuse_client import get_langfuse_client


logger = logging.getLogger('django')

DEFAULT_FOLDER = 'report/guest-discussion'
DEFAULT_ROUTE = '/chaupal-story'
PAGE_LIMIT = 100


def list_dataset_names(langfuse, folder):
    """Return names of datasets in ``folder`` (Langfuse folders = '/'-separated name prefixes)."""
    prefix = folder.rstrip('/') + '/'
    names = []
    page = 1
    while True:
        res = langfuse.api.datasets.list(page=page, limit=PAGE_LIMIT)
        names.extend(d.name for d in res.data if d.name == folder or d.name.startswith(prefix))
        if page >= res.meta.total_pages:
            break
        page += 1
    return names


def to_bedrock_messages(item_input):
    """Normalize a dataset item input into Bedrock Converse messages."""
    if isinstance(item_input, str):
        item_input = json.loads(item_input)
    messages = item_input.get('messages') if isinstance(item_input, dict) else item_input
    if not isinstance(messages, list):
        raise ValueError(f"Unsupported dataset item input: {type(item_input).__name__}")

    bedrock_messages = []
    for message in messages:
        content = message['content']
        if isinstance(content, str):
            content = [{'text': content}]
        bedrock_messages.append({'role': message['role'], 'content': copy.deepcopy(content)})
    return bedrock_messages


class Command(BaseCommand):
    help = "Run Langfuse guest-discussion datasets through the /chaupal-story bot and print outputs."

    def add_arguments(self, parser):
        parser.add_argument('--folder', default=DEFAULT_FOLDER, help="Langfuse dataset folder.")
        parser.add_argument('--route', default=DEFAULT_ROUTE, help="CompanyBot route.")

    def handle(self, *args, **options):
        folder = options['folder']
        route = options['route']
        tools = None

        company_bot = CompanyBot.objects.filter(route=route).first()
        if not company_bot:
            raise CommandError(f"No CompanyBot with route '{route}'")
        system_prompt = [{'text': company_bot.context}]
        if company_bot.tool_context:
            tools = (json.loads(company_bot.tool_context)).get('content_tool')

        langfuse = get_langfuse_client()
        dataset_names = list_dataset_names(langfuse, folder)
        if not dataset_names:
            raise CommandError(f"No Langfuse datasets found in folder '{folder}'")
        logger.info("Found %d dataset(s) in '%s': %s", len(dataset_names), folder, dataset_names)

        for dataset_name in dataset_names:
            dataset = langfuse.get_dataset(dataset_name)
            logger.info("Dataset '%s': %d item(s)", dataset_name, len(dataset.items))

            for item in dataset.items:
                try:
                    messages = to_bedrock_messages(item.input)
                    output = handle_bedrock_model(
                        company_bot=company_bot, system_prompt=system_prompt, messages=messages,
                        model_name=company_bot.llm_model, temperature=company_bot.bot_temperature,
                        max_token=company_bot.max_token, tools=tools
                    )
                except Exception as e:
                    logger.error("Dataset '%s' item %s failed: %s", dataset_name, item.id, e, exc_info=True)
                    continue

                self.stdout.write(f"\n===== {dataset_name} | item {item.id} =====")
                self.stdout.write(
                    json.dumps(output, ensure_ascii=False, indent=2) if isinstance(output, (dict, list)) else str(output)
                )

        langfuse.flush()
