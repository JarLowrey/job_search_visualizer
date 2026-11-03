#!/usr/bin/env python3
"""
job_tracker.py - find your job applications in an email export and report each one's
company, position and current status. Rules only (regular expressions); no AI involved.

  python gmail_scraper.py --since 2026-10-20 --out emails.json   # download
  python job_tracker.py scan emails.json [--since D] [--before D] [--format table|json|csv]  # sort
  python job_tracker.py imap --since 2026-10-20                  # both in one go
  python job_tracker.py validate emails.json expected.json                            # check vs hand labels

How it works
  1. Each message is checked: is it a status notice about an application you made?
     Sent mail, job-board marketing, account/security mail and cold recruiter outreach are dropped.
  2. Each notice gets a status: rejected, withdrawn, accepted, offer, interviewing,
     pending (something is waiting on you), referred or applied (received/confirmed) or unknown (mentions an application, says nothing about its status).
  3. Company and position are pulled from the subject, the text and the signature.
  4. Notices are grouped into applications (company + position). An application's status is
     the status of its most recent notice.

Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import textwrap
from email.utils import parseaddr

# --------------------------------------------------------------------------- statuses

# Within one email, the first status in this list that matches wins.
STATUS_ORDER = ["rejected", "withdrawn", "accepted", "offer", "interviewing", "referred", "pending", "applied"]

# "Unfortunately" also opens "unfortunately both of our recruiters are out of the office", so on its own it is
# not a rejection. It counts when the same sentence says what was decided.
_REJECT_CONTEXT = (
    r"(?:(?:is not|isn't|are not|aren't|not) (?:the |a )?(?:right |good |best |perfect )?(?:fit|match)"
    r"|not (?:be |been )?(?:selected|considered|moving|proceeding|advancing|continuing)"
    r"|not able to (?:move|proceed|offer)|no longer|unable to|(?:cannot|can't) (?:move|proceed|offer|consider)"
    r"|will not|won't be|decided|other candidates?|another candidate|unsuccessful|chosen"
    r"|(?:position|role|job|opening) (?:has been |was |is )?(?:filled|closed))")

PATTERNS = {
    "rejected": [
        rf"\bunfortunately\b(?=[^.!?]*\b{_REJECT_CONTEXT})",
        r"\b(?:is not|isn't|are not|aren't|not) (?:the |a )?(?:right |good |best |perfect )?(?:fit|match)\b",
        r"\blooking for (?:someone|a candidate|candidates) (?:with|who|that)\b[^.!?]{0,60}\b(?:more|different|stronger|deeper|relevant)\b",
        r"\bgood luck (?:with|in) your (?:job |career )?search\b",
        r"\bwish you (?:the best|every success|all the best|luck)\b[^.!?]{0,50}\b(?:search|future|endeavou?rs?)\b",
        r"\bnot (?:to )?(?:move|moving|proceed|proceeding|continue|continuing) forward\b",
        r"\bdecided (?:not to|to (?:move|proceed|go) (?:forward )?with (?:other|another))",
        r"\b(?:don't|do not) have a match\b",
        r"\bnot (?:been )?selected\b",
        r"\bno longer (?:being )?considered\b",
        r"\bregret to (?:inform|tell)\b",
        r"\b(?:position|role|job|opening) (?:has been|is|was) (?:filled|closed|cancell?ed|put on hold)\b",
        r"\bwill not be (?:moving|proceeding)\b",
        r"\bunable to (?:offer|move|proceed)\b",
        r"\bpursu(?:e|ing) (?:other |another )?candidates?\b",
        r"\bmore closely match\b",
        r"\bdisappointing news\b",
        r"\bsorry to (?:inform|tell|let)\b",
        r"\b(?:do|does) not (?:currently )?have an? (?:open|available|suitable|matching) (?:role|position|opening)s?\b",
        r"\bno (?:open|available|suitable) (?:role|position|opening)s?\b",
    ],
    "withdrawn": [
        r"\bapplication (?:has been|was|is) withdrawn\b",
        r"\bwithdraw(?:n|al) (?:of )?your application\b",
        r"\bconfirm(?:ing)? (?:your )?withdrawal\b",
    ],
    "accepted": [
        r"\bwelcome (?:aboard|to the team)\b",
        r"\bexcited to have you (?:join|on board)\b",
        r"\byour start date\b",
        r"\bthank(?:s| you) for accepting (?:our|the) offer\b",
    ],
    "offer": [
        r"\bpleased to (?:offer|extend)\b",
        r"\b(?:extend|extending) (?:you )?(?:an|the) offer\b",
        r"\boffer of employment\b",
        r"\boffer letter\b|\bhire letter\b",
        r"\bwould like to offer you\b",
        r"\b(?:congratulations|congrats)\b[^.!?]{0,60}\boffer\b",
        r"\breceiving an offer\b",
    ],
    "interviewing": [
        r"\b(?:schedule|scheduled|book|arrange|set up) (?:an?|your|the|a time for)? ?(?:[\w-]+ ){0,3}?(?:interview|call|chat|screen|conversation)\b",
        r"\binvite you to (?:an? |the )?(?:[\w-]+ ){0,2}?(?:interview|screen)\b",
        r"\b(?:your|the) interview (?:is|has been|will be) (?:scheduled|confirmed|set)\b",
        r"\b(?:select|pick|choose) (?:a|your) (?:time|slot|date)\b",
        r"\binterview (?:invitation|invite|confirmation)\b",
        r"\bmove (?:you )?(?:forward )?to (?:the )?(?:next (?:round|stage)|interview)\b",
        r"\badvance(?:d)? (?:you )?to (?:the )?(?:next|interview|onsite|final)\b",
        r"\bmaking it to (?:the |our )?(?:final|next|second|third|on-?site)\b",
        r"\bfinal (?:interview|round)\b",
        r"\bconfirm your interview\b",
        r"\b(?:you(?:'re| are)|been) invited to (?:an? |the |our )?(?:[\w-]+ ){0,2}?(?:interview|screen|call|chat|conversation)\b",
        r"\bavailable (?:for|to) (?:an? |a quick |a short |a brief )?(?:[\w-]+ ){0,2}?(?:interview|call|chat|screen|conversation)\b",
        r"\bconfirmed you for (?:an? |the |your )?(?:[\w-]+ ){0,2}?interviews?\b",
        r"\binterview (?:schedule|details|logistics)\b",
        r"\byour (?:upcoming |scheduled )?(?:on-?site|virtual|phone|video|technical|panel) interviews?\b",
        r"\bthank(?:s| you)\b[^.!?]{0,50}\b(?:speak(?:ing)?|talk(?:ing)?|meet(?:ing)?|chat(?:ting)?|interview(?:ing)?) with\b",
    ],
    "pending": [
        r"\b(?:complete|take|finish|submit|start) (?:the |your |this |these |those |a |an |our |some )?(?:[\w'-]+ ){0,5}?(?:assessment|coding challenge|take-?home|questionnaire|screening questions|application questions|exercise|scenarios?|work (?:sample|simulation)|simulation|case study|homework|screen)\b",
        r"\b(?:provide|submit|send|share|complete|answer|upload|fill)\s+(?:out\s+)?(?:some |the |any )?(?:additional|more|further|requested|required)\s+(?:application\s+)?(?:questions|information|details|documents|materials)\b",
        r"\baction (?:is )?required\b",
        r"\b(?:please|kindly) (?:reply|respond|upload|provide|send|submit|fill|confirm|complete|review and sign)\b",
        r"\b(?:is|are) (?:still )?(?:waiting|awaiting)(?: for| on)? your\b",
        r"\b(?:haven't|have not) (?:yet )?(?:seen|received|heard)\b[^.!?]{0,60}\byour\b",
    ],
    "referred": [
        r"\byou(?:'ve| have) been referred\b",
        r"\b(?:employee )?referral\b[^.!?\n]{0,120}\b(?:application|candidacy|candidate|position|role)\b",
        r"\b(?:application|candidacy|candidate)\b[^.!?\n]{0,120}\b(?:employee )?referral\b",
        r"\breferred by\b[^.!?\n]{0,100}\b(?:apply|application|candidate|position|role)\b",
    ],
    "applied": [
        r"\b(?:received|have received|has been received) (?:your|the) (?:job )?application\b",
        r"\b(?:your|the) application (?:has been|was|is) (?:received|submitted|successfully)",
        r"\bthank(?:s| you) (?:so much )?for (?:applying|submitting|your application|your interest)\b",
        r"\bwe(?:'ve| have| just| recently)? received your\b",
        r"\bapplication (?:sent|submitted)\b",
        r"\bapplication\b[^.!?\n]{0,100}\bwas (?:sent|submitted|received)\b",
        r"\bconfirm(?:ing)? (?:that )?we(?:'ve| have)? received\b",
        r"\bapplication (?:has been )?received\b",
        r"\b(?:review|reviewing) your application\b",
        r"\bsubmitting your application\b",
        r"\byou(?:'ve| have) (?:successfully )?applied\b",
    ],
}
PATTERNS = {k: [re.compile(p, re.I) for p in v] for k, v in PATTERNS.items()}

# "We're excited to move forward" says you advanced but not what happens next. It only decides the
# status when nothing more concrete (an interview, a task waiting on you) is found in the email.
WEAK_INTERVIEWING = [re.compile(p, re.I) for p in (
    r"\b(?:decided|happy|pleased|excited|glad|like|want(?:ed)?) to (?:move|proceed|continue|go) (?:forward|ahead|on)\b",
    r"\bexcited to continue with you\b",
)]

# A sentence with one of these words describes something that might happen, not something that did.
CONDITIONAL = re.compile(r"\b(?:if|should|in case|unless|whether|once|when)\b", re.I)
# ...but these are politeness, not conditions: "send times that work, if at all possible".
POLITE = re.compile(
    r"\bif (?:at all |that is |that's )?possible\b|\bif (?:that|this|it) works\b"
    r"|\bwhen(?:ever)? you(?:'re| are) (?:ready|available|free)\b|\bwhen you (?:get|have) (?:a|the) (?:chance|moment|minute)\b"
    r"|\bif you(?:'d| would) (?:like|prefer)\b|\bif you(?:'re| are) (?:interested|available|able|open)\b", re.I)


def is_conditional(sentence: str) -> bool:
    return bool(CONDITIONAL.search(POLITE.sub(" ", sentence)))

# Words that show a candidate is being talked to about their own application.
APP_REF = re.compile(
    r"\b(?:your|the|an?|my)\s+(?:job\s+|recent\s+)?application\b|\bapplying (?:for|to)\b|\bapplied\b"
    r"|\byour candidacy\b|\bapplication questions\b|\byou(?:'ve| have) been referred\b"
    r"|\byour (?:interview\w*|resume|cv|offer)\b|\bmaking it to\b|\bfinal (?:interview|round)\b|\binterview process\b"
    r"|\b(?:your|the|our)\s+(?:[\w-]+\s+){0,3}(?:assessment|coding challenge|take-?home)\b"
    r"|\bfor applying\b|\bapplying with\b|\byour (?:[\w-]+\s+){1,2}interview\w*"
    r"|\binterview (?:schedule|details|logistics)\b|\bconfirmed you for\b", re.I)
# "the Online Mentor role", "our Software Development Engineer position" (capitalised title, so case-sensitive)
ROLE_REF = re.compile(r"\b(?:the|our|this)\s+(?:[A-Z][\w/+#.&-]*\s+){1,5}(?:role|position)\b")

# ------------------------------------------------------------------ who sends what

ATS_DOMAINS = {
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com", "myworkday.com", "workday.com",
    "myworkdayjobs.com", "icims.com", "smartrecruiters.com", "jobvite.com", "workablemail.com", "workable.com",
    "bamboohr.com", "recruitee.com", "teamtailor.com", "successfactors.com", "taleo.net", "shazamme.com",
    "codesignal.com", "hackerrank.com", "codility.com", "karat.com", "hirevue.com",
}
AUTOMATED_LOCAL = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|notifications?|careers?|recruit\w*|talent\w*|"
    r"jobs?|hiring|hr|team|hello|info|support|admin|mailer|candidates?|applications?)\b", re.I)

AUTOMATED_ANYWHERE = re.compile(r"(?:recruiting|recruitment|careers?)$|no[-_.]?reply|do[-_.]?not[-_.]?reply", re.I)
# Services that carry other companies' mail: never the employer, so never used as the company name.
THIRD_PARTY_DOMAINS = {
    "docusign.net", "docusign.com", "goodtime.io", "woventeams.com", "talentwise.com", "indeedemail.com",
    "governmentjobs.com", "stackoverflow.com", "linkedin.com", "calendly.com", "zoom.us", "adobesign.com",
}

# Senders that never write about your applications. Add your own.
IGNORE_SENDERS = [re.compile(p, re.I) for p in (
    r"@(?:[\w-]+\.)*(?:github\.com|steampowered\.com|stackoverflow\.email|ollama\.com|greenhouse-jobs\.com)$",
    r"^(?:no-?reply@accounts\.google\.com|noreply-accounts@google\.com|cloudplatform-noreply@google\.com)$",
)]
# Subjects of account / marketing mail.
IGNORE_SUBJECT = re.compile(
    r"\bverify\b|verification|\bpassword\b|security (?:alert|code)|sign.?in\b|one-?time|dream job|job alert|jobs? for you"
    r"|recommended|newsletter|\bdigest\b", re.I)

TITLE_WORDS = re.compile(
    r"\b(?:engineer\w*|developer|manager|designer|scientist|analyst|architect|director|lead|interns?|internship|specialist|"
    r"consultant|researcher|recruiter|administrator|programmer|swe|sde|sre|technician|associate|officer|head of|"
    r"vp|president|writer|counsel|accountant|representative|strategist|coordinator|product|software|data|staff|"
    r"principal|senior|sr|jr|junior)\b", re.I)

# ------------------------------------------------------------------ text helpers

def norm_quotes(s: str) -> str:
    return (s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
             .replace("\xa0", " "))


def clean_body(raw: str) -> str:
    """Plain text without quoted reply history, link syntax or table junk."""
    t = norm_quotes(html.unescape(raw or "")).replace("\r\n", "\n").replace("\r", "\n")
    # everything after "On <date>, <name> wrote:" is quoted history (the header may wrap over two lines)
    t = re.split(r"\n[ \t]*On [^\n]{0,200}(?:\n[^\n]{0,120})?wrote:[ \t]*(?:\n|$)", "\n" + t, maxsplit=1)[0]
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)          # [text](url) -> text
    t = re.sub(r"\(?\s*<?https?://[^\s)>]+>?\s*\)?", " ", t)  # bare urls
    lines = []
    for ln in t.split("\n"):
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.fullmatch(r"[|\s*_=\-]*", s):
            lines.append("")
            continue
        s = s.strip("|* ").strip()
        lines.append(re.sub(r"[ \t]+", " ", s))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def sentences(text: str) -> list[str]:
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"\s*\n\s*", " ", para).strip()
        out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip())
    return out


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def sender_parts(sender: str) -> tuple[str, str]:
    _, addr = parseaddr(sender or "")
    local, _, domain = addr.lower().partition("@")
    return local, domain


def registrable(domain: str) -> str:
    p = domain.split(".")
    if len(p) >= 3 and len(p[-1]) == 2 and p[-2] in ("co", "com", "org", "ac", "gov"):
        return ".".join(p[-3:])
    return ".".join(p[-2:]) if len(p) >= 2 else domain


def is_automated(local: str, domain: str) -> bool:
    return (registrable(domain) in ATS_DOMAINS or bool(AUTOMATED_LOCAL.match(local))
            or bool(AUTOMATED_ANYWHERE.search(local)))


# ------------------------------------------------------------------ extraction

CAP = r"(?:[A-Z]|\d+[A-Z])[\w&'-]*(?:\.[\w&'-]+)*"
JOIN = r"(?:(?:of|and|the)\s+)?"            # "City of Seattle", "Bank of America"
NAME = rf"({CAP}(?:\s+{JOIN}{CAP}){{0,3}})"
NAME_LAZY = rf"({CAP}(?:\s+{JOIN}{CAP}){{0,2}}?)"


# Words that start a phrase ("Job Application received", "Thank you...") but are never a company.
NOT_A_COMPANY = {"job", "your", "new", "thank", "thanks", "we", "our", "my", "this", "the", "a", "an", "re", "fw",
                 "offer", "interview", "application", "assessment", "update", "invitation", "request", "reminder",
                 "next", "action", "important", "congratulations", "congrats", "welcome", "hello", "hi", "dear",
                 "success", "status", "completed", "technical", "phone"}
GENERIC_END = {"interview", "assessment", "application", "position", "role", "job", "opportunity", "update",
               "request", "confirmation", "invitation", "challenge", "department", "team"}


WEAK_TITLE_WORDS = {"lead", "product", "software", "data", "staff", "principal", "senior", "sr", "jr", "junior", "head"}
# nouns that name a job; the weak words above can also sit inside a company name ("JBS Custom Software Solutions")
STRONG_TITLE_WORDS = re.compile(
    r"\b(?:engineer\w*|developer|manager|designer|scientist|analyst|architect|director|interns?|internship|specialist|consultant|"
    r"researcher|recruiter|administrator|programmer|swe|sde|sre|technician|associate|officer|vp|president|writer|"
    r"counsel|accountant|representative|strategist|coordinator)\b", re.I)


def usable_company(name: str) -> bool:
    words = name.split()
    if not words:
        return False
    if len(words) > 1 and words[0].lower() == "the":          # "The Humane League"
        words = words[1:]
    first, last = words[0].lower().strip(".,'"), words[-1].lower().strip(".,'")
    return (first not in NOT_A_COMPANY and last not in GENERIC_END and first not in WEAK_TITLE_WORDS
            and not STRONG_TITLE_WORDS.search(" ".join(words)))


# Job boards that appear in a subject next to the employer: "DroneSeed Angelist Application".
PLATFORM_WORDS = {"angelist", "angellist", "wellfound", "indeed", "linkedin", "glassdoor", "ziprecruiter", "dice",
                  "monster", "handshake", "simplyhired", "builtin", "triplebyte", "otta", "stackoverflow"}


def tidy_company(name: str) -> str:
    name = re.sub(r"'s\b.*$", "", name).strip(" .,-")
    words = name.split()
    kept = [w for w in words if w.lower() not in PLATFORM_WORDS]
    return " ".join(kept) if kept else name


def extract_company(subject: str, text: str, local: str, domain: str) -> str:
    return _extract_company(subject, text, local, domain)[0]


def _extract_company(subject: str, text: str, local: str, domain: str) -> tuple[str, bool]:
    """(name, guessed_from_sender_domain)"""
    f = flat(text)
    subject = re.sub(r"^(?:\s*(?:re|fw|fwd)\s*:|\s*\[[^\]]*\])+\s*", "", subject, flags=re.I)
    # "... application for <title, may contain commas> at <Company> was sent" (subject) and Indeed's footer
    # "This email relates to your application for <title> at <Company>, submitted on Indeed." (body)
    at_end = r"(?=\s+(?i:was|has|is|have)\b|\s*[-–—|!.,;:]|\s*$)"
    for src, lead in ((subject, r"(?i:for|as)"),
                      (f, r"(?i:(?:application|applying|applied|apply|candidacy|position|role))\s+(?i:for|as)")):
        m = re.search(rf"\b{lead}\s+(?i:the\s+|an?\s+)?(.{{3,120}}?)\s+(?i:at|with)\s+{NAME}{at_end}", src)
        if m and TITLE_WORDS.search(m.group(1)) and usable_company(tidy_company(m.group(2))):
            return tidy_company(m.group(2)), False
    cands = []
    # subject: "OpenAI Application Update", "Thank you for applying to Docker!", "application for Circle!"
    cands.append(re.search(rf"^\W*{NAME_LAZY}\s+(?:Application|Candidate|Careers|Recruiting|Hiring)\b", subject))
    cands.append(re.search(rf"(?i:applying|applied|application|interest|apply)\s+(?i:to|at|with|in)\s+(?i:the\s+)?(?i:our\s+)?{NAME}", subject))
    cands.append(re.search(rf"(?i:complete|take)\s+(?i:the)\s+{NAME_LAZY}\s+(?i:(?:industry|technical|online|coding)\s+)*(?i:assessment|challenge)", subject))
    cands.append(re.search(rf"(?i:application|request|update|assessment|interview|invitation)\s+(?i:for)\s+(?i:the\s+)?{NAME}", subject))
    cands.append(re.search(rf"(?i:your)\s+{NAME_LAZY}\s+(?i:application)\b", subject))           # "your Encamp application"
    cands.append(re.search(rf"(?i:offers?)\s+(?i:from|at|with)\s+{NAME}", subject))             # "offer from 2U"
    cands.append(re.search(rf"^\W*{NAME_LAZY}\s+[-–—|:]\s+\S", subject))                         # "Twitch - Request for ..."
    cands.append(re.search(rf"(?i:position|role|job|opening)\s+(?i:at|with)\s+{NAME}", subject))  # "Application for the position at X"
    for m in cands:
        if m and usable_company(tidy_company(m.group(1))):
            return tidy_company(m.group(1)), False
    # "... application for Senior Software Engineer at Keona Health was sent"
    m = re.search(rf"\b(?i:for|as)\s+(?i:the\s+|an?\s+)?([^,.!?]{{3,90}}?)\s+(?i:at|with)\s+{NAME}", subject)
    if m and TITLE_WORDS.search(m.group(1)) and usable_company(tidy_company(m.group(2))):
        return tidy_company(m.group(2)), False
    # body: "Sent from Airbnb through CodeSignal", "applying to Shopify", "role at Encamp", "Encamp has reviewed"
    body_pats = [
        rf"Sent from {NAME_LAZY} through\b",
        rf"(?i:applying|applied|apply|interest)\s+(?i:to|at|with|in)\s+(?i:the\s+)?(?i:our\s+)?{NAME}",
        rf"(?i:role|position|career|job|opening|opportunity|opportunities)s?\s+(?i:at|with)\s+(?i:the\s+)?{NAME}",
        rf"(?i:position|role)\b[^.]{{0,120}}?\s(?i:with|at)\s+(?i:the\s+)?{NAME}",
        rf"{NAME_LAZY}\s+has\s+(?i:reviewed|received|decided)\b",
    ]
    for p in body_pats:
        for m in re.finditer(p, f):
            if usable_company(tidy_company(m.group(1))):
                return tidy_company(m.group(1)), False
    sig = [
        rf"(?m)\bThe\s+{NAME_LAZY}\s+(?:(?:Recruiting|Recruitment|Talent Acquisition|Talent|Careers|Hiring|People)\s+)?(?i:Team)\b",
        rf"(?m)^[\s\-–—]*{NAME_LAZY}\s+(?:Recruiting(?: Team)?|Recruitment|Talent Acquisition(?: Team)?|Careers(?: Team)?|Hiring Team)\s*$",
        rf"(?m)^\s*({CAP})\s+Team\s*$",
    ]
    for p in sig:
        m = re.search(p, text)
        if m and usable_company(tidy_company(m.group(1))):
            return tidy_company(m.group(1)), False
    # "invited to an interview with Twitch!", "Thanks for taking the time to interview with Indeed!"
    for src in (subject, f):
        m = re.search(rf"\b(?i:interviews?|screen|call|chat)\s+(?i:with)\s+(?i:the\s+)?{NAME}", src)
        if m and usable_company(tidy_company(m.group(1))):
            return tidy_company(m.group(1)), False
    reg = registrable(domain)
    if reg not in ATS_DOMAINS and reg not in THIRD_PARTY_DOMAINS:
        base = reg.split(".")[0]
        if base and base not in ("gmail", "outlook", "yahoo", "hotmail", "icloud"):
            # the subject starts with the company's real spelling: "The Humane League ..." from thehumaneleague.org,
            # "Trilogy ..." from trilogyed.com. Take the longest run of leading capitalised words the domain agrees with.
            lead = re.match(rf"\W*((?:{CAP}\s*){{1,4}})", subject)
            if lead:
                words = lead.group(1).split()
                for k in range(len(words), 0, -1):
                    cand = " ".join(words[:k])
                    squashed = re.sub(r"[^a-z0-9]", "", cand.lower())
                    agrees = ((len(squashed) >= 4 and base.startswith(squashed))
                              or (len(base) >= 5 and squashed.startswith(base) and len(squashed) <= len(base) + 6))
                    if agrees and usable_company(cand):
                        return cand, False
            return base.title(), True
    elif reg in ATS_DOMAINS and local and not AUTOMATED_LOCAL.match(local):
        return local.title(), True
    return "Unknown", True


POSITION = re.compile(
    r"(?:(?i:application|applying|applied|apply|interview|candidacy|assessment|interest|offer)\s+(?i:for)\s+"
    r"|(?i:application|applying|interest)\s+(?i:in)\s+"
    r"|(?i:(?:following\s+)?(?:role|position))\s*:\s*)"
    r"(?i:(?:the|our|a|an)\s+)?(?i:(?:role|position)\s+(?:of|as)\s+)?"
    r"(?P<p>[^\n]+?)"
    r"(?=\s+(?i:role|position|opening|job|team)\b|\s*[.!;?](?=\s|$)|,\s+(?i:and)\b|\s+(?i:and)\s+(?i:we|are|will|our|the)\b"
    r"|\s+(?i:at)\s+[A-Z]|\s+(?i:at)\s+(?i:this|that)\b|\s+(?i:with)\s+(?i:the\s+)?[A-Z]"
    r"|\s+(?i:advertised)\b|\s*$)"
)


# "Interview Confirmation: Web Developer II"
POSITION_SUBJECT = re.compile(
    r"(?i:interview|application|phone screen|onsite|assessment)\s+(?i:confirmation|invitation|update|status|schedule|"
    r"request|details)\s*[:\-–]\s*(?P<p>[^\n]+?)\s*$")
# a labelled line: "Role: Senior Engineer", "Here is the JD we have in mind for you: Software Development Engineer"
POSITION_LINE = re.compile(
    r"(?im)\b(?:JD|job description|job title|position title|role|position)\b[^:\n]{0,50}:[ \t]*(?P<p>[^\n]{3,100})$")
# "our Software Development Engineer role", "the Web Developer II position"
POSITION_ROLE = re.compile(
    r"\b(?:the|our|this)\s+(?P<p>(?:[A-Z][\w/+#.&-]*\s+){1,6}?)(?:role|position|opening|opportunity)\b")
_NOT_A_TITLE = {"this", "that", "new", "same", "open", "following", "next", "first", "last", "other", "right"}


def _clean_position(p: str) -> str:
    return re.sub(r"\s+", " ", norm_quotes(p).strip(" \t\"'.,:;!"))


def extract_position(subject: str, text: str) -> str | None:
    m = POSITION_SUBJECT.search(subject)
    if m and TITLE_WORDS.search(m.group("p")):
        return _clean_position(m.group("p"))
    for src in (subject, flat(text)):
        for m in POSITION.finditer(src):
            p = _clean_position(m.group("p"))
            if 3 <= len(p) <= 120 and TITLE_WORDS.search(p):
                return p
    for m in POSITION_LINE.finditer(text):
        p = _clean_position(m.group("p"))
        if 3 <= len(p) <= 100 and TITLE_WORDS.search(p):
            return p
    for src in (subject, flat(text)):
        for m in POSITION_ROLE.finditer(src):
            p = _clean_position(m.group("p"))
            words = p.split()
            if words and words[0].lower() not in _NOT_A_TITLE and (TITLE_WORDS.search(p) or len(words) >= 2):
                return p
    return None


MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"


def extract_item(subject: str, text: str, match_text: str) -> str:
    """What is waiting on you, with a due date if the email gives one."""
    s = re.sub(r"^(?:\s*(?:re|fw|fwd)\s*:|\s*\[[^\]]*\]|\s*(?:action required|next steps?|reminder)\s*:)+", "", subject, flags=re.I).strip()
    if re.match(r"(?i)(?:complete|take|submit|schedule|upload|reply|respond|provide|confirm)\b", s):
        item = s
    else:
        item = match_text[:1].upper() + match_text[1:]
    m = re.search(rf"(?:expires?|due|deadline|by|before|until)\b[^.\n]{{0,60}}?\b({MONTH})\.? (\d{{1,2}})(?:st|nd|rd|th)?\b", flat(text), re.I)
    if m:
        item += f" (due {m.group(1)[:3].title()} {int(m.group(2))})"
    return item


# ------------------------------------------------------------------ one message

def classify_message(msg: dict) -> dict:
    labels = set(msg.get("labels") or [])
    sender = norm_quotes(msg.get("from") or "")
    local, domain = sender_parts(sender)
    subject = norm_quotes(html.unescape(msg.get("subject") or "")).strip()
    out = {"id": msg.get("id"), "thread": msg.get("threadId") or msg.get("id"),
           "ts": int(msg.get("internalDate") or 0), "from": sender, "subject": subject,
           "url": msg.get("url") or msg.get("viewUrl") or "", "job": False, "reason": ""}

    if labels & {"SENT", "DRAFT", "SPAM", "TRASH"}:
        out["reason"] = "sent, draft or spam"; return out
    if any(r.search(sender.lower()) or r.search(f"{local}@{domain}") for r in IGNORE_SENDERS):
        out["reason"] = "ignored sender"; return out
    if IGNORE_SUBJECT.search(subject):
        out["reason"] = "account or marketing subject"; return out

    if re.search(r"please type your reply above this line|^\[request received\]", (msg.get("body") or "")[:400] + "\n" + subject,
                 re.I | re.M):
        out["reason"] = "helpdesk ticket"; return out

    text = clean_body(msg.get("body"))
    found: dict[str, tuple[str, str]] = {}
    for status in STATUS_ORDER:
        for s in [subject] + sentences(text):
            if status != "applied" and is_conditional(s):
                continue
            m = next((m for rx in PATTERNS[status] if (m := rx.search(s))), None)
            if m:
                found[status] = (s, m.group(0))
                break

    if "interviewing" not in found and "pending" not in found:
        for sent in [subject] + sentences(text):
            m = None if is_conditional(sent) else next((m for rx in WEAK_INTERVIEWING if (m := rx.search(sent))), None)
            if m:
                found["interviewing"] = (sent, m.group(0))
                break

    automated = is_automated(local, domain)
    has_ref = bool(APP_REF.search(subject + "\n" + text))
    loose_ref = has_ref or bool(ROLE_REF.search(subject + "\n" + text) or re.search(r"\bapplication\b", subject, re.I))
    if found:
        if not automated and not loose_ref:
            out["reason"] = "personal email that never mentions an application"; return out
        status = next(s for s in STATUS_ORDER if s in found)
        evidence, matched = found[status]
    elif automated and has_ref:
        status, matched = "unknown", ""
        evidence = (sentences(text) or [subject])[0]
    else:
        out["reason"] = "no application status signal"; return out

    out.update(job=True, reason="", status=status, evidence=evidence[:240],
               position=extract_position(subject, text))
    out["company"], out["company_guessed"] = _extract_company(subject, text, local, domain)
    if status == "pending":
        out["item"] = extract_item(subject, text, matched)
    return out


# ------------------------------------------------------------------ grouping into applications

_SUFFIX = {"inc", "llc", "ltd", "corp", "co", "company", "the"}


def _tokens(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split() if t not in _SUFFIX]


def same_company(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    n = min(len(ta), len(tb))
    return n > 0 and ta[:n] == tb[:n]


def pos_key(p: str | None) -> str:
    if not p:
        return ""
    t = re.sub(r"[^a-z0-9]+", " ", p.lower().replace("–", "-").replace("—", "-"))
    return " ".join({"sr": "senior", "jr": "junior"}.get(w, w) for w in t.split())


def iso(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).date().isoformat()


def build_applications(notices: list[dict]) -> list[dict]:
    apps: list[dict] = []
    for n in sorted((n for n in notices if n["job"]), key=lambda n: n["ts"]):
        cands = [a for a in apps if same_company(a["company"], n["company"])]
        pk = pos_key(n["position"])
        app = None
        if pk:
            app = next((a for a in cands if a["pk"] == pk), None)
            if app is None:
                blank = [a for a in cands if not a["pk"]]
                if blank:                       # first notice named no role; this one does
                    app = blank[-1]; app["pk"] = pk; app["position"] = n["position"]
        else:
            app = next((a for a in reversed(apps) if n["thread"] in a["threads"]), None)
            if app is None and cands:
                app = max(cands, key=lambda a: a["notices"][-1]["ts"])
        if app is None and pk:                  # same role discussed in the same thread under another name
            app = next((a for a in apps if a["pk"] == pk and n["thread"] in a["threads"]), None)
        if app is None:
            app = {"company": n["company"], "position": n["position"], "pk": pk, "threads": set(), "notices": [],
                   "rank": (n.get("company_guessed", False), len(n["company"]))}
            apps.append(app)
        rank = (n.get("company_guessed", False), len(n["company"]))
        if rank < app["rank"]:                  # a name read from the text beats a domain guess; shorter beats longer
            app["company"], app["rank"] = n["company"], rank   # "Zoom" over "Zoom Communications"
        app["threads"].add(n["thread"])
        app["notices"].append(n)

    result = []
    for a in apps:
        last = a["notices"][-1]
        result.append({
            "company": a["company"], "position": a["position"], "status": last["status"],
            "item": last.get("item") if last["status"] == "pending" else None,
            "last_date": iso(last["ts"]), "first_date": iso(a["notices"][0]["ts"]),
            "last_subject": last["subject"], "last_notification": last["evidence"], "last_url": last["url"],
            "notices": [{"id": x["id"], "date": iso(x["ts"]), "status": x["status"], "subject": x["subject"],
                         "from": x["from"], "evidence": x.get("evidence", ""), "url": x["url"]}
                        for x in a["notices"]],
        })
    return sorted(result, key=lambda r: r["last_date"], reverse=True)


def _day_ms(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def scan_messages(messages: list[dict], since: str | None = None,
                  before: str | None = None) -> tuple[list[dict], list[dict]]:
    """Classify messages dated in [since, before) (UTC days) and group them into applications."""
    floor = _day_ms(since) if since else 0
    ceiling = _day_ms(before) if before else None
    notices = [classify_message(m) for m in messages
               if int(m.get("internalDate") or 0) >= floor
               and (ceiling is None or int(m.get("internalDate") or 0) < ceiling)]
    return notices, build_applications(notices)


# ------------------------------------------------------------------ input / output

def load_messages(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["messages"] if isinstance(data, dict) else data


def load_account(path: str) -> str | None:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("account") if isinstance(data, dict) else None


def render_table(apps: list[dict]) -> str:
    if not apps:
        return "No job applications found."
    rows = []
    for a in apps:
        status = a["status"] + (f": {a['item']}" if a.get("item") else "")
        rows.append((a["company"], a["position"] or "-", status, a["last_date"], a["last_notification"]))
    w = [max(len(r[i]) for r in rows + [("Company", "Position", "Status", "Updated", "")]) for i in range(4)]
    w = [min(w[0], 18), min(w[1], 44), min(w[2], 46), 10]
    head = ("Company", "Position", "Status", "Updated", "Latest notification")
    lines = []
    for r in [head] + rows:
        cols = [textwrap.wrap(r[i], w[i]) or [""] for i in range(4)] + [textwrap.wrap(r[4], 60)[:2] or [""]]
        for k in range(max(len(c) for c in cols)):
            lines.append("  ".join((c[k] if k < len(c) else "").ljust(w[i] if i < 4 else 0) for i, c in enumerate(cols)).rstrip())
        if r is head:
            lines.append("  ".join("-" * x for x in w) + "  " + "-" * 20)
    return "\n".join(lines)


def render(apps: list[dict], fmt: str, meta: dict | None = None) -> str:
    if fmt == "json":
        # with run details this is the file the web page loads; without them, a bare list of applications
        data = {**meta, "count": len(apps), "applications": apps} if meta else apps
        return json.dumps(data, indent=1, ensure_ascii=False)
    if fmt == "csv":
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["company", "position", "status", "item", "last_date", "last_subject", "last_notification", "last_url"])
        for a in apps:
            wr.writerow([a["company"], a["position"] or "", a["status"], a["item"] or "", a["last_date"],
                         a["last_subject"], a["last_notification"], a["last_url"]])
        return buf.getvalue()
    return render_table(apps)


# ------------------------------------------------------------------ manual overrides

OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides.json")
OVERRIDE_STATUSES = set(STATUS_ORDER) | {"unknown"}


def load_overrides(path: str | None = None) -> list[dict]:
    """Statuses you decided yourself: [{"company": "Coinbase", "position": null, "status": "unknown", "note": "..."}].
    `position` is optional (leave it out to match every application at that company). A missing file is fine."""
    path = path or OVERRIDES_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    out = []
    for o in data if isinstance(data, list) else []:
        if isinstance(o, dict) and o.get("company") and o.get("status") in OVERRIDE_STATUSES:
            out.append(o)
        else:
            print(f"ignored override {o!r}: needs a company and one of {sorted(OVERRIDE_STATUSES)}", file=sys.stderr)
    return out


def apply_overrides(apps: list[dict], overrides: list[dict]) -> list[dict]:
    """Replace the current status of matching applications. The emails (notices) are left as they were."""
    for a in apps:
        for o in overrides:
            if not same_company(a["company"], o["company"]):
                continue
            if o.get("position") and pos_key(o["position"]) != pos_key(a["position"]):
                continue
            a["status"], a["manual_status"] = o["status"], True
            if o.get("note"):
                a["status_note"] = o["note"]
            if o["status"] != "pending":
                a["item"] = None
    return apps


# ------------------------------------------------------------------ validation

def norm_name(s):
    return " ".join(_tokens(s))


def _accepted(w: dict, field: str) -> list:
    """The primary hand label first, then any alternatives written down as acceptable."""
    return [w[field]] + list(w.get(field + "_alt", []))


def validate(emails_path: str, expected_path: str) -> int:
    """Compare results with hand labels.

    A label may carry '<field>_alt' lists for judgement calls (e.g. a soft rejection) and a message
    may be labelled "job": null for chatty replies that need no verdict. Results that only match an
    alternative are counted separately from exact matches so they cannot hide errors."""
    messages = load_messages(emails_path)
    with open(expected_path, encoding="utf-8") as fh:
        exp = json.load(fh)
    notices, apps = scan_messages(messages, exp.get("since"))
    by_id = {n["id"]: n for n in notices}
    problems = []

    # 1) message level: is it an application notice, and with which status?
    tp = fp = fn = skipped = 0
    status_ok = status_alt = status_total = 0
    for mid, want in exp["emails"].items():
        got = by_id.get(mid)
        subj = (got or {}).get("subject", mid)
        if got is None:
            problems.append(f"message {mid}: not classified"); continue
        if want["job"] is None:
            skipped += 1
        elif want["job"] and got["job"]:
            tp += 1; status_total += 1
            ok = _accepted(want, "status")
            if got["status"] == ok[0]:
                status_ok += 1
            elif got["status"] in ok:
                status_alt += 1
            else:
                problems.append(f"STATUS   {subj!r}: expected {ok[0]}"
                                + (f" (or {', '.join(ok[1:])})" if len(ok) > 1 else "") + f", got {got['status']}")
        elif want["job"]:
            fn += 1; problems.append(f"MISSED   {subj!r}: expected {want['status']} notice, dropped ({got['reason']})")
        elif got["job"]:
            fp += 1; problems.append(f"FALSE+   {subj!r}: kept as {got['status']} but is not an application notice")
    judged = len(exp["emails"]) - skipped
    tn = judged - tp - fp - fn

    # 2) application level
    want_apps = exp["applications"]
    used = set()
    app_exact = app_soft = 0
    for w in want_apps:
        cos = [norm_name(c) for c in _accepted(w, "company")]
        poss = [pos_key(p) for p in _accepted(w, "position")]
        hit = next((i for i, a in enumerate(apps) if i not in used and norm_name(a["company"]) in cos
                    and pos_key(a["position"]) in poss), None)
        label = f"{w['company']} / {w['position'] or '(no position stated)'}"
        if hit is None:
            same_co = [a for a in apps if norm_name(a["company"]) in cos]
            problems.append(f"APP MISSING  {label}" + (f"  (found instead: {[a['position'] for a in same_co]})" if same_co else ""))
            continue
        used.add(hit)
        a = apps[hit]
        errs, soft = [], False
        if a["status"] not in _accepted(w, "status"):
            errs.append(f"status {a['status']} != {w['status']}")
        soft |= a["status"] != w["status"]
        if a["last_date"] not in _accepted(w, "last_date"):
            errs.append(f"date {a['last_date']} != {w['last_date']}")
        soft |= a["last_date"] != w["last_date"]
        soft |= norm_name(a["company"]) != norm_name(w["company"]) or pos_key(a["position"]) != pos_key(w["position"])
        for frag in w.get("item_contains", []):
            if frag.lower() not in (a.get("item") or "").lower():
                errs.append(f"item {a.get('item')!r} lacks {frag!r}")
        if errs:
            problems.append(f"APP WRONG    {label}: " + "; ".join(errs))
        elif soft:
            app_soft += 1
        else:
            app_exact += 1
    for i, a in enumerate(apps):
        if i not in used:
            problems.append(f"APP EXTRA    {a['company']} / {a['position'] or '(none)'} [{a['status']}]")

    print(f"Messages checked: {judged}  (application notices: {tp + fn}, other mail: {tn + fp})"
          + (f"; {skipped} chatty replies not scored" if skipped else ""))
    print(f"  notices found {tp}/{tp + fn}   false positives {fp}   correct status {status_ok + status_alt}/{status_total}"
          + (f" ({status_alt} only via an alternative label)" if status_alt else ""))
    print(f"Applications: {app_exact + app_soft}/{len(want_apps)} right (company, position, status, date), "
          f"{len(apps)} produced" + (f"; {app_exact} exactly, {app_soft} only via an alternative label" if app_soft else ""))
    if problems:
        print("\nProblems:")
        for p in problems:
            print("  -", p)
    else:
        print("\nNo differences from the hand labels.")
    return 0 if not problems else 1


# ------------------------------------------------------------------ command line


def main(argv=None) -> int:
    import gmail_scraper
    gmail_scraper.load_env_file()  # so GMAIL_USER / GMAIL_APP_PASSWORD in .env are visible below
    ap = argparse.ArgumentParser(description="Track job applications from email, with rules instead of AI.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def output_options(p):
        p.add_argument("--format", choices=["table", "json", "csv"], default="table")
        p.add_argument("--out", help="write to this file instead of the screen")
        p.add_argument("--overrides", help="JSON file of statuses you set yourself (default: overrides.json next to "
                                           "this script, if it exists)")

    s = sub.add_parser("scan", help="read an emails.json (from gmail_scraper.py) and list applications")
    s.add_argument("emails")
    s.add_argument("--since", help="ignore mail before this date (YYYY-MM-DD, UTC)")
    s.add_argument("--before", help="ignore mail on/after this date (YYYY-MM-DD, UTC)")
    output_options(s)

    v = sub.add_parser("validate", help="compare results with hand labels")
    v.add_argument("emails"); v.add_argument("expected")

    g = sub.add_parser("imap", help="download from Gmail (gmail_scraper.py) and scan in one step")
    g.add_argument("--user", default=os.environ.get("GMAIL_USER"),
                   help="Gmail address (default: GMAIL_USER from .env or the environment)")
    g.add_argument("--since", required=True)
    g.add_argument("--before")
    g.add_argument("--all", action="store_true", help="no keyword filter on the download")
    g.add_argument("--save-emails", help="also keep the downloaded messages as emails.json")
    output_options(g)

    a = ap.parse_args(argv)
    if a.cmd == "validate":
        return validate(a.emails, a.expected)
    account = None
    if a.cmd == "scan":
        messages = load_messages(a.emails)
        account = load_account(a.emails)
    else:
        if not a.user:
            ap.error("--user is required (or set GMAIL_USER in .env)")
        account = a.user
        messages, query = gmail_scraper.fetch_messages(a.user, gmail_scraper.get_password(), a.since,
                                                       a.before, everything=a.all)
        if a.save_emails:
            with open(a.save_emails, "w", encoding="utf-8") as fh:
                json.dump({"account": a.user, "since": a.since, "before": a.before, "query": query,
                           "count": len(messages), "messages": messages}, fh, ensure_ascii=False, indent=1)
    _, apps = scan_messages(messages, a.since, a.before)
    apps = apply_overrides(apps, load_overrides(a.overrides))
    meta = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "account": account,
            "since": a.since, "before": a.before, "messages_read": len(messages)}
    text = render(apps, a.format, meta)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        # a Windows console (cp1252) cannot show emoji from email text; replace instead of crashing
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, "replace").decode(enc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
