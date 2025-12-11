"""
Crawl the iQiyi trending movies chart with requests and BeautifulSoup.
The script respects the robots.txt rules before fetching pages or images,
keeps basic movie info in ``result.txt`` and stores poster images in
``picture``.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

TRENDING_URL = "https://www.iqiyi.com/trending/"
ROBOTS_URL = "https://www.iqiyi.com/robots.txt"
OUTPUT_TEXT = Path("result.txt")
PICTURE_DIR = Path("picture")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class RobotsGate:
    """Light wrapper around :class:`RobotFileParser` with requests support."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.parser = RobotFileParser(ROBOTS_URL)

    def load(self) -> None:
        response = self.session.get(ROBOTS_URL, timeout=10)
        response.raise_for_status()
        self.parser.parse(response.text.splitlines())

    def allowed(self, url: str) -> bool:
        return self.parser.can_fetch(USER_AGENT, url)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.iqiyi.com/",
    })
    return session


def fetch_page(session: requests.Session, robots: RobotsGate, url: str) -> str:
    if not robots.allowed(url):
        raise PermissionError(f"Fetching {url} is disallowed by robots.txt")

    response = session.get(url, timeout=10)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return response.text


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^\w\-\.]+", "_", name).strip("_")
    return safe or "poster"


def extract_json_from_script(text: str) -> Iterable[str]:
    """
    Yield JSON strings from inline script text.

    The trending page usually exposes preloaded data on ``window``. This
    helper extracts JSON-looking blobs after an assignment to avoid
    depending on exact variable names.
    """

    patterns = [
        r"=\s*(\{.*?\})\s*;\s*$",
        r"=\s*JSON.parse\('(.*)'\)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.DOTALL):
            yield match.group(1)


def iter_candidate_lists(payload: Any) -> Iterable[List[Dict[str, Any]]]:
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            yield payload
        for item in payload:
            yield from iter_candidate_lists(item)
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from iter_candidate_lists(value)


def pick_movies_from_data(data: Any) -> List[Dict[str, Any]]:
    title_keys = ["name", "title", "albumName"]
    cover_keys = ["imageUrl", "img", "coverUrl", "poster", "image"]
    desc_keys = ["desc", "description", "brief", "subtitle"]
    score_keys = ["score", "hot", "hotScore", "rating"]
    link_keys = ["url", "playUrl", "pageUrl"]

    for candidate in iter_candidate_lists(data):
        movies: List[Dict[str, Any]] = []
        for idx, item in enumerate(candidate, start=1):
            title = next((item.get(key) for key in title_keys if item.get(key)), None)
            cover = next((item.get(key) for key in cover_keys if item.get(key)), None)
            if not title or not cover:
                continue

            movies.append({
                "rank": item.get("rank") or idx,
                "title": str(title).strip(),
                "poster": str(cover),
                "description": next((item.get(key) for key in desc_keys if item.get(key)), ""),
                "score": next((item.get(key) for key in score_keys if item.get(key)), ""),
                "url": next((item.get(key) for key in link_keys if item.get(key)), ""),
            })
        if movies:
            return movies
    return []


def parse_movies_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")

    movie_cards = []
    for idx, card in enumerate(soup.select("[data-rank], .rank-item"), start=1):
        title_tag = card.select_one(".title, .main-title, .name")
        poster_tag = card.select_one("img")
        if not title_tag or not poster_tag:
            continue
        movie_cards.append({
            "rank": card.get("data-rank") or idx,
            "title": title_tag.get_text(strip=True),
            "poster": poster_tag.get("src") or poster_tag.get("data-src"),
            "description": card.get("data-desc") or "",
            "score": card.get("data-score") or "",
            "url": card.find("a").get("href") if card.find("a") else "",
        })

    if movie_cards:
        return movie_cards

    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue
        for json_blob in extract_json_from_script(text):
            try:
                data = json.loads(json_blob)
            except json.JSONDecodeError:
                continue
            movies = pick_movies_from_data(data)
            if movies:
                return movies

    return []


def download_poster(session: requests.Session, robots: RobotsGate, url: str, rank: int, title: str) -> Optional[Path]:
    if not url:
        return None

    absolute_url = url
    parsed = urlparse(url)
    if not parsed.scheme:
        absolute_url = urljoin(TRENDING_URL, url)

    if not robots.allowed(absolute_url):
        print(f"Skip poster for {title}: disallowed by robots.txt")
        return None

    response = session.get(absolute_url, timeout=20, stream=True)
    response.raise_for_status()

    suffix = os.path.splitext(parsed.path)[1] or ".jpg"
    filename = f"{rank:02d}_{sanitize_filename(title)}{suffix}"
    PICTURE_DIR.mkdir(exist_ok=True)
    poster_path = PICTURE_DIR / filename

    with open(poster_path, "wb") as file:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            file.write(chunk)

    return poster_path


def save_results(movies: List[Dict[str, Any]]) -> None:
    with OUTPUT_TEXT.open("w", encoding="utf-8") as file:
        for movie in movies:
            file.write(
                f"{movie.get('rank')}\t{movie.get('title')}\t"
                f"{movie.get('score')}\t{movie.get('description')}\t{movie.get('url')}\n"
            )


def crawl_trending_movies() -> None:
    session = build_session()
    robots = RobotsGate(session)
    robots.load()

    html = fetch_page(session, robots, TRENDING_URL)
    movies = parse_movies_from_html(html)
    if not movies:
        raise RuntimeError("No movies were parsed from the trending page.")

    enriched_movies: List[Dict[str, Any]] = []
    for movie in movies:
        poster_path = download_poster(
            session,
            robots,
            movie.get("poster", ""),
            int(movie.get("rank") or len(enriched_movies) + 1),
            movie.get("title", "poster"),
        )
        movie["poster_path"] = str(poster_path) if poster_path else ""
        enriched_movies.append(movie)

    save_results(enriched_movies)
    print(f"Saved {len(enriched_movies)} movies to {OUTPUT_TEXT}")


if __name__ == "__main__":
    crawl_trending_movies()
