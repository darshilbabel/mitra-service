"""
Re-transliterate stored chat entries for one or more state-machine stages and
regenerate the affected reports.

Background
----------
Some state machines (e.g. INTRODUCTION, ORGANIZATION) were earlier configured
with text_conversion_type = TRANSLATE. Their user messages were therefore stored
translated (wrong) in ``CompanyChat.translated_message``. The config has since
been fixed to TRANSLITERATE, but the already-stored chats — and every report
built from them — still contain the translated text.

This command fixes historic data in two steps:

  1. Re-transliterate the stored user chats for the given stage(s) so
     ``CompanyChat.translated_message`` holds the transliterated (not translated)
     text — exactly like chatbot/scripts/retransliterate_failed_chats.py does.
  2. Regenerate the report for every affected session by calling the same
     ``create_story_object`` that the ``/api/end-story/`` endpoint uses, so the
     report is rebuilt from the corrected chats. Nothing about how the report is
     generated changes.

Usage
-----
    # Chaupal bot, revert INTRODUCTION + ORGANIZATION for a time window
    python manage.py regenerate_transliterated_reports \
        --timestamp-from "2026-06-01 00:00" --timestamp-to "2026-06-30 23:59" \
        --session-type shikshalokam_chaupal \
        --statemachine INTRODUCTION,ORGANIZATION \
        --route /shikshalokam_chaupal

    # Preview only — count what would change, no writes / no API calls
    python manage.py regenerate_transliterated_reports \
        --session-type shikshalokam_chaupal --statemachine INTRODUCTION --dry-run

    # Several bots/flows in ONE run — comma-separated routes, flow auto-resolved
    # per session (do NOT pass --flow when mixing flows):
    python manage.py regenerate_transliterated_reports \
        --route "/shikshalokam_chaupal,/guided_guest" \
        --statemachine INTRODUCTION,ORGANIZATION \
        --timestamp-from "2026-07-01" --timestamp-to "2026-07-31 23:59"

    # Patch mode — fix only name/org, keep narrative, no LLM (recommended):
    python manage.py regenerate_transliterated_reports \
        --route /shikshalokam_chaupal --statemachine INTRODUCTION,ORGANIZATION \
        --timestamp-from "2026-07-01" --timestamp-to "2026-07-31 23:59" \
        --patch-fields

    # Re-run only the report regeneration (chats already fixed)
    python manage.py regenerate_transliterated_reports \
        --session-type shikshalokam_chaupal --statemachine INTRODUCTION \
        --skip-transliterate

Scoping is by --route + --statemachine + --timestamp + --session-type (the
primary mechanism). --session is an optional convenience to target exact ids.

Arguments
---------
    --timestamp-from / --timestamp-to
        Filter CompanyChat rows by created_at. Accept 'YYYY-MM-DD' (whole day) or
        'YYYY-MM-DD HH:MM[:SS]' (exact time). Both optional. Interpreted in the
        server timezone.
    --session-type
        Optional ChatSession.session_type filter. Comma-separated for several
        types (e.g. 'shikshalokam_chaupal,guest-mi-story').
    --statemachine
        One or more CompanyStateMachine names (== CompanyChat.stage), comma
        separated (e.g. 'INTRODUCTION,ORGANIZATION'). Required.
    --route
        One or more CompanyBot route(s), comma-separated, processed in one run
        (e.g. '/shikshalokam_chaupal,/guided_guest'). Each route is scoped and
        run independently. Default '/shikshalokam_chaupal'.
    --flow
        Optional SessionFlowName override applied to ALL routes. Leave unset to
        auto-resolve each session's own flow from Story.other_params['flow']
        (required when processing multiple routes/flows at once).
    --patch-fields
        Fix only the personal fields in --field-map (name/org/...) in the stored
        story + translations and rebuild the PDF; no LLM, narrative untouched.
    --field-map
        Patch mode only. STAGE=field pairs, e.g.
        'INTRODUCTION=user_name,ORGANIZATION=organization'.
<<<<<<< HEAD
=======
    --language
        Override the language passed to report regeneration. If omitted, taken
        from the existing Story.language, then the ChatSession.language.
>>>>>>> upstream/release-2.2.0
    --bot-profile-id
        Profile id used as the bot sender (its messages are excluded from
        re-transliteration). Default 1.
    --skip-transliterate   Skip step 1 (only regenerate reports).
    --skip-report          Skip step 2 (only re-transliterate chats).
    --limit                Process at most N sessions (0 = no limit).
    --dry-run              Report counts only; no DB writes, no report calls.
<<<<<<< HEAD

Report language
---------------
The report is always regenerated in the session's own language -- the same value
``/api/end-story/`` passes, so there is no --language override. ``Story.other_params``
is nevertheless stored in English: ``save_story()`` transliterates the LLM output
back to English using a Transliterate ``Voice`` on the *story* bot. If that Voice is
missing, ``transliterate_to_english_if_needed()`` silently keeps the original script,
``other_params`` ends up in Devanagari, and dashboards reading
``other_params->>'location'`` stop matching the story. This command therefore refuses
to regenerate such sessions instead of corrupting them.
=======
>>>>>>> upstream/release-2.2.0
"""

