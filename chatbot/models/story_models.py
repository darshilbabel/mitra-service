import io
import os
import logging
from urllib.parse import urlparse
from django.db import models, transaction
from django.db.models.functions import Lower
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.core.validators import MinLengthValidator
from simple_history.models import HistoricalRecords
from chatbot.models import Profile, TagChoices, StoryLanguageChoices, StorySourceChoices, MediaTypeChoices, \
    StoryStatusChoices, Company, TagSourceChoices, ReportTypeChoices
from pillow_heif import register_heif_opener
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

S3_BASE_URL = os.getenv('S3_MEDIA_URL')
register_heif_opener()

logger = logging.getLogger('django')


def _delete_s3_key(key):
    if not key:
        return
    try:
        from chatbot.services.storage import StorageFactory
        StorageFactory.get_storage_handler().delete_file(key)
    except Exception as e:
        logger.warning("StoryMedia S3 delete failed for key=%s: %s", key, e)


class LeaderCategory(models.Model):
    """
    Master list of leader categories a programme can belong to, such as Woman Leader or
    School Leader. Reports inherit their category from the programme they are mapped to,
    rather than from the person who filed them.
    """

    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=1000)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Leader Category"
        verbose_name_plural = "Leader Categories"
        indexes = [
            models.Index(fields=['code']),
        ]
        # Matched with name__iexact when resolving CSV corrections, so uniqueness has to
        # be case-insensitive: a plain unique flag would still allow two rows differing
        # only in case and leave that lookup returning an arbitrary one. Mirrors the
        # constraint on Role.name.
        constraints = [
            models.UniqueConstraint(Lower('name'), name='unique_leader_category_name_ci'),
        ]


class Role(models.Model):
    """
    Master list of roles a report can be attributed to, such as Woman Leader or Parent.
    Capture flows resolve whatever the user says about themselves onto one of these rows,
    so the set here is the only vocabulary a report's role can use.
    """

    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=1000)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Role"
        verbose_name_plural = "Roles"
        indexes = [
            models.Index(fields=['code']),
        ]
        constraints = [
            # Roles are resolved with Role.objects.filter(name__iexact=...).first(), and
            # the capture prompts expose names rather than codes. A plain unique=True
            # would still allow 'Parent' and 'parent' to coexist and leave that lookup
            # ambiguous, so uniqueness is enforced case-insensitively.
            models.UniqueConstraint(Lower('name'), name='unique_role_name_ci'),
        ]


