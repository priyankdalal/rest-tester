from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Any, Iterator

_DEFAULT_DELIMITER = ","
_DEFAULT_QUOTECHAR = '"'
_SAMPLE_BYTES = 8192
_COMMON_DELIMITERS = (",", ";", "\t", "|")
_BOOLEAN_VALUES = {"true", "false", "1", "0", "yes", "no"}
_AUTO_VALUES = {"", "auto"}
# Counting accepted rows is a full sequential read, so it is bounded. Past the
# cap the row total is reported as a lower bound and the UI says "or more".
_DEFAULT_COUNT_LIMIT = 200_000


@dataclass(frozen=True)
class CsvImportSettings:
    path: str
    encoding: str = "utf-8"
    delimiter: str = _DEFAULT_DELIMITER
    quotechar: str = _DEFAULT_QUOTECHAR
    has_header: bool = True
    start_row: int = 0
    max_rows: int | None = None
    skip_blank_rows: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "quotechar": self.quotechar,
            "has_header": self.has_header,
            "start_row": self.start_row,
            "max_rows": self.max_rows,
            "skip_blank_rows": self.skip_blank_rows,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CsvImportSettings":
        return cls(
            path=str(data["path"]),
            encoding=str(data.get("encoding", "utf-8")),
            delimiter=str(data.get("delimiter", _DEFAULT_DELIMITER)),
            quotechar=str(data.get("quotechar", _DEFAULT_QUOTECHAR)),
            has_header=bool(data.get("has_header", True)),
            start_row=int(data.get("start_row", 0)),
            max_rows=(None if data.get("max_rows") is None else int(data["max_rows"])),
            skip_blank_rows=bool(data.get("skip_blank_rows", True)),
        )


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    inferred_type: str
    blank_count: int
    sample_values: tuple[str, ...]


@dataclass(frozen=True)
class CsvPreview:
    columns: tuple[str, ...]
    column_profiles: tuple[ColumnProfile, ...]
    rows: tuple[dict[str, str], ...]
    total_rows_sampled: int
    duplicate_row_count: int
    blank_row_count: int
    row_offset: int = 0
    total_rows_available: int = 0
    total_is_exact: bool = True


