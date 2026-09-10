"""
Audit StoryMedia rows against the objects actually present in the S3 bucket.

Read-only. This command never writes to the database and never mutates S3.

Why this exists
---------------
Story images are NOT uploaded through Django. The client:

  1. POSTs to `get_presigned_url` (chatbot/views/aws_views.py), which builds the
     object key as `{folder_structure}{storyId}/{epoch_ms}-{fileName}` and hands
     back a presigned PUT URL.
  2. PUTs the bytes straight to S3.
  3. POSTs to `/api/storymedia/` (StoryMediaListCreateView) with `file_url` to
     create the StoryMedia row.

Steps 2 and 3 are not atomic. When step 3 never lands - client dropped, request
timed out, tab closed - the object sits in S3 with no row pointing at it, and
the report renders without the photo. `pdf/story_images_page.py` builds the
image page from `StoryMedia.objects.filter(story=..., include_in_story=True)`,
so a missing row is invisible to report generation.

How the audit works
-------------------
One sweep of the bucket, one pass over the stories, matched on the object key.

  1. LIST the whole prefix once (not once per story) and group every object by
     the identifier segment of its key - the `{storyId}` the client passed to
     the presign endpoint. That is sometimes a numeric Story.id and sometimes a
     session token, so both are kept as raw strings and resolved against real
     rows rather than guessed at from shape.

  2. Measure what share of those identifiers name a Story in THIS database.
     Below --min-resolve the run stops without writing (see 'Before trusting a
     full run' below).

  3. For each story in scope, collect the keys its StoryMedia rows claim, from
     both `file_url` and `file`, normalised to bare object keys. Compare that
     set against the objects sitting under the story's identifier:

       object present, row present   -> OK
       object present, no row        -> ORPHAN_IN_S3   (the ticket's bug)
       row present, object absent    -> MISSING_IN_S3
       neither, but base64_str set   -> BASE64_ONLY
       neither at all                -> NO_FILE_REF

  4. --scan-unattributed then sweeps whatever the story pass never claimed and
     separates "belongs to a story outside the filter" (OUT_OF_SCOPE) from
     "names a story this database does not have" (STORY_NOT_IN_DB) from
     "matches nothing at all" (UNATTRIBUTED_IN_S3). None of those three is a
     finding; they exist so they cannot be mistaken for one.

Everything is read-only: LIST against S3, SELECT against the database. The only
file written is the CSV named by --out.

Statuses
--------
Findings, in the order they usually matter:

  ORPHAN_IN_S3     object present in S3, no StoryMedia row references it.
                   The photo exists and is unreachable: the row must be created
                   and the report regenerated. It is NOT re-uploaded - the bytes
                   are already in the bucket.
  NO_FILE_REF      row has neither `file` nor `file_url` nor `base64_str`, so
                   get_public_url() returns "" and the report renders
                   <img src="">. The mirror image of ORPHAN_IN_S3 (PUT failed,
                   POST succeeded); nothing exists to restore.
  BASE64_ONLY      same blank render, but base64_str still holds the image.
                   Recoverable without touching S3 history: upload the bytes,
                   set file_url, regenerate. Split out from NO_FILE_REF because
                   the remedy is completely different.
  MISSING_IN_S3    row references a key that is absent from the bucket.
  STALE_REPORT     image row is newer than the stored PDF row, i.e. the report
                   was rendered before the photo was recorded. Needs
                   regeneration only.
  HOST_MISMATCH    key matches, but file_url points at a different host than the
                   bucket being audited. Matching is host-agnostic by design, so
                   without this a row referencing another environment would look
                   healthy while rendering from somewhere else.
  EXCLUDED         row and object both exist, but include_in_story is False.

Not findings - reported so they cannot be mistaken for findings:

  OK / OK_BASENAME    row and object agree (the latter matched on filename only)
  OK_FOREIGN_PATH     object exists under a different story id than the row's.
                      Rows migrated between environments keep the source
                      environment's id in the stored path. Do not backfill these.
  PREDATES_STORY      object was already in the bucket before the story it is
                      filed under existed, so it cannot be that story's photo.
                      A bucket that outlives its database collects these: the
                      database is reset, Story.id restarts, and old objects sit
                      under numbers that now name different stories. Excluded
                      from ORPHAN_IN_S3 because backfilling one would attach a
                      stranger's photo to a report.
  STORY_NOT_IN_DB     object filed under a story id absent from this database -
                      deleted story or partial dump, not an upload failure.
  OUT_OF_SCOPE        object belongs to a story excluded by the current filters.
  UNATTRIBUTED_IN_S3  identifier matches no story; CompanyChat.file_url is tried
                      as a last bridge back to a session.

Usage
-----
    # 1. Characterise a prefix and check its identifiers resolve to Stories.
    python manage.py audit_story_media_s3 --inspect chatbot/storymedia/

    # 2. Every flow and cycle. Photos only is the default.
    python manage.py audit_story_media_s3 --prefix chatbot/storymedia/ \
        --scan-unattributed --out orphans.csv

    # 3. One flow over a date window. --flow matches ChatSession.session_type.
    python manage.py audit_story_media_s3 --prefix chatbot/storymedia/ \
        --flow shikshalokam_chaupal --from 2026-06-01 --to 2026-08-26 \
        --out chaupal.csv

    # 4. Specific sessions reported from the field.
    python manage.py audit_story_media_s3 --prefix chatbot/storymedia/ \
        --session abc-123,def-456 --all-rows --out field_reports.csv

Scoping by flow
---------------
--flow filters ChatSession.session_type. That column is written when the session
is created - by chatbot/views/chat_view.py and the consumers - so it exists
before any Story does.

Do not assume its contents are a closed enum. The flow-specific consumers write
ChatType members ('shikshalokam_chaupal', 'normal', 'oneshot'), but chat_view.py
and the generic consumers write whatever the client sent as flow_name, and a
devqa database shows values that appear in neither ChatType nor SessionFlowName
('stakeholder-fgd', 'shiksha-samvad', 'bihar-student-fgd', 'delhi-shiksha-samvad').
Some values, notably 'guest-discussion', appear in BOTH this column and
Story.other_params['flow'] while selecting completely different sets of stories.
Check the values present in the database you are about to audit; do not carry
a value over from another environment or from an enum in the source.

It is deliberately NOT Story.other_params['flow']. That key is a client-supplied
request parameter, copied verbatim from the POST body of end_story /
end_story_v2 into a JSON blob by save_generic_story
(chatbot/utils/story_utils/common/generic_story_tasks.py). Three consequences:

  * It is written only by the story-generation path that ran to completion. A
    session whose story generation never finished has no flow recorded at all -
    and sessions where something went wrong are exactly the population this
    audit exists to find. Scoping on it silently drops them.
  * It is not a single vocabulary. The v1 endpoint dispatches on SessionFlowName
    values; the v2 endpoint resolves the same string as a Flow.flow_route
    (get_story_company_bot_simple). Downstream, Story.get_report_type() and
    migration 0087 both read it as a flow_route.
  * It is a JSON key with no index, so filtering it is a scan.

The two vocabularies are related but not equal. chatbot/management/commands/
regenerate_transliterated_reports.py carries the translation:

    SESSION_TYPE_TO_FLOW = {
        ChatType.shikshaChaupal.value:   # 'shikshalokam_chaupal'
            SessionFlowName.GuestDiscussion.value,   # 'guest-discussion'
    }

That is why --flow 'guest-discussion' and --flow 'shikshalokam_chaupal' select
the same stories through different columns, and why neither matches
'/shikshalokam_chaupal' - the leading slash belongs to a CompanyBot route, not
to either field.

--story-flow remains available for filtering Story.other_params['flow']
directly, which is the right tool when the question is specifically about the
value get_report_type() reads. Both output columns, session_type and
story_flow, are written on every row so the two can be reconciled.

Before trusting a full run
--------------------------
The audit is only meaningful when the bucket and the database come from the
SAME environment. From the bucket side there is no way to tell an object whose
StoryMedia row was lost (the bug) from an object belonging to an environment
this database has never seen (not a bug): both are keys with no matching row.
So every identifier that fails to resolve becomes a false ORPHAN_IN_S3.

A full run therefore measures the resolve rate itself and refuses to write
anything below --min-resolve (default 80%). Measured on a mismatched pairing,
3% of identifiers resolving turns into an ORPHAN_IN_S3 for essentially every
object swept - a full page of confident, uniformly wrong findings. Printing the
rate and trusting the reader to act on it is not enough protection for that, so
the check sits here, where the findings are produced.

--inspect remains the cheap way to see the number, and its id-range comparison
tells the two failure modes apart: a partial dump overlaps the bucket's range
sparsely, a different environment sits in a different range. If the rate is low
and you genuinely understand why, --min-resolve lets the run proceed, and every
ORPHAN_IN_S3 row is stamped untrustworthy in its notes column so the caveat
travels with the CSV instead of staying in someone's memory.

Environment
-----------
Reads the same variables the app uses: S3_BUCKET_NAME, AWS_REGION,
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_MEDIA_URL. The S3 client is taken
from StorageFactory.get_storage_handler(provider="AWS") - the same factory
aws_views.py uses - so credentials resolve exactly as they do in the running
app; --bucket overrides the bucket for cross-env checks. Provider is forced to
"AWS" rather than read from STORAGE_CLOUD_PROVIDER because this command talks
to the S3 API directly (ListObjectsV2 paginators); a LOCAL handler has no
client to give it.
"""

