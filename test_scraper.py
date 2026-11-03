#!/usr/bin/env python3
"""
Tests for gmail_scraper.py against a fake Gmail IMAP server (real sockets, real imaplib).

  python test_scraper.py

The fake server speaks the parts of IMAP and Gmail's extensions the scraper uses:
LOGIN, LIST (with the \\All flag), EXAMINE, UID SEARCH X-GM-RAW, UID FETCH with X-GM-MSGID /
X-GM-THRID / X-GM-LABELS / INTERNALDATE / BODY.PEEK[]. It can also drop the connection mid-fetch.
It does NOT implement Gmail's search language: it returns every message and records the query.
What this cannot prove: that Gmail itself accepts the query and answers exactly like the fake.
Run `gmail_scraper.py --dry-run` against the real account to check that part.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
import unittest
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gmail_scraper as gs  # noqa: E402
import job_tracker as jt  # noqa: E402

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def internaldate(ms: int, offset_hours: int = 0) -> str:
    tz = dt.timezone(dt.timedelta(hours=offset_hours))
    d = dt.datetime.fromtimestamp(ms / 1000, tz)
    sign = "-" if offset_hours < 0 else "+"
    return f'{d.day:02d}-{MONTHS[d.month - 1]}-{d.year} {d:%H:%M:%S} {sign}{abs(offset_hours):02d}00'


class FakeGmail:
    """A mailbox plus knobs for misbehaviour."""

    def __init__(self, messages, password="secretapppw"):
        self.messages = messages          # dicts: raw, msgid, thrid, labels, ms, tail(bool)
        self.password = password
        self.queries: list[str] = []
        self.drop_next_fetch = False
        self.logins = 0
        self.folder_name = "[Gmail]/Todos"   # localised on purpose
        self.peeked_only = True


class Handler(socketserver.StreamRequestHandler):
    def reply(self, data: bytes):
        self.wfile.write(data)
        self.wfile.flush()

    def handle(self):
        fake: FakeGmail = self.server.fake
        self.reply(b"* OK [CAPABILITY IMAP4rev1 X-GM-EXT-1] ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8").rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            if cmd == "CAPABILITY":
                self.reply(b"* CAPABILITY IMAP4rev1 X-GM-EXT-1\r\n" + tag.encode() + b" OK done\r\n")
            elif cmd == "LOGIN":
                fake.logins += 1
                user, _, pw = args.partition(" ")
                if pw.strip('"') == fake.password:
                    self.reply(tag.encode() + b" OK LOGIN completed\r\n")
                else:
                    self.reply(tag.encode() + b" NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)\r\n")
            elif cmd == "LIST":
                self.reply(b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n'
                           b'* LIST (\\HasNoChildren \\Sent) "/" "[Gmail]/Enviados"\r\n'
                           + f'* LIST (\\HasNoChildren \\All) "/" "{fake.folder_name}"\r\n'.encode()
                           + tag.encode() + b" OK done\r\n")
            elif cmd in ("SELECT", "EXAMINE"):
                if args.strip('"') != fake.folder_name:
                    self.reply(tag.encode() + b" NO [NONEXISTENT] Unknown Mailbox\r\n")
                    continue
                if cmd == "SELECT":
                    fake.peeked_only = False
                self.reply(f"* {len(fake.messages)} EXISTS\r\n".encode() + tag.encode()
                           + b" OK [READ-ONLY] selected\r\n")
            elif cmd == "UID":
                sub, _, sargs = args.partition(" ")
                if sub.upper() == "SEARCH":
                    m = re.match(r'X-GM-RAW "((?:[^"\\]|\\.)*)"$', sargs)
                    fake.queries.append(re.sub(r"\\(.)", r"\1", m.group(1)) if m else f"BAD:{sargs}")
                    uids = " ".join(str(i + 1) for i in range(len(fake.messages)))
                    self.reply(f"* SEARCH {uids}\r\n".encode() + tag.encode() + b" OK SEARCH done\r\n")
                elif sub.upper() == "FETCH":
                    ids, _, items = sargs.partition(" ")
                    if "BODY.PEEK[]" not in items:
                        fake.peeked_only = False
                    if fake.drop_next_fetch:
                        fake.drop_next_fetch = False
                        return                      # slam the connection shut
                    self.fetch(fake, tag, ids)
            elif cmd == "LOGOUT":
                self.reply(b"* BYE bye\r\n" + tag.encode() + b" OK LOGOUT completed\r\n")
                return
            else:
                self.reply(tag.encode() + b" BAD unknown command\r\n")

    def fetch(self, fake: FakeGmail, tag: str, ids: str):
        wanted = set()
        for part in ids.split(","):
            if ":" in part:
                a, b = part.split(":")
                wanted.update(range(int(a), int(b) + 1))
            else:
                wanted.add(int(part))
        out = b""
        for uid in sorted(wanted):
            if not 1 <= uid <= len(fake.messages):
                continue
            m = fake.messages[uid - 1]
            labels = " ".join(m["labels"])
            head = (f'* {uid} FETCH (X-GM-MSGID {m["msgid"]} X-GM-THRID {m["thrid"]} '
                    f'X-GM-LABELS ({labels}) ').encode()
            late = f' UID {uid} INTERNALDATE "{internaldate(m["ms"], m.get("tz", 0))}"'
            early = late
            if m.get("tail"):                      # Gmail may put some fields after the literal
                out += head + f"BODY[] {{{len(m['raw'])}}}\r\n".encode() + m["raw"] + late.encode() + b")\r\n"
            else:
                out += head + early.strip().encode() + f" BODY[] {{{len(m['raw'])}}}\r\n".encode() \
                    + m["raw"] + b")\r\n"
        self.reply(out + tag.encode() + b" OK Success\r\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_mail(subject, body, sender="no-reply@example.com", html=None, date=None, attach=None,
              charset="utf-8", extra_headers=None, html_only=False, stub=None) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "jarlowrey@gmail.com"
    m["Subject"] = subject
    if date:
        m["Date"] = date
    for k, v in (extra_headers or {}).items():
        m[k] = v
    if html is not None and html_only:          # a message with no plain-text part at all
        m.set_content(html, subtype="html")
    elif html is not None and stub is not None:  # plain part is a stub, e.g. "view in browser"
        m.set_content(stub)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(body)
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(body, charset=charset)
    if attach:
        m.add_attachment(attach, maintype="application", subtype="pdf", filename="offer.pdf")
    return m.as_bytes()


def ms(y, mo, d, h=12) -> int:
    return int(dt.datetime(y, mo, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)


class ScraperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.since = "2026-10-20"
        pdf = b"%PDF-1.4 " + os.urandom(150_000)
        odd = make_mail("Odd charset", "We received your application for Engineer.")
        odd = odd.replace(b'charset="utf-8"', b'charset="x-made-up"')
        msgs = [
            # 0 plain text, threaded, system + user labels (one with spaces and parentheses)
            dict(raw=make_mail("Thank you for applying to Acme", "Hi James,\n\nThanks for applying to Acme.",
                               "jobs@acme.com"), msgid=1001, thrid=2001,
                 labels=['\\Inbox', '\\Important', '"Job Search (2026)"'], ms=ms(2026, 10, 25)),
            # 1 HTML only; fields after the literal
            dict(raw=make_mail("Your application", None, "no-reply@globex.com",
                               html="<html><head><title>x</title><style>p{color:red}</style></head><body>"
                                    "<p>Dear&nbsp;James,</p><p>We regret to inform you that we will not be moving "
                                    "forward with your application for <b>Staff Engineer</b>.</p>"
                                    "<ul><li>Best wishes</li></ul></body></html>", html_only=True),
                 msgid=1002, thrid=2002, labels=['\\Inbox'], ms=ms(2026, 10, 26), tail=True),
            # 2 plain part is a stub, real text in HTML
            dict(raw=make_mail("Interview invitation", None, "talent@initech.com",
                               html="<div>We would like to invite you to an interview for the Backend Engineer "
                                    "role at Initech.</div>", stub="Please view this message in your browser."),
                 msgid=1003, thrid=2003, labels=['\\Inbox'], ms=ms(2026, 10, 27)),
            # 2b stub the phrase list has never heard of: caught by "tiny next to the HTML"
            dict(raw=make_mail("Update", None, "talent@hooli.com",
                               html="<div>Thank you for your application. After careful review we have decided not "
                                    "to move forward with your candidacy for the Data Engineer position at Hooli. "
                                    "We appreciate your time.</div>", stub="Open in app"),
                 msgid=1010, thrid=2010, labels=['\\Inbox'], ms=ms(2026, 10, 28, 6)),
            # 3 attachment must not leak into the body
            dict(raw=make_mail("Offer letter", "Congratulations, we are pleased to offer you the role.",
                               "hr@umbrella.com", attach=pdf), msgid=1004, thrid=2004,
                 labels=['\\Inbox'], ms=ms(2026, 10, 28)),
            # 4 non-ASCII subject (RFC 2047) and body in latin-1
            dict(raw=make_mail("Votre candidature à Société Générale – réponse",
                               "Bonjour James,\n\nMerci pour votre candidature. Café à Paris.",
                               "recrutement@sg.fr", charset="iso-8859-1"), msgid=1005, thrid=2005,
                 labels=['\\Inbox'], ms=ms(2026, 10, 29)),
            # 5 unknown charset must not crash
            dict(raw=odd, msgid=1006, thrid=2006, labels=['\\Inbox'], ms=ms(2026, 10, 30)),
            # 6 garbage Date header: INTERNALDATE wins; sent mail carries SENT
            dict(raw=make_mail("Re: Thanks", "Following up on my application.", "jarlowrey@gmail.com",
                               date="not a date at all"), msgid=1007, thrid=2001, labels=['\\Sent'],
                 ms=ms(2026, 10, 31)),
            # 7 before --since: must be filtered out by the exact UTC cut-off
            dict(raw=make_mail("Old news", "Your application was received."), msgid=1008, thrid=2008,
                 labels=['\\Inbox'], ms=ms(2026, 10, 19, 23)),
            # 8 INTERNALDATE in a non-UTC zone (Oct 20 02:00 UTC is Oct 19 18:00 at -08:00)
            dict(raw=make_mail("Application received", "We received your application."), msgid=1009,
                 thrid=2009, labels=['\\Inbox'], ms=ms(2026, 10, 20, 2), tz=-8),
        ]
        cls.fake = FakeGmail(msgs)
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.server.fake = cls.fake
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ["GMAIL_APP_PASSWORD"] = "secretapppw"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def scrape(self, **kw):
        kw.setdefault("batch", 4)
        return gs.fetch_messages("jarlowrey@gmail.com", "secretapppw", self.since, host="127.0.0.1",
                                 port=self.port, use_ssl=False, **kw)

    # ---- query building
    def test_query_is_wellformed(self):
        q = gs.build_query("2026-10-20", "2026-11-02", None, False)
        self.assertIn("after:2026/10/19", q)           # a day early, for time zones
        self.assertIn("before:2026/11/03", q)          # a day late
        self.assertIn("-in:sent", q)
        self.assertIn('"next steps"', q)
        self.assertNotIn("(", gs.build_query("2026-10-20", None, None, True))   # --all: no keyword group

    def test_default_query_searches_for_referrals_and_applications(self):
        q = gs.build_query("2026-10-20", None, None, False)
        for word in ("referral", "referred", "application", "applied"):
            self.assertIn(word, q)

    def test_query_reaches_server_intact(self):
        self.fake.queries.clear()
        _, q = self.scrape(terms='interview OR "next steps"')
        self.assertEqual(self.fake.queries, [q])
        self.assertIn('"next steps"', self.fake.queries[0])

    # ---- fetching and parsing
    def test_batches_and_exact_utc_window(self):
        msgs, _ = self.scrape()
        ids = {m["id"] for m in msgs}
        self.assertNotIn(format(1008, "x"), ids)         # 2026-10-19 23:00Z is before --since
        self.assertIn(format(1009, "x"), ids)            # Oct 20 02:00Z counts, whatever zone the server printed
        self.assertEqual(len(msgs), 9)
        self.assertEqual([m["internalDate"] for m in msgs], sorted(m["internalDate"] for m in msgs))

    def test_before_is_exclusive(self):
        msgs, _ = self.scrape(before="2026-10-27")
        self.assertTrue(all(m["internalDate"] < ms(2026, 10, 27, 0) for m in msgs))
        self.assertEqual(len(msgs), 3)                   # Oct 20 (02:00Z), Oct 25, Oct 26

    def by_id(self, n):
        msgs, _ = self.scrape()
        return next(m for m in msgs if m["id"] == format(n, "x"))

    def test_plain_message_fields_and_labels(self):
        m = self.by_id(1001)
        self.assertEqual(m["from"], "jobs@acme.com")
        self.assertEqual(m["threadId"], format(2001, "x"))
        self.assertEqual(m["labels"], ["INBOX", "IMPORTANT", "Job Search (2026)"])
        self.assertEqual(m["internalDate"], ms(2026, 10, 25))
        self.assertIn("Thanks for applying to Acme", m["body"])
        self.assertTrue(m["url"].endswith("#all/thread-f:2001"))

    def test_html_only_and_tail_fields(self):
        m = self.by_id(1002)                              # fields after the literal still parsed
        self.assertEqual(m["internalDate"], ms(2026, 10, 26))
        self.assertIn("We regret to inform you", m["body"])
        self.assertIn("Staff Engineer", m["body"])
        self.assertNotIn("<p>", m["body"])
        self.assertNotIn("color:red", m["body"])          # <style> dropped
        self.assertNotIn("\xa0", m["body"])

    def test_stub_plain_part_falls_back_to_html(self):
        self.assertIn("Backend Engineer", self.by_id(1003)["body"])

    def test_unknown_stub_falls_back_to_html(self):
        self.assertIn("Data Engineer position at Hooli", self.by_id(1010)["body"])

    def test_attachment_not_in_body(self):
        m = self.by_id(1004)
        self.assertIn("pleased to offer", m["body"])
        self.assertNotIn("%PDF", m["body"])
        self.assertLess(len(m["body"]), 500)

    def test_non_ascii(self):
        m = self.by_id(1005)
        self.assertEqual(m["subject"], "Votre candidature à Société Générale – réponse")
        self.assertIn("Café à Paris", m["body"])

    def test_unknown_charset_does_not_crash(self):
        self.assertIn("received your application", self.by_id(1006)["body"])

    def test_bad_date_header_uses_internaldate_and_sent_label(self):
        m = self.by_id(1007)
        self.assertEqual(m["internalDate"], ms(2026, 10, 31))
        self.assertEqual(m["labels"], ["SENT"])

    # ---- robustness
    def test_reconnects_after_dropped_connection(self):
        self.fake.drop_next_fetch = True
        before = self.fake.logins
        msgs, _ = self.scrape()
        self.assertEqual(len(msgs), 9)
        self.assertGreater(self.fake.logins - before, 1)  # logged in again

    def test_dry_run_downloads_nothing(self):
        msgs, _ = self.scrape(dry_run=True)
        self.assertEqual(msgs, [])

    def test_bad_password(self):
        import imaplib
        with self.assertRaises(imaplib.IMAP4.error) as ctx:
            gs.fetch_messages("jarlowrey@gmail.com", "wrong", self.since, host="127.0.0.1",
                              port=self.port, use_ssl=False)
        self.assertIn("AUTHENTICATIONFAILED", str(ctx.exception))

    def test_all_mail_found_by_flag_not_name(self):
        s = gs.GmailSession("jarlowrey@gmail.com", "secretapppw", host="127.0.0.1", port=self.port, use_ssl=False)
        s.connect()
        try:
            self.assertEqual(s.mailbox, "[Gmail]/Todos")
        finally:
            s.close()

    def test_nothing_is_modified(self):
        self.fake.peeked_only = True
        self.scrape()
        self.assertTrue(self.fake.peeked_only, "scraper used SELECT or a non-PEEK fetch")

    def test_command_line_writes_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "emails.json")
            rc = gs.main(["--user", "jarlowrey@gmail.com", "--since", self.since, "--out", out,
                          "--host", "127.0.0.1", "--port", str(self.port), "--no-ssl"])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["count"], 9)
            self.assertEqual(len(jt.load_messages(out)), 9)


class HelperTests(unittest.TestCase):
    """Pure functions in gmail_scraper.py; no server needed."""

    def test_parse_internaldate(self):
        self.assertEqual(gs.parse_internaldate("02-Nov-2026 10:00:00 -0800"), ms(2026, 11, 2, 18))
        self.assertEqual(gs.parse_internaldate(" 2-nov-2026 10:00:00 +0000"), ms(2026, 11, 2, 10))
        self.assertEqual(gs.parse_internaldate("02-Nov-2026 10:00:00 +0530"), ms(2026, 11, 2, 4) + 30 * 60_000)

    def test_parse_internaldate_rejects_garbage(self):
        for bad in ("", None, "yesterday", "02-Xyz-2026 10:00:00 +0000"):
            self.assertIsNone(gs.parse_internaldate(bad))

    def test_parse_day(self):
        self.assertEqual(gs.parse_day("2026-10-20"), dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc))
        with self.assertRaises(ValueError):
            gs.parse_day("10/20/2026")

    def test_imap_quote_escapes(self):
        self.assertEqual(gs.imap_quote('say "hi" \\ now'), '"say \\"hi\\" \\\\ now"')

    def test_parse_labels(self):
        self.assertEqual(gs.parse_labels('\\Inbox "Job Search (2026)" Custom "a \\"b\\""'),
                         ["INBOX", "Job Search (2026)", "Custom", 'a "b"'])
        self.assertEqual(gs.parse_labels(""), [])

    def test_html_to_text(self):
        out = gs.html_to_text("<html><head><title>t</title></head><body><script>x()</script>"
                              "<p>Hello&nbsp;there</p><p>Second&#8203; line</p><br>End</body></html>")
        self.assertEqual(out, "Hello there\n\nSecond line\n\nEnd")

    def test_html_to_text_survives_broken_markup(self):
        self.assertIn("text", gs.html_to_text("<p>text<div><b>unclosed"))

    def test_split_fetch_joins_trailing_fields(self):
        data = [(b"1 (UID 1 BODY[] {3}", b"abc"), b" INTERNALDATE \"x\")", b")"]
        self.assertEqual(gs.split_fetch(data), [(f'1 (UID 1 BODY[] {{3}} INTERNALDATE "x"))', b"abc")])
        self.assertEqual(gs.split_fetch(None), [])
        self.assertEqual(gs.split_fetch([b")"]), [])           # stray bytes with no message before them

    def test_message_to_dict_without_gmail_extensions(self):
        raw = make_mail("Hello", "Body text", "a@example.com")
        d = gs.message_to_dict(raw, '1 (UID 7 INTERNALDATE "02-Nov-2026 10:00:00 +0000")', "me@example.com")
        self.assertEqual((d["id"], d["threadId"], d["url"], d["labels"]), ("uid7", "", "", []))
        self.assertEqual(d["internalDate"], ms(2026, 11, 2, 10))

    def test_message_to_dict_falls_back_to_date_header(self):
        raw = make_mail("Hello", "Body", date="Mon, 02 Nov 2026 10:00:00 +0000")
        d = gs.message_to_dict(raw, "1 (UID 7)", "me@example.com")
        self.assertEqual(d["internalDate"], ms(2026, 11, 2, 10))

    def test_subject_whitespace_is_collapsed(self):
        raw = make_mail("Line one line two", "Body").replace(
            b"Subject: Line one line two", b"Subject: Line one\r\n   line two")   # folded header
        self.assertEqual(gs.message_to_dict(raw, "1 (UID 1)", "u")["subject"], "Line one line two")

    def test_body_is_capped(self):
        raw = make_mail("Big", "x" * (gs.MAX_BODY_CHARS + 5000))
        self.assertEqual(len(gs.message_to_dict(raw, "1 (UID 1)", "u")["body"]), gs.MAX_BODY_CHARS)

    def test_query_options(self):
        q = gs.build_query("2026-10-20", None, "foo OR bar", False)
        self.assertIn("(foo OR bar)", q)
        self.assertNotIn("before:", q)
        self.assertIn("-in:drafts", q)


class CommandLineErrors(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

    def test_password_from_env_strips_spaces(self):
        os.environ["GMAIL_APP_PASSWORD"] = "abcd efgh ijkl mnop"
        self.assertEqual(gs.get_password(), "abcdefghijklmnop")

    def env_file(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        old, gs.ENV_FILE = gs.ENV_FILE, path
        self.addCleanup(setattr, gs, "ENV_FILE", old)
        return path

    def test_password_from_env_file(self):
        os.environ.pop("GMAIL_APP_PASSWORD", None)
        self.env_file('# comment\n\nexport GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"\nGMAIL_USER=me@example.com\n')
        self.assertEqual(gs.get_password(), "abcdefghijklmnop")
        self.assertEqual(os.environ["GMAIL_USER"], "me@example.com")

    def test_environment_beats_env_file(self):
        os.environ["GMAIL_APP_PASSWORD"] = "fromenv"
        self.env_file("GMAIL_APP_PASSWORD=fromfile\n")
        self.assertEqual(gs.get_password(), "fromenv")

    def test_env_file_ignores_junk_and_handles_single_quotes(self):
        os.environ.pop("GMAIL_APP_PASSWORD", None)
        self.env_file("not a setting\n=novalue\nGMAIL_APP_PASSWORD='pw with space'\n")
        self.assertEqual(gs.get_password(), "pwwithspace")

    def test_missing_env_file_is_fine(self):
        gs.ENV_FILE, old = os.path.join(tempfile.gettempdir(), "no-such-dir", ".env"), gs.ENV_FILE
        self.addCleanup(setattr, gs, "ENV_FILE", old)
        os.environ["GMAIL_APP_PASSWORD"] = "pw"
        self.assertEqual(gs.get_password(), "pw")

    def test_missing_password_exits(self):
        os.environ.pop("GMAIL_APP_PASSWORD", None)
        gs.ENV_FILE, old = os.path.join(tempfile.gettempdir(), "no-such-dir", ".env"), gs.ENV_FILE
        self.addCleanup(setattr, gs, "ENV_FILE", old)
        stdin, sys.stdin = sys.stdin, io.StringIO()            # not a tty, so no prompt
        try:
            with self.assertRaises(SystemExit):
                gs.get_password()
        finally:
            sys.stdin = stdin

    def run_main(self, *argv):
        old, sys.stderr = sys.stderr, io.StringIO()
        try:
            return gs.main(list(argv))
        finally:
            sys.stderr = old

    def test_user_required(self):
        os.environ.pop("GMAIL_USER", None)
        gs.ENV_FILE, old = os.path.join(tempfile.gettempdir(), "no-such-dir", ".env"), gs.ENV_FILE
        self.addCleanup(setattr, gs, "ENV_FILE", old)
        with self.assertRaises(SystemExit):
            self.run_main("--since", "2026-10-20")

    def test_bad_dates_rejected(self):
        for args in (["--since", "20-10-2026"], ["--since", "2026-10-20", "--before", "soon"]):
            with self.subTest(args), self.assertRaises(SystemExit):
                self.run_main("--user", "me@example.com", *args)

    def test_unreachable_host_exits_cleanly(self):
        os.environ["GMAIL_APP_PASSWORD"] = "pw"
        with self.assertRaises(SystemExit) as ctx:
            self.run_main("--user", "me@example.com", "--since", "2026-10-20", "--host", "127.0.0.1",
                          "--port", "1", "--no-ssl")
        self.assertIn("Could not reach", str(ctx.exception))


@unittest.skipUnless(os.path.exists(os.path.join(HERE, "fixtures", "emails_since_2026-10-20.json")),
                     "fixtures/ holds personal mail and is not committed")
class RoundTrip(unittest.TestCase):
    """The 46 real emails from fixtures/ pushed through fake Gmail -> scraper -> tracker must give
    the same answer as reading the fixture directly (18/18 applications, 23/23 notices)."""

    def test_fixture_through_scraper_matches_hand_labels(self):
        fx = os.path.join(HERE, "fixtures")
        src = jt.load_messages(os.path.join(fx, "emails_since_2026-10-20.json"))
        sysname = {"INBOX": "\\Inbox", "SENT": "\\Sent", "DRAFT": "\\Draft", "SPAM": "\\Spam",
                   "TRASH": "\\Trash", "IMPORTANT": "\\Important"}
        msgs = []
        for m in src:
            raw = make_mail(m["subject"], m["body"], m["from"])
            msgs.append(dict(raw=raw, msgid=int(m["id"], 16), thrid=int(m["threadId"], 16),
                             labels=[sysname.get(x, f'"{x}"') for x in m.get("labels", [])],
                             ms=int(m["internalDate"])))
        fake = FakeGmail(msgs)
        server = Server(("127.0.0.1", 0), Handler)
        server.fake = fake
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            got, _ = gs.fetch_messages("jarlowrey@gmail.com", "secretapppw", "2026-10-20", batch=10,
                                       host="127.0.0.1", port=server.server_address[1], use_ssl=False)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(got), len(src))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "emails.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"messages": got}, fh)
            rc = jt.validate(path, os.path.join(fx, "expected_since_2026-10-20.json"))
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
