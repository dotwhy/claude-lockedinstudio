"""The copy audit: does it catch the mistakes that actually shipped?

Each test below is a real defect from this project's history, reproduced as a
rendered email. If the audit stops catching one of these, that defect can ship
again.
"""
import config
import copy_audit


def audit_body(body, name="test", subject="Subject"):
    return copy_audit.audit_text(name, subject, body)


GOOD = (
    "Hey Sammy,\n\n"
    "Your Steal a Brainrot uploads pull about 3x your usual views.\n\n"
    "We build Roblox games for creators, end to end — www.lockedinstudio.com\n\n"
    "Worth a quick chat?\n\n"
    "Laurenz, LockedIn Studio\n\n"
    "—\nLockedIn Studio\nReply 'unsubscribe' and I won't email again."
)


def test_a_good_email_has_no_problems():
    assert audit_body(GOOD) == []


# --- the placeholder that reached two creators ------------------------------
def test_catches_placeholder_portfolio_link():
    bad = GOOD.replace("www.lockedinstudio.com", "your-portfolio-link-here")
    assert any("placeholder" in p for p in audit_body(bad))


def test_catches_placeholder_postal_address():
    bad = GOOD.replace("LockedIn Studio\nReply", "LockedIn Studio · your real postal address here\nReply")
    assert any("placeholder" in p for p in audit_body(bad))


def test_catches_angle_bracket_placeholders():
    bad = GOOD.replace("www.lockedinstudio.com", "<your website>")
    assert any("placeholder" in p for p in audit_body(bad))


def test_catches_example_dot_com():
    bad = GOOD.replace("www.lockedinstudio.com", "https://your-portfolio-link.example.com")
    assert any("placeholder" in p for p in audit_body(bad))


# --- the dead invitation ----------------------------------------------------
def test_catches_inviting_a_look_with_nothing_linked():
    # The re-touch said "worth a quick look?" and linked nothing. 39 people
    # were queued to receive it.
    bad = (
        "Hey Sammy,\n\nReached out a while back about building you a Roblox "
        "game. If owning your own is on your radar, worth a quick look? "
        "No stress if not.\n\nLaurenz\nReply 'unsubscribe' to opt out."
    )
    assert any("links nothing" in p for p in audit_body(bad))


def test_an_invitation_with_a_link_is_fine():
    assert audit_body(GOOD) == []


def test_catches_mentioning_a_portfolio_with_no_link():
    bad = (
        "Hey Sammy,\n\nDid the portfolio land?\n\n"
        "Laurenz\nReply 'unsubscribe' to opt out."
    )
    assert any("links nothing" in p for p in audit_body(bad))


# --- rendering failures -----------------------------------------------------
def test_catches_unrendered_template_slot():
    bad = GOOD.replace("Hey Sammy", "Hey {first_name}")
    assert any("unrendered" in p for p in audit_body(bad))


def test_catches_the_word_none_leaking_in():
    bad = GOOD.replace("www.lockedinstudio.com", "None")
    assert any("None" in p for p in audit_body(bad))


def test_catches_a_blank_gap_where_a_value_should_be():
    bad = GOOD.replace("Your Steal a Brainrot uploads pull about 3x your usual views.\n\n", "\n")
    assert any("blank gap" in p for p in audit_body(bad))


def test_catches_a_dangling_separator():
    # What an empty STUDIO_ADDRESS used to leave in the footer.
    bad = GOOD.replace("—\nLockedIn Studio", "—\n·\nLockedIn Studio")
    assert any("dangling separator" in p for p in audit_body(bad))


def test_catches_a_separator_with_nothing_after_it():
    bad = GOOD.replace("LockedIn Studio\nReply", "LockedIn Studio · \nReply")
    assert any("nothing after it" in p for p in audit_body(bad))


def test_the_footers_own_em_dash_rule_is_not_flagged():
    # The sign-off rule above the footer is a lone em dash by design. Flagging
    # it would make the audit cry wolf on every single email.
    assert not any("separator" in p for p in audit_body(GOOD))


# --- compliance -------------------------------------------------------------
def test_catches_a_missing_unsubscribe():
    bad = GOOD.replace("Reply 'unsubscribe' and I won't email again.", "Cheers")
    assert any("unsubscribe" in p for p in audit_body(bad))


def test_catches_placeholder_in_the_subject():
    problems = copy_audit.audit_text("t", "A game built for <channel>", GOOD)
    assert any("subject" in p for p in problems)


# --- tone -------------------------------------------------------------------
def test_catches_em_dash_overuse():
    bad = GOOD + "\n" + "— " * 8
    assert any("em dash" in p for p in audit_body(bad))


# --- the real templates -----------------------------------------------------
def test_every_live_template_passes(monkeypatch):
    # The whole point: this fails CI if anyone edits a template badly.
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "www.lockedinstudio.com")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "")
    assert copy_audit.audit() == []


def test_every_live_template_passes_with_an_address(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "www.lockedinstudio.com")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "Musterstr 1, 10115 Berlin")
    assert copy_audit.audit() == []


def test_templates_are_audited_without_a_personalized_line(monkeypatch):
    # Validation rejects roughly one line in six, and those still send.
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "www.lockedinstudio.com")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "")
    names = [name for name, _, _ in copy_audit._templates()]
    assert "opener (no line)" in names
    assert "retouch (no line)" in names


def test_audit_surfaces_bad_config(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "your-portfolio-link-here")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "")
    assert copy_audit.audit() != []
