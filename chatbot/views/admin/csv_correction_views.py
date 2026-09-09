import csv
import io
import json
from collections import Counter

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView

from chatbot.constants import api_responses

CSV_REQUIRED_COLS = {"id", "State", "District"}

ERROR_REASON_COL = "error_reason"

MAPPING_REQUIRED_FIELDS = {"state", "district"}

STATE_DISTRICT_BOT_ROUTE = "/state-classification-guest-discussion"


def _load_state_district_mapping():
    """
    Fetch and index the state/district mapping from the CompanyBot whose route is
    STATE_DISTRICT_BOT_ROUTE. Raises ValueError with a user-facing message on any
    failure, so the caller can hard-fail the whole upload before touching any row.
    """
    from chatbot.models.company_models import CompanyBot

    try:
        bot = CompanyBot.objects.get(route=STATE_DISTRICT_BOT_ROUTE)
    except CompanyBot.DoesNotExist:
        raise ValueError(api_responses.CSV_STATE_DISTRICT_BOT_NOT_FOUND)

    try:
        raw = bot.dynamic_context
        data = raw if isinstance(raw, dict) else json.loads(raw)
        states = data["states"]
    except (TypeError, ValueError, KeyError):
        raise ValueError(api_responses.CSV_STATE_DISTRICT_MAPPING_INVALID)

    state_names = set()
    state_districts = {}
    district_index = {}
    for state in states:
        state_name = state["name"].strip()
        state_names.add(state_name)
        district_names = {d["name"].strip() for d in state.get("districts", [])}
        state_districts[state_name] = district_names
        for district_name in district_names:
            district_index.setdefault(district_name, []).append(state_name)

    return {
        "state_names": state_names,
        "state_districts": state_districts,
        "district_index": district_index,
    }


def _extract_fields(row: dict) -> dict:
    return {
        "id":       (row.get("id") or "").strip(),
        "state":    (row.get("State") or "").strip(),
        "district": (row.get("District") or "").strip(),
    }


def _validate_row(fields: dict, mapping: dict) -> list:
    errors = []

    state_val = fields["state"]
    district_val = fields["district"]

    if not state_val and not district_val:
        return errors

    if state_val and state_val not in mapping["state_names"]:
        errors.append(api_responses.CSV_ROW_UNKNOWN_STATE_TEMPLATE.format(state=state_val))
        return errors

    if not district_val:
        return errors

    if state_val:
        if district_val not in mapping["state_districts"].get(state_val, set()):
            actual_states = mapping["district_index"].get(district_val)
            if actual_states:
                errors.append(
                    api_responses.CSV_ROW_DISTRICT_WRONG_STATE_TEMPLATE.format(
                        district=district_val,
                        actual_states=", ".join(sorted(actual_states)),
                        state=state_val,
                    )
                )
            else:
                errors.append(
                    api_responses.CSV_ROW_UNKNOWN_DISTRICT_TEMPLATE.format(district=district_val)
                )
    else:
        candidate_states = mapping["district_index"].get(district_val)
        if not candidate_states:
            errors.append(
                api_responses.CSV_ROW_UNKNOWN_DISTRICT_TEMPLATE.format(district=district_val)
            )
        elif len(candidate_states) > 1:
            errors.append(
                api_responses.CSV_ROW_AMBIGUOUS_DISTRICT_TEMPLATE.format(
                    district=district_val,
                    candidate_states=", ".join(sorted(candidate_states)),
                )
            )
        else:
            fields["state"] = candidate_states[0]

    return errors


def _apply_to_story(story, fields: dict) -> bool:
    """Apply only the values that differ. Return True if anything changed."""
    changed = False

    if fields["state"] and story.state != fields["state"]:
        story.state = fields["state"]
        changed = True
    if fields["district"] and story.district != fields["district"]:
        story.district = fields["district"]
        changed = True

    if changed:
        _update_mapping_stage(story)

    return changed


def _update_mapping_stage(story):
    from chatbot.models.enums import StoryStatusChoices

    fully_mapped = all(getattr(story, f, None) for f in MAPPING_REQUIRED_FIELDS)
    story.stage = StoryStatusChoices.COMPLETED if fully_mapped else StoryStatusChoices.PENDING