class Story(models.Model):
    """
    Represents a story created by a user or AI within a chat session.
    Stores content, metadata, language, status, and translation support.
    """

    title = models.CharField(max_length=1000)
    author = models.ForeignKey(Profile, on_delete=models.SET_NULL, null=True, blank=True)
    content = models.TextField(null=True, blank=True)
    blurb = models.TextField(null=True, blank=True)
    tweet = models.TextField(null=True, blank=True)
    session = models.CharField(max_length=255, unique=True)
    objective = models.TextField(null=True, blank=True)
    action_steps = models.TextField(null=True, blank=True)
    impact = models.TextField(null=True, blank=True)
    micro_improvement = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=1000, null=True, blank=True)
    district = models.CharField(max_length=1000, null=True, blank=True)
    state = models.CharField(max_length=1000, null=True, blank=True)
    block = models.CharField(max_length=1000, null=True, blank=True)
    village = models.CharField(max_length=1000, null=True, blank=True)
    program = models.ForeignKey(
        'chatbot.Program', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stories'
    )
    leader_category = models.ForeignKey(
        LeaderCategory, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stories'
    )
    role = models.ForeignKey(
        Role, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stories'
    )
    report_type = models.CharField(
        max_length=50, choices=ReportTypeChoices.choices,
        null=True, blank=True, db_index=True,
        help_text="Report classification, derived from the PDF template of the story's flow."
    )
    formatted_content = models.TextField(null=True, blank=True)
    language = models.CharField(max_length=1000, choices=StoryLanguageChoices.choices,
                                default=StoryLanguageChoices.ENGLISH)
    source = models.CharField(max_length=1000, choices=StorySourceChoices.choices,
                              default=StorySourceChoices.AI_GENERATED)
    story_code = models.CharField(max_length=100, null=True, blank=True)
    stage = models.CharField(max_length=100, choices=StoryStatusChoices.choices, default=StoryStatusChoices.PENDING)
    summary = models.TextField(null=True, blank=True)
    other_params = models.JSONField(null=True, blank=True)

    client_created_at = models.DateTimeField(null=True, blank=True)
    client_updated_at = models.DateTimeField(null=True, blank=True)
    validation_logs = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title

    def get_translation(self, language):
        """Get story content in specified language"""
        if language == self.language:
            return self

        try:
            return self.translations.get(language=language)
        except StoryTranslation.DoesNotExist:
            return None

    def get_available_languages(self):
        """Get list of available languages for this story"""
        langs = [self.language]
        langs.extend(self.translations.values_list('language', flat=True))
        return langs

    def get_translation_languages(self):
        """Get only translation languages (excludes main story language)"""
        return list(self.translations.values_list('language', flat=True))

    def get_report_type(self):
        """Report classification tag, taken from the PDF template of the story's flow."""
        # Local import avoids circular dependency: story_models is loaded before
        # company_models by chatbot/models/__init__.py.
        from chatbot.models.company_models import PDFTemplates

        flow_route = (self.other_params or {}).get('flow')
        if not flow_route:
            return None

        # A flow may have several templates (e.g. a guest and an auth variant), so the
        # query is ordered to make the result stable - an unordered .first() could return
        # a different row, and therefore a different stored report_type, between runs.
        # user_type is deliberately not filtered on here: the tag classifies the report,
        # not the rendering variant, so every template on a flow carries the same tag.
        # Filtering by user_type would return None whenever a story's audience has no
        # matching template, dropping the classification entirely.
        pdf_template = PDFTemplates.objects.filter(
            flow__flow_route=flow_route
        ).exclude(tag__isnull=True).exclude(tag='').order_by('id').first()

        return pdf_template.tag if pdf_template else None

    def save(self, *args, **kwargs):
        location_changed = False
        if self.pk:
            old = Story.objects.filter(pk=self.pk).values('state', 'district').first()
            if old:
                location_changed = (old['state'] != self.state) or (old['district'] != self.district)
        else:
            location_changed = bool(self.state)

        if location_changed:
            self._derive_program_and_leader_category()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                extended = list(update_fields)
                for field in ('program', 'leader_category'):
                    if field not in extended:
                        extended.append(field)
                kwargs['update_fields'] = extended

        # Report type is a stored column so the dashboard can filter and aggregate on it
        # in SQL. Derived once, on the first save that can resolve it, and never
        # overwritten afterwards.
        if not self.report_type:
            derived = self.get_report_type()
            if derived in ReportTypeChoices.values:
                self.report_type = derived
                # A partial save (e.g. the state cron's update_fields=['state','district'])
                # would otherwise drop the value silently.
                update_fields = kwargs.get('update_fields')
                if update_fields is not None and 'report_type' not in update_fields:
                    kwargs['update_fields'] = list(update_fields) + ['report_type']
            elif derived:
                logger.warning(
                    "Unrecognised report type tag %r for session %s; leaving report_type unset",
                    derived, self.session,
                )

        super().save(*args, **kwargs)

    def _derive_program_and_leader_category(self):
        # Local imports avoid circular dependency: story_models → chat_models/company_models
        # are all loaded by chatbot/models/__init__.py in sequence.
        from chatbot.models.chat_models import ChatSession
        from chatbot.models.company_models import CompanyBotProgramMapping

        if not self.state:
            self.program = None
            self.leader_category = None
            return

        chat_session = (
            ChatSession.objects
            .filter(session=self.session)
            .select_related('company_bot')
            .first()
        )
        if not chat_session or not chat_session.company_bot:
            logger.warning(
                'Story._derive: no ChatSession/company_bot for session=%s; skipping derivation',
                self.session,
            )
            self.program = None
            self.leader_category = None
            return

        # Matched case-insensitively: CompanyBotProgramMapping.clean() only guards values
        # typed in the admin, while state is also written by the state categorisation cron
        # and the CSV correction tool. An exact match let a value such as 'bihar' from
        # those producers silently resolve to no program and no leader category.
        mapping = CompanyBotProgramMapping.objects.filter(
            company_bot=chat_session.company_bot,
            state__iexact=self.state,
            is_active=True,
        ).select_related('program', 'leader_category').first()

        if mapping:
            self.program = mapping.program
            self.leader_category = mapping.leader_category
        else:
            self.program = None
            self.leader_category = None

    class Meta:
        indexes = [
            models.Index(fields=['title']),
            models.Index(fields=['session']),
            models.Index(fields=['author'])
        ]


