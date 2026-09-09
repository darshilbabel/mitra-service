"""
User-facing response and validation messages, kept in one place.

Messages that reach an admin or an API client live here rather than inline at the point
they are raised, so wording can be reviewed and changed without hunting through views and
models. Several of these are asserted verbatim by QA, so treat the text as part of the
contract - change it deliberately, not incidentally.

Entries ending in `_TEMPLATE` take `.format(...)` arguments; the rest are complete
messages.

Named in snake_case to match the other modules in this package and PEP 8.
"""


# -------------- CSV CORRECTION: UPLOAD ------------------

CSV_NO_PERMISSION = "You do not have permission to change stories."
CSV_NO_FILE_UPLOADED = "No file uploaded."
CSV_INVALID_FILE_FORMAT = "Invalid file format. Please upload a .csv file."
CSV_EMPTY_FILE = "CSV file is empty."
CSV_MISSING_COLUMNS_TEMPLATE = (
    "Missing required column(s): {columns}. The CSV must have these columns."
)

CSV_PARSE_FAILED_TEMPLATE = "Could not parse CSV: {error}"
CSV_DUPLICATE_IDS_TEMPLATE = (
    "Duplicate id values detected — upload rejected. Duplicates: {ids}"
)


# -------------- CSV CORRECTION: ROW VALIDATION ------------------

CSV_ROW_ID_EMPTY = "id is empty"
CSV_ROW_STATE_DISTRICT_BLANK = "Row not processed: State and District both blank"

CSV_ROW_UNKNOWN_STATE_TEMPLATE = "Unknown State '{state}'"
CSV_ROW_UNKNOWN_DISTRICT_TEMPLATE = "Unknown District '{district}'"
CSV_ROW_DISTRICT_WRONG_STATE_TEMPLATE = (
    "District '{district}' belongs to {actual_states}, not '{state}'"
)
CSV_ROW_AMBIGUOUS_DISTRICT_TEMPLATE = (
    "District '{district}' is ambiguous — found in states: {candidate_states}. "
    "Specify State to disambiguate."
)
CSV_ROW_STORY_NOT_FOUND_TEMPLATE = "No Story found with id='{story_id}'"
CSV_ROW_DB_ERROR_TEMPLATE = "DB error: {error}"
CSV_ROW_SAVE_FAILED_TEMPLATE = "Save failed: {error}"

CSV_STATE_DISTRICT_BOT_NOT_FOUND = (
    "CompanyBot with route '/state-classification-guest-discussion' does not exist."
)
CSV_STATE_DISTRICT_MAPPING_INVALID = (
    "State/District mapping in CompanyBot dynamic_context is missing or invalid JSON."
)


# -------------- MODEL VALIDATION ------------------

FLOW_DEFAULT_FLOW_NOT_CHILD = "default_flow must be a direct child of this flow."

PROGRAM_MAPPING_UNKNOWN_STATE_TEMPLATE = (
    "'{state}' is not a known state. Choose one of: {valid_states}."
)
