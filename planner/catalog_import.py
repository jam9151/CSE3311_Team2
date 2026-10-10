"""Pulling real titles into the catalog from outside sources.

    Books         Open Library    no key needed
    Movies, TV    TMDB            free key, TMDB_API_KEY
    Games         RAWG            free key, RAWG_API_KEY

Each source turns what the API returns into Records, and save_record() writes a
Record to the catalog. Nothing here talks to the network except HttpFetcher, so
the sources are tested with a fake one. Run it through
`python manage.py import_catalog`.

The only number the scheduler needs is how long something takes, so every
record ends up as typical_minutes: a runtime for a film, episodes times the
episode length for a show, pages times a reading pace for a book, and average
hours played for a game.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from .models import CatalogItem, MediaType

# 230 words a minute at about 300 words a page. A rough pace, same spirit as
# the hand-entered reading times in the seed data.
MINUTES_PER_PAGE = 1.3

MIN_FILM_MINUTES = 40
MAX_FILM_MINUTES = 300
# Daytime soaps run to thousands of episodes. Nobody is planning those.
MAX_TV_MINUTES = 25_000
MIN_BOOK_PAGES = 80
MAX_BOOK_PAGES = 1500
MAX_GAME_HOURS = 300

# TMDB genre names that read differently from the ones in our seed data, so
# the Discover genre filter doesn't end up with two spellings of one thing.
GENRE_ALIASES = {
    "Science Fiction": "Science fiction",
    "Sci-Fi & Fantasy": "Science fiction",
    "Action & Adventure": "Action",
    "War & Politics": "War",
}
# Not something you start for an evening of planned viewing.
SKIP_TV_GENRES = {"Talk", "News", "Soap"}


class ImportFailure(Exception):
    """A source can't continue: bad key, rate limited out, or the network is down."""


def normalize_genre(name):
    name = (name or "").strip()
    return GENRE_ALIASES.get(name, name)


def parse_date(value):
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def clip(value, limit):
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def tidy_description(text, limit=700):
    """First paragraph of a blurb, cut at a sentence end when it runs long."""
    text = (text or "").replace("\r", "").strip()
    # Open Library blurbs often continue with a ---------- rule and extra notes.
    text = text.split("\n\n----------")[0].split("\n----------")[0]
    text = text.split("\n\n")[0].strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…"


def one_decimal(value, scale=1.0):
    """A rating scaled to out-of-10, or None when the source has no real score."""
    try:
        number = float(value) * scale
    except (TypeError, ValueError):
        return None
    return round(number, 1) if number > 0 else None


@dataclass
class Record:
    source: str
    source_id: str
    title: str
    media_type: str
    typical_minutes: int
    genre: str = ""
    creator: str = ""
    description: str = ""
    release_date: date | None = None
    rating: float | None = None


class HttpFetcher:
    """GET some JSON, politely: identify ourselves, pace calls, back off when told to."""

    def __init__(self, user_agent, pause=0.0, retries=3, timeout=30, sleep=time.sleep):
        self.user_agent = user_agent
        self.pause = pause
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep

    def get_json(self, url, params=None, headers=None):
        """Parsed JSON, or None if the server says the thing doesn't exist."""
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        request = urllib.request.Request(
            f"{url}?{query}" if query else url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})},
        )

        last_problem = "no response"
        for attempt in range(1, self.retries + 1):
            self.sleep(self.pause)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                if error.code in (401, 403):
                    # The message names the URL without its query string, which is
                    # where an API key would be.
                    raise ImportFailure(
                        f"{url} refused the request ({error.code}). Check that the API key is right."
                    ) from error
                last_problem = f"HTTP {error.code}"
                if error.code == 429 or error.code >= 500:
                    wait = error.headers.get("Retry-After", "") if error.headers else ""
                    self.sleep(int(wait) if wait.isdigit() else 2**attempt)
                    continue
                raise ImportFailure(f"{url} returned {error.code}.") from error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_problem = str(getattr(error, "reason", error))
                self.sleep(2**attempt)
        raise ImportFailure(f"Gave up on {url} after {self.retries} tries ({last_problem}).")