def _neutralise_formula(value):
    """
    Stop a spreadsheet from evaluating uploaded text as a formula.

    The rejection file echoes back cells the uploader supplied, so a value such as
    =cmd|'/c calc'!A1 would execute when the file is opened in Excel or Sheets. Prefixing
    with an apostrophe makes the cell literal text; the apostrophe is not displayed.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _build_rejection_csv(rejected_rows: list, original_headers: list) -> str:
    headers = [h for h in original_headers if h != ERROR_REASON_COL]
    headers.append(ERROR_REASON_COL)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    # Headers come from the uploaded file too, so they need the same treatment. The
    # fieldnames stay unchanged so DictWriter can still map each row's keys.
    writer.writerow({h: _neutralise_formula(h) for h in headers})
    for item in rejected_rows:
        row = {k: _neutralise_formula(v) for k, v in dict(item["row"]).items()}
        row[ERROR_REASON_COL] = _neutralise_formula(item["error"])
        writer.writerow(row)
    return output.getvalue()


@method_decorator(staff_member_required, name="dispatch")
class CsvCorrectionView(TemplateView):
    """
    Admin screen for correcting report metadata in bulk from an uploaded CSV.
    GET renders the upload page; POST validates every row against the master data before
    committing anything, then returns a summary and, where rows failed, a rejection file
    carrying the reason for each. Requires the Story change permission, not merely staff
    access, because a single upload can rewrite every report in the database.
    """

    template_name = "admin/csv_correction/csv_correction.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["model_name"] = "Story"
        return context

    def post(self, request, *args, **kwargs):
        # staff_member_required only proves the user can reach the admin. Bulk-rewriting
        # every Story needs the change permission for the model itself, checked before
        # anything is parsed or saved.
        if not request.user.has_perm("chatbot.change_story"):
            return JsonResponse(
                {"success": False, "error": api_responses.CSV_NO_PERMISSION},
                status=403,
            )

        uploaded_file = request.FILES.get("csv_file")

        if not uploaded_file:
            return JsonResponse({"success": False, "error": api_responses.CSV_NO_FILE_UPLOADED}, status=400)
        if not uploaded_file.name.lower().endswith(".csv"):
            return JsonResponse(
                {"success": False, "error": api_responses.CSV_INVALID_FILE_FORMAT},
                status=400,
            )

        try:
            content = uploaded_file.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content))
            headers = list(reader.fieldnames or [])
            rows = list(reader)
        except Exception as exc:
            return JsonResponse({"success": False, "error": api_responses.CSV_PARSE_FAILED_TEMPLATE.format(error=exc)}, status=400)

        original_headers = [h for h in headers if h != ERROR_REASON_COL]

        missing = CSV_REQUIRED_COLS - set(original_headers)
        if missing:
            return JsonResponse(
                {"success": False,
                 "error": api_responses.CSV_MISSING_COLUMNS_TEMPLATE.format(
                     columns=", ".join(sorted(missing))
                 )},
                status=400,
            )

        if not rows:
            return JsonResponse({"success": False, "error": api_responses.CSV_EMPTY_FILE}, status=400)

        # `or ""` rather than a get() default: csv.DictReader maps every column a short
        # row does not reach to None, so the key exists with a None value and the default
        # never applies. Calling .strip() on that raised an unhandled AttributeError.
        # Matches how _extract_fields reads the same column.
        ids = [(r.get("id") or "").strip() for r in rows]
        dupes = [rid for rid, cnt in Counter(ids).items() if cnt > 1 and rid]
        if dupes:
            return JsonResponse(
                {"success": False,
                 "error": api_responses.CSV_DUPLICATE_IDS_TEMPLATE.format(
                     ids=', '.join(dupes[:10])
                 )},
                status=400,
            )

        try:
            mapping = _load_state_district_mapping()
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)

        from chatbot.models.story_models import Story

        processed = successful = unchanged = 0
        rejected_rows = []

        for row in rows:
            fields = _extract_fields(row)

            if not fields["state"] and not fields["district"]:
                rejected_rows.append({"row": row, "error": api_responses.CSV_ROW_STATE_DISTRICT_BLANK})
                continue

            processed += 1

            raw_id = fields["id"]
            if not raw_id:
                rejected_rows.append({"row": row, "error": api_responses.CSV_ROW_ID_EMPTY})
                continue

            errors = _validate_row(fields, mapping)
            if errors:
                rejected_rows.append({"row": row, "error": "; ".join(errors)})
                continue

            try:
                story = Story.objects.get(pk=int(raw_id))
            except (ValueError, TypeError, Story.DoesNotExist):
                rejected_rows.append(
                    {"row": row, "error": api_responses.CSV_ROW_STORY_NOT_FOUND_TEMPLATE.format(story_id=raw_id)}
                )
                continue
            except Exception as exc:
                rejected_rows.append({"row": row, "error": api_responses.CSV_ROW_DB_ERROR_TEMPLATE.format(error=exc)})
                continue

            if not _apply_to_story(story, fields):
                unchanged += 1
                continue

            try:
                story.save()
                successful += 1
            except Exception as exc:
                rejected_rows.append({"row": row, "error": api_responses.CSV_ROW_SAVE_FAILED_TEMPLATE.format(error=exc)})

        rejection_csv_b64 = None
        if rejected_rows:
            import base64
            rej = _build_rejection_csv(rejected_rows, original_headers)
            rejection_csv_b64 = base64.b64encode(rej.encode("utf-8")).decode("ascii")

        return JsonResponse({
            "success": True,
            "stats": {
                "total_processed": processed,
                "successful_updates": successful,
                "unchanged_rows": unchanged,
                "rejected_rows": len(rejected_rows),
            },
            "rejection_csv": rejection_csv_b64,
            "rejection_count": len(rejected_rows),
        })
