"""
Tests for _infer_media_type_from_text and its single-string helper _infer_one.

Two behaviors are load-bearing and easy to break in a refactor, so they're
pinned explicitly:

  1. WITHIN one string, keyword families are checked in a fixed order —
     book, then movie, then game. A string matching two families resolves to
     the earlier one.
  2. ACROSS candidates, the first candidate that yields a non-"unknown" answer
     wins. That's what makes the category -> title -> description fallback work.
"""
import pytest

import main


# --- _infer_one: single-string classification -------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Paperback", "book"),
    ("Hardcover Book", "book"),
    ("ISBN 9780134685991", "book"),
    ("A Novel", "book"),
    ("Audiobook", "book"),
    ("Graphic Novel", "book"),
    ("Textbook", "book"),
    ("DVD", "movie"),
    ("Blu-ray", "movie"),
    ("BluRay", "movie"),
    ("Blu Ray", "movie"),
    ("4K UHD", "movie"),
    ("Steelbook", "movie"),
    ("Includes Digital Code", "movie"),
    ("Film", "movie"),
    ("PlayStation 5", "game"),
    ("PS4", "game"),
    ("Xbox Series X", "game"),
    ("Nintendo Switch", "game"),
    ("Wii U", "game"),
    ("Steam Key", "game"),
    ("Video Game", "game"),
])
def test_infer_one_recognizes_each_keyword_family(text, expected):
    assert main._infer_one(text) == expected


@pytest.mark.parametrize("text", [
    "Grocery",
    "Kitchen Appliances",
    "",
    "12345",
])
def test_infer_one_returns_unknown_for_unrelated_text(text):
    assert main._infer_one(text) == "unknown"


def test_infer_one_is_case_insensitive():
    assert main._infer_one("PAPERBACK") == "book"
    assert main._infer_one("bLu-RaY") == "movie"
    assert main._infer_one("nInTeNdO") == "game"


# --- _infer_one: documented precedence and substring quirks -----------------

def test_book_wins_over_movie_within_one_string():
    # "steelbook" contains "book", so the book family matches first.
    assert main._infer_one("Steelbook Edition") == "book"
    assert main._infer_one("DVD and Book Bundle") == "book"


def test_movie_wins_over_game_within_one_string():
    assert main._infer_one("Halo DVD Xbox Bundle") == "movie"


def test_matching_is_substring_not_word_boundary():
    """
    Documents a known sharp edge: matching is a plain `in` check, so keywords
    embedded in longer words still match. If this ever needs to become
    word-boundary matching, these are the assertions that will flip.
    """
    assert main._infer_one("Filmmaking Masterclass") == "movie"   # "film"
    assert main._infer_one("Gamer Chair") == "game"               # "game"
    assert main._infer_one("Bookshelf") == "book"                 # "book"


# --- _infer_media_type_from_text: candidate fallback ------------------------

def test_first_candidate_wins_when_it_matches():
    assert main._infer_media_type_from_text("DVD", "Some Paperback") == "movie"


def test_falls_through_to_second_candidate():
    # Real shape: category is useless, title identifies it.
    assert main._infer_media_type_from_text("Electronics", "Halo 5 for Xbox One") == "game"


def test_falls_through_to_third_candidate():
    # category -> title -> description, the exact chain lookup_upc_upcdatabase uses.
    assert main._infer_media_type_from_text(
        "Media",
        "Inception",
        "Blu-ray disc, widescreen edition",
    ) == "movie"


def test_skips_none_candidates():
    assert main._infer_media_type_from_text(None, None, "Hardcover") == "book"


def test_skips_empty_and_falsy_candidates():
    assert main._infer_media_type_from_text("", None, "", "PlayStation 5") == "game"


def test_returns_unknown_when_no_candidate_matches():
    assert main._infer_media_type_from_text("Misc", "Widget", "A thing") == "unknown"


def test_returns_unknown_with_no_candidates():
    assert main._infer_media_type_from_text() == "unknown"


def test_returns_unknown_when_all_candidates_are_none():
    assert main._infer_media_type_from_text(None, None, None) == "unknown"