import csv
import os
from datetime import datetime, time as dtime
from urllib.parse import urlparse, unquote

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Min, Q
from django.utils import timezone

from chatbot.models import ChatSession, Story, StoryMedia, MediaTypeChoices
from chatbot.models.company_models import CompanyChat


# The share of a bucket's identifiers that must name a Story in this database
# before a full run is allowed to write findings. Below this the two sides are
# not the same environment (or the dump is a subset), and every unresolved
# identifier becomes a false ORPHAN_IN_S3. 80% is the same bar --inspect uses
# when it calls a prefix usable, so the two cannot contradict each other.
DEFAULT_MIN_RESOLVE = 0.8

# --inspect display tuning. Constant rather than a flag: it changes what the
# diagnostic looks like, never what it concludes, and no run has ever needed
# a different value.
SAMPLE_KEYS = 3

CSV_COLUMNS = [
    "status",
    "story_id",
    "session",
    # session_type is the scoping field --flow filters on; story_flow is the
    # client-supplied JSON value kept alongside it so the two can be compared.
    "session_type",
    "story_flow",
    "report_type",
    "state",
    "district",
    "block",
    "story_created_at",
    "s3_key",
    "s3_size_bytes",
    "s3_last_modified",
    "story_media_id",
    "story_media_name",
    "media_type",
    "include_in_story",
    "media_created_at",
    "pdf_updated_at",
    "public_url",
    "notes",
]


def parse_date(raw, end_of_day=False):
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]'. Returns an aware datetime."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            parsed = datetime.combine(
                parsed.date(), dtime.max if end_of_day else dtime.min
            )
        return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
    raise CommandError(f"Unparseable date: {raw!r} (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif",
    ".gif", ".bmp", ".tif", ".tiff", ".svg",
}


def url_host(raw):
    """Host of a full URL, or None for keys and blanks."""
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.startswith("s3://"):
        return raw[len("s3://"):].split("/", 1)[0] or None
    if raw.startswith("http://") or raw.startswith("https://"):
        return urlparse(raw).netloc or None
    return None


def is_image_key(key):
    return os.path.splitext(key or "")[1].lower() in IMAGE_EXTENSIONS


