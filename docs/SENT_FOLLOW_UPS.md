# Sent follow-ups

Record a follow-up only after confirming it was sent. Drafts and scheduled mail do not qualify. Keep outgoing contact separate from employer replies and interviews.

Use `tools/record_follow_up.py --tracker <canonical> --job-id <exact-id> --sent-date YYYY-MM-DD --evidence <exact-https-source> --backup-dir <backups> --dry-run`, review, then repeat without `--dry-run`. The command preserves status and other application fields. Structured imports also accept `FOLLOW_UP_DATE` and `FOLLOW_UP_EVIDENCE` together.

Validate the tracker, publish the canonical snapshot when authorized, and check the actual next action in the native widget. The installed Jobs3 widget observes atomic canonical replacements and retains its one-minute timer as a fallback. It calculates the next suggestion 14 days after the latest recorded application, employer response, interview or sent follow-up. A narrative note alone does not reset the interval.

Regression checks: `python3 -m unittest discover -s tools -p 'test_follow_up.py'`.
