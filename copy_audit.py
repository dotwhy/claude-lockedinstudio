"""Automated review of every email this system can send.

Every defect checked here is one that actually shipped to a real creator, or
came within one preview of it:

  - "A few we've shipped: your-portfolio-link-here"   (placeholder reached 2 people)
  - "LockedIn Studio · your real postal address here" (placeholder in the footer)
  - "worth a quick look?" with nothing linked          (39 queued to receive it)
  - the model narrating what data it lacked            (caught in preview)

The pattern is always the same: a template renders fine in isolation, the
failure only appears in the finished email, and nobody sees the finished email
until it's in someone's inbox. So this renders every template with realistic
inputs and reads the result the way a recipient would.

Runs three ways:
  - in the test suite, so a bad template change fails CI
  - as `/run/audit`, against live config on Railway
  - inside the sending preflight, so nothing broken can go out
"""
import re

import config

# A real-looking prospect. Values are deliberately ordinary — a template that
# only survives ideal input isn't safe.
SAMPLE = {
    "first_name": "Sammy",
    "channel_name": "SammyGames",
    "line": "Your Steal a Brainrot uploads pull about 3x your usual views.",
}

# Text that means a value was never filled in.
_PLACEHOLDER = re.compile(
    r"your-[a-z-]+|your real |your street|<[a-z ]+>|example\.(com|org)|"
    r"placeholder|lorem ipsum|\bTODO\b|xxxx|\bTBD\b", re.I)

# An unrendered format slot: "{first_name}" left in the body.
_UNRENDERED = re.compile(r"\{[a-z_]+\}")

# Words that promise something to look at. If one appears with no link in the
# same email, the reader has been invited nowhere.
_INVITATION = re.compile(
    r"\b(a quick look|take a look|have a look|check (it|us) out|see (it|more)|"
    r"portfolio|our work|the site)\b", re.I)

_URL = re.compile(r"https?://\S+|www\.\S+|\b[a-z0-9-]+\.(com|studio|io|dev|gg|tv)\b", re.I)


def _templates():
    """Every (name, subject, body) this system can put in front of a creator."""
    s = SAMPLE
    rendered = [
        ("opener", *config.opener(s["first_name"], s["channel_name"], s["line"])),
        ("followup_1", *config.followup_1(s["first_name"], s["channel_name"], s["line"])),
        ("followup_2", *config.followup_2(s["first_name"], s["channel_name"], s["line"])),
        ("retouch", *config.retouch(s["first_name"], s["channel_name"], s["line"])),
    ]
    # Every template must also survive having no personalized line, because
    # validation rejects roughly one line in six and those still send.
    rendered += [
        ("opener (no line)", *config.opener(s["first_name"], s["channel_name"], "")),
        ("retouch (no line)", *config.retouch(s["first_name"], s["channel_name"], "")),
    ]
    return rendered


def audit_text(name, subject, body):
    """Defects in one rendered email, as a list of strings."""
    problems = []
    subject = subject or ""

    for match in _PLACEHOLDER.findall(body) or []:
        problems.append(f"{name}: placeholder text in body ({match!r})")
    if _PLACEHOLDER.search(subject):
        problems.append(f"{name}: placeholder text in subject")

    if _UNRENDERED.search(body) or _UNRENDERED.search(subject):
        problems.append(f"{name}: unrendered template slot, e.g. {{first_name}}")

    if "unsubscribe" not in body.lower():
        problems.append(f"{name}: no unsubscribe line")

    # A dead invitation: asks them to look, links nothing.
    invitation = _INVITATION.search(body)
    if invitation and not _URL.search(body):
        problems.append(
            f"{name}: says {invitation.group(0)!r} but the email links nothing")

    if "None" in body:
        problems.append(f"{name}: the literal word 'None' leaked into the body")

    if "\n\n\n" in body:
        problems.append(f"{name}: blank gap where a value was expected to render")

    # A joining character left stranded on its own line — what an empty
    # STUDIO_ADDRESS used to leave behind ("LockedIn Studio · ").
    #
    # A lone em dash is NOT flagged: it's the sign-off rule above the footer,
    # and always appears by itself by design.
    for line in body.splitlines():
        if line.strip() in ("·", "|", "-", "•"):
            problems.append(f"{name}: dangling separator on an otherwise empty line")
    if re.search(r"·\s*$", body, re.M):
        problems.append(f"{name}: separator with nothing after it")

    if body.count("—") > 6:
        problems.append(f"{name}: em dash used {body.count('—')} times; reads as generated")

    return problems


def audit():
    """Audit every template. Empty list means the copy is safe to send."""
    problems = list(config.sending_config_problems())
    for name, subject, body in _templates():
        problems.extend(audit_text(name, subject, body))
    return problems


def report():
    """Human-readable audit, for the CLI and the job runner."""
    problems = audit()
    if not problems:
        print(f"Copy audit: {len(_templates())} rendered emails, no problems.")
        return 0
    print(f"Copy audit: {len(problems)} problem(s) found —")
    for problem in problems:
        print(f"  ✗ {problem}")
    return len(problems)