def csv_list(raw):
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def to_object_key(raw, bucket, media_base):
    """
    Normalise anything that identifies an object into a bare S3 key.

    Handles the four shapes that reach the DB:
      s3://bucket/key                     (what the presigned endpoint returns)
      https://bucket/key                  (AWSS3StorageHandler.get_public_url)
      https://cdn.example.com/key         (S3_MEDIA_URL + FileField name)
      chatbot/storymedia/12/1699-a.jpg    (FileField.name, already a key)
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    if raw.startswith("s3://"):
        rest = raw[len("s3://"):]
        parts = rest.split("/", 1)
        return unquote(parts[1]) if len(parts) == 2 else None

    if raw.startswith("http://") or raw.startswith("https://"):
        key = unquote(urlparse(raw).path).lstrip("/")
        # Path-style URLs put the bucket in the first path segment; virtual-host
        # and CDN URLs do not. Strip it only when it is actually there.
        if bucket and key.startswith(bucket.rstrip("/") + "/"):
            key = key[len(bucket.rstrip("/")) + 1:]
        return key or None

    if media_base:
        base_path = unquote(urlparse(media_base).path).strip("/")
        if base_path and raw.lstrip("/").startswith(base_path + "/"):
            return raw.lstrip("/")

    return unquote(raw).lstrip("/") or None


class Command(BaseCommand):
    help = "Audit StoryMedia rows against objects in S3 and report the mismatches."

    # This command reads. It never writes to the database or to S3, so the
    # system checks guard nothing here - they only bury the report under
    # staticfiles and JSONField warnings that belong to the project, not to
    # this run. Skipping them keeps the output to what was actually audited.
    requires_system_checks = []

    # ------------------------------------------------------------ formatting
    #
    # One visual language for every mode, so a reader who has seen one section
    # can read the rest: rules separate sections, fields are label-then-number
    # on a fixed column, and prose is reserved for the one line that says what
    # to do next.

    WIDTH = 70
    LABEL = 22

    def banner(self, title):
        self.stdout.write("\n" + "=" * self.WIDTH)
        self.stdout.write(f"  {title}")
        self.stdout.write("=" * self.WIDTH)

    def describe_scope(self, opts, images_only):
        """One line saying what this run covers, so a narrow run can never be
        mistaken for a full one when someone reads only the summary."""
        parts = ["photos only" if images_only else "photos and PDFs"]
        named = [
            ("flow", "flow"), ("story_flow", "story-flow"), ("report_type", "report-type"),
            ("state", "state"), ("district", "district"),
            ("session", "session"), ("story_id", "story-id"),
        ]
        scoped = [f"--{label} {opts[key]}" for key, label in named if opts.get(key)]
        if opts.get("date_from") or opts.get("date_to"):
            scoped.append(f"{opts.get('date_from') or 'start'} .. {opts.get('date_to') or 'now'}")
        parts.append(", ".join(scoped) if scoped else "all flows, all cycles")
        if opts.get("limit"):
            parts.append(f"first {opts['limit']} only")
        return " | ".join(parts)

    def rule(self, title=""):
        if title:
            self.stdout.write(f"\n--- {title} " + "-" * (self.WIDTH - len(title) - 5))
        else:
            self.stdout.write("-" * self.WIDTH)

    def field(self, label, value, note="", style=None):
        line = f"  {label:<{self.LABEL}} {value:>9}"
        if note:
            line = f"{line}   {note}"
        self.stdout.write(style(line) if style else line)

    def text_field(self, label, text, style=None):
        line = f"  {label:<{self.LABEL}} {text}"
        self.stdout.write(style(line) if style else line)

    def note(self, text, style=None):
        for line in text.strip("\n").split("\n"):
            self.stdout.write(style(f"  {line}") if style else f"  {line}")

    def add_arguments(self, parser):
        parser.add_argument(
            "--prefix",
            help="Object key prefix that story uploads live under, e.g. "
                 "'chatbot/storymedia/'. Run --discover first if unsure.",
        )
        parser.add_argument("--bucket", help="Override S3_BUCKET_NAME.")
        parser.add_argument(
            "--inspect",
            help="Print sample keys, identifier shapes and file types under one "
                 "prefix, and check how many identifiers actually resolve to a "
                 "Story - then exit. Run this before committing to a --prefix.",
        )

        # Scoping
        parser.add_argument(
            "--flow",
            help="Comma-separated ChatSession.session_type values, e.g. "
                 "'shikshalokam_chaupal' or 'stakeholder-fgd'. Not a closed "
                 "enum - some values are ChatType members and some are whatever "
                 "the client sent. Check the values present in the database you "
                 "are auditing rather than reusing a value from elsewhere.",
        )
        parser.add_argument(
            "--story-flow",
            help="Comma-separated Story.other_params['flow'] values, e.g. "
                 "'guest-discussion'. Filters the client-supplied JSON key that "
                 "get_report_type() reads, rather than the session's own type. "
                 "Prefer --flow unless you specifically mean that key; see the "
                 "'Scoping by flow' section of this command's docstring.",
        )
        parser.add_argument("--report-type", help="Comma-separated Story.report_type values.")
        parser.add_argument("--state", help="Comma-separated states (case-insensitive).")
        parser.add_argument("--district", help="Comma-separated districts (case-insensitive).")
        parser.add_argument("--session", help="Comma-separated session ids.")
        parser.add_argument("--story-id", help="Comma-separated Story ids.")
        parser.add_argument("--from", dest="date_from", help="Story.created_at >= this.")
        parser.add_argument("--to", dest="date_to", help="Story.created_at <= this.")
        parser.add_argument("--limit", type=int, help="Cap the number of stories examined.")

        # Behaviour
        parser.add_argument(
            "--min-resolve", type=float, default=DEFAULT_MIN_RESOLVE,
            help=f"Refuse to run when fewer than this share of the bucket's "
                 f"identifiers name a Story in this database (default "
                 f"{DEFAULT_MIN_RESOLVE:.0%}). A low rate means the bucket and the "
                 f"database are different environments, and every unresolved "
                 f"identifier would be reported as an orphan. Lower it only after "
                 f"reading --inspect and understanding WHY the rate is low; "
                 f"--min-resolve 0 disables the check entirely. Runs below "
                 f"{DEFAULT_MIN_RESOLVE:.0%} mark every ORPHAN_IN_S3 row as "
                 f"untrustworthy in its notes column.",
        )
        parser.add_argument(
            "--max-objects", type=int, default=2_000_000,
            help="Abort the prefix sweep past this many objects rather than "
                 "building an unbounded in-memory index.",
        )
        parser.add_argument(
            "--scan-unattributed", action="store_true",
            help="Also sweep the whole prefix for objects whose key carries no "
                 "numeric story id, and try to attribute them via CompanyChat.file_url.",
        )
        parser.add_argument(
            "--include-pdfs", action="store_true",
            help="Audit PDF objects as well as photos. OFF by default: the ticket "
                 "is about missing PHOTOS, and every report regeneration writes "
                 "another PDF into the same folder, so stale PDFs accumulate with "
                 "no row pointing at them. Counting those as orphans can outnumber "
                 "the real finding several times over, so opt in deliberately.",
        )
        parser.add_argument(
            "--all-rows", action="store_true",
            help="Write OK rows to the CSV too, not just the problems.",
        )
        parser.add_argument("--out", help="CSV output path. Defaults to stdout summary only.")

    # ------------------------------------------------------------------ setup

    def get_storage_client(self, bucket_override):
        try:
            from chatbot.services.storage import StorageFactory
        except ImportError as exc:  # pragma: no cover
            raise CommandError(f"Could not import the storage factory: {exc}")

        config = {}
        if bucket_override:
            config["bucket_name"] = bucket_override
        try:
            handler = StorageFactory.get_storage_handler(config=config)
        except ValueError as exc:
            raise CommandError(
                f"S3 not configured ({exc}). Set S3_BUCKET_NAME and AWS_REGION, "
                f"or pass --bucket."
            )
        return handler.client, handler.bucket_name

    # --------------------------------------------------------------- inspect

    def inspect_prefix(self, client, bucket, prefix, samples=SAMPLE_KEYS):
        """
        Characterise one prefix before trusting it as --prefix.

        Answers the three questions that decide whether the audit can work:
        what the identifier segment looks like, what file types are stored, and
        - the one that actually matters - how many of those identifiers resolve
        to a real Story by id or session. A prefix whose identifiers resolve to
        nothing will produce a bucketful of false orphans.
        """
        from collections import Counter

        if not prefix.endswith("/"):
            prefix += "/"

        shapes, extensions = Counter(), Counter()
        identifiers = set()
        examples = []
        total = 0

        for key, size, _ in self.list_prefix(client, bucket, prefix):
            total += 1
            tail = key[len(prefix):]
            if "/" not in tail:
                shapes["file directly under the prefix (no identifier segment)"] += 1
            else:
                segment = tail.split("/", 1)[0]
                identifiers.add(segment)
                if segment.isdigit():
                    shapes["numeric (Story.id shaped)"] += 1
                elif len(segment) == 32 and segment.isalnum():
                    shapes["32-char alphanumeric (session token shaped)"] += 1
                else:
                    shapes[f"other ({len(segment)} chars)"] += 1
            extensions[(os.path.splitext(key)[1] or "<none>").lower()] += 1
            if len(examples) < samples:
                examples.append((key, size))

        self.stdout.write(f"\nPrefix: {prefix}   ({total} objects)\n")

        # Bail before printing sample/shape/type sections that would all be
        # empty. An empty prefix is a spelling problem, not a finding.
        if not total:
            self.note(
                f"No objects at all under '{prefix}'. Nothing to characterise - check\n"
                f"the spelling, or run --discover for the real key layout.\n",
                self.style.WARNING)
            return
        self.stdout.write("sample keys:")
        for key, size in examples:
            self.stdout.write(f"  {size:>10}  {key}")

        self.stdout.write("\nidentifier segment shapes:")
        for shape, count in shapes.most_common():
            self.stdout.write(f"  {count:>7}  {shape}")

        self.stdout.write("\nfile types:")
        for ext, count in extensions.most_common(15):
            self.stdout.write(f"  {count:>7}  {ext}")

        if not identifiers:
            self.note(
                "\nObjects exist here but none carry an identifier segment - they sit "
                "directly\nunder this prefix, so no key-based join to a Story is "
                "possible.\n",
                self.style.WARNING)
            return

        by_session, by_id, resolved, rate = self.resolve_identifiers(identifiers)
        unresolved = len(identifiers) - len(resolved)

        self.stdout.write(
            f"\n{len(identifiers)} distinct identifiers under this prefix:\n"
            f"  {len(by_session):>7}  resolve to a Story.session\n"
            f"  {len(by_id):>7}  resolve to a Story.id\n"
            f"  {unresolved:>7}  resolve to neither   ({rate:.0%} resolved)"
        )
        for line in self.describe_resolve_failure(identifiers, resolved):
            self.stdout.write(line)

        if rate >= 0.8:
            self.stdout.write(self.style.SUCCESS(
                "\nThe join holds for most objects: this prefix is usable as --prefix.\n"
            ))
        elif rate >= 0.2:
            self.stdout.write(self.style.WARNING(
                "\nOnly a minority of identifiers resolve. The prefix looks right, but\n"
                "the database is missing most of the stories these objects belong to.\n"
                "Every unresolved identifier would be reported as an orphan, so fix\n"
                "the data before believing any count from a full run.\n"
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "\nAlmost nothing resolves. The prefix itself may be correct, but the\n"
                "bucket and the database are very likely from DIFFERENT ENVIRONMENTS,\n"
                "or the dump is a small subset. Compare the id ranges above: a partial\n"
                "dump overlaps the bucket's range sparsely, a different environment\n"
                "usually sits in a different range. Do NOT run a full audit on this\n"
                "pairing - it would report almost every object as an orphan.\n"
            ))

    def predates_story(self, story, last_modified):
        """
        True when this object CANNOT belong to this story, because it was
        already in the bucket before the story existed.

        get_presigned_url builds every key from a storyId the client already
        holds, so an upload can only ever follow story creation. An object
        older than its story is therefore filed under a REUSED id: a long-lived
        bucket kept its objects while the database behind it was reset and
        Story.id restarted from 1. Today's story 1 then inherits a folder full
        of a previous incarnation's photos.

        Measured on devqa 2026-09-08: 182 of 201 candidate objects predated
        their story, several by more than a year (story created 2026-06-17,
        object uploaded 2024-12-19). Without this check all 182 were reported
        as ORPHAN_IN_S3 - ten times the real number, every one of them work
        that does not exist.

        Deliberately conservative: if either timestamp is missing the object
        stays a finding rather than being silently dropped. This proves an
        object cannot belong to a story; it never proves that one does.
        """
        if not last_modified or not story.created_at:
            return False
        return last_modified < story.created_at

    def low_confidence_note(self):
        """
        The caveat appended to any finding whose truth depends on the bucket and
        the database being the same environment.

        Two statuses qualify. ORPHAN_IN_S3 is "an object here has no row", which
        is indistinguishable from "this object belongs to an environment whose
        rows this database never had". MISSING_IN_S3 is the mirror - "a row
        points at a key that is not here" - which is equally what you get when
        the row's key belongs to a bucket other than the one being audited.
        Both invert into noise on a mismatched pairing, so both carry the mark.
        """
        if not self.low_resolve:
            return ""
        return (
            f" | UNTRUSTWORTHY: only {self.resolve_rate:.0%} of this bucket's "
            f"identifiers resolve to a Story here, so this row may be an "
            f"environment mismatch rather than a real finding"
        )

    def resolve_identifiers(self, identifiers):
        """
        How many of the bucket's identifier segments name a Story in THIS
        database.

        This is the single most important number the command computes. It is
        what separates "the bucket and the database are the same environment"
        from "they are not", and every ORPHAN_IN_S3 depends on the answer: an
        identifier that resolves to no Story is indistinguishable, from the
        bucket side, from an upload whose row was lost.

        Returns (by_session, by_id, resolved, rate).
        """
        segments = list(identifiers)
        if not segments:
            return set(), set(), set(), 0.0
        numeric = [int(s) for s in segments if s.isdigit()]
        by_session = set(
            Story.objects.filter(session__in=segments).values_list("session", flat=True)
        )
        by_id = set(
            str(i) for i in
            Story.objects.filter(id__in=numeric).values_list("id", flat=True)
        )
        resolved = by_session | by_id
        return by_session, by_id, resolved, len(resolved) / len(segments)

    def describe_resolve_failure(self, identifiers, resolved):
        """
        The lines that separate the two ways a low resolve rate happens: a
        partial dump overlaps the bucket's id range but sparsely, while a
        different environment usually occupies a different range entirely.
        """
        lines = []
        numeric = [int(s) for s in identifiers if s.isdigit()]
        if numeric:
            db_range = Story.objects.aggregate(lo=Min("id"), hi=Max("id"))
            lines.append(f"  {'story ids in bucket':<{self.LABEL}} "
                         f"{min(numeric)} .. {max(numeric)}")
            lines.append(f"  {'story ids in database':<{self.LABEL}} "
                         f"{db_range['lo']} .. {db_range['hi']}")
        missing = sorted(set(identifiers) - resolved)[:8]
        if missing:
            lines.append(f"  {'not in the database':<{self.LABEL}} {', '.join(missing)}")
        return lines

    def list_prefix(self, client, bucket, prefix):
        """Yield (key, size, last_modified) for every object under prefix."""
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/"):
                    continue  # folder placeholder
                yield obj["Key"], obj["Size"], obj["LastModified"]

    def build_object_index(self, client, bucket, prefix, max_objects):
        """
        Sweep the prefix once and group every object by the identifier segment in
        its key. One LIST per 1000 objects, instead of one LIST per story.

        Returns (by_segment, total). Keys are the raw segment strings, kept as
        text rather than coerced to ints: the presign endpoint writes whatever
        the client passed as `storyId`, which is sometimes a numeric Story.id and
        sometimes a Story.session token. Deciding which is which is the caller's
        job, done by matching against real rows instead of guessing from shape.
        """
        by_segment = {}
        total = 0

        self.stdout.write(f"Indexing objects under {prefix} ...")
        for key, size, last_modified in self.list_prefix(client, bucket, prefix):
            total += 1
            if total > max_objects:
                raise CommandError(
                    f"Prefix holds more than {max_objects} objects. Narrow --prefix "
                    f"or raise --max-objects."
                )
            if total % 50_000 == 0:
                self.stdout.write(f"  ... {total} objects indexed")

            segment = key[len(prefix):].split("/", 1)[0]
            self.stdout.write(f"Segment: {segment}")
            by_segment.setdefault(segment, []).append((key, size, last_modified))

        numeric = sum(1 for seg in by_segment if seg.isdigit())
        self.stdout.write(
            f"Indexed {total} objects across {len(by_segment)} distinct identifiers "
            f"({numeric} numeric, {len(by_segment) - numeric} non-numeric)\n"
        )
        return by_segment, total

    # ------------------------------------------------------------------ query

    def build_queryset(self, opts):
        qs = Story.objects.all()

        story_ids = csv_list(opts.get("story_id"))
        if story_ids:
            qs = qs.filter(id__in=[int(sid) for sid in story_ids])

        sessions = csv_list(opts.get("session"))
        if sessions:
            qs = qs.filter(session__in=sessions)

        # Flow scoping goes through ChatSession.session_type rather than
        # Story.other_params['flow']. The docstring's 'Scoping by flow' section
        # has the full reasoning; the short version is that the JSON key is a
        # client-supplied parameter written only by the story-generation path
        # that completed, while session_type exists from session creation - so
        # it is present even for the sessions this audit is hunting.
        #
        # Story.session and ChatSession.session are both unique CharFields with
        # no ForeignKey between them, so this is a subquery on the session
        # string. One extra query for the whole run, not one per story.
        flows = csv_list(opts.get("flow"))
        if flows:
            qs = qs.filter(
                session__in=ChatSession.objects.filter(
                    session_type__in=flows
                ).values("session")
            )

        # Escape hatch: the raw JSON key, for when the question really is about
        # the value get_report_type() and migration 0087 read.
        story_flows = csv_list(opts.get("story_flow"))
        if story_flows:
            flow_q = Q()
            for flow in story_flows:
                flow_q |= Q(other_params__flow=flow)
            qs = qs.filter(flow_q)

        report_types = csv_list(opts.get("report_type"))
        if report_types:
            qs = qs.filter(report_type__in=report_types)

        states = csv_list(opts.get("state"))
        if states:
            state_q = Q()
            for state in states:
                state_q |= Q(state__iexact=state)
            qs = qs.filter(state_q)

        districts = csv_list(opts.get("district"))
        if districts:
            district_q = Q()
            for district in districts:
                district_q |= Q(district__iexact=district)
            qs = qs.filter(district_q)

        date_from = parse_date(opts.get("date_from"))
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        date_to = parse_date(opts.get("date_to"), end_of_day=True)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        return qs.order_by("id")

    # ----------------------------------------------------------------- handle

    def preflight(self):
        """
        A restored dump that omitted the storymedia table looks exactly like a
        catastrophic data-loss bug: every story reports NO_MEDIA. Print the raw
        table counts up front so that case is obvious before anyone reads the
        summary, and say so explicitly when media coverage is implausibly low.
        """
        total_stories = Story.objects.count()
        total_media = StoryMedia.objects.count()
        with_media = StoryMedia.objects.values("story_id").distinct().count()

        self.rule("database")
        self.field("stories", total_stories)
        self.field("StoryMedia rows", total_media)
        self.field("stories with media", with_media)

        if total_stories and (with_media / total_stories) < 0.05:
            share = with_media / total_stories
            self.stdout.write("")
            self.note(
                f"STOP: only {share:.1%} of stories have any StoryMedia row.",
                self.style.ERROR)
            self.note("""
