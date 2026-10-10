"""The channel-id resolver.

Regression cover for a real bug: a YouTube channel page embeds the channel id
of every channel it links to, and the first `"channelId"` in the HTML is
usually a shelf video's uploader, not the page's own channel. Taking it would
silently point a source at the wrong feed.
"""

from src.main import resolve_channel_id

OWN = "UCB_qr75-ydFVKSF9Dmo6izg"
OTHER = "UCd8iY-kEHtaB8qt8MH--zGw"


def test_canonical_link_wins_over_an_earlier_unrelated_channel_id():
    html = (
        '<script>{"channelId":"%s"}</script>'
        '<link rel="canonical" href="https://www.youtube.com/channel/%s">'
    ) % (OTHER, OWN)
    assert resolve_channel_id(html) == OWN


def test_og_url_is_used_when_there_is_no_canonical_link():
    html = (
        '{"channelId":"%s"}'
        '<meta property="og:url" content="https://www.youtube.com/channel/%s">'
    ) % (OTHER, OWN)
    assert resolve_channel_id(html) == OWN


def test_external_id_is_the_last_resort():
    html = '{"channelId":"%s","externalId":"%s"}' % (OTHER, OWN)
    assert resolve_channel_id(html) == OWN


def test_a_bare_channel_id_is_never_trusted():
    """Better to tell the user to look than to hand them a wrong channel."""
    assert resolve_channel_id('{"channelId":"%s"}' % OTHER) is None


def test_no_match_returns_none():
    assert resolve_channel_id("<html>nothing here</html>") is None
