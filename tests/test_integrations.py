from datetime import datetime, timedelta, timezone

import discord

from blep.cogs import fortnite, lastfm, roblox, social
from blep.core import variables
from blep.core.script import Script


# --------------------------------------------------------------------------- Last.fm
def test_periods():
    assert lastfm.parse_period("7d") == "7day"
    assert lastfm.parse_period("1M") == "1month"
    assert lastfm.parse_period("3m") == "3month"
    assert lastfm.parse_period("6month") == "6month"
    assert lastfm.parse_period("1y") == "12month"
    assert lastfm.parse_period("all") == "overall"
    assert lastfm.parse_period("overall") == "overall"
    assert lastfm.parse_period("@someone") is None


def test_pick_image_skips_placeholder_and_empty():
    images = [
        {"#text": "https://x/34s/a.png", "size": "small"},
        {"#text": f"https://x/300x300/{lastfm.PLACEHOLDER_IMAGE}.png", "size": "extralarge"},
        {"#text": "", "size": "mega"},
    ]
    assert lastfm.pick_image(images) == "https://x/34s/a.png"
    assert lastfm.pick_image(None) is None


def test_link_escapes_brackets_and_parentheses():
    text = lastfm.link("Song [Live]", "https://www.last.fm/music/A/_/Song+(Live)")
    assert text == "[Song \\[Live\\]](https://www.last.fm/music/A/_/Song+%28Live%29)"


def test_as_list_and_name_of():
    assert lastfm.as_list({"a": 1}) == [{"a": 1}]
    assert lastfm.as_list(None) == []
    assert lastfm.name_of({"#text": "Artist"}) == "Artist"
    assert lastfm.name_of({"name": "Artist"}) == "Artist"


def test_ttl_cache_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(lastfm.time, "monotonic", lambda: now[0])
    cache: lastfm.TTLCache[str, int] = lastfm.TTLCache(10, maxsize=2)
    cache.set("a", 1)
    assert cache.get("a") == 1
    now[0] += 11
    assert cache.get("a") is None
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)  # evicts the oldest
    assert cache.get("a") is None and cache.get("c") == 3
    cache.discard_if(lambda key: key == "b")
    assert cache.get("b") is None


def test_mode_variables_separate_lastfm_user_and_author():
    np = lastfm.NowPlaying(
        username="blepfm", name="Magnolia", artist="Playboi Carti", url="https://www.last.fm/music/x/_/y",
        album="Playboi Carti", cover="https://img/cover.png", now_playing=True, track_plays=1234,
        artist_plays=50, user_plays=98765,
    )
    mapping = variables.build()
    mapping.update({"author": "cat", "author.name": "cat", "author.nickname": "kitty"})
    mapping.update(lastfm.nowplaying_variables(np, crown=True))
    script = Script(
        "{embed}$v{title: {track.name} by {artist.name}}$v{description: {lower(album.name)} {user.artist_crown}}"
        "$v{footer: {user.name} - {author.name} - {proper(user.plays)} - {track.plays}}$v{thumbnail: {album.cover}}",
        mapping=mapping,
    ).compile()
    [embed] = script.embeds
    assert embed.title == "Magnolia by Playboi Carti"
    assert embed.description == "playboi carti 👑"
    assert embed.footer.text == "blepfm - cat - 98,765 - 1234"
    assert embed.thumbnail.url == "https://img/cover.png"


def test_sample_track_validates_default_example():
    mapping = lastfm.nowplaying_variables(lastfm.SAMPLE_TRACK)
    Script("{embed}$v{title:Now Playing}$v{description: {track.name}}$v{thumbnail: {album.cover}}",
           mapping=mapping).kwargs()


# --------------------------------------------------------------------------- Social
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <title>MrBeast</title>
 <author><name>MrBeast</name><uri>https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA</uri></author>
 <entry>
  <id>yt:video:abc12345678</id>
  <yt:videoId>abc12345678</yt:videoId>
  <title>New Video &amp; Stuff</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/abc12345678"/>
  <published>2026-10-16T12:00:00+00:00</published>
  <media:group><media:thumbnail url="https://i1.ytimg.com/vi/abc12345678/hqdefault.jpg" width="480"/></media:group>
 </entry>
 <entry>
  <yt:videoId>old12345678</yt:videoId>
  <title>Old</title>
  <published>2020-01-01T00:00:00Z</published>
 </entry>
