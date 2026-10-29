import hmac
import hashlib
import json
from functools import wraps

from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpRequest
from django.conf import settings
from typing import Optional

from chatbot.celery_tasks.langfuse_tasks import run_langfuse_experiment


def verify_langfuse_signature(request: HttpRequest, secret: Optional[str] = None) -> bool:
    """
    Verify the x-langfuse-signature header from Langfuse.
    Format: t=<timestamp>,v1=<signature>
    """
    secret = secret or getattr(settings, "LANGFUSE_WEBHOOK_SECRET", None)

    if not secret:
        # Request signing not enabled; skip verification
        return True

    signature_header = request.headers.get("x-langfuse-signature", "")
    if not signature_header:
        return False

    # Parse signature header: t=<timestamp>,v1=<signature>
    parts = {}
    for part in signature_header.split(","):
        key, value = part.split("=", 1)
        parts[key] = value

    signature = parts.get("v1")
    if not signature:
        return False

    # Body should be raw bytes for HMAC
    body = request.body

    # Compute HMAC-SHA256
    expected_sig = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_sig)


def require_langfuse_signature(view_func):
    """Decorator to verify Langfuse webhook signature."""
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not verify_langfuse_signature(request):
            return JsonResponse(
                {"error": "Invalid signature"},
                status=401
            )
        return view_func(request, *args, **kwargs)
    return wrapper

@csrf_exempt
@require_http_methods(["POST"])
@require_langfuse_signature
def trigger_experiment_webhook(request: HttpRequest) -> JsonResponse:
    """
        Webhook endpoint triggered by Langfuse when user clicks 'Run' on a custom experiment.

        Expected payload:
        {
            "evaluator_bot_route": string,
            "chatbot_bot_route": string
        }

        This view should return 2xx quickly and spawn the experiment async.
    """
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400
        )

    dataset_id = payload.get("datasetId")
    dataset_name = payload.get("datasetName")
    config = payload.get("payload", {})

    try:
        config = json.loads(config)

    except json.JSONDecodeError:
        return JsonResponse(
            { "error": "Invalid payload sent" },
            status=400
        )

    if not dataset_id:
        return JsonResponse(
            {"error": "datasetId is required"},
            status=400
        )

    run_langfuse_experiment.delay(
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        config=config
    )

    return JsonResponse({
        "status": "accepted",
        "message": "Experiment run initiated",
        "datasetId": dataset_id
    }, status=202)
