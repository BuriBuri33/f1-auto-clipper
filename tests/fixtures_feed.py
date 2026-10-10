"""A real-shaped YouTube channel Atom feed, trimmed to three entries."""

FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>UCtesta0000000000000000A</yt:channelId>
  <title>Test Channel</title>
  <entry>
    <id>yt:video:AAAAAAAAAA1</id>
    <yt:videoId>AAAAAAAAAA1</yt:videoId>
    <yt:channelId>UCtesta0000000000000000A</yt:channelId>
    <title>Radio: "He went off the track!"</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=AAAAAAAAAA1"/>
    <published>{recent}</published>
    <updated>{recent}</updated>
  </entry>
  <entry>
    <id>yt:video:AAAAAAAAAA2</id>
    <yt:videoId>AAAAAAAAAA2</yt:videoId>
    <yt:channelId>UCtesta0000000000000000A</yt:channelId>
    <title>Second recent upload</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=AAAAAAAAAA2"/>
    <published>{recent2}</published>
    <updated>{recent2}</updated>
  </entry>
  <entry>
    <id>yt:video:AAAAAAAAAA3</id>
    <yt:videoId>AAAAAAAAAA3</yt:videoId>
    <yt:channelId>UCtesta0000000000000000A</yt:channelId>
    <title>An old upload from the back catalogue</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=AAAAAAAAAA3"/>
    <published>{old}</published>
    <updated>{old}</updated>
  </entry>
</feed>
"""


def build_feed(recent, recent2, old):
    return FEED_TEMPLATE.format(recent=recent, recent2=recent2, old=old).encode("utf-8")
