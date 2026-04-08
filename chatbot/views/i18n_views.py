from rest_framework.views import APIView
from rest_framework.response import Response
from chatbot.models import FlowTranslationMapping, Flow


class I18nConfigView(APIView):
    def get(self, request):
        default_flow_route = "common_flow"
        flow_route = request.GET.get("flow_route", default_flow_route)
        label = request.GET.get("label")
        language = request.GET.get("language")

        # Fetch flows in one query
        flows = Flow.objects.filter(flow_route__in=[flow_route, default_flow_route])
        flow_map = {f.flow_route: f for f in flows}

        flow = flow_map.get(flow_route) or flow_map.get(default_flow_route)
        common_flow = flow_map.get(default_flow_route)

        if not flow or not common_flow:
            return Response({"error": "Common flow not found"}, status=404)

        allowed_languages = set(flow.languages)

        # Priority: lower = higher priority
        priority_map = {
            common_flow.id: 1,
            flow.id: 0,
        }

        mappings = FlowTranslationMapping.objects.select_related("translation_file").filter(
            flow__in=[flow, common_flow]
        )

        final = {}

        for obj in mappings:
            tf = obj.translation_file
            if not tf or not tf.s3_key:
                continue

            if tf.language not in allowed_languages:
                continue

            if label and tf.label != label:
                continue

            if language and tf.language != language:
                continue

            key = (tf.namespace, tf.language)
            current_priority = priority_map[obj.flow_id]

            # Override only if higher priority
            if key not in final or current_priority < final[key]["priority"]:
                final[key] = {
                    "url": tf.public_url,
                    "priority": current_priority
                }

        # Build response
        response = {}
        for (namespace, lang), val in final.items():
            response.setdefault(namespace, {})
            response[namespace][lang] = val["url"]

        return Response(response)