class Source:
    """Base class: `records(limit)` yields Records, `skipped` counts what it dropped and why."""

    name = ""
    media_type = ""
    key_env = ""  # empty means no key needed

    def __init__(self, fetcher, key=""):
        self.fetcher = fetcher
        self.key = key
        self.skipped = Counter()

    @classmethod
    def pause(cls, has_contact):
        return 0.1

    def records(self, limit):  # pragma: no cover - overridden
        raise NotImplementedError


# ---------------------------------------------------------------- books


# label shown in Discover, Open Library subject to search, and words that mark
# a tag as belonging to that genre. See best_book_label for how they are used.
BOOK_SUBJECTS = [
    ("Young adult", "young_adult_fiction", ("young adult",)),
    ("Mystery", "mystery_and_detective_stories", ("mystery", "detective")),
    ("Horror", "horror", ("horror",)),
    ("Classics", "classics", ("classic",)),
    ("Historical fiction", "historical_fiction", ("historical fiction", "fiction, historical")),
    ("Thriller", "thrillers", ("thriller", "suspense")),
    ("Fantasy", "fantasy", ("fantasy",)),
    ("Science fiction", "science_fiction", ("science fiction", "science-fiction", "sci-fi")),
    ("Romance", "romance", ("romance",)),
    ("Biography", "biography", ("biograph",)),
    ("History", "history", ("history",)),
    ("Psychology", "psychology", ("psycholog",)),
    ("Self-help", "self-help", ("self-help", "self help")),
    ("Humor", "humor", ("humor", "humour")),
]


def best_book_label(subjects):
    """The genre a book's own subject tags point to most, or None if none of ours.

    Open Library's subject search is loose, and a work's tags merge every
    edition, so a book can be found under a genre it only brushes against:
    Harry Potter under humor, Sapiens under graphic novels. Counting the tags
    for each genre and taking the biggest gives the answer a reader would give.
    A tie goes to the earlier genre in the list above.
    """
    tags = [str(tag).lower() for tag in subjects or []]
    best, best_score = None, 0
    for label, _, keywords in BOOK_SUBJECTS:
        score = sum(1 for tag in tags if any(word in tag for word in keywords))
        if score > best_score:
            best, best_score = label, score
    return best


class BookSource(Source):
    name = "books"
    media_type = MediaType.BOOK
    API = "https://openlibrary.org"

    @classmethod
    def pause(cls, has_contact):
        # Open Library allows 3 calls a second to clients that say who they are,
        # and 1 a second to those that don't.
        return 0.4 if has_contact else 1.1

    def records(self, limit):
        per_subject = math.ceil(limit / len(BOOK_SUBJECTS))
        seen, seen_titles, produced = set(), set(), 0

        for label, subject, _ in BOOK_SUBJECTS:
            taken = 0
            for page in (1, 2, 3):
                data = self.fetcher.get_json(
                    f"{self.API}/search.json",
                    {
                        "q": f"subject:{subject} language:eng",
                        "sort": "readinglog",
                        "limit": 100,
                        "page": page,
                        "fields": "key,title,author_name,first_publish_year,number_of_pages_median,"
                        "ratings_average,ratings_count,subject",
                    },
                )
                docs = (data or {}).get("docs") or []
                if not docs:
                    break
                for doc in docs:
                    key = doc.get("key")
                    if not key or key in seen:
                        continue
                    if best_book_label(doc.get("subject")) != label:
                        # Not left in `seen`: it is taken under the genre it fits best.
                        continue
                    seen.add(key)
                    # Open Library keeps duplicate records for some books.
                    name = (
                        (doc.get("title") or "").strip().lower(),
                        (doc.get("author_name") or [""])[0].strip().lower(),
                    )
                    if name in seen_titles:
                        self.skipped["duplicate work"] += 1
                        continue
                    record = self._record(doc, label)
                    if record is None:
                        continue
                    seen_titles.add(name)
                    yield record
                    taken += 1
                    produced += 1
                    if produced >= limit:
                        return
                    if taken >= per_subject:
                        break
                if taken >= per_subject:
                    break

    def _record(self, doc, genre):
        title = (doc.get("title") or "").strip()
        authors = doc.get("author_name") or []
        pages = doc.get("number_of_pages_median") or 0
        if not title or not authors:
            self.skipped["no title or author"] += 1
            return None
        if not MIN_BOOK_PAGES <= pages <= MAX_BOOK_PAGES:
            self.skipped["no usable page count"] += 1
            return None

        year = doc.get("first_publish_year")
        release = date(year, 1, 1) if isinstance(year, int) and 1000 <= year <= date.today().year + 1 else None
        return Record(
            source="openlibrary",
            source_id=doc["key"].rsplit("/", 1)[-1],
            title=clip(title, 200),
            media_type=MediaType.BOOK,
            typical_minutes=round(pages * MINUTES_PER_PAGE),
            genre=genre,
            creator=clip(authors[0], 160),
            description=self._description(doc["key"]),
            release_date=release,
            rating=one_decimal(doc.get("ratings_average"), scale=2),  # they score out of 5
        )

    def _description(self, key):
        work = self.fetcher.get_json(f"{self.API}{key}.json") or {}
        text = work.get("description")
        if isinstance(text, dict):
            text = text.get("value")
        return tidy_description(text if isinstance(text, str) else "")


