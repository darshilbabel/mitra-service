"""
Backfill missing StoryMedia rows found by audit_story_media_s3 (ORPHAN_IN_S3),
then delete + regenerate the affected StoryTranslation via generate_story
(v2 only).

Why this exists
---------------
audit_story_media_s3 finds S3 objects with no StoryMedia row pointing at them
(ORPHAN_IN_S3) and writes them to a CSV. The object already exists in the
bucket - nothing needs to be re-uploaded - but the row is missing, so
pdf/story_images_page.py never renders the photo and any previously
generated report is stale.

This command closes that loop from the CSV:

  1. Create the missing StoryMedia row(s) for each story_id, using the CSV's
     `s3_key` column to build `file_url` exactly as a normal upload would
     produce it (S3_MEDIA_URL + key - see StoryMedia.get_public_url()).
     `file` is left empty; the object is not re-uploaded through Django, only
     referenced.
  2. Delete the existing StoryTranslation row(s) for the story's session and
     regenerate them via generate_story (--report-version 2 only), so the new
     photo actually appears in the next translated report.
     Story itself is untouched: create_story_object/generate_story locate the
     existing Story by session and update it in place (see
     chatbot/utils/story_utils/*_story_tasks.py) - there is no
     Story.objects.create() in that path - so nothing needs to be
     snapshotted/restored for Story, only for StoryTranslation.

A story_id with no StoryTranslation row is left alone: nothing is deleted and
generate_story is not called for it. There is nothing to regenerate if a
translation was never produced.

Usage
-----
    python manage.py regenerate_reports_from_s3_audit --csv orphans.csv

    # Different column names / a global flow override / preview only
    python manage.py regenerate_reports_from_s3_audit --csv orphans.csv \
        --column story_id --flow guest-discussion --dry-run

CSV expectations
----------------
Every row in the file is processed - the caller is expected to have already
filtered it down to the rows that matter (e.g. ORPHAN_IN_S3) before passing
it in. Required columns:

  <--column, default "story_id">   Story.id
  s3_key                           bare S3 object key for the photo

Rows are grouped by story_id; distinct s3_key values under the same story_id
each get their own StoryMedia row. Duplicate story_id/s3_key pairs are
processed once.

Report regeneration
--------------------
Uses generate_story (--report-version 2) exclusively - see
chatbot/management/commands/regenerate_transliterated_reports.py for the same
entrypoint used against a session/stage scope rather than a CSV of story ids.
The flow-resolution, guard and snapshot/restore logic here is the same as
that command's Step 2, restricted to StoryTranslation only.
"""

import csv
import logging
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from chatbot.models import (
    ChatSession,
    ChatType,
    Flow,
    MediaTypeChoices,
    SessionFlowName,
    Story,
    StoryMedia,
    StoryTranslation,
    Voice,
    VoiceType,
)
from chatbot.utils.story_utils.story_utils import generate_story

logger = logging.getLogger("django")

# Same translation regenerate_transliterated_reports.py uses: shikshalokam_chaupal
# reports are generated with the GuestDiscussion flow.
SESSION_TYPE_TO_FLOW = {
    ChatType.shikshaChaupal.value: SessionFlowName.GuestDiscussion.value,
}

EXTENSION_TO_MEDIA_TYPE = {
    ".jpg": MediaTypeChoices.JPEG,
    ".jpeg": MediaTypeChoices.JPEG,
    ".png": MediaTypeChoices.PNG,
    ".webp": MediaTypeChoices.WEBP,
    ".heic": MediaTypeChoices.HEIC,
    ".heif": MediaTypeChoices.HEIF,
    ".svg": MediaTypeChoices.SVG,
}


def media_type_for(key):
    ext = os.path.splitext(key or "")[1].lower()
    return EXTENSION_TO_MEDIA_TYPE.get(ext, "")


