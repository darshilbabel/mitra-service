import django_filters
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status
from chatbot.filter.drf_filter import ChatSessionProfileFilter
from chatbot.models import ChatSession, BotVernacular, SessionFlowName, ChatType
from chatbot.models.company_models import CompanyChat, CompanyBot, CompanyStateMachine, Flow
from chatbot.models.profile_models import Profile
from chatbot.serializer.base_serializer import ChatSessionSerializer
from chatbot.serializer.company_serializer import (
    CompanyBotSerializer, BotVernacularSerializer, ImageConfigurationSerializer,
    FlowLanguagesSerializer, FlowConnectionInfoSerializer
)
from chatbot.serializer.profile_serializer import ProfileSerializer, CompanyChatSerializer


class CompanyChatListCreateView(generics.ListCreateAPIView):
    queryset = CompanyChat.objects.all().order_by('created_at')
    serializer_class = CompanyChatSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['message', 'sender', 'receiver', 'session', 'status']


class CompanyChatRetrieveUpdateDestroyView(generics.RetrieveUpdateAPIView):
    queryset = CompanyChat.objects.all()
    serializer_class = CompanyChatSerializer


class CompanyBotListCreateView(generics.ListCreateAPIView):
    queryset = CompanyBot.objects.all()
    serializer_class = CompanyBotSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['name', 'company__name', 'llm_model', 'company__slug', 'route']


class CompanyBotRetrieveUpdateDestroyView(generics.RetrieveUpdateAPIView):
    queryset = CompanyBot.objects.all()
    serializer_class = CompanyBotSerializer


