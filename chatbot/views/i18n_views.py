from rest_framework.views import APIView
from rest_framework.response import Response
from chatbot.models import FlowTranslationMapping, Flow


class I18nConfigView(APIView):
    """
    Returns translation mapping config
    Flow is mandatory, label & language are optional
    """

    def get(self, request):
        flow_route = request.GET.get("flow_route")
        label = request.GET.get("label")
        language = request.GET.get("language")

        if not flow_route:
            return Response({"error": "flow_route is required"}, status=400)

        try:
            flow = Flow.objects.get(flow_route=flow_route)
        except Flow.DoesNotExist:
            return Response({"error": "Invalid flow route"}, status=404)

        qs = FlowTranslationMapping.objects.select_related("translation_file").filter(flow=flow)

        response = {}

        for obj in qs:
            tf = obj.translation_file

            if not tf or not tf.s3_key:
                continue

            if label and tf.label != label:
                continue

            if language and tf.language != language:
                continue

            namespace = tf.namespace
            lang = tf.language

            if namespace not in response:
                response[namespace] = {}

            response[namespace][lang] = tf.public_url

        return Response(response)
