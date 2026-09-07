import logging

from django.contrib import admin
from django.db.models import Q
from pydantic import ValidationError
from simple_history.admin import SimpleHistoryAdmin
from .generic_upload_admin import BatchUploadMixin
from chatbot.filter.admin_filter import (CompanyChatCompanyFilter, ChatSessionFilter, ProfileCityFilter,
                                         ProfileStateFilter, ProfileCompanyChatFilter, ProfileEmailFilter)
from chatbot.filter.custom_date_from_filter import CustomAdvanceDateFilter
from chatbot.models import Company, Profile, ProfileType, CompanyBot, CompanyChat, ChatSession, \
    CompanyBotTypeChoices, Voice, ImageConfiguration, Flow, VoiceType
from chatbot.models.company_models import CompanyStateMachine, CompanyBotProgramMapping
from chatbot.resources.resource import CompanyChatResource
from chatbot.resources.company_resource import ChatSessionResource
from django.shortcuts import redirect
from django.contrib import messages
from django.urls import path
from django.http import HttpResponseRedirect, HttpResponseNotAllowed
from django.urls import reverse
from django.forms import ModelForm, MultipleChoiceField, CheckboxSelectMultiple
from inline_actions.admin import InlineActionsMixin, InlineActionsModelAdminMixin
from ..utils.admin_config.export_mixin import ExportAllFieldsMixin

logger = logging.getLogger(__name__)


class CompanyBotProgramMappingInline(admin.TabularInline):
    """
    Edits the state-to-programme mappings of a company bot from the bot's own page.
    Each row decides which programme and leader category a report is tagged with when the
    bot produces one for that state.
    """

    model = CompanyBotProgramMapping
    extra = 1
    fields = ('state', 'program', 'leader_category', 'is_active')