class _ColumnAccumulator:
    __slots__ = (
        "name",
        "blank_count",
        "non_blank_count",
        "all_integer",
        "all_number",
        "all_boolean",
        "sample_values",
        "sample_value_set",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.blank_count = 0
        self.non_blank_count = 0
        self.all_integer = True
        self.all_number = True
        self.all_boolean = True
        self.sample_values: list[str] = []
        self.sample_value_set: set[str] = set()

    def add(self, value: str) -> None:
        if value.strip() == "":
            self.blank_count += 1
            return
        self.non_blank_count += 1
        if not _is_integer(value):
            self.all_integer = False
        if not _is_number(value):
            self.all_number = False
        if value.strip().lower() not in _BOOLEAN_VALUES:
            self.all_boolean = False
        if value not in self.sample_value_set and len(self.sample_values) < 5:
            self.sample_values.append(value)
            self.sample_value_set.add(value)

    def build(self) -> ColumnProfile:
        if self.non_blank_count == 0:
            inferred_type = "empty"
        elif self.all_integer:
            inferred_type = "integer"
        elif self.all_number:
            inferred_type = "number"
        elif self.all_boolean:
            inferred_type = "boolean"
        else:
            inferred_type = "string"
        return ColumnProfile(
            name=self.name,
            inferred_type=inferred_type,
            blank_count=self.blank_count,
            sample_values=tuple(self.sample_values),
        )


class CsvSource:
    def __init__(self, settings: CsvImportSettings) -> None:
        self.settings = settings
        self._cached_total_row_count: int | None = None

    def read_columns(self) -> tuple[str, ...]:
        """Read only the header for AI mapping; never return row data."""
        if not self.settings.has_header:
            raise ValueError("AI mapping requires a CSV header row.")
        encoding, delimiter, quotechar = self._resolve_format()
        with open(self.settings.path, "r", encoding=encoding, newline="") as handle:
            columns = tuple(next(csv.reader(handle, delimiter=delimiter, quotechar=quotechar), ()))
        if not columns or any(not column.strip() for column in columns):
            raise ValueError("The CSV must have non-empty column names.")
        if len(set(columns)) != len(columns):
            raise ValueError("The CSV must have unique column names.")
        return columns

    def preview(
        self,
        *,
        sample_rows: int = 50,
        row_offset: int = 0,
        profile_scan_rows: int = 2000,
        count_limit: int = _DEFAULT_COUNT_LIMIT,
    ) -> CsvPreview:
        """Reads one window of accepted rows plus file-level statistics.

        ``row_offset`` selects the window, which is what lets the UI page
        through a large file without holding it in memory. Profiling and the
        blank/duplicate tallies stay tied to the first ``profile_scan_rows``
        accepted-or-blank rows so they describe the file consistently
        regardless of which page is being viewed.
        """
        columns, raw_rows = self._open_row_iterator()
        accumulators = [_ColumnAccumulator(name) for name in columns]
        preview_rows: list[dict[str, str]] = []
        total_rows_sampled = 0
        duplicate_row_count = 0
        blank_row_count = 0
        accepted_count = 0
        total_is_exact = True
        window_end = row_offset + sample_rows
        seen_signatures: set[tuple[str, ...]] = set()

        try:
            for data_index, row_dict in raw_rows:
                if data_index < self.settings.start_row:
                    continue
                is_blank = _is_blank_row(row_dict)
                if not (self.settings.skip_blank_rows and is_blank):
                    if self.settings.max_rows is not None and accepted_count >= self.settings.max_rows:
                        break

                if total_rows_sampled < profile_scan_rows:
                    total_rows_sampled += 1
                    if is_blank:
                        blank_row_count += 1

                    row_signature = tuple(row_dict.get(column, "") for column in columns)
                    if row_signature in seen_signatures:
                        duplicate_row_count += 1
                    else:
                        seen_signatures.add(row_signature)

                    for accumulator in accumulators:
                        accumulator.add(row_dict.get(accumulator.name, ""))

                if self.settings.skip_blank_rows and is_blank:
                    continue

                if row_offset <= accepted_count < window_end:
                    preview_rows.append(dict(row_dict))
                accepted_count += 1
                if accepted_count >= count_limit:
                    total_is_exact = False
                    break
        finally:
            raw_rows.close()

        return CsvPreview(
            columns=columns,
            column_profiles=tuple(accumulator.build() for accumulator in accumulators),
            rows=tuple(preview_rows),
            total_rows_sampled=total_rows_sampled,
            duplicate_row_count=duplicate_row_count,
            blank_row_count=blank_row_count,
            row_offset=row_offset,
            total_rows_available=accepted_count,
            total_is_exact=total_is_exact,
        )

    def iter_rows(self) -> Iterator[tuple[int, dict[str, str]]]:
        _, raw_rows = self._open_row_iterator()
        accepted_count = 0
        try:
            for data_index, row_dict in raw_rows:
                if data_index < self.settings.start_row:
                    continue
                is_blank = _is_blank_row(row_dict)
                if self.settings.skip_blank_rows and is_blank:
                    continue
                if self.settings.max_rows is not None and accepted_count >= self.settings.max_rows:
                    break
                accepted_count += 1
                yield accepted_count, row_dict
        finally:
            raw_rows.close()

    def total_row_count(self) -> int:
        if self._cached_total_row_count is None:
            self._cached_total_row_count = sum(1 for _ in self.iter_rows())
        return self._cached_total_row_count

    def _open_row_iterator(self) -> tuple[tuple[str, ...], Iterator[tuple[int, dict[str, str]]]]:
        encoding, delimiter, quotechar = self._resolve_format()
        handle = open(self.settings.path, "r", encoding=encoding, newline="")
        reader = csv.reader(handle, delimiter=delimiter, quotechar=quotechar)

        if self.settings.has_header:
            header_row = next(reader, None)
            columns = tuple(header_row or ())
            data_rows: Iterator[list[str]] = iter(reader)
        else:
            buffered_rows: list[list[str]] = []
            columns: tuple[str, ...] | None = None
            for raw_row in reader:
                buffered_rows.append(raw_row)
                if raw_row and any(value.strip() for value in raw_row):
                    columns = tuple(f"Column{index}" for index in range(1, len(raw_row) + 1))
                    break
            if columns is None:
                max_width = max((len(row) for row in buffered_rows), default=0)
                columns = tuple(f"Column{index}" for index in range(1, max_width + 1))

            def iter_data_rows() -> Iterator[list[str]]:
                yield from buffered_rows
                yield from reader

            data_rows = iter_data_rows()

        def iterator() -> Iterator[tuple[int, dict[str, str]]]:
            try:
                for data_index, raw_row in enumerate(data_rows):
                    yield data_index, _row_to_dict(columns, raw_row)
            finally:
                handle.close()

        return columns, iterator()

    def _resolve_format(self) -> tuple[str, str, str]:
        encoding = self.settings.encoding
        if _is_auto_setting(encoding):
            encoding = detect_encoding(self.settings.path)

        delimiter = self.settings.delimiter
        quotechar = self.settings.quotechar
        if delimiter == "" or quotechar == "":
            sample_bytes = _read_sample_bytes(self.settings.path)
            sample_text = sample_bytes.decode(encoding, errors="ignore")
            sniffed_delimiter, sniffed_quotechar = sniff_dialect(sample_text)
            if delimiter == "":
                delimiter = sniffed_delimiter
            if quotechar == "":
                quotechar = sniffed_quotechar

        return encoding, delimiter, quotechar


def sniff_dialect(sample_text: str) -> tuple[str, str]:
    if not sample_text.strip():
        return _DEFAULT_DELIMITER, _DEFAULT_QUOTECHAR

    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters="".join(_COMMON_DELIMITERS))
        return dialect.delimiter or _DEFAULT_DELIMITER, dialect.quotechar or _DEFAULT_QUOTECHAR
    except csv.Error:
        pass

    lines = [line for line in sample_text.splitlines() if line.strip()][:10]
    best_delimiter: str | None = None
    best_score: tuple[int, int, int] | None = None
    for delimiter in _COMMON_DELIMITERS:
        counts = [line.count(delimiter) for line in lines]
        positives = [count for count in counts if count > 0]
        if not positives:
            continue
        spread = max(positives) - min(positives)
        score = (len(positives), -spread, sum(positives))
        if best_score is None or score > best_score:
            best_delimiter = delimiter
            best_score = score

    if best_delimiter is None:
        return _DEFAULT_DELIMITER, _DEFAULT_QUOTECHAR
    return best_delimiter, _DEFAULT_QUOTECHAR


def detect_encoding(path: str) -> str:
    sample = _read_sample_bytes(path)
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8"


def _read_sample_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(_SAMPLE_BYTES)


def _row_to_dict(columns: tuple[str, ...], raw_row: list[str]) -> dict[str, str]:
    row_dict: dict[str, str] = {}
    for index, column in enumerate(columns):
        row_dict[column] = raw_row[index] if index < len(raw_row) else ""
    return row_dict


def _is_blank_row(row_dict: dict[str, str]) -> bool:
    return all(value.strip() == "" for value in row_dict.values())


def _is_auto_setting(value: str | None) -> bool:
    return value is None or value.lower() in _AUTO_VALUES


def _is_integer(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
