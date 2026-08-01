"""Tests for normalize_barcode — the first test, mainly proving the harness works."""
import main


def test_strips_non_digits():
    assert main.normalize_barcode("978-0-13-468599-1") == "9780134685991"


def test_handles_spaces_and_letters():
    assert main.normalize_barcode(" abc 123 def 456 ") == "123456"


def test_empty_string():
    assert main.normalize_barcode("") == ""


def test_already_clean():
    assert main.normalize_barcode("012345678905") == "012345678905"


def test_only_non_digits():
    assert main.normalize_barcode("no-digits-here") == ""
