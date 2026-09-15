"""The opener line: the performance note, the prompt contract, and the template.

No Anthropic calls here — the note is pure arithmetic, and the prompt is
asserted on as a string. Judging the QUALITY of generated lines needs an eval,
which is TODOS.md item 3.
"""
import config
import personalize


# --- performance note -------------------------------------------------------
def test_outperforming_video_is_described_as_a_multiple():
    note = personalize.performance_note(240_000, 80_000)
    assert "3.0x" in note
    assert "240,000" in note


def test_typical_video_is_described_plainly():
    note = personalize.performance_note(85_000, 80_000)
    assert "typical" in note
    assert "x their usual" not in note


def test_underperforming_video_tells_the_model_to_stay_quiet():
    # Opening a cold email by noting someone's video flopped is a bad line.
    note = personalize.performance_note(20_000, 80_000)
    assert "do not comment" in note


def test_missing_numbers_produce_no_note():
    # An empty note beats a vague one — otherwise the model reaches for filler
    # like "your recent video is doing great".
    assert personalize.performance_note(None, 80_000) == ""
    assert personalize.performance_note(240_000, None) == ""
    assert personalize.performance_note(None, None) == ""


def test_zero_average_does_not_divide_by_zero():
    assert personalize.performance_note(240_000, 0) == ""


def test_threshold_boundaries():
    assert "x their usual" in personalize.performance_note(120_000, 80_000)   # 1.5x
    assert "typical" in personalize.performance_note(119_000, 80_000)         # just under


# --- the prompt contract ----------------------------------------------------
def build(channel="SammyGames", subs=82_000, latest="Steal a Brainrot",
          latest_views=None, avg_views=None):
    return personalize.PROMPT.format(
        studio=config.STUDIO_CONTEXT,
        channel=channel,
        subs=f"{subs:,}" if subs else "unknown",
        latest=latest,
        performance=personalize.performance_note(latest_views, avg_views),
    )


def test_prompt_carries_studio_context():
    # Without this the line can only comment on their content, and the
    # connection to the offer has to be carried by the template.
    assert "LockedIn Studio builds full Roblox games" in build()


def test_prompt_carries_the_performance_note():
    assert "3.0x" in build(latest_views=240_000, avg_views=80_000)


def test_prompt_forbids_pitching_in_the_line():
    prompt = build()
    assert "Do NOT pitch" in prompt
    assert "maximum 25 words" in prompt


def test_prompt_forbids_inventing_facts():
    assert "Never invent a fact" in build()


def test_prompt_handles_a_channel_with_no_video_data():
    prompt = build(latest=None, latest_views=None, avg_views=None)
    assert "None" in prompt or "(unknown)" in prompt


# --- the rendered email -----------------------------------------------------
def test_opener_stays_short(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "lockedin.studio/work")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "Street 1, Berlin")
    line = "Your Steal a Brainrot uploads pull 3x your channel average."
    _, body = config.opener("Sammy", "SammyGames", line)

    before_footer = body.split("—")[0]
    assert len(before_footer.split()) < 45, "opener drifted back into a pitch"


def test_opener_keeps_can_spam_requirements(monkeypatch):
    # Sending cold email from a personal Gmail without these is what gets an
    # account flagged.
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "Street 1, Berlin")
    _, body = config.opener("Sammy", "SammyGames", "a line")
    assert "unsubscribe" in body.lower()
    assert "Street 1, Berlin" in body


def test_opener_includes_the_personalized_line():
    line = "Your Steal a Brainrot uploads pull 3x your channel average."
    _, body = config.opener("Sammy", "SammyGames", line)
    assert line in body


def test_followups_reply_in_thread():
    # A subject of None keeps the original subject and threads the reply,
    # instead of starting a second conversation.
    assert config.followup_1("Sammy", "SammyGames", "")[0] is None
    assert config.followup_2("Sammy", "SammyGames", "")[0] is None


# --- the last gate before an email reaches a real creator -------------------
# Everything here is a line that actually came back from the model in
# production, or a near-miss of one.

def test_model_narrating_its_own_limits_is_rejected():
    # This exact line was generated for c0nvextv and would have been emailed.
    bad = ("I don't have access to c0nvextv's latest video title, view count, "
           "or performance data, so I can't write a line that proves real "
           "research without inventing facts.")
    assert personalize.validate_line(bad) is None


