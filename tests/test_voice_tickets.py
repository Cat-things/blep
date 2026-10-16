"""Pure-logic tests for the VoiceMaster, Tickets, Webhooks and Embeds modules."""

from types import SimpleNamespace

from blep.cogs.tickets import (
    clean_text,
    normalise_style,
    pad_case,
    parse_target_emoji,
    render_markdown,
    render_ticket_name,
    slugify,
)
from blep.cogs.voicemaster import render_channel_name
from blep.cogs.webhooks import generate_identifier, split_webhook_flags


class FakeUser(SimpleNamespace):
    def __str__(self) -> str:
        return self.name


def fake_user(**overrides):
    data = {
        "id": 123,
        "mention": "<@123>",
        "name": "bryce",
        "discriminator": "0",
        "display_name": "Bryce Gaming",
        "bot": False,
        "guild": None,
    }
    data.update(overrides)
    return FakeUser(**data)


# ----------------------------------------------------------------------------- tickets
def test_slugify_form_keys():
    assert slugify("What's your age?") == "whats_your_age"
    assert slugify("  Reason   for ticket ") == "reason_for_ticket"
    assert slugify("???") == "field"
    assert len(slugify("x" * 100)) <= 32


def test_case_padding():
    assert pad_case(7, 3) == "007"
    assert pad_case(1234, 3) == "1234"
    assert pad_case(7, 0) == "7"


def test_ticket_names():
    user = fake_user()
    assert render_ticket_name("ticket-{case}", case=7, padding=4, member=user) == "ticket-0007"
    assert render_ticket_name("{user.name}-{case}", case=12, padding=0, member=user) == "bryce-12"
    assert render_ticket_name("{user.display_name} {option}", case=1, padding=0, member=user,
                              option_label="General Help") == "bryce-gaming-general-help"
    # A template that renders to nothing falls back to the case number.
    assert render_ticket_name("!!!", case=3, padding=2, member=user) == "ticket-03"


def test_option_emoji_parsing():
    assert parse_target_emoji("General Help 💬") == ("General Help", "💬")
    assert parse_target_emoji("Report <:report:123456789012345678>") == ("Report", "<:report:123456789012345678>")
    assert parse_target_emoji("Billing") == ("Billing", None)
    assert parse_target_emoji('"Ban Appeal"') == ("Ban Appeal", None)


def test_styles():
    assert normalise_style("Green") == "green"
    assert normalise_style("blue") == "blurple"
    assert normalise_style("gray") == "grey"
    assert normalise_style("purple") is None


def test_clean_text_cannot_split_scripts():
    assert "$v" not in clean_text("hello $v{title: injected}")
    assert clean_text(None) is None


def test_markdown_is_escaped():
    rendered = render_markdown('<script>alert(1)</script> **bold** `code <b>` https://example.com/a?b=1&c=2')
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<code>code &lt;b&gt;</code>" in rendered
    assert '<a href="https://example.com/a?b=1&amp;c=2">' in rendered


def test_markdown_emojis_and_timestamps():
    rendered = render_markdown("hi <:wave:123456789012345678> <t:0:f>")
    assert "cdn.discordapp.com/emojis/123456789012345678.png" in rendered
    assert "Jan 01, 1970" in rendered


def test_markdown_code_blocks_are_not_formatted():
    rendered = render_markdown("```py\nprint('**not bold**')\n```")
    assert "<pre>" in rendered and "<strong>" not in rendered


# ----------------------------------------------------------------------------- webhooks
def test_webhook_flags():
    message, embeds, options = split_webhook_flags(
        "{embed}$v{title: hello} --add {embed}$v{title: hi} --add {embed}$v{title: hey} --username Bob --avatar https://x.y/a.png"
    )
    assert message == "{embed}$v{title: hello}"
    assert embeds == ["{embed}$v{title: hi}", "{embed}$v{title: hey}"]
    assert options == {"username": "Bob", "avatar": "https://x.y/a.png"}


def test_webhook_flags_plain_text():
    assert split_webhook_flags("hello world") == ("hello world", [], {})
    assert split_webhook_flags("not--add a flag") == ("not--add a flag", [], {})


def test_webhook_identifiers():
    identifier = generate_identifier()
    assert len(identifier) == 7 and identifier.isalnum() and identifier.lower() == identifier


# ----------------------------------------------------------------------------- voicemaster
def test_voice_channel_names():
    user = fake_user()
    assert render_channel_name(None, user) == "Bryce Gaming's channel"
    assert render_channel_name("{user.name}'s vc", user) == "bryce's vc"
    assert render_channel_name("   ", user) == "Bryce Gaming's channel"
    assert len(render_channel_name("x" * 300, user)) == 100