That includes the PDF row update_story_pdf requires to already exist, so
this table is almost certainly incomplete or was never loaded. It is not a
plausible production state. ORPHAN_IN_S3 inflates to ~100% - every object has
no row.

Treat this run as a plumbing test only. The counts are not findings.
""", self.style.ERROR)
        return total_media

    def warn_flow_coverage(self, opts):
        """
        --flow resolves through ChatSession, so a story whose session has no
        ChatSession row cannot be selected by it - silently, because the result
        is a smaller sweep rather than an error.

        That share is not hypothetical: a partial restore can leave almost every
        story without one, in which case --flow reaches a sliver of the table and
        still prints a tidy summary underneath. Say the number out loud before
        anyone reads the counts.

        Costs one count query, and only when --flow is actually in play.
        """
        if not csv_list(opts.get("flow")):
            return

        total = Story.objects.count()
        if not total:
            return

        reachable = Story.objects.filter(
            session__in=ChatSession.objects.values("session")
        ).count()
        share = reachable / total

        self.stdout.write(
            f"--flow coverage: {reachable} of {total} stories ({share:.1%}) have a "
            f"ChatSession row and are therefore reachable by --flow"
        )
        if share < 0.5:
            self.stdout.write(self.style.ERROR(
                f"\n  {total - reachable} stories ({1 - share:.1%}) have no ChatSession row for\n"
                f"  their session. --flow cannot select them whatever value is passed, so\n"
                f"  this run covers at most {share:.1%} of the database.\n"
                f"\n"
                f"  On a partial dump that is expected. On a full one it means the scope is\n"
                f"  far narrower than it looks, and a zero finding count says nothing.\n"
                f"  Prefer running unscoped when the question is 'across every cycle'\n"
                f"  rather than 'this one flow'.\n"
            ))

    def warn_if_empty(self, opts, total):
        """
        A filter that matches nothing looks identical to a clean audit: zeros
        everywhere. Say so loudly instead, and name the filters that were applied.
        Returns False when the caller should stop.
        """
        if total:
            return True

        applied = {
            key: opts.get(key)
            for key in ("flow", "story_flow", "report_type", "state", "district",
                        "session", "story_id", "date_from", "date_to")
            if opts.get(key)
        }
        if not applied:
            self.stdout.write(self.style.WARNING(
                "No stories in this database at all - restore a dump before auditing."
            ))
            return False

        self.stdout.write(self.style.ERROR(
            "0 stories matched. This is a filter miss, not a clean audit."
        ))
        for key, value in applied.items():
            self.stdout.write(f"    --{key.replace('_', '-')} = {value}")
        if "flow" in applied:
            self.stdout.write(self.style.WARNING(
                "\n--flow matches ChatSession.session_type, e.g. "
                "'shikshalokam_chaupal'. That column is not a closed enum, and the "
                "values differ between environments. It is not "
                "Story.other_params['flow'] (use --story-flow for that - note some "
                "values such as 'guest-discussion' exist in BOTH columns and select "
                "different stories), and it is not a CompanyBot route such as "
                "'/shikshalokam_chaupal'. Check the values actually present in this "
                "database."
            ))
        if "story_flow" in applied:
            self.stdout.write(self.style.WARNING(
                "\n--story-flow matches the client-supplied Story.other_params['flow'] "
                "JSON key, which is absent on any story whose generation did not "
                "complete. If you meant 'every session of this type', use --flow."
            ))
        return False

    # Set by the resolve-rate gate in handle(). low_resolve stays False unless
    # --min-resolve was lowered below the default and the observed rate is under
    # it, in which case every ORPHAN_IN_S3 row says so in its notes.
    resolve_rate = 1.0
    low_resolve = False

    # Cache of session -> ChatSession.session_type, filled one batch at a time
    # by iterate(). Every output row carries session_type so that a CSV can be
    # reconciled against the --flow filter that produced it; without the cache
    # that column would cost one query per story.
    _session_types = None

    def prime_session_types(self, sessions):
        """
        Resolve session_type for a whole batch in one query.

        Misses are cached as "" as well, so a story whose session has no
        ChatSession row (partial dump, purged session) is not re-queried every
        time it is seen.
        """
        if self._session_types is None:
            self._session_types = {}
        wanted = {s for s in sessions if s and s not in self._session_types}
        if not wanted:
            return
        for session, session_type in ChatSession.objects.filter(
            session__in=wanted
        ).values_list("session", "session_type"):
            self._session_types[session] = session_type or ""
        for session in wanted:
            self._session_types.setdefault(session, "")

    def session_type_for(self, session):
        return (self._session_types or {}).get(session, "")

    def iterate(self, stories, limit, chunk_size=500):
        """
        Yield stories in primary-key batches with story_media prefetched.

        Two reasons this is not a plain .iterator():

        * `story.story_media.all()` inside the loop is one query per story. On
          ~10k stories that is ~10k round trips at the database - fine locally,
          rude against production. prefetch_related collapses each batch to one
          extra query.
        * prefetch_related is silently ignored by .iterator() before Django 4.1,
          so combining them would reintroduce the N+1 without any warning.
          Keyset pagination sidesteps the version question entirely.

        Keyset (pk > last) rather than OFFSET, so page N does not get slower as
        N grows.
        """
        qs = stories.prefetch_related("story_media")
        if limit:
            batch = list(qs[:limit])
            self.prime_session_types([s.session for s in batch])
            yield from batch
            return

        last_pk = 0
        while True:
            batch = list(qs.filter(pk__gt=last_pk)[:chunk_size])
            if not batch:
                return
            self.prime_session_types([s.session for s in batch])
            for story in batch:
                yield story
            last_pk = batch[-1].pk

    def handle(self, *args, **opts):
        media_base = os.getenv("S3_MEDIA_URL") or ""

        client, bucket = self.get_storage_client(opts.get("bucket"))

        prefix = opts.get("prefix")

        if opts["inspect"]:
            self.inspect_prefix(client, bucket, opts["inspect"])
            return

        if not prefix:
            prefix = "chatbot/storymedia/"
        if not prefix.endswith("/"):
            prefix += "/"

        # Photos are the finding; PDFs are regeneration exhaust. Default to
        # photos so the common run is the correct one and the noisy variant
        # has to be asked for by name.
        images_only = not opts["include_pdfs"]

        rows = []
        counters = {
            "stories": 0,
            "OK": 0,
            "OK_BASENAME": 0,
            "ORPHAN_IN_S3": 0,
            "MISSING_IN_S3": 0,
            "EXCLUDED": 0,
            "STALE_REPORT": 0,
            "UNATTRIBUTED_IN_S3": 0,
            "NO_FILE_REF": 0,
            "HOST_MISMATCH": 0,
        }

        self.banner("StoryMedia <-> S3 audit")
        self.text_field("bucket", bucket)
        self.text_field("prefix", prefix)
        self.text_field("scope", self.describe_scope(opts, images_only))

        self.preflight()
        self.warn_flow_coverage(opts)
        stories = self.build_queryset(opts)
        total = stories.count()
        if not self.warn_if_empty(opts, total):
            return
        self.field("stories in scope", total)

        allowed_hosts = {bucket}
        media_host = url_host(media_base)
        if media_host:
            allowed_hosts.add(media_host)

        # One sweep of the whole prefix, always. Listing per story instead would
        # leave all_keys below empty, and OK_FOREIGN_PATH depends on it: every
        # migrated row would then be reported MISSING_IN_S3.
        object_index, _ = self.build_object_index(
            client, bucket, prefix, opts["max_objects"]
        )

        # Flat set of every key under the prefix. Needed because the id segment
        # in a row's stored URL does not always equal that row's story_id -
        # migrated rows keep the SOURCE environment's story id in the path.
        # Looking only inside this story's own folder would call such a row
        # MISSING_IN_S3 even though the object is right there under another
        # folder.
        all_keys = set()
        for objs in object_index.values():
            all_keys.update(key for key, _, _ in objs)

        # ------------------------------------------------------------ the gate
        #
        # From the bucket side, an identifier belonging to another environment
        # and an identifier whose StoryMedia row was lost are the same thing: a
        # key with no matching row. So on a mismatched pairing every unresolved
        # identifier becomes a confident ORPHAN_IN_S3, and the run produces a
        # full page of uniformly wrong findings. --inspect reports this number
        # too, but a diagnostic nobody is obliged to read is not a safeguard.
        index_segments = set(object_index)
        if not index_segments:
            raise CommandError(
                f"  No objects found under prefix '{prefix}' in bucket {bucket}.\n"
                f"Run --discover to see the real key layout, or --inspect to "
                f"characterise a candidate prefix."
            )

        _, _, resolved_segments, self.resolve_rate = self.resolve_identifiers(index_segments)

        self.rule("environment check")
        self.field("identifiers in bucket", len(index_segments))
        self.field("naming a story here", len(resolved_segments),
                   f"({self.resolve_rate:.0%})")
        self.field("required minimum", f"{opts['min_resolve']:.0%}")

        if self.resolve_rate < opts["min_resolve"]:
            detail = "\n".join(self.describe_resolve_failure(index_segments, resolved_segments))
            raise CommandError(
                f"\n"
                f"  STOPPED - nothing was written.\n"
                f"\n"
                f"  This bucket and this database are not the same environment, so an\n"
                f"  object with no row cannot be told from an object whose story this\n"
                f"  database never had.\n"
                f"\n"
                f"{detail}\n"
                f"\n"
                f"  Fix by either:\n"
                f"    --bucket <host>       point at this database's own bucket\n"
                f"    restore a dump        from the environment this bucket serves\n"
                f"\n"
                f"  Or override, only if you know why the rate is low:\n"
                f"    --min-resolve {max(self.resolve_rate - 0.05, 0):.2f}    every ORPHAN_IN_S3 and MISSING_IN_S3\n"
                f"                          row is then stamped UNTRUSTWORTHY in the CSV."
            )

        self.low_resolve = self.resolve_rate < DEFAULT_MIN_RESOLVE
        if self.low_resolve:
            self.stdout.write("")
            self.note(
                f"proceeding at {self.resolve_rate:.0%} because --min-resolve was lowered - "
                f"every\nORPHAN_IN_S3 and MISSING_IN_S3 row is stamped UNTRUSTWORTHY.",
                self.style.WARNING)

        matched_segments = set()

        for story in self.iterate(stories, opts.get("limit")):
            counters["stories"] += 1
            if counters["stories"] % 200 == 0:
                self.stdout.write(f"  ... {counters['stories']}/{total}")

            media_rows = list(story.story_media.all())
            pdf_row = next(
                (m for m in media_rows if m.media_type == MediaTypeChoices.PDF), None
            )

            # Map every key this story's rows claim, from both file_url and file.
            key_to_media = {}
            basename_to_media = {}
            for media in media_rows:
                for raw in (media.file_url, media.file.name if media.file else None):
                    key = to_object_key(raw, bucket, media_base)
                    if not key:
                        continue
                    key_to_media.setdefault(key, media)
                    basename_to_media.setdefault(key.rsplit("/", 1)[-1], media)

            seen_media_ids = set()

            # The presign endpoint puts whatever the client sent as `storyId` into
            # the key. In practice that is sometimes the numeric Story.id and
            # sometimes the session token, so both are treated as identifiers.
            identifiers = [str(story.id)]
            if story.session:
                identifiers.append(story.session)

            story_objects = []
            for ident in identifiers:
                if ident in object_index:
                    matched_segments.add(ident)
                    story_objects.extend(object_index[ident])

            for key, size, last_modified in story_objects:
                if images_only and not is_image_key(key):
                    continue

                media = key_to_media.get(key)
                status = "OK"
                notes = ""

                if media is None:
                    media = basename_to_media.get(key.rsplit("/", 1)[-1])
                    if media is not None:
                        status = "OK_BASENAME"
                        notes = "matched on filename only - stored path differs from the S3 key"

                if media is None:
                    if self.predates_story(story, last_modified):
                        status = "PREDATES_STORY"
                        notes = (
                            f"object predates the story it is filed under "
                            f"(story created {story.created_at:%Y-%m-%d}, object "
                            f"uploaded {last_modified:%Y-%m-%d}) - the id was reused "
                            f"after a database reset, so this is NOT a lost row"
                        )
                    else:
                        status = "ORPHAN_IN_S3"
                        notes = (
                            "object uploaded to S3 but no StoryMedia row references it - "
                            "the /api/storymedia/ POST never completed"
                        )
                        notes += self.low_confidence_note()
                else:
                    seen_media_ids.add(media.id)
                    if not media.include_in_story and media.media_type != MediaTypeChoices.PDF:
                        status = "EXCLUDED"
                        notes = "row exists and object exists, but include_in_story=False so the report skips it"
                    else:
                        # Matching is host-agnostic by design - the same key is
                        # written with s3://, https://bucket/ and CDN forms. That
                        # means a row pointing at ANOTHER environment's host still
                        # matches a key here and looks OK, while the report
                        # actually renders from that other host. Catch it.
                        host = url_host(media.file_url)
                        if host and host not in allowed_hosts:
                            status = "HOST_MISMATCH"
                            notes = (
                                f"key matches, but file_url points at {host} rather "
                                f"than {bucket} - the report renders from that host, "
                                f"so the photo appears only if it serves the file"
                            )

                if status.startswith("OK") and not opts["all_rows"]:
                    counters[status] += 1
                    continue

                counters[status] = counters.get(status, 0) + 1
                rows.append(self.row(story, key, size, last_modified, media, pdf_row, status, notes))

            # Rows whose object is not in S3 at all.
            for media in media_rows:
                if media.id in seen_media_ids:
                    continue
                claimed = to_object_key(
                    media.file_url or (media.file.name if media.file else None),
                    bucket, media_base,
                )
                if not claimed:
                    # Neither file nor file_url. get_public_url() returns "" for
                    # these, so story_images_page emits <img src=""> and the
                    # report shows nothing - a rendering failure that is invisible
                    # to any S3 comparison, because there is no key to compare.
                    if images_only and media.media_type == MediaTypeChoices.PDF:
                        continue

                    # Whether base64_str still holds the bytes decides the
                    # remedy, so it is checked rather than left to the reader:
                    # with it the image can be re-uploaded and the row repaired,
                    # without it the photo is simply gone.
                    b64_len = len(media.base64_str or "")
                    if b64_len:
                        counters["BASE64_ONLY"] = counters.get("BASE64_ONLY", 0) + 1
                        status_no_key, note_no_key = "BASE64_ONLY", (
                            f"no file and no file_url, so the report renders a blank "
                            f"image - but base64_str still holds {b64_len} chars, so "
                            f"the photo is recoverable: upload it and set file_url"
                        )
                    else:
                        counters["NO_FILE_REF"] = counters.get("NO_FILE_REF", 0) + 1
                        status_no_key, note_no_key = "NO_FILE_REF", (
                            "no file, no file_url and no base64_str - get_public_url() "
                            "returns an empty string and nothing exists to restore"
                        )
                    rows.append(self.row(
                        story, "", "", "", media, pdf_row, status_no_key, note_no_key,
                    ))
                    continue
                if images_only and not is_image_key(claimed):
                    continue
                if not claimed.startswith(prefix):
                    continue  # belongs to another prefix (e.g. server-side PDF upload)

                if claimed in all_keys:
                    # The object exists, just not under this story's own id.
                    # Real case: rows migrated between environments keep the
                    # source env's story id in the stored path.
                    foreign = claimed[len(prefix):].split("/", 1)[0]
                    counters["OK_FOREIGN_PATH"] = counters.get("OK_FOREIGN_PATH", 0) + 1
                    if opts["all_rows"]:
                        rows.append(self.row(
                            story, claimed, "", "", media, pdf_row, "OK_FOREIGN_PATH",
                            f"object exists, but filed under id {foreign} rather than "
                            f"this row's story_id {story.id} - typically a row migrated "
                            f"from another environment; renders fine, do not backfill",
                        ))
                    continue

                counters["MISSING_IN_S3"] += 1
                rows.append(self.row(
                    story, claimed, "", "", media, pdf_row, "MISSING_IN_S3",
                    "StoryMedia row points at a key that is absent from the bucket - "
                    "row created but the PUT never landed"
                    + self.low_confidence_note(),
                ))

            # Report rendered before the image was recorded.
            if pdf_row is not None:
                for media in media_rows:
                    if media.media_type == MediaTypeChoices.PDF:
                        continue
                    if not media.include_in_story:
                        continue
                    if media.created_at and pdf_row.updated_at and media.created_at > pdf_row.updated_at:
                        counters["STALE_REPORT"] += 1
                        rows.append(self.row(
                            story,
                            to_object_key(media.file_url or (media.file.name if media.file else None),
                                          bucket, media_base) or "",
                            "", "", media, pdf_row, "STALE_REPORT",
                            "image row is newer than the stored PDF - the report was "
                            "rendered before this photo was recorded; needs regeneration",
                        ))

        if opts["scan_unattributed"]:
            rows.extend(self.scan_unattributed(
                object_index, matched_segments, media_base, counters,
                images_only=images_only,
            ))

        self.write_out(rows, opts.get("out"))
        self.summarise(counters)

    # -------------------------------------------------------- unattributed

    def scan_unattributed(self, object_index, matched_segments, media_base, counters,
                          images_only=False):
        """
        Identifiers in the bucket that no audited story claimed.

        Rather than guessing from the key shape, this works by subtraction: every
        segment the story pass matched is removed, and what remains is checked
        against the whole Story table. A leftover that does belong to a story
        outside the current filter is reported as OUT_OF_SCOPE, not as a finding -
        conflating the two would manufacture orphans out of a narrow --flow.

        Whatever matches no story at all is genuinely unplaceable by key, so
        CompanyChat.file_url is tried as the last bridge back to a session.
        """
        leftovers = {}
        for seg, objs in object_index.items():
            if seg in matched_segments:
                continue
            # Apply the same image filter the story pass used, so the CSV does
            # not silently mix PDFs into these buckets while excluding them
            # everywhere else.
            if images_only:
                objs = [o for o in objs if is_image_key(o[0])]
                if not objs:
                    continue
            leftovers[seg] = objs
        if not leftovers:
            return []

        self.stdout.write(
            f"\nResolving {len(leftovers)} identifiers in the bucket that no "
            f"audited story claimed ..."
        )

        # One query instead of one per segment.
        segments = list(leftovers)
        numeric = [int(s) for s in segments if s.isdigit()]
        known = Story.objects.filter(Q(session__in=segments) | Q(id__in=numeric))
        known_by_session = {s.session: s for s in known if s.session}
        known_by_id = {str(s.id): s for s in known}

        rows = []
        candidates = []
        for segment, objects in leftovers.items():
            owner = known_by_session.get(segment) or known_by_id.get(segment)
            if owner is not None:
                counters["OUT_OF_SCOPE"] = counters.get("OUT_OF_SCOPE", 0) + len(objects)
                continue

            # A key naming a story id that simply is not in the database is a
            # different animal from the orphan bug: nothing was lost at upload
            # time, the Story row itself is absent - deleted, purged, or never
            # included in this dump. Counting these as orphans would inflate the
            # finding enormously, so they get their own status.
            if segment.isdigit():
                counters["STORY_NOT_IN_DB"] = counters.get("STORY_NOT_IN_DB", 0) + len(objects)
                for key, size, last_modified in objects:
                    rows.append({
                        **{col: "" for col in CSV_COLUMNS},
                        "status": "STORY_NOT_IN_DB",
                        "story_id": segment,
                        "s3_key": key,
                        "s3_size_bytes": size,
                        "s3_last_modified": last_modified.isoformat() if last_modified else "",
                        "notes": (
                            "object filed under a Story.id that does not exist in this "
                            "database - deleted story or incomplete dump, NOT the "
                            "upload-orphan bug"
                        ),
                    })
                continue

            candidates.extend(objects)

        for key, size, last_modified in candidates:
            basename = key.rsplit("/", 1)[-1]
            if StoryMedia.objects.filter(
                Q(file_url__endswith=key) | Q(file__endswith=basename)
            ).exists():
                continue

            chat = (
                CompanyChat.objects
                .filter(file_url__icontains=basename)
                .order_by("created_at")
                .first()
            )
            session = chat.session if chat else ""
            story = Story.objects.filter(session=session).first() if session else None

            counters["UNATTRIBUTED_IN_S3"] += 1
            rows.append({
                **{col: "" for col in CSV_COLUMNS},
                "status": "UNATTRIBUTED_IN_S3",
                "s3_key": key,
                "s3_size_bytes": size,
                "s3_last_modified": last_modified.isoformat() if last_modified else "",
                "session": session,
                "story_id": story.id if story else "",
                "story_flow": (story.other_params or {}).get("flow", "") if story else "",
                "state": story.state if story else "",
                "district": story.district if story else "",
                "public_url": f"{(media_base or '').rstrip('/')}/{key}" if media_base else "",
                "notes": (
                    "identifier in the key matches no Story; attributed via "
                    "CompanyChat.file_url"
                    if chat else
                    "identifier in the key matches no Story and no CompanyChat row - "
                    "not attributable from stored data, needs the S3 access log"
                ),
            })

        # Fill session_type for the whole set in one query rather than inside the
        # loop above, so the column is free relative to the work already done.
        self.prime_session_types([r.get("session") for r in rows])
        for r in rows:
            r["session_type"] = self.session_type_for(r.get("session"))

        return rows

    # ------------------------------------------------------------------ output

    def row(self, story, key, size, last_modified, media, pdf_row, status, notes):
        params = story.other_params or {}
        return {
            "status": status,
            "story_id": story.id,
            "session": story.session,
            "session_type": self.session_type_for(story.session),
            "story_flow": params.get("flow", ""),
            "report_type": story.report_type or "",
            "state": story.state or "",
            "district": story.district or "",
            "block": story.block or "",
            "story_created_at": story.created_at.isoformat() if story.created_at else "",
            "s3_key": key,
            "s3_size_bytes": size,
            "s3_last_modified": last_modified.isoformat() if hasattr(last_modified, "isoformat") else last_modified,
            "story_media_id": media.id if media else "",
            "story_media_name": media.name if media else "",
            "media_type": media.media_type if media else "",
            "include_in_story": media.include_in_story if media else "",
            "media_created_at": media.created_at.isoformat() if media and media.created_at else "",
            "pdf_updated_at": pdf_row.updated_at.isoformat() if pdf_row and pdf_row.updated_at else "",
            "public_url": media.get_public_url() if media else "",
            "notes": notes,
        }

    def write_out(self, rows, out_path):
        if not out_path:
            return
        with open(out_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        self.stdout.write(f"\nWrote {len(rows)} rows to {out_path}")

    def summarise(self, counters):
        """
        Grouped by what the reader has to DO, not by status name.

        A flat list of twelve statuses makes every line look equally urgent, so
        the two that need work sit at the same weight as the nine that are
        context. These groups put the actionable rows first and label the rest
        explicitly as background, so a run can be read in one pass.
        """
        groups = [
            ("NEEDS ACTION", self.style.ERROR, [
                ("ORPHAN_IN_S3", "photo in S3, no row -> create row, regenerate"),
                ("BASE64_ONLY", "photo in base64_str -> upload, set url, regenerate"),
                ("STALE_REPORT", "report older than the photo -> regenerate"),
                ("HOST_MISMATCH", "row points at another host -> fix stored URL"),
            ]),
            ("CANNOT BE RECOVERED", self.style.WARNING, [
                ("NO_FILE_REF", "no key and no base64 - the photo is gone"),
                ("MISSING_IN_S3", "row names a key that is not in the bucket"),
            ]),
            ("HEALTHY", None, [
                ("OK", ""),
                ("OK_BASENAME", "matched on filename, stored path differs"),
                ("OK_FOREIGN_PATH", "migrated row - do not backfill"),
                ("EXCLUDED", "include_in_story=False - check if deliberate"),
            ]),
            ("NOT FINDINGS", None, [
                ("PREDATES_STORY", "object older than the story - reused id"),
                ("STORY_NOT_IN_DB", "story absent from this database"),
                ("OUT_OF_SCOPE", "story outside the current filters"),
                ("UNATTRIBUTED_IN_S3", "key matches no story at all"),
            ]),
        ]

        self.rule("results")
        self.field("stories examined", counters["stories"])

        for title, style, statuses in groups:
            present = [(s, note) for s, note in statuses if counters.get(s, 0)]
            if not present:
                continue
            self.stdout.write("")
            self.stdout.write(style(f"  {title}") if style else f"  {title}")
            for status, note in present:
                self.field(f"  {status}", counters.get(status, 0), note)

        orphans = counters.get("ORPHAN_IN_S3", 0)
        stale = counters.get("STALE_REPORT", 0)
        base64_only = counters.get("BASE64_ONLY", 0)

        self.rule("what to do next")
        if not (orphans or stale or base64_only):
            self.note("Nothing to remediate in this scope.")
            return
        if orphans:
            self.note(f"{orphans} photo(s) are in S3 with no StoryMedia row. Create the row "
                      f"from the\nexisting key, then regenerate the report. Do NOT re-upload - "
                      f"the bytes\nare already in the bucket.")
        if base64_only:
            self.note(f"{base64_only} photo(s) survive only as base64_str. Upload the bytes, set "
                      f"file_url,\nthen regenerate.")
        if stale:
            self.note(f"{stale} report(s) were rendered before their photo was recorded. "
                      f"Regeneration\nalone is enough.")
        if self.low_resolve:
            self.stdout.write("")
            self.note("These counts ran below --min-resolve. Every row is stamped "
                      "UNTRUSTWORTHY\nin the CSV - confirm the environment pairing before "
                      "acting on them.", self.style.WARNING)
