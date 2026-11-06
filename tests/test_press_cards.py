import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from scraper import listing_links, classify

CARD = """<main>
<a href="https://nanoavionics.com/news/nanoavionics-secures-e122-5m-contract"><div class="small-12 columns nopad news-top">PRESS RELEASES 2025-04-03</div>
<div class="small-12 columns nopad news-title">NanoAvionics Secures €122.5M Contract to Build 280 Satellites</div>
<div class="small-12 columns nopad news-excerpt">Initial 280 satellites for Meridian Space.</div>
<div class="news-link">Learn more</div></a>
<a href="https://nanoavionics.com/news/meet-mp42d"><div class="news-title">Meet MP42D, our new bus</div><div class="news-excerpt">A new smallsat.</div></a>
</main>"""

def test_titles_come_from_cards():
    src = {"url": "https://nanoavionics.com/news/", "url_contains": "/news/",
           "link_selector": "a[href*='/news/']", "title_selector": ".news-title",
           "excerpt_selector": ".news-excerpt"}
    links = listing_links(CARD, src)
    assert links[0]["title"] == "NanoAvionics Secures €122.5M Contract to Build 280 Satellites"
    assert links[0]["excerpt"] == "Initial 280 satellites for Meridian Space."
    assert links[1]["title"] == "Meet MP42D, our new bus"
    assert len(links) == 2

def test_alerts_ignore_site_chrome():
    # "contract" appearing only in site navigation must not flag an unrelated article
    assert classify("Meet MP42D, our new bus A new smallsat.", ["contract", "launch"]) == []
