import json
import os
from django.contrib import admin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html
from simple_history.admin import SimpleHistoryAdmin
from chatbot.filter.custom_date_from_filter import CustomAdvanceDateFilter
from chatbot.form.i18n_form import TranslationFileAdminForm
from chatbot.models import TranslationFile, FlowTranslationMapping, Flow
from chatbot.utils.S3.s3_service import is_same_bucket, s3_file_exists
from chatbot.utils.i18n_helper_utils import handle_translation_s3


@admin.register(TranslationFile)
class TranslationFileAdmin(SimpleHistoryAdmin):
    form = TranslationFileAdminForm
    list_display = (
        'namespace', 'label', 'language', 'get_s3_url', 'created_at',
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
        'fields': ('namespace', 'language', 'label', 'data', 'get_s3_url')
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
        'flow', 'translation_file', 'created_at',
    )

    list_filter = (
        'flow', CustomAdvanceDateFilter
    )

    search_fields = (
        'flow__flow_name', 'translation_file__s3_key',
    )

    ordering = (
        'flow',
        'translation_file__namespace',
        'translation_file__language',
    )

    raw_id_fields = (
        'flow', 'translation_file',
    )

    readonly_fields = ('created_at', 'updated_at')
    actions = ['export_flow_translations']

    fieldsets = (
        ('Mapping Info', {
            'fields': ('flow', 'translation_file')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('flow', 'translation_file')

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'import/',
                self.admin_site.admin_view(self.import_view),
                name='flow_translation_import',
            ),
        ]
        return custom_urls + urls

    def export_flow_translations(self, request, queryset):
        flows_data = {}

        for mapping in queryset.select_related('flow', 'translation_file'):
            flow = mapping.flow
            tf = mapping.translation_file

            flow_key = flow.flow_route

            flows_data.setdefault(flow_key, {
                "flow": flow.flow_route,
                "flow_name": flow.flow_name,
                "translations": {}
            })

            lang = tf.language
            namespace = tf.namespace

            flows_data[flow_key]["translations"].setdefault(lang, {})
            flows_data[flow_key]["translations"][lang].setdefault(namespace, {})

            flows_data[flow_key]["translations"][lang][namespace][tf.label] = {
                "meta": {
                    "namespace": tf.namespace,
                    "language": tf.language,
                    "label": tf.label,
                    "s3_key": tf.s3_key,
                    "created_at": tf.created_at.isoformat(),
                    "updated_at": tf.updated_at.isoformat(),
                },
                "data": tf.data
            }

        response = HttpResponse(
            json.dumps(flows_data, indent=2, ensure_ascii=False),
            content_type='application/json'
        )
        response['Content-Disposition'] = 'attachment; filename=flow_translations.json'

        return response

    export_flow_translations.short_description = "Export selected flow translations"

    def import_view(self, request):
        from django.shortcuts import render

        if request.method == "GET":
            context = dict(
                self.admin_site.each_context(request),
                opts=self.model._meta,
            )

            return render(request, "admin/i18n/import_form.html", context)

        if request.method == "POST":
            file = request.FILES.get("file")

            if not file:
                self.message_user(request, "No file uploaded", level="error")

                return redirect(
                    reverse(
                        f'admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist'
                    )
                )

            try:
                data = json.load(file)
                self.process_import(data, request)
                self.message_user(request, "Import successful", level="success")
            except Exception as e:
                self.message_user(request, f"Import failed: {str(e)}", level="error")

            return redirect(
                reverse(
                    f'admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist'
                )
            )

    @transaction.atomic
    def process_import(self, data, request):
        created_count = 0
        updated_count = 0

        for flow_key, flow_data in data.items():

            flow = Flow.objects.filter(flow_route=flow_key).first()

            if not flow:
                self.message_user(request, f"Skipping missing flow: {flow_key}", level="warning")
                continue

            translations = flow_data.get("translations", {})

            for lang, namespaces in translations.items():
                for namespace, labels in namespaces.items():
                    for label, content in labels.items():

                        label = label.strip()
                        meta = content.get("meta", {})
                        translation_json = content.get("data", {})

                        if not translation_json:
                            continue

                        defaults = {
                            "data": translation_json,
                        }

                        if meta.get("s3_key"):
                            defaults["s3_key"] = meta["s3_key"]

                        tf = TranslationFile.objects.filter(
                            namespace=namespace, language=lang, label=label
                        ).first()

                        if tf:
                            tf.data = translation_json
                            if meta.get("s3_key"):
                                tf.s3_key = meta["s3_key"]

                            if tf.s3_key and not s3_file_exists(tf.s3_key):
                                # re-upload only if missing
                                handle_translation_s3(tf)

                            tf.save(skip_s3=True)
                            created = False
                        else:
                            tf = TranslationFile(
                                namespace=namespace,
                                language=lang,
                                label=label,
                                data=translation_json,
                                s3_key=meta.get("s3_key")
                            )
                            if tf.s3_key and not s3_file_exists(tf.s3_key):
                                # re-upload only if missing
                                handle_translation_s3(tf)

                            tf.save(skip_s3=True)
                            created = True

                        if created:
                            created_count += 1
                        else:
                            updated_count += 1

                        FlowTranslationMapping.objects.get_or_create(
                            flow=flow,
                            translation_file=tf
                        )

        self.message_user(
            request,
            f"Import done: {created_count} created, {updated_count} updated",
            level="success"
        )
