#!/usr/bin/env python3
"""
gmail_scraper.py - pull job-related mail out of Gmail over IMAP and save it as emails.json.
No AI and nothing to install (Python 3.9+, standard library only).

One-time setup
  1. Turn on 2-Step Verification for the Google account.
  2. Create an app password: Google Account > Security > App passwords.
  3. copy .env.example to .env and put the password in it (.env is gitignored):
         GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"            (spaces are fine)
     A GMAIL_APP_PASSWORD already set in the environment takes precedence.

Run
  python gmail_scraper.py --user you@gmail.com --since 2026-10-20 --out emails.json
  python job_tracker.py scan emails.json --format json --out applications.json

  --before DATE   stop at this date (exclusive).  Dates are UTC days.
  --dry-run       log in, count what would be downloaded, download nothing.
  --all           skip the job-keyword filter and download every message in the range.
  --query "..."   use your own Gmail search words instead of the default job keywords.

The scraper only reads: the mailbox is opened read-only and messages are fetched with
BODY.PEEK, so nothing is marked read, moved or deleted.

Output (emails.json) has one entry per message:
  id          Gmail's message id (same value the Gmail API shows)
  threadId    Gmail's thread id
  internalDate  milliseconds since epoch, as Gmail received it
  from        sender address            subject   decoded subject
  labels      INBOX, SENT, DRAFT, IMPORTANT, ... plus your own label names
  body        plain text (HTML is converted when there is no usable plain part)
  url         link that opens the thread in Gmail
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import imaplib
import json
import os
import re
import socket
import ssl
import sys
import time
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

# Words that appear in nearly every application notice. Gmail does the matching on its side,
# so only plausible job mail is downloaded. Use --all if you suspect something is being missed.
JOB_TERMS = [
    "application", "applications", "applied", "applying", "applicant", "candidate", "candidacy",
    "interview", "interviews", "interviewing", "assessment", "recruiter", "recruiting", "recruitment",
    "hiring", "offer", "regret", "referral", "referred", '"next steps"', '"your interest"', '"move forward"',
]
STUB_PLAIN = re.compile(
    r"view (?:this|the) (?:message|email|e-mail)|html (?:version|email|e-mail)|enable html|"
    r"(?:email|e-mail) client (?:does not|doesn't|cannot|can't)|best viewed in", re.I)
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
MAX_BODY_CHARS = 100_000
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


# ------------------------------------------------------------------ small helpers

def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_day(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)


def build_query(since: str, before: str | None, terms: str | None, everything: bool) -> str:
    """Gmail search string. Starts a day early/ends a day late so time zones cannot hide mail;
    exact UTC cut-offs are applied after download."""
    start = parse_day(since) - dt.timedelta(days=1)
    parts = [f"after:{start:%Y/%m/%d}"]
    if before:
        parts.append(f"before:{parse_day(before) + dt.timedelta(days=1):%Y/%m/%d}")
    parts += ["-in:sent", "-in:drafts"]
    if not everything:
        parts.append("(" + (terms or " OR ".join(JOB_TERMS)) + ")")
    return " ".join(parts)


def imap_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_internaldate(s: str) -> int | None:
    """'02-Nov-2026 10:00:00 -0800' -> epoch milliseconds (locale independent)."""
    m = re.match(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)", s or "")
    if not m or m.group(2).title() not in MONTHS:
        return None
    sign = -1 if m.group(7) == "-" else 1
    tz = dt.timezone(sign * dt.timedelta(hours=int(m.group(8)), minutes=int(m.group(9))))
    when = dt.datetime(int(m.group(3)), MONTHS[m.group(2).title()], int(m.group(1)),
                       int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=tz)
    return int(when.timestamp() * 1000)


# ------------------------------------------------------------------ message -> dict

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(s: str) -> str:
    p = _Text()
    try:
        p.feed(s)
        p.close()
    except Exception:  # malformed markup: keep whatever was read
        pass
    text = "".join(p.out)
    text = re.sub(r"[​‌‍͏­﻿]", "", text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        for charset in (part.get_content_charset(), "utf-8"):
            try:
                return payload.decode(charset or "utf-8", "replace")
            except LookupError:
                continue
        return ""


def extract_body(msg) -> str:
    plain = html = ""
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind == "text/plain" and not plain:
            plain = _part_text(part)
        elif kind == "text/html" and not html:
            html = _part_text(part)
    # Some senders put a stub ("view this email in your browser") in the plain part and the
    # real text in HTML. Use the HTML when the plain part is empty, a known stub, or tiny next to it.
    plain = plain.strip()
    if html:
        converted = html_to_text(html)
        if (not plain or STUB_PLAIN.search(plain)
                or (len(plain) < 200 and len(converted) >= 2 * len(plain) + 40)):
            return converted[:MAX_BODY_CHARS]
    return plain[:MAX_BODY_CHARS]


def parse_labels(inner: str) -> list[str]:
    labels = []
    for quoted, atom in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', inner):
        name = re.sub(r"\\(.)", r"\1", quoted) if quoted else atom
        if not name:
            continue
        labels.append(name[1:].upper() if name.startswith("\\") else name)
    return labels


def message_to_dict(raw: bytes, meta: str, user: str) -> dict:
    """Turn one fetched message (raw RFC 822 bytes + the FETCH metadata text) into a record."""
    def grab(rx):
        m = re.search(rx, meta)
        return m.group(1) if m else None

    msg = message_from_bytes(raw, policy=policy.default)
    msgid, thrid = grab(r"X-GM-MSGID (\d+)"), grab(r"X-GM-THRID (\d+)")
    uid = grab(r"UID (\d+)") or "0"
    ts = parse_internaldate(grab(r'INTERNALDATE "([^"]+)"') or "")
    if ts is None:
        try:
            ts = int(parsedate_to_datetime(str(msg.get("Date"))).timestamp() * 1000)
        except (TypeError, ValueError):
            ts = 0
    inner = re.search(r'X-GM-LABELS \(((?:"(?:[^"\\]|\\.)*"|[^()"])*)\)', meta)
    return {
        "id": format(int(msgid), "x") if msgid else f"uid{uid}",
        "threadId": format(int(thrid), "x") if thrid else "",
        "internalDate": ts,
        "from": parseaddr(str(msg.get("From") or ""))[1],
        "subject": " ".join(str(msg.get("Subject") or "").split()),
        "labels": parse_labels(inner.group(1)) if inner else [],
        "body": extract_body(msg),
        "url": f"https://mail.google.com/mail/?authuser={user}#all/thread-f:{thrid}" if thrid else "",
    }


def split_fetch(data) -> list[tuple[str, bytes]]:
    """imaplib FETCH reply -> [(metadata text, raw message bytes)].
    Gmail may send some fields after the message literal; those arrive as plain bytes items."""
    records: list[list] = []
    for item in data or []:
        if isinstance(item, tuple):
            records.append([item[0], item[1], b""])
        elif isinstance(item, (bytes, bytearray)) and records:
            records[-1][2] += bytes(item)
    return [((h + t).decode("latin-1"), raw) for h, raw, t in records]


# ------------------------------------------------------------------ the IMAP session

class GmailSession:
    def __init__(self, user: str, password: str, host="imap.gmail.com", port=993, use_ssl=True,
                 timeout=60, retries=3):
        self.user, self.password = user, password
        self.host, self.port, self.use_ssl = host, port, use_ssl
        self.timeout, self.retries = timeout, retries
        self.imap = None
        self.mailbox = None

    # -- connection
    def connect(self) -> None:
        cls = imaplib.IMAP4_SSL if self.use_ssl else imaplib.IMAP4
        self.imap = cls(host=self.host, port=self.port, timeout=self.timeout)
        self.imap.login(self.user, self.password)
        self.mailbox = self.mailbox or self._find_all_mail()
        typ, _ = self.imap.select(imap_quote(self.mailbox), readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"could not open {self.mailbox!r}")

    def close(self) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
            try:
                self.imap.shutdown()     # always release the socket, even after a failed logout
            except Exception:
                pass
        self.imap = None

    def _find_all_mail(self) -> str:
        """The All Mail folder's name is translated per account language; find it by its \\All flag."""
        typ, lines = self.imap.list()
        for line in lines or []:
            text = line.decode("latin-1") if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)', text)
            if m and "\\all" in m.group("flags").lower():
                return re.sub(r"\\(.)", r"\1", m.group("name").strip('"'))
        return "[Gmail]/All Mail"

    def _retry(self, what: str, fn):
        for attempt in range(1, self.retries + 1):
            try:
                return fn()
            except (imaplib.IMAP4.abort, socket.timeout, ssl.SSLError, ConnectionError, OSError) as e:
                if attempt == self.retries:
                    raise
                warn(f"  {what}: connection problem ({e}); reconnecting (attempt {attempt + 1}/{self.retries})")
                self.close()
                time.sleep(2 * attempt)
                self.connect()

    # -- commands
    def search(self, query: str) -> list[str]:
        def run():
            typ, data = self.imap.uid("SEARCH", "X-GM-RAW", imap_quote(query))
            if typ != "OK":
                raise imaplib.IMAP4.error(f"search failed: {data}")
            return (data[0] or b"").split()
        return [u.decode() for u in self._retry("search", run)]

    def fetch(self, uids: list[str]) -> list[dict]:
        def run():
            typ, data = self.imap.uid("FETCH", ",".join(uids),
                                      "(X-GM-MSGID X-GM-THRID X-GM-LABELS INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"fetch failed: {data}")
            return data
        out = []
        for meta, raw in split_fetch(self._retry("fetch", run)):
            try:
                out.append(message_to_dict(raw, meta, self.user))
            except Exception as e:  # one unreadable message must not stop the run
                warn(f"  skipped a message that could not be read: {type(e).__name__}: {e}")
        return out


def fetch_messages(user: str, password: str, since: str, before: str | None = None,
                   terms: str | None = None, everything: bool = False, batch: int = 25,
                   dry_run: bool = False, **conn) -> tuple[list[dict], str]:
    """Download messages in [since, before) as UTC days. Returns (messages, gmail_query_used)."""
    query = build_query(since, before, terms, everything)
    floor = int(parse_day(since).timestamp() * 1000)
    ceiling = int(parse_day(before).timestamp() * 1000) if before else None

    session = GmailSession(user, password, **conn)
    try:
        session.connect()
        uids = session.search(query)
        warn(f"Gmail search matched {len(uids)} message(s): {query}")
        if dry_run:
            return [], query
        seen: dict[str, dict] = {}
        for i in range(0, len(uids), batch):
            for m in session.fetch(uids[i:i + batch]):
                if m["internalDate"] >= floor and (ceiling is None or m["internalDate"] < ceiling):
                    seen[m["id"]] = m
            warn(f"  downloaded {min(i + batch, len(uids))}/{len(uids)}")
    finally:
        session.close()
    return sorted(seen.values(), key=lambda m: m["internalDate"]), query


# ------------------------------------------------------------------ command line

def load_env_file(path: str | None = None) -> None:
    """Read KEY=VALUE lines from .env (next to this script, gitignored) into os.environ.
    Variables already set in the environment win. A missing file is fine."""
    try:
        with open(path or ENV_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.strip():
            os.environ.setdefault(key.strip(), value)


def get_password() -> str:
    load_env_file()
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not pw and sys.stdin.isatty():
        pw = getpass.getpass("Gmail app password: ")
    if not pw:
        sys.exit("Put GMAIL_APP_PASSWORD=... in the .env file next to this script (or set it in the environment) "
                 "using a Gmail app password (Google Account > Security > App passwords; needs 2-Step Verification).")
    return pw.replace(" ", "")


def main(argv=None) -> int:
    load_env_file()
    ap = argparse.ArgumentParser(description="Download job-related Gmail messages to emails.json (read-only).")
    ap.add_argument("--user", default=os.environ.get("GMAIL_USER"), help="Gmail address (or set GMAIL_USER)")
    ap.add_argument("--since", required=True, help="first day to include, YYYY-MM-DD (UTC)")
    ap.add_argument("--before", help="day to stop at, exclusive, YYYY-MM-DD (UTC)")
    ap.add_argument("--out", default="emails.json")
    ap.add_argument("--all", action="store_true", help="no keyword filter: download everything in the range")
    ap.add_argument("--query", help="your own Gmail search terms instead of the default job keywords")
    ap.add_argument("--dry-run", action="store_true", help="log in and count matches, download nothing")
    ap.add_argument("--batch", type=int, default=25, help="messages per request (default 25)")
    ap.add_argument("--host", default="imap.gmail.com")
    ap.add_argument("--port", type=int, default=993)
    ap.add_argument("--no-ssl", action="store_true", help="plain connection (testing only)")
    a = ap.parse_args(argv)
    if not a.user:
        ap.error("--user is required (or set GMAIL_USER)")
    for label, value in (("--since", a.since), ("--before", a.before)):
        if value:
            try:
                parse_day(value)
            except ValueError:
                ap.error(f"{label} must look like 2026-10-20")

    try:
        messages, query = fetch_messages(
            a.user, get_password(), a.since, a.before, a.query, a.all, a.batch, a.dry_run,
            host=a.host, port=a.port, use_ssl=not a.no_ssl)
    except imaplib.IMAP4.error as e:
        text = str(e)
        if "AUTHENTICATIONFAILED" in text.upper() or "invalid credentials" in text.lower():
            sys.exit("Gmail refused the login. Use an app password (not your normal password), make sure "
                     "2-Step Verification is on, and check IMAP is enabled in Gmail settings. "
                     "Workspace accounts may need an admin to allow app passwords.")
        sys.exit(f"Gmail error: {text}")
    except (socket.gaierror, socket.timeout, ConnectionError, ssl.SSLError, OSError) as e:
        sys.exit(f"Could not reach {a.host}: {e}")

    if a.dry_run:
        return 0
    payload = {"account": a.user, "since": a.since, "before": a.before,
               "fetched": dt.date.today().isoformat(), "query": query,
               "count": len(messages), "messages": messages}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    warn(f"Saved {len(messages)} message(s) to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