from collections import namedtuple
from datetime import datetime, time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from chatbot.models import (
    CompanyBot,
    CompanyChat,
    ChatSession,
    ChatType,
    SessionFlowName,
    Voice,
    VoiceType,
)
<<<<<<< HEAD
=======
from chatbot.models.company_models import CompanyStateMachine
>>>>>>> upstream/release-2.2.0
from chatbot.utils.transliterate_utils import (
    transliterate_text,
    get_transliteration_output,
)
<<<<<<< HEAD
from chatbot.utils.story_utils.story_utils import create_story_object, get_story_company_bot
=======
from chatbot.utils.story_utils.story_utils import create_story_object
>>>>>>> upstream/release-2.2.0


# Session-type -> report flow fallback. shikshaChaupal reports are generated
# with the GuestDiscussion flow (see chatbot/utils/story_utils/story_utils.py).
SESSION_TYPE_TO_FLOW = {
    ChatType.shikshaChaupal.value: SessionFlowName.GuestDiscussion.value,
}

DateArg = namedtuple("DateArg", ["value", "has_time"])


def parse_date_arg(raw):
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]' and remember which."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return DateArg(datetime.strptime(raw, fmt), True)
        except ValueError:
            continue
    try:
        return DateArg(datetime.strptime(raw, "%Y-%m-%d"), False)
    except ValueError as e:
        raise CommandError(f"Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]', got '{raw}'") from e


def _make_aware(dt):
    if getattr(settings, "USE_TZ", False) and timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _created_at_range(date_from, date_to):
    """created_at filter honouring time-of-day; date-only bounds cover the full day."""
    f = {}
    if date_from is not None:
        start = date_from.value if date_from.has_time else datetime.combine(date_from.value.date(), time.min)
        f["created_at__gte"] = _make_aware(start)
    if date_to is not None:
        end = date_to.value if date_to.has_time else datetime.combine(date_to.value.date(), time.max)
        f["created_at__lte"] = _make_aware(end)
    return f


class Command(BaseCommand):
    help = (
        "Re-transliterate stored chats for given state-machine stages and "
        "regenerate the affected reports via create_story_object (/end-story)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--timestamp-from", type=str, default=None)
        parser.add_argument("--timestamp-to", type=str, default=None)
        parser.add_argument(
            "--session-type",
            type=str,
            default=None,
            help="Optional ChatSession.session_type filter. Comma-separated for "
                 "several types, e.g. 'shikshalokam_chaupal,guest-mi-story'.",
        )
        parser.add_argument(
            "--session",
            type=str,
            default=None,
            help="Target one or more exact session id(s), comma separated. "
                 "When set, scoping is by these sessions (timestamp / session-type "
                 "act only as extra optional filters).",
        )
        parser.add_argument(
            "--statemachine",
            type=str,
            required=True,
            help="Comma-separated CompanyStateMachine name(s), e.g. 'INTRODUCTION,ORGANIZATION'.",
        )
        parser.add_argument(
            "--route",
            type=str,
            default="/shikshalokam_chaupal",
            help="One or more CompanyBot route(s), comma-separated, to process in a "
                 "single run, e.g. '/shikshalokam_chaupal,/guided_guest'. Each route "
                 "is scoped and processed independently; the flow is auto-resolved "
                 "per session unless --flow is given.",
        )
        parser.add_argument(
            "--flow",
            type=str,
            default=None,
            help="Optional SessionFlowName override for report regeneration applied to "
                 "ALL routes. Leave unset to auto-resolve each session's own flow "
                 "(required when processing multiple routes/flows at once).",
        )