class StoryMedia(models.Model):
    """
    Stores media files associated with a story.
    Handles file uploads, format conversion, and base64 encoding.
    """

    def get_file_upload_path(self, filename):
        folder_name = 'chatbot/storymedia/{}'.format(self.story.id)
        upload_path = f"{folder_name}/{filename}"
        return upload_path

    name = models.CharField(max_length=1000)
    file = models.FileField(upload_to=get_file_upload_path, max_length=1000, null=True, blank=True)
    story = models.ForeignKey(Story, related_name='story_media', on_delete=models.CASCADE)
    include_in_story = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    base64_str = models.TextField(null=True, blank=True)
    source_path = models.TextField(null=True, blank=True)
    media_type = models.CharField(max_length=100, choices=MediaTypeChoices.choices, null=True, blank=True)
    file_url = models.CharField(max_length=2000, null=True, blank=True)

    def get_public_url(self):
        if self.file:
            return f"{S3_BASE_URL}{self.file.name}"
        elif self.file_url:
            return self.file_url
        else:
            return ""

    def save(self, *args, **kwargs):
        try:
            if not self.file:
                super().save(*args, **kwargs)
                return
            file_ext = os.path.splitext(self.file.name)[1].lower()
            print("file_ext:", file_ext)
            print("File name:", self.file.name)

            # Convert HEIC/HEIF to JPEG
            if file_ext in ['.heic', '.heif']:
                try:
                    self.file.seek(0)
                    image = Image.open(self.file)
                    converted_io = io.BytesIO()
                    image.save(converted_io, format='JPEG')

                    # Replace the file with JPEG
                    converted_io.seek(0)
                    new_filename = os.path.splitext(self.file.name)[0] + ".jpg"
                    self.file = ContentFile(converted_io.read(), name=new_filename)
                    self.media_type = MediaTypeChoices.JPEG

                    print("Converted HEIC/HEIF to JPEG:", new_filename)
                except UnidentifiedImageError:
                    print("Could not identify image file. Make sure it's valid.")
                except Exception as e:
                    print("Unexpected error during HEIF conversion:", str(e))

        except Exception as e:
            print("Error during save():", str(e))

        super().save(*args, **kwargs)


@receiver(post_delete, sender=StoryMedia)
def delete_story_media_file_from_s3(sender, instance, **kwargs):
    """
    Fired on every StoryMedia delete, including the CASCADE Django runs
    when the parent Story is deleted (Collector sends signals per instance
    even though it never calls instance.delete()).
    """
    if instance.file:
        key = instance.file.name
    elif instance.file_url:
        key = urlparse(instance.file_url).path.lstrip('/')
    else:
        key = None
    if key:
        transaction.on_commit(lambda: _delete_s3_key(key))


class Tag(models.Model):
    """
    Represents a reusable tag used to categorize stories.
    Can be company-specific and linked to a creator profile.
    """

    name = models.CharField(max_length=1000, unique=True, null=False, blank=False,
                            validators=[MinLengthValidator(limit_value=3)])
    status = models.CharField(max_length=100, choices=TagChoices.choices, default=TagChoices.PENDING)
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True)

    source_type = models.CharField(
        max_length=50, choices=TagSourceChoices.choices, null=True, blank=True
    )

    description = models.TextField(null=True, blank=True)

    created_by = models.ForeignKey(Profile, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Global Tag"
        verbose_name_plural = "Global Tags"

    def __str__(self):
        return self.name


class StoryTag(models.Model):
    """
    Maps tags to stories with optional primary tag designation.
    Ensures a story cannot have duplicate tags.
    """

    story = models.ForeignKey(Story, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.DO_NOTHING)
    is_primary = models.BooleanField(default=False)

    created_by = models.ForeignKey(Profile, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.story.title} - {self.tag.name}"

    class Meta:
        unique_together = ('story', 'tag')


class StoryTranslation(models.Model):
    """
    Stores translated versions of a story in different languages.
    Maintains localized content while linking to the original story.
    """

    story = models.ForeignKey(Story, related_name='translations', on_delete=models.CASCADE)
    language = models.CharField(max_length=10, choices=StoryLanguageChoices.choices)

    title = models.CharField(max_length=1000)
    content = models.TextField(null=True, blank=True)
    blurb = models.TextField(null=True, blank=True)
    tweet = models.TextField(null=True, blank=True)
    objective = models.TextField(null=True, blank=True)
    action_steps = models.TextField(null=True, blank=True)
    impact = models.TextField(null=True, blank=True)
    micro_improvement = models.TextField(null=True, blank=True)
    formatted_content = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=1000, null=True, blank=True)
    district = models.CharField(max_length=1000, null=True, blank=True)
    state = models.CharField(max_length=1000, null=True, blank=True)
    block = models.CharField(max_length=1000, null=True, blank=True)
    other_params = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('story', 'language')
        indexes = [
            models.Index(fields=['story', 'language']),
        ]

    def __str__(self):
        return f"{self.story.title} ({self.language})"
