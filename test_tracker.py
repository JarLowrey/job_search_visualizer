#!/usr/bin/env python3
"""
Tests for job_tracker.py using small synthetic messages (no real mail, so they run anywhere).

  python test_tracker.py

Covers the pure pieces: text cleaning, sender rules, status detection, company/position extraction,
grouping into applications, output formats, validation and the command line.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_tracker as jt  # noqa: E402


def ms(y, mo, d, h=12) -> int:
    return int(dt.datetime(y, mo, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)


def msg(subject, body, sender="no-reply@example.com", mid="m1", thread=None, when=None, labels=None):
    return {"id": mid, "threadId": thread or mid, "internalDate": when or ms(2026, 10, 25),
            "from": sender, "subject": subject, "body": body, "labels": labels or ["INBOX"],
            "url": f"https://mail.example.com/{mid}"}


class TextHelpers(unittest.TestCase):
    def test_clean_body_drops_quoted_history_links_and_tables(self):
        raw = ("Thanks for applying.\r\nSee [our site](https://acme.example/careers) or https://acme.example/x now.\n"
               "> quoted line\n| a | b |\n\nOn Mon, Oct 5, 2026, Bob <b@example.com> wrote:\nold stuff that must go")
        out = jt.clean_body(raw)
        self.assertIn("Thanks for applying.", out)
        self.assertIn("our site", out)
        self.assertNotIn("http", out)
        self.assertNotIn("quoted line", out)
        self.assertNotIn("old stuff", out)

    def test_clean_body_normalises_smart_quotes_and_entities(self):
        self.assertEqual(jt.clean_body("We’re glad &amp; excited"), "We're glad & excited")

    def test_clean_body_handles_none(self):
        self.assertEqual(jt.clean_body(None), "")

    def test_sentences_split_across_lines_and_paragraphs(self):
        self.assertEqual(jt.sentences("One thing.\nStill one? Yes!\n\nNew paragraph."),
                         ["One thing.", "Still one?", "Yes!", "New paragraph."])
        self.assertEqual(jt.sentences("A sentence wrapped\nacross two lines."), ["A sentence wrapped across two lines."])

    def test_sender_parts_and_registrable(self):
        self.assertEqual(jt.sender_parts('"Jobs" <Jobs@Mail.Acme.com>'), ("jobs", "mail.acme.com"))
        self.assertEqual(jt.sender_parts(None), ("", ""))
        self.assertEqual(jt.registrable("mail.acme.com"), "acme.com")
        self.assertEqual(jt.registrable("careers.acme.co.uk"), "acme.co.uk")
        self.assertEqual(jt.registrable("localhost"), "localhost")

    def test_is_automated(self):
        self.assertTrue(jt.is_automated("no-reply", "acme.com"))
        self.assertTrue(jt.is_automated("anything", "mail.greenhouse.io"))
        self.assertTrue(jt.is_automated("acme-recruiting", "acme.com"))
        self.assertFalse(jt.is_automated("jane.doe", "acme.com"))

    def test_conditional_vs_polite(self):
        self.assertTrue(jt.is_conditional("If you are selected we will call."))
        self.assertFalse(jt.is_conditional("Send times that work, if at all possible."))

    def test_pos_key_and_same_company(self):
        self.assertEqual(jt.pos_key("Sr. Software Engineer"), "senior software engineer")
        self.assertEqual(jt.pos_key(None), "")
        self.assertTrue(jt.same_company("Zoom", "Zoom Communications"))
        self.assertTrue(jt.same_company("Acme, Inc.", "ACME"))
        self.assertFalse(jt.same_company("Acme", "Globex"))
        self.assertFalse(jt.same_company("", "Acme"))

    def test_iso_is_utc_day(self):
        self.assertEqual(jt.iso(ms(2026, 10, 25, 23)), "2026-10-25")


class Classification(unittest.TestCase):
    def classify(self, subject, body, sender="no-reply@example.com", **kw):
        return jt.classify_message(msg(subject, body, sender, **kw))

    def status(self, body, subject="Your application", **kw):
        n = self.classify(subject, body, **kw)
        self.assertTrue(n["job"], n["reason"])
        return n["status"]

    def test_each_status(self):
        cases = {
            "applied": "Thank you for applying. We have received your application.",
            "rejected": "We regret to inform you that we will not be moving forward with your application.",
            "interviewing": "We would like to schedule an interview with you for your application.",
            "offer": "We are pleased to offer you the position. Your offer letter is attached.",
            "accepted": "Thank you for accepting our offer! Welcome aboard. Your start date is Monday.",
            "withdrawn": "This confirms your application has been withdrawn.",
            "assessment": "Please complete the coding challenge to continue your application.",
            "referred": "You've been referred by Sam for your application to the Backend Engineer position.",
        }
        for want, body in cases.items():
            with self.subTest(want):
                self.assertEqual(self.status(body), want)

    def test_referred_variants(self):
        bodies = [
            "You've been referred by Sam Lee for the Backend Engineer position at Acme.",
            "Sam Lee submitted an employee referral for your candidacy for the Backend Engineer role.",
            "You were referred by Sam Lee, a current employee, for the Backend Engineer position.",
        ]
        for body in bodies:
            with self.subTest(body):
                self.assertEqual(self.status(body, subject="Referral for Backend Engineer"), "referred")

    def test_referral_notice_with_a_task_is_still_referred(self):
        # the email says how the application started; the task shows up on a later notice
        body = ("We're excited you have been referred to Acme! As part of our referral process, we invite you "
                "to complete some additional application questions.")
        self.assertEqual(self.status(body, subject="Information Request for Acme"), "referred")

    def test_referred_then_pending_then_task_item_kept(self):
        _, apps = jt.scan_messages([
            msg("Information Request for Acme", "We're excited you have been referred to Acme! Please complete some "
                "additional application questions.", "no-reply@acme.com", mid="r", when=ms(2026, 10, 20)),
            msg("Action Required: Complete the Acme coding assessment",
                "Please complete the coding assessment for your application. It expires November 5.",
                "no-reply@codesignal.com", mid="p", when=ms(2026, 10, 22)),
        ])
        self.assertEqual([n["status"] for n in apps[0]["notices"]], ["referred", "assessment"])
        self.assertEqual(apps[0]["status"], "assessment")
        self.assertIn("Nov 5", apps[0]["item"])

    def test_internet_is_not_an_internship_title(self):
        self.assertIsNone(jt.extract_position(
            "Complete the Acme assessment",
            "Finish the assessment in one sitting, so find a space with a reliable internet connection."))

    def test_referred_is_not_a_final_state(self):
        # referred -> applied: the later notice wins, so a referral is only the starting point
        _, apps = jt.scan_messages([
            msg("Referral for Backend Engineer", "You've been referred by Sam Lee for the Backend Engineer position at Acme.",
                "jobs@acme.com", mid="r", when=ms(2026, 10, 20)),
            msg("Thank you for applying to Acme", "We received your application for Backend Engineer at Acme.",
                "jobs@acme.com", mid="a", when=ms(2026, 10, 22)),
        ])
        self.assertEqual(len(apps), 1)
        self.assertEqual([n["status"] for n in apps[0]["notices"]], ["referred", "applied"])
        self.assertEqual(apps[0]["status"], "applied")

    def test_referred_alone_is_an_application(self):
        _, apps = jt.scan_messages([msg("Referral for Backend Engineer",
                                        "You've been referred by Sam Lee for the Backend Engineer position at Acme.",
                                        "jobs@acme.com")])
        self.assertEqual(apps[0]["status"], "referred")

    def test_rejection_beats_applied_in_same_email(self):
        body = "Thank you for applying. Unfortunately we decided not to move forward with your application."
        self.assertEqual(self.status(body), "rejected")

    def test_unfortunately_alone_is_not_a_rejection(self):
        n = self.classify("Your application",
                          "Unfortunately both of our recruiters are out of the office this week.")
        self.assertNotEqual(n.get("status"), "rejected")

    def test_conditional_sentence_does_not_trigger_rejection(self):
        body = "We received your application. If you are not selected, we will not be moving forward."
        self.assertEqual(self.status(body), "applied")

    def test_weak_move_forward_means_interviewing(self):
        self.assertEqual(self.status("We are excited to move forward with your application."), "interviewing")

    def test_pending_item_includes_due_date(self):
        n = self.classify("Complete the Hooli coding challenge",
                          "Please complete the coding challenge by October 31 to continue your application.")
        self.assertEqual(n["status"], "assessment")
        self.assertIn("(due Oct 31)", n["item"])

    def test_tasks_are_split_by_kind(self):
        cases = {
            "assessment": ("Next Step: Complete the Acme coding assessment", "Please complete the coding assessment for your application."),
            "scheduling": ("Phone Screen", "Thanks for your application. Please let me know three dates and times that work best for you."),
            "paperwork": ("Application Confirmation and NDA Request", "Thanks for applying to Acme! We kindly request "
                          "your agreement to a Non-Disclosure Agreement (NDA)."),
            "pending": ("Your application", "Action required: please confirm your application."),
        }
        for want, (subject, body) in cases.items():
            with self.subTest(want):
                self.assertEqual(self.classify(subject, body)["status"], want)

    def test_more_confirmation_and_rejection_wording(self):
        self.assertEqual(self.status("Thank you again for your interest in the Senior Software Engineer position.",
                                     subject="Acme: Thanks for Completing Your Application"), "applied")
        self.assertEqual(self.status("A huge thank you for choosing to apply for a role here at Acme."), "applied")
        self.assertEqual(self.status("After reviewing your experience, we unfortunately don't see a strong match "
                                     "for our current open roles."), "rejected")

    def test_dropped_messages_have_reasons(self):
        cases = [
            (msg("Your application", "We received your application.", labels=["SENT"]), "sent"),
            (msg("Your application", "We received your application.", labels=["SPAM"]), "sent"),
            (msg("Security alert", "We received your application.", "no-reply@accounts.google.com"), "ignored sender"),
            (msg("Verify your email", "Thanks for applying."), "account or marketing"),
            (msg("Weekly digest", "Thanks for applying."), "account or marketing"),
            (msg("Hello", "Just a note about lunch."), "no application status signal"),
            (msg("[Request received] Your application", "We received your application."), "helpdesk"),
            (msg("Re: dinner", "Unfortunately that won't work, we decided on Friday.", "friend@gmail.com"),
             "personal email"),
        ]
        for m, reason in cases:
            with self.subTest(m["subject"]):
                n = jt.classify_message(m)
                self.assertFalse(n["job"])
                self.assertIn(reason, n["reason"])

    def test_unknown_status_for_automated_mail_that_mentions_application(self):
        n = self.classify("Acme Application", "Here is a copy of your application for your records.",
                          "no-reply@acme.com")
        self.assertTrue(n["job"])
        self.assertEqual(n["status"], "unknown")

    def test_security_code_mail_is_ignored(self):
        n = self.classify("Security code for your application to Acme",
                          "Copy this code into the security code field on your application:", "no-reply@greenhouse-mail.io")
        self.assertFalse(n["job"])
        self.assertIn("account or marketing", n["reason"])

    def test_unknown_is_current_status_only_when_it_is_the_latest_notice(self):
        code = msg("Acme Application", "Here is a copy of your application for your records.",
                   "no-reply@acme.com", mid="c", when=ms(2026, 10, 20))
        thanks = msg("Thank you for applying to Acme", "We received your application.", "jobs@acme.com",
                     mid="t", when=ms(2026, 10, 21))
        _, only_code = jt.scan_messages([code])
        _, both = jt.scan_messages([code, thanks])
        self.assertEqual(only_code[0]["status"], "unknown")
        self.assertEqual([n["status"] for n in both[0]["notices"]], ["unknown", "applied"])
        self.assertEqual(both[0]["status"], "applied")

    def test_message_fields_are_carried_through(self):
        n = jt.classify_message(msg("Thanks for applying to Docker!", "We received your application.",
                                    mid="abc", thread="t9", when=ms(2026, 10, 25)))
        self.assertEqual((n["id"], n["thread"], n["ts"], n["url"]),
                         ("abc", "t9", ms(2026, 10, 25), "https://mail.example.com/abc"))

    def test_missing_fields_do_not_crash(self):
        n = jt.classify_message({})
        self.assertFalse(n["job"])


class Extraction(unittest.TestCase):
    def company(self, subject, body="", sender="no-reply@example.com"):
        local, domain = jt.sender_parts(sender)
        return jt.extract_company(subject, jt.clean_body(body), local, domain)

    def test_company_from_subject_patterns(self):
        self.assertEqual(self.company("Thank you for applying to Docker!"), "Docker")
        self.assertEqual(self.company("OpenAI Application Update"), "OpenAI")
        self.assertEqual(self.company("Your Encamp application"), "Encamp")
        self.assertEqual(self.company("Your offer from 2U"), "2U")
        self.assertEqual(self.company("Application for Senior Software Engineer at Keona Health was sent"),
                         "Keona Health")

    def test_company_from_body_and_signature(self):
        self.assertEqual(self.company("Update", "We are glad you are applying to Shopify."), "Shopify")
        self.assertEqual(self.company("Update", "Thanks.\n\nThe Initech Recruiting Team"), "Initech")

    def test_platform_name_is_not_part_of_company(self):
        self.assertEqual(self.company("DroneSeed Angelist Application"), "DroneSeed")

    def test_company_guessed_from_domain_flag(self):
        name, guessed = jt._extract_company("Update", "Nothing useful here.", "jobs", "globex.com")
        self.assertEqual((name, guessed), ("Globex", True))

    def test_unknown_company_for_free_mail_and_ats(self):
        self.assertEqual(jt._extract_company("Update", "", "bob", "gmail.com"), ("Unknown", True))
        self.assertEqual(jt._extract_company("Update", "", "no-reply", "greenhouse.io"), ("Unknown", True))

    def test_usable_company_rejects_generic_words(self):
        for bad in ("Job", "Thank You", "Application Update", "Senior Engineer", ""):
            with self.subTest(bad):
                self.assertFalse(jt.usable_company(bad))
        self.assertTrue(jt.usable_company("The Humane League"))
        self.assertTrue(jt.usable_company("Acme"))

    def test_tidy_company(self):
        self.assertEqual(jt.tidy_company("Acme's careers"), "Acme")
        self.assertEqual(jt.tidy_company("Indeed"), "Indeed")     # never empties the name

    def test_position_patterns(self):
        cases = [
            ("Thanks", "Thank you for your application for Senior Software Engineer at Acme.", "Senior Software Engineer"),
            ("Interview Confirmation: Web Developer II", "", "Web Developer II"),
            ("Update", "Role: Backend Engineer", "Backend Engineer"),
            ("Update", "We are reviewing your resume for our Data Analyst role.", "Data Analyst"),
        ]
        for subject, body, want in cases:
            with self.subTest(want):
                self.assertEqual(jt.extract_position(subject, jt.clean_body(body)), want)

    def test_position_absent(self):
        self.assertIsNone(jt.extract_position("Hello", "Thanks for stopping by."))


class Grouping(unittest.TestCase):
    def notice(self, company, position, status, when, mid, thread=None, guessed=False):
        return {"id": mid, "thread": thread or mid, "ts": when, "from": "x@example.com", "subject": f"{company} {status}",
                "url": "", "job": True, "reason": "", "status": status, "evidence": f"{status} evidence",
                "position": position, "company": company, "company_guessed": guessed}

    def test_latest_notice_decides_status_and_dates(self):
        apps = jt.build_applications([
            self.notice("Acme", "Engineer", "applied", ms(2026, 10, 20), "1"),
            self.notice("Acme", "Engineer", "interviewing", ms(2026, 10, 25), "2"),
            self.notice("Acme", "Engineer", "rejected", ms(2026, 10, 30), "3"),
        ])
        self.assertEqual(len(apps), 1)
        a = apps[0]
        self.assertEqual((a["status"], a["first_date"], a["last_date"]), ("rejected", "2026-10-20", "2026-10-30"))
        self.assertEqual([n["status"] for n in a["notices"]], ["applied", "interviewing", "rejected"])

    def test_input_order_does_not_matter(self):
        notices = [self.notice("Acme", "Engineer", "rejected", ms(2026, 10, 30), "3"),
                   self.notice("Acme", "Engineer", "applied", ms(2026, 10, 20), "1")]
        self.assertEqual(jt.build_applications(notices)[0]["status"], "rejected")

    def test_different_roles_at_one_company_stay_separate(self):
        apps = jt.build_applications([
            self.notice("Acme", "Backend Engineer", "applied", ms(2026, 10, 20), "1"),
            self.notice("Acme", "Data Analyst", "applied", ms(2026, 10, 21), "2"),
        ])
        self.assertEqual(len(apps), 2)

    def test_notice_without_role_joins_existing_application(self):
        apps = jt.build_applications([
            self.notice("Acme", "Engineer", "applied", ms(2026, 10, 20), "1", thread="t"),
            self.notice("Acme", None, "rejected", ms(2026, 10, 28), "2", thread="t"),
        ])
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["status"], "rejected")

    def test_first_notice_without_role_adopts_later_role(self):
        apps = jt.build_applications([
            self.notice("Acme", None, "applied", ms(2026, 10, 20), "1"),
            self.notice("Acme", "Engineer", "interviewing", ms(2026, 10, 22), "2"),
        ])
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["position"], "Engineer")

    def test_text_company_name_beats_domain_guess_and_shorter_wins(self):
        apps = jt.build_applications([
            self.notice("Zoomcom", "Engineer", "applied", ms(2026, 10, 20), "1", guessed=True),
            self.notice("Zoom", "Engineer", "applied", ms(2026, 10, 21), "2", guessed=False),
        ])
        # same_company needs a shared token prefix; these are different tokens, so two apps is correct
        self.assertEqual(len(apps), 2)
        apps = jt.build_applications([
            self.notice("Zoom Communications", "Engineer", "applied", ms(2026, 10, 20), "1"),
            self.notice("Zoom", "Engineer", "applied", ms(2026, 10, 21), "2"),
        ])
        self.assertEqual((len(apps), apps[0]["company"]), (1, "Zoom"))

    def test_non_job_notices_ignored_and_result_sorted_newest_first(self):
        junk = dict(self.notice("Nope", None, "applied", ms(2026, 10, 29), "9"), job=False)
        apps = jt.build_applications([
            self.notice("Old", "Engineer", "applied", ms(2026, 10, 20), "1"),
            self.notice("New", "Engineer", "applied", ms(2026, 10, 28), "2"),
            junk,
        ])
        self.assertEqual([a["company"] for a in apps], ["New", "Old"])

    def test_item_only_kept_for_pending(self):
        pending = dict(self.notice("Acme", "Engineer", "pending", ms(2026, 10, 20), "1"), item="Do the thing")
        self.assertEqual(jt.build_applications([pending])[0]["item"], "Do the thing")
        done = dict(pending, id="2", thread="2", ts=ms(2026, 10, 21), status="applied")
        self.assertIsNone(jt.build_applications([pending, done])[0]["item"])

    def test_empty(self):
        self.assertEqual(jt.build_applications([]), [])


class RequisitionGrouping(unittest.TestCase):
    def test_same_requisition_written_differently_is_one_application(self):
        _, apps = jt.scan_messages([
            msg("Thank you for your application for the Senior Software Development Engineer - R-111084",
                "Thank you for your application for the Senior Software Development Engineer - R-111084.",
                "no-reply@acme.com", mid="a", when=ms(2026, 11, 3)),
            msg("Acme | Hiring Manager Screen Confirmation | R-111084 Senior Software Development Engineer",
                "I'm pleased to confirm your interview for the Senior Software Development Engineer role.",
                "recruiter@acme.com", mid="b", when=ms(2026, 11, 6)),
        ])
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["status"], "interviewing")

    def test_notice_without_title_joins_by_requisition(self):
        _, apps = jt.scan_messages([
            msg("Thank you for your application for the Backend Engineer - R-200001",
                "Thank you for your application for the Backend Engineer - R-200001.", "no-reply@acme.com", mid="a",
                when=ms(2026, 11, 1)),
            msg("Thank you for your application for the Frontend Engineer - R-200002",
                "Thank you for your application for the Frontend Engineer - R-200002.", "no-reply@acme.com", mid="b",
                when=ms(2026, 11, 2)),
            msg("Phone Screen | For: R-200001", "We received your application. Please let me know three dates and "
                "times that work for a call.", "recruiter@acme.com", mid="c", when=ms(2026, 11, 3)),
        ])
        backend = next(a for a in apps if "Backend" in a["position"])
        self.assertEqual(backend["status"], "scheduling")
        self.assertEqual(next(a for a in apps if "Frontend" in a["position"])["status"], "applied")

    def test_reminders_follow_their_task_and_do_not_reopen_a_rejection(self):
        _, apps = jt.scan_messages([
            msg("Thank you for applying to Acme!", "We received your application for the Backend Engineer role at Acme.",
                "no-reply@acme.com", mid="a1", when=ms(2026, 10, 29, 10)),
            msg("Next Step: Complete the Acme coding assessment", "Please complete the coding assessment for your application.",
                "no-reply@codesignal.com", mid="t1", when=ms(2026, 10, 29, 11)),
            msg("Thank you for your interest in Acme!", "Unfortunately we decided not to move forward with your "
                "application for Backend Engineer.", "no-reply@acme.com", mid="r1", when=ms(2026, 11, 3, 10)),
            msg("Final Reminder: Your Acme coding assessment expires soon!", "Please complete the coding assessment "
                "for your application. Sent from Acme through CodeSignal.", "no-reply@codesignal.com", mid="t2",
                when=ms(2026, 11, 3, 12)),
            msg("Thank you for applying to Acme!", "We received your application for the Frontend Engineer role at Acme.",
                "no-reply@acme.com", mid="a2", when=ms(2026, 11, 5, 10)),
            msg("Coding Assessment from Acme has expired", "Your invitation to complete the coding assessment for your "
                "application has expired.", "no-reply@codesignal.com", mid="x", when=ms(2026, 11, 5, 11)),
        ])
        backend = next(a for a in apps if "Backend" in a["position"])
        frontend = next(a for a in apps if "Frontend" in a["position"])
        self.assertEqual(backend["status"], "rejected")
        self.assertEqual([n["status"] for n in backend["notices"]][-1], "expired")
        self.assertEqual(frontend["status"], "applied")

    def test_pos_key_ignores_requisition_numbers_and_trailing_words(self):
        self.assertEqual(jt.pos_key("R-111084 Senior Software Development Engineer"),
                         jt.pos_key("Sr Software Development Engineer - R-111084"))
        self.assertEqual(jt.pos_key("Software Engineer, AI Agents opportunity"), jt.pos_key("Software Engineer, AI Agents"))
        self.assertEqual(jt.pos_key("Staff Engineer (Ref ID: 47964)"), jt.pos_key("Staff Engineer"))


class Scanning(unittest.TestCase):
    MESSAGES = [
        msg("Thank you for applying to Acme", "We received your application for Backend Engineer at Acme.",
            "jobs@acme.com", mid="a", when=ms(2026, 10, 20)),
        msg("Acme Application Update", "Unfortunately we decided not to move forward with your application "
            "for Backend Engineer.", "jobs@acme.com", mid="b", when=ms(2026, 10, 28)),
        msg("Hello", "Lunch?", "friend@example.org", mid="c", when=ms(2026, 10, 29)),
        msg("Old", "We received your application.", "jobs@oldco.com", mid="d", when=ms(2026, 10, 1)),
    ]

    def test_scan_filters_by_utc_window(self):
        notices, _ = jt.scan_messages(self.MESSAGES, since="2026-10-20")
        self.assertEqual({n["id"] for n in notices}, {"a", "b", "c"})
        notices, _ = jt.scan_messages(self.MESSAGES, since="2026-10-20", before="2026-10-28")
        self.assertEqual({n["id"] for n in notices}, {"a"})              # 'before' is exclusive of that day

    def test_scan_groups_messages_into_one_application(self):
        _, apps = jt.scan_messages(self.MESSAGES, since="2026-10-20")
        self.assertEqual(len(apps), 1)
        self.assertEqual((apps[0]["company"], apps[0]["status"]), ("Acme", "rejected"))

    def test_scan_without_dates_reads_everything(self):
        notices, _ = jt.scan_messages(self.MESSAGES)
        self.assertEqual(len(notices), 4)


class Rendering(unittest.TestCase):
    def setUp(self):
        _, self.apps = jt.scan_messages(Scanning.MESSAGES, since="2026-10-20")

    def test_json_with_and_without_meta(self):
        self.assertEqual(json.loads(jt.render(self.apps, "json")), self.apps)
        wrapped = json.loads(jt.render(self.apps, "json", {"account": "me@example.com"}))
        self.assertEqual((wrapped["account"], wrapped["count"]), ("me@example.com", 1))
        self.assertEqual(wrapped["applications"], self.apps)

    def test_csv_has_header_and_one_row_per_application(self):
        rows = list(csv.reader(io.StringIO(jt.render(self.apps, "csv"))))
        self.assertEqual(rows[0][:3], ["company", "position", "status"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "Acme")

    def test_table(self):
        table = jt.render(self.apps, "table")
        self.assertIn("Company", table)
        self.assertIn("Acme", table)
        self.assertIn("rejected", table)

    def test_empty_table(self):
        self.assertEqual(jt.render([], "table"), "No job applications found.")


class Overrides(unittest.TestCase):
    def apps(self):
        _, apps = jt.scan_messages(Scanning.MESSAGES, since="2026-10-20")
        return apps

    def test_override_replaces_current_status_but_not_the_emails(self):
        apps = jt.apply_overrides(self.apps(), [{"company": "acme", "status": "unknown", "note": "not confirmed"}])
        self.assertEqual((apps[0]["status"], apps[0]["manual_status"], apps[0]["status_note"]),
                         ("unknown", True, "not confirmed"))
        self.assertEqual([n["status"] for n in apps[0]["notices"]], ["applied", "rejected"])

    def test_position_narrows_the_match(self):
        other = [{"company": "Acme", "position": "Data Analyst", "status": "offer"}]
        self.assertEqual(jt.apply_overrides(self.apps(), other)[0]["status"], "rejected")
        same = [{"company": "Acme", "position": "backend engineer", "status": "offer"}]
        self.assertEqual(jt.apply_overrides(self.apps(), same)[0]["status"], "offer")

    def test_other_companies_untouched(self):
        apps = jt.apply_overrides(self.apps(), [{"company": "Globex", "status": "offer"}])
        self.assertNotIn("manual_status", apps[0])

    def test_pending_item_cleared_unless_status_is_pending(self):
        app = {"company": "Acme", "position": None, "status": "pending", "item": "Do it", "notices": []}
        self.assertIsNone(jt.apply_overrides([dict(app)], [{"company": "Acme", "status": "unknown"}])[0]["item"])
        self.assertEqual(jt.apply_overrides([dict(app)], [{"company": "Acme", "status": "pending"}])[0]["item"], "Do it")

    def test_load_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "o.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([{"company": "A", "status": "unknown"}, {"company": "B", "status": "bogus"}, {"status": "offer"}, 5], fh)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                got = jt.load_overrides(path)
            self.assertEqual(got, [{"company": "A", "status": "unknown"}])
            self.assertEqual(err.getvalue().count("ignored override"), 3)
            self.assertEqual(jt.load_overrides(os.path.join(d, "missing.json")), [])

    def test_command_line_applies_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            emails, ov = os.path.join(d, "e.json"), os.path.join(d, "o.json")
            with open(emails, "w", encoding="utf-8") as fh:
                json.dump({"messages": Scanning.MESSAGES}, fh)
            with open(ov, "w", encoding="utf-8") as fh:
                json.dump([{"company": "Acme", "status": "unknown"}], fh)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                jt.main(["scan", emails, "--since", "2026-10-20", "--format", "json", "--overrides", ov])
            self.assertEqual(json.loads(buf.getvalue())["applications"][0]["status"], "unknown")


class Files(unittest.TestCase):
    def write(self, d, name, data):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_load_messages_accepts_list_or_wrapped(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(jt.load_messages(self.write(d, "a.json", [{"id": "1"}])), [{"id": "1"}])
            self.assertEqual(jt.load_messages(self.write(d, "b.json", {"messages": [{"id": "2"}]})), [{"id": "2"}])

    def test_load_account(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(jt.load_account(self.write(d, "a.json", {"account": "me@example.com"})), "me@example.com")
            self.assertIsNone(jt.load_account(self.write(d, "b.json", [])))


class Validation(unittest.TestCase):
    def run_validate(self, expected):
        with tempfile.TemporaryDirectory() as d:
            ep, xp = os.path.join(d, "e.json"), os.path.join(d, "x.json")
            with open(ep, "w", encoding="utf-8") as fh:
                json.dump({"messages": Scanning.MESSAGES}, fh)
            with open(xp, "w", encoding="utf-8") as fh:
                json.dump(expected, fh)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = jt.validate(ep, xp)
        return rc, buf.getvalue()

    GOOD = {"since": "2026-10-20",
            "emails": {"a": {"job": True, "status": "applied"}, "b": {"job": True, "status": "rejected"},
                       "c": {"job": False}},
            "applications": [{"company": "Acme", "position": "Backend Engineer", "status": "rejected",
                              "last_date": "2026-10-28"}]}

    def test_matching_labels_pass(self):
        rc, out = self.run_validate(self.GOOD)
        self.assertEqual(rc, 0, out)
        self.assertIn("No differences", out)

    def test_wrong_status_and_missing_app_are_reported(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["emails"]["a"]["status"] = "offer"
        bad["applications"][0]["company"] = "Globex"
        rc, out = self.run_validate(bad)
        self.assertEqual(rc, 1)
        self.assertIn("STATUS", out)
        self.assertIn("APP MISSING", out)
        self.assertIn("APP EXTRA", out)

    def test_alternative_labels_accepted_and_counted_separately(self):
        alt = json.loads(json.dumps(self.GOOD))
        alt["emails"]["b"] = {"job": True, "status": "interviewing", "status_alt": ["rejected"]}
        rc, out = self.run_validate(alt)
        self.assertEqual(rc, 0, out)
        self.assertIn("only via an alternative label", out)

    def test_false_positive_and_miss_are_reported(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["emails"]["c"] = {"job": True, "status": "applied"}     # we dropped it
        bad["emails"]["a"] = {"job": False}                         # we kept it
        rc, out = self.run_validate(bad)
        self.assertEqual(rc, 1)
        self.assertIn("MISSED", out)
        self.assertIn("FALSE+", out)

    def test_job_null_is_not_scored(self):
        skip = json.loads(json.dumps(self.GOOD))
        skip["emails"]["c"] = {"job": None}
        rc, out = self.run_validate(skip)
        self.assertEqual(rc, 0, out)
        self.assertIn("1 chatty replies not scored", out)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.emails = os.path.join(self.tmp.name, "emails.json")
        with open(self.emails, "w", encoding="utf-8") as fh:
            json.dump({"account": "me@example.com", "messages": Scanning.MESSAGES}, fh)

    def run_main(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = jt.main(list(argv))
        return rc, buf.getvalue()

    def test_scan_prints_table(self):
        rc, out = self.run_main("scan", self.emails, "--since", "2026-10-20")
        self.assertEqual(rc, 0)
        self.assertIn("Acme", out)

    def test_scan_json_to_file_has_run_details(self):
        out_path = os.path.join(self.tmp.name, "applications.json")
        rc, out = self.run_main("scan", self.emails, "--since", "2026-10-20", "--format", "json", "--out", out_path)
        self.assertEqual((rc, out), (0, ""))
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual((data["account"], data["since"], data["messages_read"], data["count"]),
                         ("me@example.com", "2026-10-20", 4, 1))
        self.assertTrue(data["generated_at"])

    def test_unencodable_characters_do_not_crash_console_output(self):
        path = os.path.join(self.tmp.name, "emoji.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([msg("Thank you for applying to Acme", "We received your application \U0001F44B",
                           "jobs@acme.com")], fh, ensure_ascii=False)
        raw = io.BytesIO()
        cp1252 = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(cp1252):
            self.assertEqual(jt.main(["scan", path]), 0)
        cp1252.flush()
        self.assertIn(b"Acme", raw.getvalue())

    def test_scan_csv(self):
        _, out = self.run_main("scan", self.emails, "--format", "csv")
        self.assertTrue(out.startswith("company,position,status"))

    def test_bad_arguments_exit(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            jt.main(["scan"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            jt.main(["scan", self.emails, "--format", "xml"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