class Command(BaseCommand):
    help = (
        "Create missing StoryMedia rows from an audit_story_media_s3 CSV, then "
        "delete + regenerate StoryTranslation for the affected stories."
    )

    MAX_REPORT_ATTEMPTS = 3

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="CSV produced by audit_story_media_s3.")
        parser.add_argument(
            "--column", default="story_id",
            help="Column holding Story.id in the CSV. Default: story_id.",
        )
        parser.add_argument(
            "--flow", default=None,
            help="Override flow resolution for every row, instead of resolving "
                 "per-session from ChatSession.session_type.",
        )
        parser.add_argument("--dry-run", action="store_true")

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        self.flow_override = opts["flow"]
        self._flow_row_cache = {}
        self._story_voice_cache = {}
        dry_run = opts["dry_run"]

        media_base = os.getenv("S3_MEDIA_URL") or ""
        if not media_base:
            raise CommandError("S3_MEDIA_URL is not set - cannot build file_url values.")

        by_story = self._read_csv(opts["csv"], opts["column"])
        if not by_story:
            self.stdout.write(self.style.WARNING("No rows found in CSV."))
            return

        logger.info(
            "[regen_from_audit] START csv=%s column=%s stories=%d flow_override=%s dry_run=%s",
            opts["csv"], opts["column"], len(by_story), self.flow_override, dry_run,
        )

        created = skipped_no_story = 0
        regen_ok = regen_skipped = regen_failed = 0

        for story_id in sorted(by_story):
            story = Story.objects.filter(id=story_id).first()
            if not story:
                self.stdout.write(self.style.WARNING(f"  story_id={story_id}: no Story row, skip."))
                logger.error("[regen_from_audit] story_id=%s SKIPPED: no Story row", story_id)
                skipped_no_story += 1
                continue

            for key in by_story[story_id]:
                if dry_run:
                    self.stdout.write(f"  [dry-run] would create StoryMedia story_id={story_id} key={key}")
                    created += 1
                    continue

                _, was_created = StoryMedia.objects.get_or_create(
                    story=story,
                    file_url=media_base + key,
                    defaults={
                        "name": os.path.basename(key),
                        "media_type": media_type_for(key),
                        "include_in_story": True,
                    },
                )
                if not was_created:
                    self.stdout.write(f"  story_id={story_id}: StoryMedia already exists for key={key}, skip.")
                    continue
                    
                created += 1
                logger.info(
                    "[regen_from_audit] story_id=%s created StoryMedia key=%s file_url=%s",
                    story_id, key, media_base + key,
                )

            outcome = self._regenerate_translation(story.session, dry_run)
            if outcome == "ok":
                regen_ok += 1
            elif outcome == "skipped":
                regen_skipped += 1
            else:
                regen_failed += 1

        self._summary(created, skipped_no_story, regen_ok, regen_skipped, regen_failed, dry_run)

    # ------------------------------------------------------------------ #
    def _read_csv(self, path, column):
        """Return {story_id (int): [distinct s3_key, ...]} from the CSV."""
        by_story = {}
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if column not in (reader.fieldnames or []):
                raise CommandError(f"Column '{column}' not found in {path}. Found: {reader.fieldnames}")
            if "s3_key" not in (reader.fieldnames or []):
                raise CommandError(f"Column 's3_key' not found in {path}. Found: {reader.fieldnames}")

            for row in reader:
                raw_id = (row.get(column) or "").strip()
                key = (row.get("s3_key") or "").strip()
                if not raw_id or not key:
                    continue
                try:
                    story_id = int(raw_id)
                except ValueError:
                    self.stdout.write(self.style.WARNING(f"  Skipping row: '{raw_id}' is not a valid story id."))
                    continue
                keys = by_story.setdefault(story_id, [])
                if key not in keys:
                    keys.append(key)
        return by_story

    # ------------------------------------------------------------------ #
    def _resolve_flow(self, chat_session):
        if self.flow_override:
            return self.flow_override
        st = chat_session.session_type
        return SESSION_TYPE_TO_FLOW.get(st, st)

    def _flow_row(self, flow):
        if flow not in self._flow_row_cache:
            self._flow_row_cache[flow] = Flow.objects.filter(
                flow_route=flow, active=True, story_bot__isnull=False
            ).select_related("story_bot").first()
        return self._flow_row_cache[flow]

    def _story_bot_can_transliterate(self, flow, language, flow_row):
        if language == "en":
            return True
        key = (flow, language)
        if key not in self._story_voice_cache:
            try:
                story_bot = flow_row.story_bot
                self._story_voice_cache[key] = Voice.objects.filter(
                    company_bot=story_bot, type=VoiceType.Transliterate, language=language
                ).exists()
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.WARNING(f"  Could not resolve story bot for flow='{flow}': {exc}"))
                logger.error("[regen_from_audit] could not resolve story bot for flow=%s: %s", flow, exc, exc_info=True)
                self._story_voice_cache[key] = False
        return self._story_voice_cache[key]

    def _regenerate_translation(self, session, dry_run):
        """Delete + regenerate StoryTranslation for one session.
        Returns "ok", "skipped", or "failed"."""
        chat_session = ChatSession.objects.filter(session=session).first()
        if not chat_session:
            self.stdout.write(self.style.WARNING(f"  session={session}: ChatSession missing, skip regen."))
            logger.error("[regen_from_audit] session=%s SKIPPED: ChatSession row missing", session)
            return "skipped"

        if not StoryTranslation.objects.filter(story__session=session).exists():
            self.stdout.write(f"  session={session}: no StoryTranslation, skip regen.")
            logger.info("[regen_from_audit] session=%s skipped: no StoryTranslation", session)
            return "skipped"

        profile_id = chat_session.profile_id
        language = chat_session.language or "en"
        flow = self._resolve_flow(chat_session)
        flow_row = self._flow_row(flow)

        logger.info(
            "[regen_from_audit] session=%s profile_id=%s flow=%s language=%s",
            session, profile_id, flow, language,
        )

        if not flow_row:
            self.stdout.write(self.style.ERROR(
                f"  Skipping session={session}: no active Flow row with a story_bot for flow='{flow}'."
            ))
            logger.error("[regen_from_audit] session=%s flow=%s SKIPPED: no active Flow row with story_bot", session, flow)
            return "skipped"

        if not profile_id:
            self.stdout.write(self.style.ERROR(
                f"  Skipping session={session}: generate_story needs a profile, but profile_id is empty."
            ))
            logger.error("[regen_from_audit] session=%s flow=%s SKIPPED: profile_id required", session, flow)
            return "skipped"

        if not self._story_bot_can_transliterate(flow, language, flow_row):
            self.stdout.write(self.style.ERROR(
                f"  Skipping session={session}: no Transliterate Voice for language='{language}' "
                f"on the story bot of flow='{flow}'."
            ))
            logger.error(
                "[regen_from_audit] session=%s flow=%s language=%s SKIPPED: no Transliterate Voice on story bot",
                session, flow, language,
            )
            return "skipped"

        if dry_run:
            self.stdout.write(
                f"  [dry-run] would delete StoryTranslation and regenerate session={session} "
                f"flow={flow} language={language}"
            )
            return "ok"

        translation_snapshot = list(StoryTranslation.objects.filter(story__session=session).values())
        with transaction.atomic():
            deleted, _ = StoryTranslation.objects.filter(story__session=session).delete()
        logger.info("[regen_from_audit] session=%s deleted story_translations=%s", session, deleted)

        fatal_exc = None
        for attempt in range(1, self.MAX_REPORT_ATTEMPTS + 1):
            try:
                story_id, _content, error_msg, error_type = generate_story(
                    profile_id=profile_id,
                    session=session,
                    access_token=None,
                    flow=flow,
                    language=language,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[regen_from_audit] session=%s profile_id=%s flow=%s language=%s "
                    "attempt=%s/%s RAISED (fatal, no further retries): %s",
                    session, profile_id, flow, language,
                    attempt, self.MAX_REPORT_ATTEMPTS, exc, exc_info=True,
                )
                fatal_exc = exc
                break

            if not error_msg:
                self.stdout.write(self.style.SUCCESS(
                    f"  Regenerated session={session} story_id={story_id} (attempt {attempt})"
                ))
                logger.info(
                    "[regen_from_audit] session=%s regenerated story_id=%s flow=%s language=%s attempt=%s",
                    session, story_id, flow, language, attempt,
                )
                return "ok"

            logger.error(
                "[regen_from_audit] session=%s profile_id=%s flow=%s language=%s "
                "attempt=%s/%s FAILED: error_type=%s error_msg=%s",
                session, profile_id, flow, language,
                attempt, self.MAX_REPORT_ATTEMPTS, error_type, error_msg,
            )

        if fatal_exc is not None:
            self.stdout.write(self.style.ERROR(
                f"  Exception session={session}: {fatal_exc}. Restoring original StoryTranslation from snapshot."
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f"  Report failed session={session} after {self.MAX_REPORT_ATTEMPTS} attempts. "
                f"Restoring original StoryTranslation from snapshot."
            ))
        self._restore_translation_snapshot(session, translation_snapshot)
        return "failed"

    # ------------------------------------------------------------------ #
    def _restore_translation_snapshot(self, session, translation_snapshot):
        if not translation_snapshot:
            return
        try:
            with transaction.atomic():
                new_translations = []
                for row in translation_snapshot:
                    row = dict(row)
                    row.pop("id", None)
                    new_translations.append(StoryTranslation(**row))
                StoryTranslation.objects.bulk_create(new_translations)
            self.stdout.write(self.style.WARNING(f"  Restored original StoryTranslation for session={session}."))
            logger.info(
                "[regen_from_audit] session=%s snapshot restored, translations=%s",
                session, len(translation_snapshot),
            )
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(
                f"  !!! MANUAL RECOVERY NEEDED !!! session={session}: restore failed: {exc}"
            ))
            logger.critical(
                "[regen_from_audit] session=%s RESTORE FAILED, StoryTranslation snapshot is about to be "
                "lost -- manual recovery needed. error=%s translation_snapshot=%r",
                session, exc, translation_snapshot, exc_info=True,
            )

    # ------------------------------------------------------------------ #
    def _summary(self, created, skipped_no_story, regen_ok, regen_skipped, regen_failed, dry_run):
        logger.info(
            "[regen_from_audit] DONE dry_run=%s storymedia_created=%d skipped_no_story=%d "
            "regen_ok=%d regen_skipped=%d regen_failed=%d",
            dry_run, created, skipped_no_story, regen_ok, regen_skipped, regen_failed,
        )
        self.stdout.write("\n" + "=" * 50)
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}StoryMedia created: {created}"))
        if skipped_no_story:
            self.stdout.write(self.style.WARNING(f"{prefix}story_id with no Story row: {skipped_no_story}"))
        self.stdout.write(self.style.SUCCESS(f"{prefix}Reports regenerated: {regen_ok}"))
        if regen_skipped:
            self.stdout.write(self.style.WARNING(f"{prefix}Regeneration skipped (no translation/guard failed): {regen_skipped}"))
        if regen_failed:
            self.stdout.write(self.style.ERROR(f"{prefix}Regeneration failed: {regen_failed}"))
        self.stdout.write("=" * 50)
