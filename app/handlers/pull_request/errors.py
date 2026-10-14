"""Why a section of the PR report has nothing to show."""


class SectionUnavailable(Exception):
    """
    A section ran and produced nothing usable — the model's answer could not
    be parsed, or it returned nothing.

    These sections used to return "" and be left out of the report. On a push
    the report was then rewritten without them, so the previous commit's
    findings vanished as though fixed, and a review that never happened read
    the same as one that found nothing. handle() lists it under "not run on
    this commit" instead. Not retried: a bad answer is not a passing outage.
    """
