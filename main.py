#!/usr/bin/env python3
"""
main.py - run the whole pipeline: download mail with gmail_scraper, then sort it with job_tracker.

  python main.py --since 2026-10-20 [--before D] [--user you@gmail.com] [--all]

Writes emails.json, then applications.json (the file job-tracker.html loads by default).
"""
from __future__ import annotations

import argparse
import sys

import gmail_scraper
import job_tracker


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download job mail from Gmail, then build applications.json.")
    ap.add_argument("--since", required=True, help="first day to include, YYYY-MM-DD (UTC)")
    ap.add_argument("--before", help="day to stop at, exclusive, YYYY-MM-DD (UTC)")
    ap.add_argument("--user", help="Gmail address (default: GMAIL_USER from .env)")
    ap.add_argument("--all", action="store_true", help="no keyword filter on the download")
    ap.add_argument("--emails", default="emails.json", help="where to keep the downloaded mail")
    ap.add_argument("--out", default="applications.json", help="where to write the result")
    a = ap.parse_args(argv)

    scraper_args = ["--since", a.since, "--out", a.emails]
    if a.before:
        scraper_args += ["--before", a.before]
    if a.user:
        scraper_args += ["--user", a.user]
    if a.all:
        scraper_args.append("--all")
    code = gmail_scraper.main(scraper_args)
    if code:
        return code

    tracker_args = ["scan", a.emails, "--since", a.since, "--format", "json", "--out", a.out]
    if a.before:
        tracker_args += ["--before", a.before]
    code = job_tracker.main(tracker_args)
    if not code:
        print(f"Wrote {a.out}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
