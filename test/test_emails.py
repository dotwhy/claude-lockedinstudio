"""Email extraction: the regex, the junk filter, and the priority order.

The bug this module was extracted to fix — discovery re-adding an address that
already hard-bounced — is covered in test_candidates.py, because the fix lives
in the suppression table rather than here.
"""
import emails


# --- clean() ----------------------------------------------------------------
def test_bare_address():
    assert emails.clean("sammy@business.com") == "sammy@business.com"


def test_address_buried_in_a_sentence():
    text = "For business enquiries please contact sammy@business.com thanks!"
    assert emails.clean(text) == "sammy@business.com"


def test_address_is_lowercased():
    # Creators type addresses however they like; the DB treats email as a
    # unique key, so casing must be normalised or duplicates slip through.
    assert emails.clean("Sammy@Business.COM") == "sammy@business.com"


def test_first_usable_address_wins_when_several_present():
    text = "sammy@business.com or backup@other.com"
    assert emails.clean(text) == "sammy@business.com"


def test_junk_is_skipped_in_favour_of_a_real_address():
    # A description that mentions a thumbnail file before the real contact.
    text = "thumb@2x.png — business: sammy@business.com"
    assert emails.clean(text) == "sammy@business.com"


def test_no_address_returns_none():
    assert emails.clean("subscribe for more roblox content") is None


def test_empty_and_none_input():
    assert emails.clean("") is None
    assert emails.clean(None) is None


def test_text_with_only_junk_returns_none():
    assert emails.clean("noreply@youtube.com") is None


# --- is_junk() --------------------------------------------------------------
def test_platform_addresses_are_junk():
    assert emails.is_junk("noreply@youtube.com")
    assert emails.is_junk("someone@example.com")


def test_image_filenames_are_junk():
    # These match the email regex but are filenames in video descriptions.
    assert emails.is_junk("banner@2x.png")
    assert emails.is_junk("cover@1x.jpg")


def test_junk_check_is_case_insensitive():
    assert emails.is_junk("NoReply@Studio.com")


def test_real_address_is_not_junk():
    assert not emails.is_junk("sammy@business.com")


def test_empty_is_junk():
    assert emails.is_junk("")
    assert emails.is_junk(None)


# --- first_in() -------------------------------------------------------------
def test_first_in_prefers_earlier_sources():
    # Channel description is passed first, so it must beat a video description.
    found = emails.first_in(
        "contact: about@channel.com",
        "also reach me at video@channel.com",
    )
    assert found == "about@channel.com"


def test_first_in_falls_through_to_later_sources():
    found = emails.first_in(
        "no contact details here",
        "business: video@channel.com",
    )
    assert found == "video@channel.com"


def test_first_in_skips_empty_sources():
    assert emails.first_in(None, "", "business: x@y.com") == "x@y.com"


def test_first_in_returns_none_when_nothing_matches():
    assert emails.first_in("nothing", "here either", None) is None


# --- the bounce list is data, not a live check -------------------------------
def test_august_bounces_are_present_for_migration():
    # These get seeded into `suppression` by db.init(). The list itself is not
    # consulted at runtime — see the module docstring.
    assert len(emails.AUGUST_BOUNCES) == 8
    assert "flamingo@ellify.com" in emails.AUGUST_BOUNCES


def test_bounced_address_still_parses_cleanly():
    # clean() must NOT reject bounces — suppression does that, at intake time.
    # If clean() silently dropped them, the suppression check would never fire
    # and we'd lose the audit trail of what was rejected and why.
    assert emails.clean("flamingo@ellify.com") == "flamingo@ellify.com"
