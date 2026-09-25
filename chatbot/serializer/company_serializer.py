from rest_framework import serializers
from chatbot.models import BotVernacular
from chatbot.models.company_models import CompanyStateMachine, CompanyBot, Company, ImageConfiguration, Flow


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ('name', 'slug')


class CompanyBotSerializer(serializers.ModelSerializer):
    company = CompanySerializer(read_only=True)
    statemachine_length = serializers.SerializerMethodField()
    state_machine_steps = serializers.SerializerMethodField()

    class Meta:
        model = CompanyBot
        fields = '__all__'

    def get_statemachine_length(self, obj):
        return obj.companystatemachine_set.count()

    def get_state_machine_steps(self, obj):
        """Return ordered list of state machine step info for bot's state machine."""
        steps = CompanyStateMachine.objects.filter(company_bot=obj).order_by('step')
        return [
            {
                "step": s.step,
                "operation_type": s.operation_type,
                "bot_question": s.bot_question,
                "validate_method": s.validate_method,
                "validation_type": s.validation_type,
                "render_as": s.render_as,
            }
            for s in steps
        ]


class CompanyStateMachineSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyStateMachine
        fields = '__all__'


class BotVernacularSerializer(serializers.ModelSerializer):
    company_bot = CompanyBotSerializer(read_only=True)
    default_name = serializers.SerializerMethodField()

    class Meta:
        model = BotVernacular
        fields = '__all__'

    def get_default_name(self, obj):
        english_bot = BotVernacular.objects.filter(company_bot=obj.company_bot, language='en').first()
        return english_bot.name if english_bot else ""


class ImageConfigurationSerializer(serializers.ModelSerializer):
    """Serializer for ImageConfiguration model."""
    image_size_mb = serializers.SerializerMethodField()

    class Meta:
        model = ImageConfiguration
        fields = ('id', 'name', 'max_images', 'image_size', 'image_size_mb')
        read_only_fields = ('id',)

    def get_image_size_mb(self, obj):
        """Convert image size from bytes to MB for easier reading."""
        return round(obj.image_size / 1048576, 2)


class FlowLanguagesSerializer(serializers.ModelSerializer):
    """Serializer for Flow languages."""
    
    class Meta:
        model = Flow
        fields = ('flow_route', 'languages')
        read_only_fields = ('flow_route', 'languages')


class ChildFlowSerializer(serializers.ModelSerializer):
    """Serializer for child flow minimal information."""
    
    class Meta:
        model = Flow
        fields = ('flow_route', 'flow_name', 'active')
        read_only_fields = ('flow_route', 'flow_name', 'active')


class FlowConnectionInfoSerializer(serializers.ModelSerializer):
    """Serializer for Flow connection information."""
    bot_route = serializers.CharField(source='bot.route', read_only=True)
    isParentFlow = serializers.SerializerMethodField()
    children_flows = serializers.SerializerMethodField()
    image_config = serializers.SerializerMethodField()
    editor_config = serializers.SerializerMethodField()
    default_flow = serializers.CharField(source='default_flow.flow_route', read_only=True, allow_null=True, default=None)

    class Meta:
        model = Flow
        fields = ('flow_route', 'websocket_url', 'bot_route', 'isParentFlow', 'children_flows', 'image_config', 'create_story', 'editor_config', 'default_flow')
        read_only_fields = ('flow_route', 'websocket_url', 'bot_route', 'isParentFlow', 'children_flows', 'image_config', 'default_flow')
    
    def get_isParentFlow(self, obj):
        """Check if this flow has children."""
        return obj.child_flows.exists()
    
    def get_children_flows(self, obj):
        """Get list of child flows if this is a parent flow."""
        if obj.child_flows.exists():
            return ChildFlowSerializer(obj.child_flows.all(), many=True).data
        return []

    def get_editor_config(self, obj):
        # A flow may have several templates and nothing orders pdf_templates, so an
        # unordered .first() can hand a different editor_config to the frontend between
        # requests. Ordered for a stable result; the first template that actually carries
        # an editor_config wins, so a template without one no longer masks the others.
        for template in obj.pdf_templates.order_by('id'):
            if template.constants_json:
                editor_config = template.constants_json.get("editor_config")
                if editor_config is not None:
                    return editor_config
        return None

    
    def get_image_config(self, obj):
        """Get image configuration for this flow."""
        if obj.image_config:
            return ImageConfigurationSerializer(obj.image_config).data
        return None