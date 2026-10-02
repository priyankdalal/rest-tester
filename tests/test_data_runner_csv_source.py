from __future__ import annotations

import inspect
import itertools
from pathlib import Path

import pytest

from api_tester.data_runner.csv_source import (
    CsvImportSettings,
    CsvSource,
    detect_encoding,
    sniff_dialect,
)


def test_csv_import_settings_round_trip() -> None:
    settings = CsvImportSettings(
        path="rows.csv",
        encoding="auto",
        delimiter="",
        quotechar="",
        has_header=False,
        start_row=2,
        max_rows=10,
        skip_blank_rows=False,
    )
    assert CsvImportSettings.from_dict(settings.to_dict()) == settings


@pytest.mark.parametrize(
    ("sample_text", "expected"),
    [
        ("id,name\n1,Alice\n2,Bob\n", (",", '"')),
        ("id;name\n1;Alice\n2;Bob\n", (";", '"')),
        ("id\tname\n1\tAlice\n2\tBob\n", ("\t", '"')),
    ],
)
def test_sniff_dialect_detects_common_delimiters(
    sample_text: str, expected: tuple[str, str]
) -> None:
    assert sniff_dialect(sample_text) == expected


def test_detect_encoding_prefers_bom_and_falls_back_to_cp1252(tmp_path: Path) -> None:
    utf8_bom = tmp_path / "utf8-bom.csv"
    utf8_bom.write_bytes(b"\xef\xbb\xbfcol\nvalue\n")
    assert detect_encoding(str(utf8_bom)) == "utf-8-sig"

    utf8_plain = tmp_path / "utf8.csv"
    utf8_plain.write_text("col\nvalue\n", encoding="utf-8")
    assert detect_encoding(str(utf8_plain)) == "utf-8"

    cp1252_file = tmp_path / "cp1252.csv"
    cp1252_file.write_bytes("caf\xe9\n".encode("cp1252"))
    assert detect_encoding(str(cp1252_file)) == "cp1252"