<<<<<<< HEAD
=======
        parser.add_argument("--language", type=str, default=None)
>>>>>>> upstream/release-2.2.0
        parser.add_argument("--bot-profile-id", type=int, default=1)
        parser.add_argument(
            "--patch-fields",
            action="store_true",
            help="Patch mode: do NOT call the LLM / regenerate the report. Only fix "
                 "the transliterated personal fields (per --field-map) in the stored "
                 "story + its translations, then rebuild the PDF. Title, challenges, "
                 "solutions and all narrative text stay exactly as-is.",
        )
        parser.add_argument(
            "--field-map",
            type=str,
            default="INTRODUCTION=user_name,ORGANIZATION=organization",
            help="Patch mode only. Comma-separated STAGE=field pairs mapping a state "
                 "machine stage to the story other_params field it fills. The value is "
                 "taken from the first user chat at that stage (already transliterated "
                 "by Step 1). Default: 'INTRODUCTION=user_name,ORGANIZATION=organization'.",
        )
        parser.add_argument("--skip-transliterate", action="store_true")
        parser.add_argument("--skip-report", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        stages = [s.strip() for s in (opts["statemachine"] or "").split(",") if s.strip()]
        if not stages:
            raise CommandError("--statemachine must list at least one stage name.")

        date_from = parse_date_arg(opts["timestamp_from"]) if opts["timestamp_from"] else None
        date_to = parse_date_arg(opts["timestamp_to"]) if opts["timestamp_to"] else None

        session_types = [s.strip() for s in (opts["session_type"] or "").split(",") if s.strip()]
        session_ids = [s.strip() for s in (opts["session"] or "").split(",") if s.strip()]
        routes = [r.strip() for r in (opts["route"] or "").split(",") if r.strip()]
        if not routes:
            raise CommandError("--route must list at least one bot route.")
        flow_override = opts["flow"]
<<<<<<< HEAD
        bot_profile_id = opts["bot_profile_id"]
        self.bot_profile_id = bot_profile_id
        self._story_voice_cache = {}
=======
        language_override = opts["language"]
        bot_profile_id = opts["bot_profile_id"]
        self.bot_profile_id = bot_profile_id
>>>>>>> upstream/release-2.2.0
        patch_fields = opts["patch_fields"]
        field_map = self._parse_field_map(opts["field_map"]) if patch_fields else {}
        skip_transliterate = opts["skip_transliterate"]
        skip_report = opts["skip_report"]
        limit = opts["limit"]
        dry_run = opts["dry_run"]

        if patch_fields:
            unmapped = [s for s in field_map if s not in stages]
            if unmapped:
                self.stdout.write(self.style.WARNING(
                    f"--field-map stages {unmapped} are not in --statemachine {stages}; "
                    f"those chats won't be re-transliterated in Step 1."
                ))

        # --- process each route independently, then aggregate ---------------
        g_affected = g_r_success = g_r_failed = 0
        for route in routes:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n### route {route}"))
            affected, r_success, r_failed = self._process_route(
                route=route, stages=stages, date_from=date_from, date_to=date_to,
                session_types=session_types, session_ids=session_ids,
<<<<<<< HEAD
                flow_override=flow_override,
=======
                flow_override=flow_override, language_override=language_override,
>>>>>>> upstream/release-2.2.0
                patch_fields=patch_fields, field_map=field_map,
                skip_transliterate=skip_transliterate, skip_report=skip_report,
                limit=limit, dry_run=dry_run,
            )
            g_affected += affected
            g_r_success += r_success
            g_r_failed += r_failed

        self._summary(g_affected, g_r_success, g_r_failed, dry_run)

    # ------------------------------------------------------------------ #
    def _process_route(self, route, stages, date_from, date_to, session_types,
<<<<<<< HEAD
                       session_ids, flow_override, patch_fields,
=======
                       session_ids, flow_override, language_override, patch_fields,
>>>>>>> upstream/release-2.2.0
                       field_map, skip_transliterate, skip_report, limit, dry_run):
        """Scope + Step 1 + Step 2 for a single bot route. Returns
        (affected_sessions, reports_success, reports_failed)."""
        company_bot = CompanyBot.objects.filter(route=route).first()
        if not company_bot:
            self.stdout.write(self.style.ERROR(f"CompanyBot route='{route}' not found, skipping."))
            return 0, 0, 0

        # --- scope sessions --------------------------------------------------
        if session_ids:
            # Explicit session targeting — do not restrict by bot; the route bot
            # is still used only to locate the transliteration Voice provider.
            sessions_qs = ChatSession.objects.filter(session__in=session_ids).exclude(language="en")
        else:
            sessions_qs = ChatSession.objects.filter(company_bot=company_bot).exclude(language="en")
        if session_types:
            sessions_qs = sessions_qs.filter(session_type__in=session_types)
        session_language = {s.session: s.language for s in sessions_qs}
        if not session_language:
            self.stdout.write(self.style.WARNING("  No matching non-English sessions for this route."))
            return 0, 0, 0

        if limit and limit > 0:
            limited_ids = sorted(session_language.keys())[:limit]
            session_language = {sid: session_language[sid] for sid in limited_ids}

        # --- select candidate chats -----------------------------------------
        chat_filter = {
            "session__in": list(session_language.keys()),
            "stage__in": stages,
            "translated_message__isnull": False,
        }
        chat_filter.update(_created_at_range(date_from, date_to))
        chats = (
            CompanyChat.objects.filter(**chat_filter)
            .exclude(sender_id=self.bot_profile_id)   # user messages only; skip bot (Profile id=1)
            .order_by("created_at")
        )

        self.stdout.write(
            f"  Scope: session_type={session_types or 'ANY'}, "
            f"session={session_ids or 'ANY'}, stages={stages}, "
            f"chats matched={chats.count()}"
        )

        # --- cache transliterate voice providers per language ----------------
        voice_cache = {}

        def get_voice(lang):
            if lang not in voice_cache:
                voice_cache[lang] = Voice.objects.filter(
                    company_bot=company_bot, type=VoiceType.Transliterate, language=lang
                ).first()
            return voice_cache[lang]

        # =========================== STEP 1 ================================== #
        affected_sessions = set()
        t_success = t_failed = t_skipped = 0

        if skip_transliterate:
            affected_sessions = {c.session for c in chats.only("session")}
            self.stdout.write("  Step 1 skipped (--skip-transliterate).")
        else:
            for chat in chats.iterator():
                lang = session_language.get(chat.session)
                if not lang:
                    t_skipped += 1
                    continue
                voice_provider = get_voice(lang)
                if not voice_provider:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  No Transliterate Voice for language='{lang}', "
                            f"skipping chat {chat.id}"
                        )
                    )
                    t_failed += 1
                    continue

                if dry_run:
                    affected_sessions.add(chat.session)
                    t_success += 1
                    continue

                response = transliterate_text(
                    source_language=lang,
                    target_language="en",
                    message_body=chat.message,
                    is_sentence=True,
                    voice_provider=voice_provider,
                )
                output = get_transliteration_output(response)
                if output:
                    chat.translated_message = output
                    chat.save(update_fields=["translated_message"])
                    affected_sessions.add(chat.session)
                    t_success += 1
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  Transliteration failed for chat {chat.id}: {response}"
                        )
                    )
                    t_failed += 1

            self.stdout.write(
                f"  Step 1 (re-transliterate): success={t_success}, "
                f"failed={t_failed}, skipped={t_skipped}"
            )

        # =========================== STEP 2 ================================== #
        if skip_report:
            self.stdout.write("  Step 2 skipped (--skip-report).")
            return len(affected_sessions), 0, 0

        ordered_sessions = sorted(affected_sessions)
        if limit and limit > 0:
            ordered_sessions = ordered_sessions[:limit]

        r_success = r_failed = 0
        for session in ordered_sessions:
            chat_session = ChatSession.objects.filter(session=session).first()
            if not chat_session:
                self.stdout.write(self.style.WARNING(f"  ChatSession '{session}' missing, skip."))
                r_failed += 1
                continue

            profile_id = chat_session.profile_id
            flow = flow_override or self._resolve_flow(session, chat_session)