# ---------------------------------------------------------------- TMDB


class TmdbSource(Source):
    """Shared by movies and TV: the key, the base URL, and walking two lists."""

    key_env = "TMDB_API_KEY"
    API = "https://api.themoviedb.org/3"
    lists = ("top_rated", "popular")
    kind = ""  # "movie" or "tv"

    @classmethod
    def pause(cls, has_contact):
        return 0.05

    def _get(self, path, **params):
        # TMDB hands out a short v3 key and a long v4 token. Either one works.
        if self.key.startswith("eyJ") or len(self.key) > 40:
            headers = {"Authorization": f"Bearer {self.key}"}
        else:
            headers, params = {}, {**params, "api_key": self.key}
        return self.fetcher.get_json(f"{self.API}{path}", params, headers)

    def records(self, limit):
        tried, produced = set(), 0
        for page in range(1, 101):
            got_any = False
            for which in self.lists:
                data = self._get(f"/{self.kind}/{which}", page=page, language="en-US")
                for row in (data or {}).get("results") or []:
                    got_any = True
                    item_id = row.get("id")
                    if not item_id or item_id in tried:
                        continue
                    tried.add(item_id)
                    record = self._record(item_id)
                    if record is None:
                        continue
                    yield record
                    produced += 1
                    if produced >= limit:
                        return
            if not got_any:
                return

    def _record(self, item_id):  # pragma: no cover - overridden
        raise NotImplementedError


class MovieSource(TmdbSource):
    name = "movies"
    media_type = MediaType.MOVIE
    kind = "movie"

    def _record(self, item_id):
        data = self._get(f"/movie/{item_id}", append_to_response="credits", language="en-US")
        if not data or not data.get("title"):
            self.skipped["details not found"] += 1
            return None
        runtime = data.get("runtime") or 0
        if not MIN_FILM_MINUTES <= runtime <= MAX_FILM_MINUTES:
            self.skipped["no usable runtime"] += 1
            return None

        crew = (data.get("credits") or {}).get("crew") or []
        director = next((p.get("name") for p in crew if p.get("job") == "Director"), "")
        genres = data.get("genres") or []
        return Record(
            source="tmdb",
            source_id=f"movie-{item_id}",
            title=clip(data["title"], 200),
            media_type=MediaType.MOVIE,
            typical_minutes=runtime,
            genre=clip(normalize_genre(genres[0].get("name")) if genres else "", 60),
            creator=clip(director, 160),
            description=tidy_description(data.get("overview")),
            release_date=parse_date(data.get("release_date")),
            rating=one_decimal(data.get("vote_average")),
        )