</feed>"""


def test_parse_youtube_feed():
    name, videos = social.parse_youtube_feed(FEED)
    assert name == "MrBeast"
    assert [v.id for v in videos] == ["abc12345678", "old12345678"]
    assert videos[0].title == "New Video & Stuff"
    assert videos[0].url == "https://www.youtube.com/shorts/abc12345678"
    assert videos[0].published == datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    assert videos[0].thumbnail.endswith("hqdefault.jpg")
    assert videos[1].url == "https://www.youtube.com/watch?v=old12345678"
    variables_ = social.youtube_variables("UCX6OQ3DkcsbYNE6H8uQQuVA", name, videos[0])
    assert variables_["youtube.channel.url"] == "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA"


def test_youtube_targets():
    channel_id = "UCX6OQ3DkcsbYNE6H8uQQuVA"
    assert social.youtube_targets(channel_id) == (channel_id, [])
    assert social.youtube_targets(f"https://www.youtube.com/channel/{channel_id}/videos") == (channel_id, [])
    assert social.youtube_targets("https://youtube.com/@MrBeast/shorts") == (None, ["https://www.youtube.com/@MrBeast"])
    assert social.youtube_targets("@MrBeast")[1][0] == "https://www.youtube.com/@MrBeast"
    assert social.youtube_targets("youtube.com/c/Name")[1] == ["https://www.youtube.com/c/Name"]
    assert social.youtube_targets("not a/handle") == (None, [])


def test_extract_channel_id_prefers_canonical():
    html = (
        '<html>"channelId":"UCaaaaaaaaaaaaaaaaaaaaaa"'
        '<link rel="canonical" href="https://www.youtube.com/channel/UCbbbbbbbbbbbbbbbbbbbbbb"></html>'
    )
    assert social.extract_channel_id(html) == "UCbbbbbbbbbbbbbbbbbbbbbb"
    assert social.extract_channel_id("<html></html>") is None


def test_clean_account():
    assert social.clean_account("twitch", "https://www.twitch.tv/KaiCenat/") == "kaicenat"
    assert social.clean_account("kick", "kick.com/Some_User") == "some-user"
    assert social.clean_account("reddit", "r/Memes") == "memes"
    assert social.clean_account("reddit", "https://www.reddit.com/r/aww/new/") == "aww"
    assert social.valid_account("reddit", "aww") and not social.valid_account("reddit", "a b")


def test_seen_and_recency():
    assert social.merge_seen(["c", "b"], ["b", "a"]) == ["c", "b", "a"]
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert social.is_recent(now - timedelta(hours=5), now=now)
    assert not social.is_recent(now - timedelta(days=3), now=now)
    assert social.is_recent(None, now=now)


def test_reddit_image():
    assert social.reddit_image({"url": "https://i.redd.it/x.jpg"}) == "https://i.redd.it/x.jpg"
    assert social.reddit_image({"url": "https://i.redd.it/x.jpg", "spoiler": True}) is None
    gallery = {"is_gallery": True, "url": "https://reddit.com/gallery/x",
               "media_metadata": {"a": {"s": {"u": "https://preview.redd.it/a.jpg"}}}}
    assert social.reddit_image(gallery) == "https://preview.redd.it/a.jpg"
    assert social.reddit_image({"url": "https://example.com/article"}) is None


def test_match_feed():
    feeds = [
        {"account_id": "UCbbbbbbbbbbbbbbbbbbbbbb", "account_name": "Linus Tech Tips"},
        {"account_id": "aww", "account_name": "aww"},
    ]
    assert social.Social.match_feed(feeds, "youtube", "@LinusTechTips") is feeds[0]
    assert social.Social.match_feed(feeds, "youtube", "UCbbbbbbbbbbbbbbbbbbbbbb") is feeds[0]
    assert social.Social.match_feed(feeds, "reddit", "r/AWW") is feeds[1]
    assert social.Social.match_feed(feeds, "reddit", "pics") is None


def test_retry_after():
    assert social.retry_after({"Retry-After": "30"}) == 30
    assert social.retry_after({}) == social.DEFAULT_BACKOFF


# --------------------------------------------------------------------------- Fortnite
def _entry(offer, name, price, section, index, item_id, sort=0):
    return {
        "offerId": offer,
        "finalPrice": price,
        "regularPrice": price,
        "sortPriority": sort,
        "layout": {"name": section, "index": index},
        "brItems": [{"id": item_id, "name": name, "rarity": {"value": "epic", "displayValue": "Epic"},
                     "images": {"icon": f"https://img/{item_id}.png"}, "type": {"displayValue": "Outfit"}}],
    }


SHOP = {
    "hash": "x",
    "date": "2026-10-16T00:00:00Z",
    "entries": [
        _entry("o1", "Toosie Slide", 500, "Emotes", 2, "EID_TOOSIE"),
        _entry("o2", "Peely", 1200, "Featured", 0, "CID_PEELY", sort=5),
        _entry("o3", "Jonesy", 800, "Featured", 0, "CID_JONESY", sort=10),
        {"offerId": "o4", "finalPrice": 1800, "regularPrice": 2000, "layout": {"name": "Featured", "index": 0},
         "bundle": {"name": "Peely Bundle", "image": "https://img/bundle.png"},
         "brItems": [{"id": "CID_PEELY", "name": "Peely"}, {"id": "BID_BANANA", "name": "Banana"}]},
    ],
}


def test_shop_signature_ignores_order_and_hash():
    reordered = dict(SHOP, hash="y", entries=list(reversed(SHOP["entries"])))
    assert fortnite.shop_signature(SHOP) == fortnite.shop_signature(reordered)
    changed = dict(SHOP, entries=SHOP["entries"][:2])
    assert fortnite.shop_signature(SHOP) != fortnite.shop_signature(changed)


def test_shop_sections_and_offers():
    sections = fortnite.group_sections(fortnite.shop_entries(SHOP))
    assert [name for name, _ in sections] == ["Featured", "Emotes"]
    assert [fortnite.entry_name(e) for e in sections[0][1]] == ["Jonesy", "Peely", "Peely Bundle"]
    offers = fortnite.shop_offers(SHOP)
    assert offers["cid_peely"][1]["offerId"] == "o2"  # cheapest single offer wins over the bundle
    assert offers["bid_banana"][1]["offerId"] == "o4"
    assert fortnite.format_price(SHOP["entries"][3]) == "~~2,000~~ **1,800** V-Bucks"
    assert fortnite.shop_date(SHOP).isoformat() == "2026-10-16"


def test_shop_embeds_fit_discord_limits():
    big = dict(SHOP, entries=[
        _entry(f"o{i}", f"Item number {i} with a long name", 1500, f"Section {i % 40}", i % 40, f"ID{i}")
        for i in range(400)
    ])
    embed = fortnite.shop_summary_embed(big)
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    assert all(len(f.value) <= 1024 for f in embed.fields)
    page = fortnite.entry_embed(SHOP["entries"][3])
    assert page.title == "Peely Bundle" and page.image.url == "https://img/bundle.png"


def test_cosmetic_embed():
    item = {
        "id": "EID_TOOSIE", "name": "Toosie Slide", "description": "Emote", "rarity": {"value": "icon",
        "displayValue": "Icon Series"}, "type": {"displayValue": "Emote"}, "set": {"value": "Drake"},
        "introduction": {"text": "Introduced in Chapter 2, Season 2."}, "added": "2020-06-01T00:00:00Z",
        "images": {"icon": "https://img/icon.png"}, "shopHistory": ["2020-06-01T00:00:00Z"],
        "series": {"value": "Icon Series", "colors": ["5cf2f3ff"]},
    }
    embed = fortnite.cosmetic_embed(item)
    assert embed.color == discord.Color(0x5CF2F3)
    names = [f.name for f in embed.fields]
    assert {"Rarity", "Type", "Set", "Introduction", "Shop appearances"} <= set(names)


# --------------------------------------------------------------------------- Roblox
def test_roblox_variables():
    profile = roblox.RobloxProfile(
        id=156, name="builderman", display_name="Builderman", description="hi",
        created=datetime(2006, 3, 8, tzinfo=timezone.utc), verified=True, followers=1234567, following=3,
        previous_names=["oldname"], badges=["Administrator", "Veteran"], rap=None,
    )
    values = roblox.profile_variables(profile, now=datetime(2006, 3, 18, tzinfo=timezone.utc))
    assert values["roblox.name"] == "builderman"
    assert values["roblox.followers"] == "1,234,567"
    assert values["roblox.verified"] == "Yes" and values["roblox.banned"] == "No"
    assert values["roblox.premium"] == variables.NA and values["roblox.rap"] == variables.NA
    assert values["roblox.created"] == "March 08, 2006"
    assert values["roblox.created.short"] == "03/08/2006"
    assert values["roblox.created.raw"] == str(int(profile.created.timestamp()))
    assert values["roblox.age"] == "10"
    assert values["roblox.badges"] == "Administrator, Veteran"
    assert values["roblox.url"] == "https://www.roblox.com/users/156/profile"


def test_roblox_dates():
    assert roblox._parse_date("2006-02-27T21:06:40.3Z") == datetime(2006, 2, 27, 21, 6, 40, 300000, tzinfo=timezone.utc)
    assert roblox._parse_date("2024-01-01T00:00:00.1234567Z").microsecond == 123456
    assert roblox._parse_date(None) is None
