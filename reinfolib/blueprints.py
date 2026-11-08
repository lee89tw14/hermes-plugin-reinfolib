"""Weekly history/diff check blueprint (M3), separate from legacy weekly-city-check.

Registered only when the v2 tools registered and ctx.register_automation_blueprint
exists. It only adds a catalog entry; this plugin never creates a cron job.
"""
import logging

logger = logging.getLogger(__name__)

KEY = 'weekly-history-check'
TITLE = 'Weekly city publication and record-change check'
DESCRIPTION = (
    'once a week, find the newest published quarter of MLIT transaction records for one city '
    '(probing back up to 8 quarters) and compare it with the previous saved fetch of the same quarter. '
    'Changes are API record changes, not new transactions. HTTP 404 is not zero sales. '
    'The default day is Monday and the default time is 09:00. This plugin does not create a cron job.'
)
SCHEDULE_TEMPLATE = '{minute} {hour} * * {dow}'
PROMPT_TEMPLATE = """\
Check MLIT transaction records for city {city}. Call only reinfolib_history and reinfolib_diff. \
city is a 5-digit code, not a municipality name. Example: 13104 is Shinjuku-ku. \
A designated city needs a ward code, not the city code. Example: 14101, not 14100.

Step 1. Call reinfolib_history once with city {city}, price_classification 01, language ja and max_quarters 8. \
Pass no other arguments. It probes quarters backward from the latest started quarter, with 1 second between requests.

Step 2. Read outcome.
- outcome found and found.previous_snapshot exists: call reinfolib_diff once with \
base_snapshot_id found.previous_snapshot.snapshot_id and target_snapshot_id found.snapshot_id.
  - If the diff has ok true and no_api_record_changes true, there is nothing to report: answer exactly [SILENT] and nothing else.
  - If the diff has ok true and changes: send one short message with found.period, added_n, removed_n, \
possible_revision_n, unchanged_n and the two fetched_at values. Say these are API record changes, not new transactions \
and not price changes. possible_revisions are heuristic pairs, not the same transaction.
  - If the diff has ok false (for example not_found because the earlier snapshot expired after 30 days): \
report found.period and found.upstream_count, say the comparison was not available, and do not use [SILENT].
- outcome found without found.previous_snapshot: send one short message with found.period, found.upstream_count \
and found.fetched_at. Say this is the first saved fetch of that quarter, so nothing was compared. Do not use [SILENT].
- outcome none_found_in_range: every attempted quarter was HTTP 404. That is not zero sales and not unchanged prices; \
it can also mean a wrong code. List the attempted periods in one short message. Do not use [SILENT].
- outcome failed or budget_exhausted: the check did not finish. Name the period, error_type and status_code of the \
last attempted quarter and its attempts. Do not treat it as no change. Do not use [SILENT].
- ok false: error_type invalid_argument or config was refused before a request, so name detail; any other error_type \
is a failure, so name error_type and status_code. Do not use [SILENT].
Attempted quarters with cached true were not requested in this run; their cached_at says when the 404 was seen.

Counts are API-covered records, not unique transactions. Include attribution, processing and credit unchanged \
whenever you send a message. Do not say that rows are new sales. \
If the tools are missing: say the jp-reinfolib v2 tools are not loaded. Do not guess.
"""
SLOTS = (
    {
        'name': 'city',
        'type': 'text',
        'label': 'Which city code?',
        'help': 'A 5-digit code, not the municipality name. Example: 13104 is Shinjuku-ku. '
                'A designated city needs a ward code, not the city code.',
    },
    {
        'name': 'day',
        'type': 'enum',
        'label': 'Which day?',
        'default': 'monday',
        'options': ('sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'),
        'strict': True,
        'help': "One weekday. Hermes's timezone, or the computer's clock if none is set.",
    },
    {
        'name': 'time',
        'type': 'time',
        'label': 'What time?',
        'default': '09:00',
        'help': "24-hour time in Hermes's timezone, or the computer's clock if none is set. Example: 09:00.",
    },
)


def register_blueprint(ctx) -> bool:
    """True only when Hermes accepted the blueprint. Never raises."""
    register = getattr(ctx, 'register_automation_blueprint', None)
    if not callable(register):
        return False
    try:
        accepted = register(KEY, title=TITLE, description=DESCRIPTION, category='weekly',
                            schedule_template=SCHEDULE_TEMPLATE, prompt_template=PROMPT_TEMPLATE,
                            slots=list(SLOTS), deliver_default='origin', skills=[], tags=['japan', 'mlit'])
    except Exception:
        logger.warning('jp-reinfolib: history blueprint was not registered')
        return False
    return accepted is not None
