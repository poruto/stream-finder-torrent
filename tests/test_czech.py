from streamfinder.matching import parse_release
from streamfinder.torrents.providers import RawTorrent
from streamfinder.torrents.search import (
    SearchQuery,
    TorrentSearcher,
    choose_picks,
    czech_kind,
)
from streamfinder.torrents.sktorrent import SkTorrentProvider, scene_name


def test_scene_name_movie_with_two_titles():
    name = scene_name(
        "Interstellar / Interstellar (2014)(CZ/EN)[H265][1080p] = CSFD 85%",
        ["cz", "gb"],
        19,
        ["Interstellar"],
    )
    info = parse_release(name)
    assert (
        info.title == "interstellar" and info.year == 2014 and info.resolution == 1080
    )
    assert czech_kind(info) == "dub"


def test_scene_name_czech_series_season():
    name = scene_name(
        "Perníkový táta / Breaking Bad 3. série (2010)(CZ/EN)[1080p][HEVC] = CSFD 93%",
        ["cz", "gb"],
        16,
        ["Breaking Bad", "Perníkový táta"],
    )
    info = parse_release(name)
    assert info.seasons == [3] and info.year == 2010
    assert "breaking bad" in info.title or "pernikovy tata" in info.title


def test_scene_name_subtitled_category():
    name = scene_name("Shogun (2024)(CZTit)[1080p]", ["cz"], 15, ["Shogun"])
    assert czech_kind(parse_release(name)) == "subs"


def test_czech_title_stops_at_language_tags():
    assert parse_release("Avatar 3D CZ Extended Edition").title == "avatar"


def test_czech_queries_and_cache_key():
    q = SearchQuery(
        title="Cosy Dens",
        titles=["Cosy Dens", "Pelíšky"],
        year=1999,
        czech=True,
        local_titles=["Pelisky"],
    )
    queries = q.text_queries()
    assert "Pelisky 1999" in queries and "Cosy Dens 1999 CZ" in queries
    assert (
        q.cache_key()
        != SearchQuery(title="Cosy Dens", titles=[], year=1999).cache_key()
    )


def test_czech_preference_recommends_dub():
    q = SearchQuery(title="Shrek", titles=["Shrek"], year=2001, czech=True)
    raw = [
        RawTorrent(
            "Shrek 2001 1080p BluRay x264 AAC-YTS",
            "a" * 40,
            2 * 1024**3,
            500,
            10,
            "YTS",
        ),
        RawTorrent(
            "Shrek 2001 CZ dabing 1080p",
            "b" * 40,
            4 * 1024**3,
            30,
            1,
            "SkTorrent",
            title="Shrek (2001)(CZ)[1080p]",
        ),
    ]
    ranked = TorrentSearcher._rank(raw, q)
    assert choose_picks(ranked, None, czech=True)["recommended"] == "b" * 40
    plain = TorrentSearcher._rank(
        raw, SearchQuery(title="Shrek", titles=["Shrek"], year=2001)
    )
    assert choose_picks(plain)["recommended"] == "a" * 40
    shown = next(r for r in ranked if r.raw.info_hash == "b" * 40).to_dict()
    assert shown["name"] == "Shrek (2001)(CZ)[1080p]" and shown["czech"] == "dub"


PAGE = """
<TD align="center" class=lista valign="top"><a href=torrents_v2.php?category=1 title='x'><b>Filmy</b></a>
<img src=https://sktorrent.eu/torrent/images/flag/cz.png>
<A HREF="details.php?name=Pelisky&id=0123456789abcdef0123456789abcdef01234567" title="x"><img src="x"><br>Pelíšky (1999)(CZ)[1080p] = CSFD 91%</A>
<br>Velkost 2.3 GB | Pridany 11/10/2026<br>Odosielaju : 19<br>Stahuju : 2</div></td>
<TD align="center" class=lista valign="top"><a href=torrents_v2.php?category=18 title='x'><b>Hry</b></a>
<A HREF="details.php?name=Game&id=fedcba9876543210fedcba9876543210fedcba98" title="x"><br>Pelisky game</A>
<br>Velkost 1 GB<br>Odosielaju : 5<br>Stahuju : 0</div></td>
"""


def test_sktorrent_parses_results_and_skips_other_categories():
    provider = SkTorrentProvider("", "", 5)
    found = provider.parse(PAGE, ["Pelíšky"], {1, 19})
    assert len(found) == 1
    item = found[0]
    assert item.info_hash == "0123456789abcdef0123456789abcdef01234567"
    assert item.seeders == 19 and item.leechers == 2 and item.size > 2 * 1024**3
    assert item.title == "Pelíšky (1999)(CZ)[1080p]"
    assert czech_kind(parse_release(item.name)) == "dub"


def test_private_trackers_only_for_sktorrent_results():
    provider = SkTorrentProvider("user", "secret", 5)
    provider._announce = ["https://sktorrent.eu/torrent/announce.php?pid=x"]
    provider._seen["a" * 40] = None
    assert provider.extra_trackers("A" * 40)[0].endswith("pid=x")
    assert provider.extra_trackers("b" * 40) == []
