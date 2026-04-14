import os
from django.db import models
from django.core.exceptions import ValidationError
from simple_history.models import HistoricalRecords
from chatbot.utils.i18n_helper_utils import handle_translation_s3, normalize_label


class I18nTag(models.Model):
    """
    Model for storing internationalization tag names.
    Tags are used to group related translations together.
    """
    tag_name = models.CharField(
        max_length=255,
        unique=True,
        help_text="Unique tag name for grouping translations (e.g., 'welcome_message', 'button_labels')."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return self.tag_name

    class Meta:
        verbose_name = "I18n Tag"
        verbose_name_plural = "I18n Tags"
        indexes = [
            models.Index(fields=['tag_name']),
        ]
        ordering = ['tag_name']


class I18nTranslation(models.Model):
    """
    Model for storing internationalization translations.
    Each translation is associated with a tag and can have multiple variables in different languages.
    """
    tag_id = models.ForeignKey(
        I18nTag,
        on_delete=models.CASCADE,
        related_name='translations',
        help_text="The tag this translation belongs to."
    )
    variable_name = models.CharField(
        max_length=255,
        help_text="Variable name for this translation (e.g., 'title', 'description', 'button_text')."
    )
    language = models.CharField(
        max_length=10,
        help_text="Language code (e.g., 'en', 'hi', 'kn', 'te')."
    )
    value = models.TextField(
        help_text="Translated text value for this variable in the specified language."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"{self.tag_id.tag_name} - {self.variable_name} ({self.language})"

    class Meta:
        verbose_name = "I18n Translation"
        verbose_name_plural = "I18n Translations"
        indexes = [
            models.Index(fields=['tag_id', 'variable_name', 'language']),
            models.Index(fields=['language']),
            models.Index(fields=['variable_name']),
        ]
        unique_together = [['tag_id', 'variable_name', 'language']]
        ordering = ['tag_id', 'variable_name', 'language']

    def clean(self):
        """Validate the translation data."""
        super().clean()
        
        # Validate language code format (should be lowercase)
        if self.language:
            self.language = self.language.lower().strip()
            
        # Validate that value is not empty
        if not self.value or not self.value.strip():
            raise ValidationError({
                'value': "Translation value cannot be empty."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class TranslationFile(models.Model):
    """
    Stores a complete translation JSON file for a specific language.
    This represents a single uploaded translation file (usually synced with S3).
    """

    language = models.CharField(
        max_length=10, null=False, blank=False,
        help_text="Language code (e.g., 'en', 'hi', 'kn')"
    )

    data = models.JSONField(
        help_text="Full translation JSON content", null=False, blank=False
    )

    s3_key = models.CharField(
        max_length=500, null=True, blank=True,
        help_text="S3 path (e.g., translations/home/en_v2.json)"
    )

    namespace = models.CharField(
        max_length=100, null=False, blank=False,
        help_text="Logical group of translations (e.g., 'home', 'chat')"
    )
    label = models.CharField(
        max_length=100, null=False, blank=False,
        help_text="""
        Defines a logical variant/group of translation files.<br><br>

        This is used to group related translations across namespaces into a single variant
        (e.g., a specific flow experience, experiment, or release).<br><br>
        For simplicity and consistency, you can keep the label the same as the flow_route (e.g., flow_route="teacher_dashboard"
        → label="teacher_dashboard").
        <br/><br/><b>Examples:</b>
        <ul>
          <li>'common_flow' → fallback translations used when a specific flow mapping is not available</li>
          <li>'mitra_guest' → guest user experience</li>
          <li>'teacher_dashboard' → teacher-specific UI</li>
          <li>'experiment_a' → A/B testing variant</li>
        </ul>

        <br/><b>IMPORTANT:</b>
        <ul>
          <li>Label should be lowercase and use only letters, numbers, hyphens, and underscores; spaces and other special characters are automatically converted to underscores (e.g., "Teacher Dashboard" → "teacher_dashboard", "exp-a" → "exp-a").</li>
          <li>This is NOT related to file versioning (e.g., _v1, _v2 in S3 keys).</li>
          <li>Multiple labels can exist for the same namespace and language.</li>
          <li>Currently, labels are used only for logical grouping/organization.</li>
          <li>Labels affect API responses ONLY if explicitly passed as a filter.</li>
        </ul>
        """
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "Translation File"
        verbose_name_plural = "Translation Files"
        indexes = [
            models.Index(fields=['namespace', 'language', 'label'])
        ]
        unique_together = ['namespace', 'language', 'label']

    def __str__(self):
        return f"{self.namespace} | {self.language} | {self.label}"

    @property
    def public_url(self):
        if not self.s3_key:
            return None

        base_url = os.getenv("S3_MEDIA_URL", "").rstrip("/")
        return f"{base_url}/{self.s3_key}"

    def clean(self):
        super().clean()
        if self.language:
            self.language = self.language.lower().strip()
        if self.label:
            self.label = normalize_label(self.label)
        if not self.data:
            raise ValueError("Translation data cannot be empty")

    def save(self, *args, **kwargs):
        skip_s3 = kwargs.pop("skip_s3", False)
        self.full_clean()
        if not skip_s3:
            self = handle_translation_s3(self)
        super().save(*args, **kwargs)


class FlowTranslationMapping(models.Model):
    """
    Defines which translation file is used in a flow,
    for a specific domain (screen) and language.
    """

    flow = models.ForeignKey(
        'Flow',
        on_delete=models.CASCADE,
        related_name='translation_mappings',
        help_text="Flow where this translation will be used"
    )

    translation_file = models.ForeignKey(
        TranslationFile,
        on_delete=models.CASCADE,
        related_name='flow_usages',
        help_text="Translation file to use"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "Flow Translation Mapping"
        verbose_name_plural = "Flow Translation Mappings"
        unique_together = ['flow', 'translation_file']
        indexes = [
            models.Index(fields=['flow', 'translation_file']),
        ]

    def __str__(self):
        return f"{self.flow.flow_name} | {self.translation_file.namespace} | {self.translation_file.language}"
