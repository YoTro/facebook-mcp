"""Request-variable templates captured from the live frontend (L2).

Facebook's GraphQL ``variables`` objects are large and build-specific (they
include ~27 Relay feature-gate flags). Rather than hardcoding that in client
logic, the captured blob is kept as JSON data here and only the request-specific
fields are substituted.

Refresh with::

    .venv/bin/python tools/sniff.py --save-templates
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).parent

SEARCH_RESULTS_TEMPLATE = TEMPLATE_DIR / "search_results.json"

# experience.type values Facebook uses for each search tab.
_EXPERIENCE_BY_CATEGORY = {
    "people": "PEOPLE_TAB",
    "pages": "PAGES_TAB",
}


class TemplateError(RuntimeError):
    """A captured template is missing or malformed."""


def load_template(path: Path) -> dict[str, Any]:
    """Read a JSON template from disk."""
    if not path.is_file():
        raise TemplateError(
            f"missing template {path}; capture it with tools/sniff.py --save-templates"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise TemplateError(f"template {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TemplateError(f"template {path} must contain a JSON object")
    return data


def build_search_variables(
    query: str,
    category: str,
    limit: int,
    *,
    page_number: int = 0,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Render the captured search template into concrete ``variables``."""
    template = load_template(SEARCH_RESULTS_TEMPLATE)
    variables = copy.deepcopy(template)

    experience_type = _EXPERIENCE_BY_CATEGORY.get(category.lower())
    if experience_type is None:
        raise TemplateError(f"unsupported search category: {category!r}")

    args = variables.setdefault("args", {})
    args["text"] = query
    args.setdefault("experience", {})["type"] = experience_type
    args.setdefault("context", {})["bsid"] = str(uuid.uuid4())

    variables["count"] = max(1, min(int(limit), 50))
    variables["cursor"] = cursor or json.dumps(
        {"page_number": page_number, "flow_cursors_serialized": {}},
        separators=(",", ":"),
    )
    return variables
