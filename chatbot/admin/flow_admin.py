import json
from django.contrib import admin
from django.core.exceptions import ValidationError
from chatbot.filter.custom_date_from_filter import CustomAdvanceDateFilter
from chatbot.models import ImageConfiguration, Flow
from django.forms import ModelForm, MultipleChoiceField, CheckboxSelectMultiple
from ..constants.flow_ui_config import get_default_ui_config
from chatbot.models import HistoricalCompanyStateMachine, HistoricalCompanyBot, HistoricalVoice
from ..widgets.json_widget import PrettyJSONWidget
from simple_history.admin import SimpleHistoryAdmin


@admin.register(ImageConfiguration)
class ImageConfigurationAdmin(admin.ModelAdmin):
    """Admin interface for Image Configuration model."""
    list_display = ('name', 'max_images', 'get_image_size_mb', 'created_at')
    list_filter = ('created_at', 'max_images')
    search_fields = ('name',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name',)
        }),
        ('Image Constraints', {
            'fields': ('max_images', 'image_size'),
            'description': 'Configure image upload limits for this configuration.'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ('created_at', 'updated_at')

    def get_image_size_mb(self, obj):
        """Display image size in MB."""
        return f"{obj.image_size / 1048576:.2f} MB"
    get_image_size_mb.short_description = 'Max Image Size'

LANGUAGE_CHOICES = [
    ("en", "English"),
    ("hi", "Hindi"),
    ("kn", "Kannada"),
    ("te", "Telugu"),
    ("or", "Odia"),
]


class FlowAdminForm(ModelForm):
    languages = MultipleChoiceField(
        choices=LANGUAGE_CHOICES,
        required=False,
        widget=CheckboxSelectMultiple,
        help_text="Select one or more supported languages."
    )

    class Meta:
        model = Flow
        fields = "__all__"
        widgets = {
            "ui_config": PrettyJSONWidget(
                attrs={
                    "rows": 10,
                    "style": "font-family: monospace; white-space: pre; width: 100%;"
                }
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        value = self.instance.languages if self.instance and self.instance.pk else None
        self.fields["languages"].initial = value or ["en", "hi", "kn", "te"]

        if not self.instance or not self.instance.pk:
            self.fields["ui_config"].initial = get_default_ui_config()

    def clean_languages(self):
        value = self.cleaned_data.get("languages", [])

        if not isinstance(value, list):
            raise ValidationError("Languages must be a list of language codes.")

        if len(value) != len(set(value)):
            raise ValidationError("Language codes must be unique.")

        allowed = {code for code, _ in LANGUAGE_CHOICES}
        invalid = [code for code in value if code not in allowed]
        if invalid:
            raise ValidationError(f"Invalid language codes: {', '.join(invalid)}")

        return value

    def clean_ui_config(self):
        value = self.cleaned_data.get("ui_config")

        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                raise ValidationError("Invalid JSON format")

        return value


@admin.register(Flow)
class FlowAdmin(SimpleHistoryAdmin):
    """Admin interface for Flow model."""
    form = FlowAdminForm

    list_display = (
        'flow_name', 'flow_route', 'bot', 'company', 'active', 'hidden', 
        'user_type', 'created_at'
    )
    list_filter = (
        'active', 'hidden', 'user_type',
        'bot__company', CustomAdvanceDateFilter, 'create_story'
    )
    search_fields = ('flow_name', 'flow_route', 'bot__name')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    raw_id_fields = ('bot', 'story_bot', 'parent_flow', 'image_config', 'story_validation_bot', 'company')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('flow_name', 'flow_route', 'languages')
        }),
        ('Bot Configuration', {
            'fields': ('bot', 'story_bot', 'story_validation_bot'),
            'description': 'Configure the bots associated with this flow.'
        }),
        ('Flow Settings', {
            'fields': (
                'active', 'hidden', 'user_type', 'company', 'parent_flow', 'image_config', 'create_story',
                'ui_config',
            ),
        }),
        ('Advanced Settings', {
            'fields': ('websocket_url',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    readonly_fields = ('created_at', 'updated_at')
    
    def formfield_for_dbfield(self, db_field, request, **kwargs):
        """Customize form field for languages JSONField."""
        if db_field.name == 'languages':
            kwargs['help_text'] = 'Enter languages as JSON array, e.g., ["en", "hi", "kn"]'
        elif db_field.name == 'websocket_url':
            kwargs['help_text'] = 'Enter WebSocket route only (e.g., "ws/common/"). Do not include the full URL.'
        return super().formfield_for_dbfield(db_field, request, **kwargs)