class TvSource(TmdbSource):
    name = "tv"
    media_type = MediaType.TV
    kind = "tv"

    def _record(self, item_id):
        data = self._get(f"/tv/{item_id}", language="en-US")
        if not data or not data.get("name"):
            self.skipped["details not found"] += 1
            return None

        genres = [g.get("name") for g in data.get("genres") or []]
        if SKIP_TV_GENRES & set(genres):
            self.skipped["talk, news or soap"] += 1
            return None

        # episode_run_time is deprecated and often empty, so fall back to the
        # length of an episode that actually aired.
        runtime = next(iter(data.get("episode_run_time") or []), 0) or (
            (data.get("last_episode_to_air") or {}).get("runtime")
            or (data.get("next_episode_to_air") or {}).get("runtime")
            or 0
        )
        episodes = data.get("number_of_episodes") or 0
        total = episodes * runtime
        if total <= 0:
            self.skipped["no usable runtime"] += 1
            return None
        if total > MAX_TV_MINUTES:
            self.skipped["too long to plan"] += 1
            return None

        creators = data.get("created_by") or []
        networks = data.get("networks") or []
        creator = (creators[0].get("name") if creators else "") or (networks[0].get("name") if networks else "")
        return Record(
            source="tmdb",
            source_id=f"tv-{item_id}",
            title=clip(data["name"], 200),
            media_type=MediaType.TV,
            typical_minutes=total,
            genre=clip(normalize_genre(genres[0]) if genres else "", 60),
            creator=clip(creator, 160),
            description=tidy_description(data.get("overview")),
            release_date=parse_date(data.get("first_air_date")),
            rating=one_decimal(data.get("vote_average")),
        )


# ---------------------------------------------------------------- games


class GameSource(Source):
    name = "games"
    media_type = MediaType.GAME
    key_env = "RAWG_API_KEY"
    API = "https://api.rawg.io/api"
    PAGE_SIZE = 40

    def records(self, limit):
        produced = 0
        for page in range(1, 101):
            data = self.fetcher.get_json(
                f"{self.API}/games",
                {"key": self.key, "page_size": self.PAGE_SIZE, "page": page, "ordering": "-added"},
            )
            rows = (data or {}).get("results") or []
            if not rows:
                return
            for row in rows:
                record = self._record(row)
                if record is None:
                    continue
                yield record
                produced += 1
                if produced >= limit:
                    return

    def _record(self, row):
        game_id = row.get("id")
        if not game_id or not row.get("name"):
            self.skipped["no title"] += 1
            return None
        # The detail call has the blurb and the developer. It also carries
        # playtime, so a list that leaves it out doesn't lose us the game.
        detail = self.fetcher.get_json(f"{self.API}/games/{game_id}", {"key": self.key}) or {}
        hours = detail.get("playtime") or row.get("playtime") or 0
        if not 1 <= hours <= MAX_GAME_HOURS:
            self.skipped["no usable playtime"] += 1
            return None

        genres = row.get("genres") or detail.get("genres") or []
        developers = detail.get("developers") or []
        metacritic = row.get("metacritic") or detail.get("metacritic")
        rating = one_decimal(metacritic, scale=0.1) if metacritic else one_decimal(row.get("rating"), scale=2)
        return Record(
            source="rawg",
            source_id=str(game_id),
            title=clip(row["name"], 200),
            media_type=MediaType.GAME,
            typical_minutes=int(hours) * 60,
            genre=clip(normalize_genre(genres[0].get("name")) if genres else "", 60),
            creator=clip(developers[0].get("name") if developers else "", 160),
            description=tidy_description(detail.get("description_raw")),
            release_date=parse_date(row.get("released") or detail.get("released")),
            rating=rating,
        )


SOURCES = {cls.name: cls for cls in (MovieSource, TvSource, BookSource, GameSource)}


# ---------------------------------------------------------------- saving


FIELDS = ["title", "media_type", "genre", "creator", "description", "release_date", "rating", "typical_minutes"]


def save_record(record):
    """Write one Record. Returns "created", "updated" or "duplicate".

    A row is matched by where it came from, so a second run refreshes it.
    Anything already in the catalog from the hand-entered seed data, with the
    same title, type and year, wins and the imported copy is dropped.
    """
    values = {field: getattr(record, field) for field in FIELDS}

    existing = CatalogItem.objects.filter(source=record.source, source_id=record.source_id).first()
    if existing:
        for field, value in values.items():
            setattr(existing, field, value)
        existing.save()
        return "updated"

    curated = CatalogItem.objects.filter(source="", media_type=record.media_type, title__iexact=record.title)
    for item in curated:
        if item.release_date is None or record.release_date is None or item.release_date.year == record.release_date.year:
            return "duplicate"

    CatalogItem.objects.create(source=record.source, source_id=record.source_id, **values)
    return "created"