<<<<<<< HEAD
            language = self._resolve_language(chat_session)
=======
            language = language_override or self._resolve_language(session, chat_session)
>>>>>>> upstream/release-2.2.0

            # ---- Patch mode: fix only personal fields, no LLM --------------
            if patch_fields:
                ok, msg = self._patch_session(
                    session, chat_session, flow, field_map, get_voice, dry_run
                )
                (self.stdout.write(self.style.SUCCESS(f"  {msg}")) if ok
                 else self.stdout.write(self.style.WARNING(f"  {msg}")))
                if ok:
                    r_success += 1
                else:
                    r_failed += 1
                continue

            # ---- Full regeneration (default) -------------------------------
<<<<<<< HEAD
            if not self._story_bot_can_transliterate(flow, language):
                self.stdout.write(self.style.ERROR(
                    f"  Skipping session={session}: no Transliterate Voice for "
                    f"language='{language}' on the story bot of flow='{flow}'. "
                    f"Regenerating would store '{language}' text in the English "
                    f"Story.other_params and drop the story from the dashboard."
                ))
                r_failed += 1
                continue

=======
>>>>>>> upstream/release-2.2.0
            if dry_run:
                self.stdout.write(
                    f"  [dry-run] would regenerate session={session} "
                    f"flow={flow} language={language}"
                )
                r_success += 1
                continue

            try:
                story_id, _content, error_msg, error_type = create_story_object(
                    profile_id=profile_id,
                    session=session,
                    access_token=None,
                    flow=flow,
                    language=language,
                )
                if error_msg:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  Report failed session={session}: {error_msg} ({error_type})"
                        )
                    )
                    r_failed += 1
                else:
                    self.stdout.write(
                        self.style.SUCCESS(f"  Report regenerated session={session} story_id={story_id}")
                    )
                    r_success += 1
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"  Exception session={session}: {exc}"))
                r_failed += 1

        return len(affected_sessions), r_success, r_failed

    # ------------------------------------------------------------------ #
    def _parse_field_map(self, raw):
        """'INTRODUCTION=user_name,ORGANIZATION=organization' -> dict."""
        mapping = {}
        for pair in (raw or "").split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise CommandError(f"--field-map entry '{pair}' must be STAGE=field.")
            stage, field = pair.split("=", 1)
            mapping[stage.strip()] = field.strip()
        if not mapping:
            raise CommandError("--field-map produced no STAGE=field pairs.")
        return mapping

    def _patch_session(self, session, chat_session, flow, field_map, get_voice, dry_run):
        """
        Fix only the mapped personal fields in the stored story + its
        translations, then rebuild the PDF. No LLM, no change to title /
        challenges / solutions / any narrative field.
        """
        from chatbot.models import Story

        story = Story.objects.filter(session=session).first()
        if not story:
            return False, f"patch skipped session={session}: no Story"

        # Collect corrected values from the first user chat at each mapped stage.
        # native = original message (in the session language, e.g. Devanagari)
        # roman  = transliterated English (translated_message, fixed by Step 1)
        updates = {}
        for stage, field in field_map.items():
            c = (CompanyChat.objects
                 .filter(session=session, stage=stage)
                 .exclude(sender_id=self.bot_profile_id)
                 .order_by("created_at")
                 .first())
            if c and c.translated_message and c.translated_message.strip():
                updates[field] = {
                    "native": (c.message or "").strip(),
                    "roman": c.translated_message.strip(),
                }

        if not updates:
            return False, f"patch skipped session={session}: no source chats for {list(field_map)}"

        if dry_run:
            preview = {f: v["roman"] for f, v in updates.items()}
            return True, f"[dry-run] would patch session={session} fields={preview} (no narrative change)"

        session_lang = chat_session.language

        # 1) English story other_params — store the romanized value (title-cased).
        op = dict(story.other_params or {})
        for field, v in updates.items():
            op[field] = v["roman"].title()
        story.other_params = op
        story.save(update_fields=["other_params"])

        # 2) Each non-English translation. For the session's own language use the
        #    original native message (exact); for others transliterate the English.
        for t in story.translations.all():
            lang = t.language
            if not lang or lang == "en":
                continue
            vp = get_voice(lang)
            top = dict(t.other_params or {})
            for field, v in updates.items():
                if lang == session_lang and v["native"]:
                    top[field] = v["native"]          # exact original script
                elif vp:
                    resp = transliterate_text(
                        source_language="en", target_language=lang,
                        message_body=v["roman"], is_sentence=(" " in v["roman"]),
                        voice_provider=vp,
                    )
                    out = get_transliteration_output(resp)
                    top[field] = out or v["roman"]
                else:
                    top[field] = v["roman"]
            t.other_params = top
            t.save(update_fields=["other_params"])

        # 3) Rebuild the PDF from the stored story — no LLM (reuses app helper).
        try:
            from chatbot.utils.shikshalokam_story_utils import update_story_pdf
            update_story_pdf(access_token=None, session=session, flow=flow)
        except Exception as exc:  # noqa: BLE001
            return False, f"patched fields but PDF rebuild failed session={session}: {exc}"

        return True, f"patched session={session} fields={list(updates)} (PDF rebuilt, narrative untouched)"

    # ------------------------------------------------------------------ #
    def _resolve_flow(self, session, chat_session):
        """Reuse the flow the report was originally generated with."""
        try:
            from chatbot.models import Story  # local import to avoid load-order issues

            story = Story.objects.filter(session=session).first()
            if story and isinstance(story.other_params, dict):
                flow = story.other_params.get("flow")
                if flow:
                    return flow
        except Exception:
            pass
        st = chat_session.session_type
        return SESSION_TYPE_TO_FLOW.get(st, st)

