import os
from django.contrib import admin
from django.utils.html import format_html
from simple_history.admin import SimpleHistoryAdmin
from chatbot.filter.custom_date_from_filter import CustomAdvanceDateFilter
from chatbot.models import TranslationFile, FlowTranslationMapping


@admin.register(TranslationFile)
class TranslationFileAdmin(SimpleHistoryAdmin):
    list_display = (
        'namespace', 'label', 'language', 'get_file_name', 'created_at',
    )

    list_filter = (
        'namespace', 'label', 'language', CustomAdvanceDateFilter
    )

    search_fields = (
        'namespace', 'label', 'language', 's3_key',
    )

    ordering = ('-created_at',)

    readonly_fields = ('get_s3_url', 'created_at', 'updated_at')

    fieldsets = (
        ('Translation File Info', {
        'fields': ('namespace', 'label', 'language', 'data', 'get_s3_url')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_file_name(self, obj):
        return f"{os.path.basename(obj.s3_key)}"
    get_file_name.short_description = "File Name"

    def get_s3_url(self, obj):
        if not obj.public_url:
            return "-"

        return format_html(
            '<a href="{}" target="_blank">{}</a>',
            obj.public_url,
            obj.public_url
        )

    get_s3_url.short_description = "S3 Public URL"


@admin.register(FlowTranslationMapping)
class FlowTranslationMappingAdmin(SimpleHistoryAdmin):
    list_display = (
        'flow', 'namespace', 'language', 'translation_file', 'created_at',
    )

    list_filter = (
        'flow', 'namespace', 'language', CustomAdvanceDateFilter
    )

    search_fields = (
        'flow__flow_name', 'namespace', 'translation_file__s3_key',
    )

    ordering = (
        'flow', 'namespace', 'language',
    )

    raw_id_fields = (
        'flow', 'translation_file',
    )

    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Mapping Info', {
            'fields': ('flow', 'namespace', 'language', 'translation_file')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('flow', 'translation_file')