def test_preview_profiles_blank_rows_duplicates_and_iteration(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    path.write_text(
        "id,amount,flag,text,empty\n"
        "1,1.5,yes,hello,\n"
        "2,2.75,no,world,\n"
        "2,2.75,no,world,\n"
        " , , , , \n"
        "3,4.0,true,third,\n",
        encoding="utf-8",
    )

    source = CsvSource(CsvImportSettings(path=str(path), skip_blank_rows=True))
    preview = source.preview(sample_rows=10, profile_scan_rows=10)

    assert preview.columns == ("id", "amount", "flag", "text", "empty")
    assert preview.total_rows_sampled == 5
    assert preview.duplicate_row_count == 1
    assert preview.blank_row_count == 1
    assert preview.rows == (
        {"id": "1", "amount": "1.5", "flag": "yes", "text": "hello", "empty": ""},
        {"id": "2", "amount": "2.75", "flag": "no", "text": "world", "empty": ""},
        {"id": "2", "amount": "2.75", "flag": "no", "text": "world", "empty": ""},
        {"id": "3", "amount": "4.0", "flag": "true", "text": "third", "empty": ""},
    )

    profiles = {profile.name: profile for profile in preview.column_profiles}
    assert profiles["id"].inferred_type == "integer"
    assert profiles["amount"].inferred_type == "number"
    assert profiles["flag"].inferred_type == "boolean"
    assert profiles["text"].inferred_type == "string"
    assert profiles["empty"].inferred_type == "empty"
    assert profiles["empty"].blank_count == 5
    assert profiles["text"].sample_values == ("hello", "world", "third")

    rows = list(source.iter_rows())
    assert rows == [
        (1, {"id": "1", "amount": "1.5", "flag": "yes", "text": "hello", "empty": ""}),
        (2, {"id": "2", "amount": "2.75", "flag": "no", "text": "world", "empty": ""}),
        (3, {"id": "2", "amount": "2.75", "flag": "no", "text": "world", "empty": ""}),
        (4, {"id": "3", "amount": "4.0", "flag": "true", "text": "third", "empty": ""}),
    ]
    assert source.total_row_count() == 4


def test_preview_auto_detects_semicolon_delimiter_and_utf8_bom(tmp_path: Path) -> None:
    semicolon = tmp_path / "semicolon.csv"
    semicolon.write_text("id;name\n1;Alice\n2;Bob\n", encoding="utf-8")

    semicolon_source = CsvSource(
        CsvImportSettings(path=str(semicolon), delimiter="", quotechar="")
    )
    semicolon_preview = semicolon_source.preview(sample_rows=2, profile_scan_rows=10)
    assert semicolon_preview.columns == ("id", "name")
    assert semicolon_preview.rows[0] == {"id": "1", "name": "Alice"}

    bom_path = tmp_path / "utf8-sig.csv"
    bom_path.write_bytes(b"\xef\xbb\xbfid,name\n1,Alice\n")
    bom_source = CsvSource(
        CsvImportSettings(path=str(bom_path), encoding="auto")
    )
    bom_preview = bom_source.preview(sample_rows=2, profile_scan_rows=10)
    assert bom_preview.columns == ("id", "name")
    assert list(bom_source.iter_rows()) == [(1, {"id": "1", "name": "Alice"})]


def test_headerless_preview_synthesizes_column_names(tmp_path: Path) -> None:
    path = tmp_path / "headerless.csv"
    path.write_text("1,alpha\n2,beta\n", encoding="utf-8")

    source = CsvSource(CsvImportSettings(path=str(path), has_header=False))
    preview = source.preview(sample_rows=2, profile_scan_rows=10)

    assert preview.columns == ("Column1", "Column2")
    assert preview.rows == (
        {"Column1": "1", "Column2": "alpha"},
        {"Column1": "2", "Column2": "beta"},
    )


def test_iter_rows_honors_start_row_and_max_rows(tmp_path: Path) -> None:
    path = tmp_path / "slice.csv"
    path.write_text(
        "id,name\n"
        "1,Alice\n"
        "2,Bob\n"
        "3,Carla\n"
        "4,Diego\n",
        encoding="utf-8",
    )

    source = CsvSource(
        CsvImportSettings(path=str(path), start_row=1, max_rows=2, skip_blank_rows=True)
    )
    assert list(source.iter_rows()) == [
        (1, {"id": "2", "name": "Bob"}),
        (2, {"id": "3", "name": "Carla"}),
    ]
    assert source.total_row_count() == 2


def test_total_row_count_can_include_blank_rows_when_requested(tmp_path: Path) -> None:
    path = tmp_path / "count.csv"
    path.write_text("id\n1\n\n2\n", encoding="utf-8")

    skipping = CsvSource(CsvImportSettings(path=str(path), skip_blank_rows=True))
    keeping = CsvSource(CsvImportSettings(path=str(path), skip_blank_rows=False))

    assert skipping.total_row_count() == 2
    assert keeping.total_row_count() == 3


def test_iter_rows_is_a_generator_and_streams_incrementally(tmp_path: Path) -> None:
    path = tmp_path / "large.csv"
    lines = ["id,value"] + [f"{index},row-{index}" for index in range(1, 5001)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    source = CsvSource(CsvImportSettings(path=str(path)))
    generator = source.iter_rows()

    assert inspect.isgenerator(generator)
    first_rows = list(itertools.islice(generator, 5))
    assert first_rows[0] == (1, {"id": "1", "value": "row-1"})
    assert first_rows[-1] == (5, {"id": "5", "value": "row-5"})


def test_missing_file_errors_propagate(tmp_path: Path) -> None:
    missing = tmp_path / "missing.csv"
    source = CsvSource(CsvImportSettings(path=str(missing)))

    with pytest.raises((FileNotFoundError, OSError)):
        next(source.iter_rows())

    with pytest.raises((FileNotFoundError, OSError)):
        source.preview()
