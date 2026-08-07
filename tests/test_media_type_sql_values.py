"""
Tests for _media_type_sql_values, which maps a user-facing media_type filter
onto a SQL WHERE fragment plus bound parameters.

The contract worth protecting: known types produce a *parameterless* fragment
with literals baked in, and anything unrecognized falls back to a parameterized
equality check. Callers extend their params list with whatever comes back, so
the fragment's placeholder count must always match the params length.
"""
import pytest

import main


@pytest.mark.parametrize("alias", ["book", "books", "BOOK", "  Books  "])
def test_book_aliases(alias):
    clause, params = main._media_type_sql_values(alias)
    assert clause == "LOWER(TRIM(media_type)) = 'book'"
    assert params == []


@pytest.mark.parametrize("alias", ["movie", "movies", "MOVIES", " Movie "])
def test_movie_aliases(alias):
    clause, params = main._media_type_sql_values(alias)
    assert clause == "LOWER(TRIM(media_type)) = 'movie'"
    assert params == []


@pytest.mark.parametrize("alias", [
    "game", "games", "video_game", "video game", "video games",
    "videogame", "videogames", "VIDEO GAME", "  Games  ",
])
def test_game_aliases_all_map_to_the_in_clause(alias):
    clause, params = main._media_type_sql_values(alias)
    assert clause == "LOWER(TRIM(media_type)) IN ('game', 'video game', 'videogame')"
    assert params == []


def test_unknown_type_falls_back_to_parameterized_equality():
    clause, params = main._media_type_sql_values("comic")
    assert clause == "LOWER(TRIM(media_type)) = ?"
    assert params == ["comic"]


def test_fallback_normalizes_case_and_whitespace():
    clause, params = main._media_type_sql_values("  CoMiC  ")
    assert clause == "LOWER(TRIM(media_type)) = ?"
    assert params == ["comic"]


def test_empty_string_falls_back_with_empty_param():
    clause, params = main._media_type_sql_values("")
    assert clause == "LOWER(TRIM(media_type)) = ?"
    assert params == [""]


def test_none_is_tolerated_and_falls_back():
    """The annotation says str, but the `or ""` guard means None must not raise."""
    clause, params = main._media_type_sql_values(None)
    assert clause == "LOWER(TRIM(media_type)) = ?"
    assert params == [""]


@pytest.mark.parametrize("media_type", [
    "book", "movie", "game", "comic", "", "video game",
])
def test_placeholder_count_always_matches_param_count(media_type):
    """
    Guards the actual integration contract: list_media does
    `params.extend(mt_params)` against this fragment, so a mismatch here
    becomes a sqlite3.ProgrammingError at query time.
    """
    clause, params = main._media_type_sql_values(media_type)
    assert clause.count("?") == len(params)