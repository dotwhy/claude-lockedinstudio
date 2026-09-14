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
