from django.contrib import admin
from django.db.models import Q
from chatbot.filter.admin_filter import (CompanyChatCompanyFilter, ChatSessionFilter, ProfileCityFilter,
                                         ProfileStateFilter, ProfileCompanyChatFilter, ProfileEmailFilter)
from chatbot.filter.custom_date_from_filter import CustomAdvanceDateFilter
from chatbot.models import Company, Profile, ProfileType, CompanyChat, ChatSession
from chatbot.models.company_models import CompanyStateMachine
from chatbot.resources.resource import CompanyChatResource
from chatbot.resources.company_resource import ChatSessionResource
from ..utils.admin_config.export_mixin import ExportAllFieldsMixin
from chatbot.models import HistoricalCompanyStateMachine, HistoricalCompanyBot, HistoricalVoice


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
        if not user.is_superuser and len(profile) > 0 and profile[0].profile_type == ProfileType.MODERATOR:
            form.base_fields = {field_name: form.base_fields[field_name] for field_name in form.base_fields
                                if field_name not in ['current_step']}
        return form


admin.site.register(Company, CompanyAdmin)
