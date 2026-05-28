"""Small wrapper around the competitor_tracker feed QA report."""

from __future__ import annotations

import json

from competitor_tracker.cli import run_feed_qa


if __name__ == "__main__":
    print(json.dumps(run_feed_qa(), ensure_ascii=False, indent=2))