class CompanyStateMachineAdmin(InlineActionsMixin, admin.TabularInline):
    model = CompanyStateMachine
    fk_name = 'company_bot'
    extra = 1
    raw_id_fields = ['preprocess_bot', 'postprocess_bot']
    fields = (
        'name', 'step', 'use_stage_chats', 'text_conversion_type',
        'bot_question', 'completion_criteria', 'context', 'tool_context',
        'operation_type', 'skip_if_authenticated',
        'preprocess_type', 'preprocess_prompt', 'preprocess_bot', 'preprocess_output_mode',
        'postprocess_type', 'postprocess_prompt', 'postprocess_output_mode',
        'skip_to_step', 'translations'
    )
    exclude = ('type',)  # ✅ hide type
    inline_actions = ['generate_translation', 'generate_audio', 'revoke_audio']

    class Media:
        js = ('chatbot/admin/js/confirm_revoke_audio.js',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.order_by('step')

    def generate_translation(self, request, obj, parent_obj=None):
        """Inline action: async-triggers translation gen for this row only."""
        from chatbot.celery_tasks.non_llm_tasks import generate_state_machine_translations

        generate_state_machine_translations.delay(parent_obj.id, state_machine_id=obj.pk)
        messages.success(
            request, f"Translation generation started for step '{obj.name}'. Please refresh your page after 15-20 seconds to see the updated JSON in the translations section."
        )

    generate_translation.short_description = "Generate Translations"

    def generate_audio(self, request, obj, parent_obj=None):
        """Inline action: async-triggers audio gen for this row only."""
        from chatbot.celery_tasks.non_llm_tasks import generate_state_machine_audio

        generate_state_machine_audio.delay(parent_obj.id, state_machine_id=obj.pk)
        messages.success(
            request, f"Audio generation started for step '{obj.name}'. Please refresh your page after 15-20 seconds to see the updated JSON in the translations section."
        )

    generate_audio.short_description = "Generate Audio"

    def revoke_audio(self, request, obj, parent_obj=None):
        """Inline action: deletes all cached audio_s3 files for this row from S3, strips them from translations."""
        from chatbot.celery_tasks.non_llm_tasks import revoke_state_machine_audio

        removed_langs, failed_langs = revoke_state_machine_audio(obj.pk)

        if not removed_langs and not failed_langs:
            messages.info(request, f"No cached audio found for step '{obj.name}'.")
        else:
            if removed_langs:
                messages.success(
                    request, f"Revoked audio for step '{obj.name}': {', '.join(removed_langs)}."
                )
            if failed_langs:
                messages.error(
                    request,
                    f"Failed to delete S3 audio for step '{obj.name}': {', '.join(failed_langs)}. "
                    "Left untouched, retry revoke.",
                )

    revoke_audio.short_description = "Revoke Audio"

    def get_revoke_audio_css(self, obj=None):
        return "confirm-revoke-audio"


class VoiceProviderAdmin(admin.TabularInline):
    model = Voice
    extra = 1

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.order_by('type', 'language')

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "other_params":
            kwargs["help_text"] = "Leave empty to auto-load provider defaults."

        return super().formfield_for_dbfield(db_field, request, **kwargs)


class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at', 'status')
    list_filter = (
        CustomAdvanceDateFilter,
    )
    search_fields = ('name',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email)
        if request.user.is_superuser:
            return qs
        elif len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            return qs.filter(id=profile[0].company.id)
        else:
            return qs.none()


@admin.register(CompanyBot)
class CompanyBotAdmin(InlineActionsModelAdminMixin, BatchUploadMixin, SimpleHistoryAdmin):

    list_display = ('name', 'company', 'created_at')
    list_filter = (
        'company',
        'name',
        'provider',
        'llm_model',
        CustomAdvanceDateFilter,
    )
    search_fields = ('name', 'company__name')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    inlines = [VoiceProviderAdmin, CompanyBotProgramMappingInline]
    actions = ['duplicate_bot', 'export_selected_bots']
    inline_actions = None  # only the CompanyStateMachine inline uses inline-actions, not this changelist

    enable_batch_upload = True
    batch_load_foreign_keys = True
    batch_upload_fields = ['name', 'company', 'provider', 'llm_model', 'context', 'max_token', 'route']

    import_template_name = 'admin/import_export/import.html'
    export_template_name = 'admin/import_export/export.html'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'export/',
                self.admin_site.admin_view(self.export_view),
                name='chatbot_companybot_export',
            ),
            path(
                'import/',
                self.admin_site.admin_view(self.import_view),
                name='chatbot_companybot_import',
            ),
            path(
                "<int:bot_id>/generate-translations/",
                self.admin_site.admin_view(self.generate_translations_view),
                name="chatbot_companybot_generate_translations",
            ),
        ]
        # Important: custom URLs must come before the default admin URLs
        return custom_urls + urls

    def generate_translations_view(self, request, bot_id):
        """Admin action: async-triggers translation gen for bot, redirects back to change page."""
        try:
        # if request.method != "POST":
        #     return HttpResponseNotAllowed(["POST"])

            from chatbot.celery_tasks.non_llm_tasks import generate_state_machine_translations

            logger.info("Generate translations triggered for company_bot_id=%s", bot_id)
            generate_state_machine_translations.delay(company_bot_id=bot_id, generate_audio=True)
            self.message_user(
                request, "Translation generation started in background. Please refresh your page after 15-20 seconds to see the updated JSON in the translations section.", messages.SUCCESS
            )

            return HttpResponseRedirect(reverse("admin:chatbot_companybot_change", args=[bot_id]))

        except Exception as e:
            logger.error("Error while generating Translations: %s", e, exc_info=True)

    def export_view(self, request):
        """Handle export requests"""
        from chatbot.views.admin.bot_admin_views import export_bots
        return export_bots(request)

    def import_view(self, request):
        """Handle import requests"""
        from chatbot.views.admin.bot_admin_views import import_bots
        return import_bots(request)

    def get_import_formats(self):
        """Define allowed import formats"""
        from import_export.formats import base_formats
        return [base_formats.CSV, base_formats.XLSX, base_formats.JSON]

    def get_export_formats(self):
        """Define allowed export formats"""
        from import_export.formats import base_formats
        return [base_formats.CSV, base_formats.XLSX, base_formats.JSON]

    def get_export_filename(self, request, queryset, file_format):
        """Generate filename for exports"""
        import datetime
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
        filename = f"company_bots_{date_str}"
        return f"{filename}.{file_format.get_extension()}"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email)
        if request.user.is_superuser:
            return qs
        elif len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            return qs.filter(company=profile[0].company)
        else:
            return qs.none()

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        user = request.user
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email)
        if not user.is_superuser and len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            company_field = form.base_fields.get('company')
            if company_field:
                form.base_fields['company'].queryset = form.base_fields['company'].queryset.filter(
                    id=profile[0].company.id)
            form.base_fields = {field_name: form.base_fields[field_name] for field_name in form.base_fields}
        form.base_fields = {field_name: form.base_fields[field_name] for field_name in form.base_fields}
        return form

    def save_related(self, request, form, formsets, change):
        """Post-save hook: warns on TTT/TTS voice count mismatch, auto-triggers translation gen for new langs."""
        company_bot = form.instance
        pre_languages = set(
            Voice.objects.filter(
                company_bot=company_bot, type=VoiceType.TextToText
            ).values_list("language", flat=True)
        )
        super().save_related(request, form, formsets, change)

        ttt_count = Voice.objects.filter(
            company_bot=company_bot, type=VoiceType.TextToText
        ).count()
        tts_count = Voice.objects.filter(
            company_bot=company_bot, type=VoiceType.TextToSpeech
        ).count()

        if ttt_count != tts_count:
            self.message_user(
                request,
                f"Voice config mismatch: {ttt_count} TextToText vs {tts_count} TextToSpeech voices. "
                "Fix counts before generating translations.",
                messages.WARNING,
            )
            return

        post_languages = set(
            Voice.objects.filter(
                company_bot=company_bot, type=VoiceType.TextToText
            ).values_list("language", flat=True)
        )
        new_languages = post_languages - pre_languages
        if new_languages:
            from chatbot.celery_tasks.non_llm_tasks import (
                generate_state_machine_translations,
            )

            for lang in new_languages:
                generate_state_machine_translations.delay(company_bot.id, language=lang)
            self.message_user(
                request,
                f"Translation generation started for new language(s): {', '.join(new_languages)}.",
                messages.SUCCESS,
            )

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Shows state-machine inline + generate-translations button URL only for STATE_MACHINE bots."""
        extra_context = extra_context or {}
        if object_id:
            obj = self.model.objects.get(pk=object_id)
            if obj.bot_type == CompanyBotTypeChoices.STATE_MACHINE:
                self.inlines = [VoiceProviderAdmin, CompanyStateMachineAdmin]
                extra_context["generate_translations_url"] = reverse(
                    "admin:chatbot_companybot_generate_translations", args=[object_id]
                )
            else:
                self.inlines = [VoiceProviderAdmin]
        else:
            self.inlines = [VoiceProviderAdmin]
        return super().changeform_view(request, object_id, form_url, extra_context)
    def get_inlines(self, request, obj=None):
        # Returns a fresh list per request. ModelAdmin instances are created once at
        # startup and shared across every request, so assigning self.inlines here would
        # leak one bot's inline set into a concurrent request for a different bot.
        # obj is None on the add form.
        if obj and obj.bot_type == CompanyBotTypeChoices.STATE_MACHINE:
            return [VoiceProviderAdmin, CompanyStateMachineAdmin, CompanyBotProgramMappingInline]
        return [VoiceProviderAdmin, CompanyBotProgramMappingInline]

    def get_inline_instances(self, request, obj=None):
        # django-admin-inlines' inline-action POST handler calls this without `obj`,
        # which would make get_inlines() drop CompanyStateMachineAdmin for STATE_MACHINE
        # bots and crash resolving the action. Recover obj from the URL when missing.
        if obj is None and request.resolver_match:
            object_id = request.resolver_match.kwargs.get("object_id")
            if object_id:
                obj = self.model.objects.filter(pk=object_id).first()
        return super().get_inline_instances(request, obj)

    # Sync Google glossary for TextToText voice providers after inline save
    def duplicate_bot(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Please select exactly one bot to duplicate.", level=messages.ERROR)
            return

        original = queryset.first()

        # Duplicate the bot
        new_bot = CompanyBot.objects.get(pk=original.pk)
        new_bot.pk = None
        new_bot.name = f"{original.name} (Copy)"
        new_bot.save()

        # Duplicate VoiceProvider inlines
        original_voice_providers = Voice.objects.filter(company_bot=original)
        for voice in original_voice_providers:
            voice.pk = None
            voice.company_bot = new_bot
            voice.save()

        # Duplicate StateMachine if present
        if original.bot_type == CompanyBotTypeChoices.STATE_MACHINE:
            original_state_machines = CompanyStateMachine.objects.filter(company_bot=original)
            for sm in original_state_machines:
                sm.pk = None
                sm.company_bot = new_bot
                sm.save()

        # Duplicate the program mappings too. Without them the copy has no active state
        # mapping, so Story._derive_program_and_leader_category finds nothing and every
        # story the new bot produces is left with no program and no leader category.
        original_program_mappings = CompanyBotProgramMapping.objects.filter(company_bot=original)
        for mapping in original_program_mappings:
            mapping.pk = None
            mapping.company_bot = new_bot
            mapping.save()

        self.message_user(request, "Bot duplicated successfully!", level=messages.SUCCESS)
        return redirect(f"/admin/chatbot/companybot/{new_bot.id}/change/")

    def export_selected_bots(self, request, queryset):
        """Custom export action"""
        selected_ids = queryset.values_list('id', flat=True)
        ids_str = ','.join(str(id) for id in selected_ids)

        # Use admin URL reverse with the app label and model name
        info = self.model._meta.app_label, self.model._meta.model_name
        url = reverse('admin:%s_%s_export' % info) + f'?ids={ids_str}'
        return HttpResponseRedirect(url)

    export_selected_bots.short_description = "Export selected bots"

    def changelist_view(self, request, extra_context=None):
        """Add custom buttons to the changelist view"""
        extra_context = extra_context or {}
        extra_context['custom_buttons'] = True
        return super().changelist_view(request, extra_context=extra_context)

    duplicate_bot.short_description = "Duplicate selected bot"


@admin.register(CompanyChat)
class CompanyChatAdmin(ExportAllFieldsMixin, admin.ModelAdmin):
    list_display = ('session', 'sender', 'receiver', 'message', 'translated_message', 'created_at', 'stage')
    list_filter = (
        CustomAdvanceDateFilter,
        ProfileCompanyChatFilter,
        ProfileEmailFilter,
        'session',
        CompanyChatCompanyFilter,
        'stage'
    )
    search_fields = ('session', 'message__icontains', 'translated_message__icontains')
    list_per_page = 20
    raw_id_fields = ('sender', 'receiver')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    export_filename = "company_chats.xlsx"
    resource_class = CompanyChatResource

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email).select_related('company').first()
        if request.user.is_superuser:
            return qs.prefetch_related('sender__company', 'receiver__company')
        elif profile and profile.profile_type == ProfileType.MODERATOR:
            return qs.filter(
                Q(sender__company=profile.company) | Q(receiver__company=profile.company)
            ).prefetch_related('sender__company', 'receiver__company')
        else:
            return qs.none()

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(request, queryset, search_term)

        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email).select_related('company').first()
        if not request.user.is_superuser and profile and profile.profile_type == ProfileType.MODERATOR:
            if profile.company:
                queryset = queryset.filter(
                    Q(sender__company=profile.company) | Q(receiver__company=profile.company)
                ).prefetch_related('sender__company', 'receiver__company')
        return queryset, use_distinct

    def get_list_filter(self, request):
        user = request.user
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email).select_related('company').first()
        if not user.is_superuser and profile and profile.profile_type == ProfileType.MODERATOR:
            company = profile.company
            if company.slug == 'fmch':
                return (CustomAdvanceDateFilter, ProfileCompanyChatFilter,
                        ProfileEmailFilter, 'session', ProfileCityFilter, ProfileStateFilter, 'message_type')
            if company.slug == 'tfistaging':
                return (CustomAdvanceDateFilter, ProfileCompanyChatFilter,
                        ProfileEmailFilter, 'session', CompanyChatCompanyFilter, 'stage')
        return super().get_list_filter(request)


@admin.register(ChatSession)
class ChatSessionAdmin(ExportAllFieldsMixin, admin.ModelAdmin):
    list_display = (
        'session', 'get_first_name', 'session_status', 'session_type', 'current_question', 'total_steps',
        'created_at'
    )
    list_filter = (
        'session',
        'title',
        ChatSessionFilter,
        'project_id',
        'session_status',
        'session_type',
        CustomAdvanceDateFilter,
    )
    search_fields = ('session', 'title', 'profile__first_name')
    raw_id_fields = ('profile',)
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    resource_class = ChatSessionResource

    def current_question(self, obj):
        return obj.current_step

    current_question.short_description = 'Current Question'

    def total_steps(self, obj):
        if obj.company_bot and CompanyStateMachine.objects.filter(company_bot=obj.company_bot).exists():
            return CompanyStateMachine.objects.filter(company_bot=obj.company_bot).count()
        return 0

    total_steps.short_description = 'Total Questions'

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('profile', 'company_bot')
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email)
        if request.user.is_superuser:
            return qs
        elif len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            return qs.filter(profile__company=profile[0].company).prefetch_related('profile__company')
        else:
            return qs.none()

    def get_list_display(self, request):
        user = request.user
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email)
        if not user.is_superuser and len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            return 'session', 'get_first_name', 'current_question', 'total_steps', 'session_status', 'created_at'
        return 'session', 'get_first_name', 'current_question', 'total_steps', 'session_status', 'created_at'

    def get_first_name(self, obj):
        return obj.profile.first_name if obj.profile else None

    get_first_name.short_description = 'First Name'

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        user = request.user
        user_email = request.user.email
        profile = Profile.objects.filter(email=user_email)
        # Check if the user is a moderator
        if not user.is_superuser and len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            # Exclude the fields for moderators
            form.base_fields = {field_name: form.base_fields[field_name] for field_name in form.base_fields
                                if field_name not in ['current_step']}
        return form


admin.site.register(Company, CompanyAdmin)


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
    ("ta", "Tamil"),
    ("pa", "Punjabi"),
    ("bn", "Bengali"),
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        value = self.instance.languages if self.instance and self.instance.pk else None
        self.fields["languages"].initial = value or ["en", "hi", "kn", "te"]

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


@admin.register(Flow)
class FlowAdmin(SimpleHistoryAdmin):
    """Admin interface for Flow model."""
    form = FlowAdminForm

    list_display = (
        'flow_name', 'flow_route', 'bot', 'active', 'hidden',
        'user_type', 'created_at'
    )
    list_filter = (
        'active', 'hidden', 'user_type',
        'bot__company', CustomAdvanceDateFilter, 'create_story'
    )
    search_fields = ('flow_name', 'flow_route', 'bot__name')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    raw_id_fields = ('bot', 'story_bot', 'parent_flow', 'default_flow', 'image_config', 'story_validation_bot')

    fieldsets = (
        ('Basic Information', {
            'fields': ('flow_name', 'flow_route', 'languages')
        }),
        ('Bot Configuration', {
            'fields': ('bot', 'story_bot', 'story_validation_bot'),
            'description': 'Configure the bots associated with this flow.'
        }),
        ('Flow Settings', {
            'fields': ('active', 'hidden', 'user_type', 'parent_flow', 'default_flow', 'image_config', 'create_story'),
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