def test_other_refusal_shapes_are_rejected():
    for bad in [
        "I cannot write a personalized line without more information.",
        "To write this properly, I'd need the video title and view count.",
        "As an AI, I don't have enough context about this channel.",
        "[INSERT PERSONALIZED LINE HERE]",
        "Insufficient data to generate a line.",
    ]:
        assert personalize.validate_line(bad) is None, bad


def test_literal_none_is_rejected():
    assert personalize.validate_line("NONE") is None
    assert personalize.validate_line("None") is None


def test_overclaiming_a_pattern_is_rejected():
    # We see ONE video. "consistently" claims a trend we never measured.
    bad = ('Your "$5000 ROBUX" video format consistently outperforms your '
           'channel average—we noticed the pattern.')
    assert personalize.validate_line(bad) is None


def test_verbal_tics_are_rejected():
    # Fine once. Across 40 emails it reads as a template — and "hit different"
    # came back in 4 of the first 6 generations.
    assert personalize.validate_line("Your dinner shorts format is hitting different.") is None
    assert personalize.validate_line("Your latest upload hit different.") is None
    assert personalize.validate_line("That edit is fire.") is None


def test_rambling_output_is_rejected():
    assert personalize.validate_line(" ".join(["word"] * 40)) is None


def test_a_good_line_survives():
    good = ("Your XXL Modpack series keeps the pacing tight while giving "
            "viewers plenty to explore.")
    assert personalize.validate_line(good) == good


def test_surrounding_quotes_and_whitespace_are_stripped():
    assert personalize.validate_line('  "Your Stumble Guys comeback was a good watch."  ') \
        == "Your Stumble Guys comeback was a good watch."


def test_internal_whitespace_is_normalised():
    assert personalize.validate_line("Your   video\n\nwas good.") == "Your video was good."


def test_empty_input_is_rejected():
    assert personalize.validate_line("") is None
    assert personalize.validate_line(None) is None


def test_prompt_tells_the_model_to_say_none_rather_than_explain():
    assert "exactly the word NONE" in build()
    assert "Never explain what you're missing" in build()


def test_prompt_bans_pattern_claims_and_tics():
    prompt = build()
    assert "consistently" in prompt
    assert "hit different" in prompt


def test_retouch_template_renders_cleanly_with_no_line(monkeypatch):
    # The fallback when validation rejects everything: the email must still
    # read properly, with no gap or stray punctuation where the line would be.
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    subject, body = config.retouch("Sammy", "SammyGames", "")
    assert "Hey Sammy," in body
    assert "\n\n\n" not in body
    assert subject


def test_opener_template_renders_cleanly_with_no_line(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "lockedin.studio/work")
    _, body = config.opener("Sammy", "SammyGames", "")
    assert "Hey Sammy," in body
    assert "unsubscribe" in body.lower()


# --- sending preflight ------------------------------------------------------
# Two real emails went out reading "A few we've shipped: your-portfolio-link-here"
# and a CAN-SPAM footer of "LockedIn Studio · your real postal address here".

def test_placeholder_portfolio_is_caught(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "Musterstr 1, 10115 Berlin, Germany")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "your-portfolio-link-here")
    problems = config.sending_config_problems()
    assert any("PORTFOLIO_URL" in p for p in problems)


def test_placeholder_address_is_caught(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "your real postal address here")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "https://lockedin.studio/work")
    problems = config.sending_config_problems()
    assert any("STUDIO_ADDRESS" in p for p in problems)
    assert any("CAN-SPAM" in p for p in problems)


def test_env_example_style_placeholders_are_caught(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "LockedIn Studio, <your street>, <city>")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "https://your-portfolio-link.example")
    assert len(config.sending_config_problems()) == 2


def test_empty_values_are_caught(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "   ")
    monkeypatch.setattr(config, "PORTFOLIO_URL", None)
    assert len(config.sending_config_problems()) == 3


def test_real_values_pass(monkeypatch):
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "Musterstr 1, 10115 Berlin, Germany")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "https://lockedin.studio/work")
    assert config.sending_config_problems() == []


def test_send_paths_refuse_when_config_is_placeholder(monkeypatch):
    # The actual protection: not a warning, a refusal.
    import engine
    monkeypatch.setattr(config, "PORTFOLIO_URL", "your-portfolio-link-here")
    assert engine._preflight_blocked("test") is True


def test_send_paths_proceed_when_config_is_real(monkeypatch):
    import engine
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "Musterstr 1, 10115 Berlin, Germany")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "https://lockedin.studio/work")
    assert engine._preflight_blocked("test") is False