<<<<<<< HEAD
    def _resolve_language(self, chat_session):
        """The report is regenerated in the conversation language -- the same value
        /api/end-story/ passes. Story.language is always 'en' (save_story hard-codes
        it), so it is not a useful fallback and is not consulted."""
        return (chat_session.language if chat_session else None) or "en"

    def _story_bot_can_transliterate(self, flow, language):
        """True when the story bot for `flow` has a Transliterate Voice for `language`.

        save_story() builds the *English* Story.other_params by transliterating the
        LLM output; with no Voice it silently returns the original script. The story
        bot must be resolved via get_story_company_bot() -- it is a different bot from
        the conversation route's bot used elsewhere in this command.
        """
        if language == "en":
            return True
        key = (str(flow), language)
        if key not in self._story_voice_cache:
            try:
                story_bot, _validate_bot = get_story_company_bot(profile=None, flow=flow)
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.WARNING(
                    f"  Could not resolve story bot for flow='{flow}': {exc}"
                ))
                self._story_voice_cache[key] = False
            else:
                self._story_voice_cache[key] = Voice.objects.filter(
                    company_bot=story_bot, type=VoiceType.Transliterate, language=language
                ).exists()
        return self._story_voice_cache[key]

=======
    def _resolve_language(self, session, chat_session):
        # The report/PDF is rendered in the conversation language (its
        # StoryTranslation), while the main Story is often stored in English.
        # So prefer the ChatSession language when it is non-English.
        if chat_session and chat_session.language and chat_session.language != "en":
            return chat_session.language
        try:
            from chatbot.models import Story

            story = Story.objects.filter(session=session).first()
            if story and story.language:
                return story.language
        except Exception:
            pass
        return (chat_session.language if chat_session else None) or "en"

>>>>>>> upstream/release-2.2.0
    def _summary(self, affected, r_success, r_failed, dry_run):
        self.stdout.write("\n" + "=" * 50)
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}Affected sessions: {affected}"))
        self.stdout.write(self.style.SUCCESS(f"{prefix}Reports regenerated: {r_success}"))
        if r_failed:
            self.stdout.write(self.style.ERROR(f"{prefix}Reports failed: {r_failed}"))
        self.stdout.write("=" * 50)