class BotVernacularListCreateView(APIView):
    """
    Read-only GET endpoint. No serializer, no create/update/delete -
    this data must not be writable through this route.
    """

    FIELDS = ('alt_introductory_message', 'introductory_message', 'name', 'error_message')

    def get(self, request, *args, **kwargs):
        """Return vernacular fields + first question from state machine for given lang+bot route."""
        language = request.query_params.get('language')
        route = request.query_params.get('company_bot__route')

        if not language or not route:
            return Response(
                {'error': 'language and company_bot__route query parameters are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        bot = CompanyBot.objects.filter(route=route).order_by('id').first()
        if not bot:
            return Response({'error': 'company bot not found for given route'}, status=status.HTTP_404_NOT_FOUND)

        vernacular = BotVernacular.objects.filter(company_bot=bot, language=language).first()
        if not vernacular:
            return Response({'error': 'bot vernacular not found'}, status=status.HTTP_404_NOT_FOUND)

        data = {field: getattr(vernacular, field) for field in self.FIELDS}

        english_bot = BotVernacular.objects.filter(company_bot=bot, language='en').first()
        data['default_name'] = english_bot.name if english_bot else ""

        # First question, translation, audio, and validations from state machine
        step_one = CompanyStateMachine.objects.filter(company_bot=bot, step=1).first()
        if step_one:
            data['first_bot_question'] = step_one.bot_question
            data['operation_type'] = step_one.operation_type

            cached = (step_one.translations or {}).get(language, {})
            translated_text = cached.get('text') if language != 'en' else None
            data['translated_bot_question'] = translated_text

            # Backfill intro fields from state machine if vernacular has empty values
            # Uses translated text when available, falls back to English bot_question
            # Deprecated: new bots should not rely on bot vernacular for first question
            display_text = translated_text or step_one.bot_question
            if not data.get('introductory_message') and display_text:
                data['introductory_message'] = display_text
            if not data.get('alt_introductory_message') and display_text:
                data['alt_introductory_message'] = display_text
            data['audio_s3_url'] = cached.get('audio_s3')

            from chatbot.utils.chat_utils import build_validation_response
            data['validations'] = build_validation_response({
                'validate_method': step_one.validate_method,
                'validation_type': step_one.validation_type,
                'render_as': step_one.render_as,
                'error_message': step_one.error_message,
                'validation_config': step_one.validation_config,
                'translations': step_one.translations,
                'min_choices': step_one.min_choices,
                'max_choices': step_one.max_choices,
            }, language=language)

        return Response(data)


class BotVernacularRetrieveUpdateDestroyView(generics.RetrieveUpdateAPIView):
    queryset = BotVernacular.objects.all()
    serializer_class = BotVernacularSerializer


class ProfileListCreateView(generics.ListCreateAPIView):
    queryset = Profile.objects.all()
    serializer_class = ProfileSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['first_name', 'email', 'company__name', 'phone', 'company__slug']


class ProfileRetrieveUpdateDestroyView(generics.RetrieveUpdateAPIView):
    queryset = Profile.objects.all()
    serializer_class = ProfileSerializer


class ChatSessionListCreateView(generics.ListCreateAPIView):
    queryset = ChatSession.objects.all()
    serializer_class = ChatSessionSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend, ChatSessionProfileFilter]
    filterset_fields = ['session', 'project_id', 'user_id', 'profile', 'session_type']


class ChatSessionRetrieveUpdateDestroyView(generics.RetrieveUpdateAPIView):
    queryset = ChatSession.objects.all()
    serializer_class = ChatSessionSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['session']


class ChatSessionRetrieveUpdateDestroyViewSession(generics.RetrieveUpdateAPIView):
    queryset = ChatSession.objects.all()
    serializer_class = ChatSessionSerializer
    lookup_field = 'session'


class FlowImageConfigView(generics.GenericAPIView):
    """
    API endpoint to get image configuration for a specific flow route.
    Query param: flow_route (required)
    Returns: ImageConfiguration object or 404
    """
    serializer_class = ImageConfigurationSerializer

    def get(self, request, *args, **kwargs):
        flow_route = request.query_params.get('flow_route')
        
        if not flow_route:
            return Response(
                {'error': 'flow_route query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            flow = Flow.objects.select_related('image_config_id').get(
                flow_route=flow_route,
                active=True
            )
            
            if not flow.image_config_id:
                return Response(
                    {'error': 'No image configuration found for this flow'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            serializer = self.get_serializer(flow.image_config_id)
            return Response(serializer.data)
            
        except Flow.DoesNotExist:
            return Response(
                {'error': 'Flow not found or inactive'},
                status=status.HTTP_404_NOT_FOUND
            )


class FlowLanguagesView(generics.GenericAPIView):
    """
    API endpoint to get supported languages for a specific flow route.
    Query param: flow_route (required)
    Returns: List of language codes
    """
    serializer_class = FlowLanguagesSerializer
    
    def get(self, request, *args, **kwargs):
        flow_route = request.query_params.get('flow_route')
        
        if not flow_route:
            return Response(
                {'error': 'flow_route query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            flow = Flow.objects.get(
                flow_route=flow_route,
                active=True
            )
            
            serializer = self.get_serializer(flow)
            return Response(serializer.data)
            
        except Flow.DoesNotExist:
            return Response(
                {'error': 'Flow not found or inactive'},
                status=status.HTTP_404_NOT_FOUND
            )


class FlowConnectionInfoView(generics.GenericAPIView):
    """
    API endpoint to get websocket URL and bot route for a flow.
    Query param: flow_route (required)
    Returns: websocket_url, bot route, isParentFlow flag, children flows, and image configuration
    """
    serializer_class = FlowConnectionInfoSerializer
    
    def get(self, request, *args, **kwargs):
        flow_route = request.query_params.get('flow_route')
        
        if not flow_route:
            return Response(
                {'error': 'flow_route query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            flow = Flow.objects.select_related('bot', 'image_config').prefetch_related('child_flows').get(
                flow_route=flow_route
            )
            
            # Check if flow is active
            if not flow.active:
                return Response(
                    {'error': 'Flow is inactive'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            serializer = self.get_serializer(flow)
            return Response(serializer.data)
            
        except Flow.DoesNotExist:
            return Response(
                {'error': 'Flow not found'},
                status=status.HTTP_404_NOT_FOUND
            )
