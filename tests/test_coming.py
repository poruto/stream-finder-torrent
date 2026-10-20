from streamfinder import create_app, i18n
from streamfinder.views import card, coming_label


def label(date, lang="en"):
    app = create_app(testing=True)
    with app.test_request_context(headers={"Cookie": f"{i18n.COOKIE}={lang}"}):
        return coming_label({"release_date": date})


def test_unreleased_title_gets_its_release_date():
    assert label("2999-12-18") == "Out 18 Dec 2999"
    assert label("2999-12-18", "cs") == "Vyjde 18. 12. 2999"


def test_released_or_undated_titles_get_none():
    assert label("2001-05-01") is None
    assert label("") is None
    assert label("2999") is None


def test_card_carries_the_label():
    app = create_app(testing=True)
    with app.test_request_context():
        item = card({"id": 1, "title": "Future", "poster_path": "/p.jpg", "release_date": "2999-01-02"}, "movie")
        assert item["coming"]
        assert card({"id": 2, "title": "Past", "poster_path": "/p.jpg", "release_date": "1999-01-02"}, "movie")["coming"] is None
