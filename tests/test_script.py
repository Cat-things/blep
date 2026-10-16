import discord

from blep.core.script import Script, ScriptError, parse_color
from blep.core.variables import substitute


def compile_(text, mapping=None, **kw):
    return Script(text, mapping=mapping or {}, **kw).compile()


def test_plain_text_is_content():
    script = compile_("hello there")
    assert script.content == "hello there"
    assert script.embeds == []


def test_bleed_style_embed():
    script = compile_(
        "{embed}$v{title: hi}$v{description: welcome}$v{color: #ff7aa2}"
        "$v{footer: foot && https://x.com/a.png}$v{field: a && b && inline}$v{message: yo}"
    )
    assert script.content == "yo"
    [embed] = script.embeds
    assert embed.title == "hi"
    assert embed.description == "welcome"
    assert embed.color == discord.Color(0xFF7AA2)
    assert embed.footer.text == "foot" and embed.footer.icon_url == "https://x.com/a.png"
    assert embed.fields[0].name == "a" and embed.fields[0].inline


def test_urls_with_colons_survive():
    script = compile_("{embed}$v{description: x}$v{image: https://cdn.example.com/a:b.png}$v{url: https://a.b/c}")
    assert script.embeds[0].image.url == "https://cdn.example.com/a:b.png"
    assert script.embeds[0].url == "https://a.b/c"


def test_invalid_urls_are_ignored():
    script = compile_("{embed}$v{description: x}$v{thumbnail: not a url}")
    assert script.embeds[0].thumbnail.url is None


def test_multiple_embeds_and_multiline_description():
    script = compile_("{embed}$v{title: one}$v{embed}$v{title: two}$v{description: line1\nline2}")
    assert [e.title for e in script.embeds] == ["one", "two"]
    assert script.embeds[1].description == "line1\nline2"


def test_variables_render_before_parsing():
    mapping = {"user.name": "blep", "guild.name": "cats"}
    script = compile_("{embed}$v{title: hi {user.name}}$v{description: {guild.name}}", mapping)
    assert script.embeds[0].title == "hi blep"
    assert script.embeds[0].description == "cats"


def test_buttons():
    script = compile_("{embed}$v{description: x}$v{button: https://blep.wtf && site}$v{button: red && no && disabled}")
    view = script.view()
    assert view is not None
    link, noop = view.children
    assert link.url == "https://blep.wtf" and link.label == "site"
    assert noop.item.disabled and noop.item.style is discord.ButtonStyle.danger


def test_empty_script_raises():
    script = Script("{embed}$v{color: red}", mapping={})
    try:
        script.kwargs()
    except ScriptError:
        pass
    else:
        raise AssertionError("expected ScriptError")


def test_from_message_roundtrip_like_output():
    embed = discord.Embed(title="t", description="d", color=0x123456)
    embed.add_field(name="n", value="v", inline=True)

    class FakeMessage:
        content = "hi"
        embeds = [embed]
        components = []

    code = Script.from_message(FakeMessage())
    script = compile_(code)
    assert script.content == "hi"
    assert script.embeds[0].title == "t"
    assert script.embeds[0].fields[0].inline


def test_parse_color():
    assert parse_color("#fff") == discord.Color(0xFFFFFF)
    assert parse_color("pink") is not None
    assert parse_color("rgb(1, 2, 3)") == discord.Color.from_rgb(1, 2, 3)
    assert parse_color("nope") is None


def test_substitute_functions_and_unknowns():
    mapping = {"track.name": "Jeans", "user.plays": "12345", "lazy": lambda: "computed"}
    assert substitute("{lower(track.name)}", mapping) == "jeans"
    assert substitute("{proper(user.plays)}", mapping) == "12,345"
    assert substitute("{lazy} {unknown} {embed}", mapping) == "computed {unknown} {embed}"
