"""repair-titles: folding filename-split series back into their folder's title (L8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from homeTheater.db.models import (
    Candidate,
    CandidateSource,
    CandidateStatus,
    OwnedFile,
    Subtitle,
    Title,
    TitleKind,
)

ROOT = "//nas/share/TV Shows"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _title(name: str, *paths: str, **fields: object) -> Title:
    title = Title(title=name, kind=TitleKind.series, **fields)
    title.owned_files = [OwnedFile(path=p, kind=TitleKind.series) for p in paths]
    return title


def _seed_killing() -> tuple[int, int, int]:
    """The real shape: two filename titles beside the enriched one."""

    from homeTheater.db import init_db, session_scope

    init_db()
    with session_scope() as s:
        good = _title(
            "The Killing",
            f"{ROOT}/The.Killing/Season 02/the.killing.s02e01.mkv",
            f"{ROOT}/The.Killing/Season 02/the.killing.s02e02.mkv",
            tmdb_id=34415,
            imdb_id="tt1637727",
            year=2011,
        )
        bkd = _title(
            "bkd-tk",
            f"{ROOT}/The.Killing/Season 01/bkd-tk.s01e06.mkv",
            followed=True,
        )
        pfa = _title("pfa-tk", f"{ROOT}/The.Killing/Season 03/pfa-tk.s03e01.mkv")
        s.add_all([good, bkd, pfa])
        s.flush()
        s.add(
            Candidate(
                title_id=bkd.id,
                season=1,
                source=CandidateSource.discovery,
                status=CandidateStatus.rejected,
            )
        )
        s.add(
            Subtitle(
                title_id=pfa.id,
                owned_file_id=pfa.owned_files[0].id,
                lang="he",
                status="downloaded",
            )
        )
        return good.id, bkd.id, pfa.id


def _titles() -> dict[int, str]:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return dict(s.execute(select(Title.id, Title.title)).tuples().all())


def test_dry_run_is_the_default_and_changes_nothing(config_file: Path) -> None:
    _reset()
    from homeTheater.scanner.repair import repair_titles

    good, bkd, pfa = _seed_killing()
    before = _titles()

    plan = repair_titles("TV Shows")

    assert [r.keep.id for r in plan.repairs] == [good]
    assert {ref.id for ref in plan.repairs[0].merge} == {bkd, pfa}
    assert _titles() == before


def test_apply_merges_and_preserves_everything(config_file: Path) -> None:
    _reset()
    from homeTheater.db import session_scope
    from homeTheater.scanner.repair import repair_titles

    good, bkd, pfa = _seed_killing()

    repair_titles("TV Shows", apply=True)

    assert _titles() == {good: "The Killing"}
    with session_scope() as s:
        title = s.get(Title, good)
        assert title is not None
        assert title.followed is True  # a followed split stays followed
        assert title.tmdb_id == 34415 and title.year == 2011
        assert len(title.owned_files) == 4
        cand = s.scalars(select(Candidate)).one()
        assert cand.title_id == good and cand.status is CandidateStatus.rejected  # still a label
        assert s.scalars(select(Subtitle)).one().title_id == good


def test_repair_is_idempotent(config_file: Path) -> None:
    _reset()
    from homeTheater.scanner.repair import repair_titles

    _seed_killing()
    repair_titles("TV Shows", apply=True)
    assert repair_titles("TV Shows").repairs == []


def test_same_named_split_fills_gaps_and_keeps_the_enriched_row(config_file: Path) -> None:
    """Severance: the enriched row wins over the file-year row of the same name."""

    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import repair_titles

    init_db()
    with session_scope() as s:
        good = _title(
            "Severance",
            f"{ROOT}/Severance/Season 01/Severance.S01E01.mkv",
            tmdb_id=95396,
            year=2022,
            followed=True,
        )
        dup = _title(
            "Severance",
            f"{ROOT}/Severance/Season 02/Severance (2021) - S02E01.mkv",
            f"{ROOT}/Severance/Season 02/Severance (2021) - S02E02.mkv",
            year=2021,
            overview="only the duplicate had this",
        )
        s.add_all([good, dup])
        s.flush()
        good_id = good.id

    repair_titles("TV Shows", apply=True)

    with session_scope() as s:
        title = s.scalars(select(Title)).one()
        assert title.id == good_id and title.year == 2022 and title.followed
        assert title.overview == "only the duplicate had this"
        assert len(title.owned_files) == 3


def test_junk_titles_metadata_is_not_inherited(config_file: Path) -> None:
    """``bkd-tk`` may have been matched to anything; its ids must not survive."""

    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import repair_titles

    init_db()
    with session_scope() as s:
        good = _title("The Killing", f"{ROOT}/The.Killing/a.s01e01.mkv")
        junk = _title("bkd-tk", f"{ROOT}/The.Killing/b.s01e02.mkv", tmdb_id=777, poster_url="/x")
        s.add_all([good, junk])
        s.flush()
        good_id = good.id

    repair_titles("TV Shows", apply=True)

    with session_scope() as s:
        title = s.scalars(select(Title)).one()
        assert title.id == good_id and title.tmdb_id is None and title.poster_url is None


def test_title_with_files_in_another_folder_only_loses_this_folders_files(
    config_file: Path,
) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import repair_titles

    init_db()
    with session_scope() as s:
        good = _title("Foo", f"{ROOT}/Foo/foo.s01e01.mkv")
        shared = _title("Shared", f"{ROOT}/Foo/x.s01e02.mkv", f"{ROOT}/Bar/x.s01e01.mkv")
        s.add_all([good, shared])
        s.flush()
        s.add(
            Candidate(
                title_id=shared.id,
                source=CandidateSource.discovery,
                status=CandidateStatus.rejected,
            )
        )
        s.add(
            Subtitle(
                title_id=shared.id,
                owned_file_id=shared.owned_files[0].id,
                lang="he",
                status="downloaded",
            )
        )
        s.flush()
        good_id, shared_id = good.id, shared.id

    repair_titles("TV Shows", apply=True)

    with session_scope() as s:
        by_path = dict(s.execute(select(OwnedFile.path, OwnedFile.title_id)).tuples().all())
        assert by_path[f"{ROOT}/Foo/x.s01e02.mkv"] == good_id
        assert by_path[f"{ROOT}/Bar/x.s01e01.mkv"] == shared_id
        # The other folder is still that title, so its decisions stay with it.
        assert s.scalars(select(Candidate)).one().title_id == shared_id
        assert s.scalars(select(Subtitle)).one().title_id == good_id


def test_unenriched_folder_with_only_junk_titles_takes_the_folder_name(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import repair_titles

    init_db()
    with session_scope() as s:
        s.add(_title("bkd-tk", f"{ROOT}/The Killing (2011)/x.s01e01.mkv"))
        s.add(_title("pfa-tk", f"{ROOT}/The Killing (2011)/y.s03e01.mkv"))

    repair_titles("TV Shows", apply=True)

    with session_scope() as s:
        title = s.scalars(select(Title)).one()
        assert (title.title, title.year, title.last_enriched_at) == ("The Killing", 2011, None)


def test_enriched_mismatch_is_reported_not_changed(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import repair_titles

    init_db()
    with session_scope() as s:
        s.add(_title("Wrong Show", f"{ROOT}/Right Show/x.s01e01.mkv", tmdb_id=5))
        # A typo'd folder is not a mismatch.
        s.add(_title("Better Call Saul", f"{ROOT}/Better Call Soul/y.s01e01.mkv", tmdb_id=6))

    plan = repair_titles("TV Shows", apply=True)

    assert plan.repairs == []
    assert [ref.title for ref in plan.suspect] == ["Wrong Show"]
    assert set(_titles().values()) == {"Wrong Show", "Better Call Saul"}


def test_two_enriched_rows_neither_named_like_the_folder_are_left_alone(
    config_file: Path,
) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import repair_titles

    init_db()
    with session_scope() as s:
        s.add(_title("Zed", f"{ROOT}/Foo/a.s01e01.mkv", f"{ROOT}/Foo/a.s01e02.mkv", tmdb_id=1))
        s.add(_title("Yak", f"{ROOT}/Foo/b.s01e03.mkv", tmdb_id=2))
        s.add(_title("bkd-xx", f"{ROOT}/Foo/c.s01e04.mkv"))

    plan = repair_titles("TV Shows", apply=True)

    assert sorted(ref.title for ref in plan.suspect) == ["Yak"]
    # The unenriched row had no identity worth keeping.
    assert sorted(_titles().values()) == ["Yak", "Zed"]


def test_folder_found_on_unc_and_mount_paths_alike(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner.repair import plan_title_repair

    init_db()
    with session_scope() as s:
        s.add(_title("Show", "\\\\nas\\share\\TV Shows\\Show\\Season 01\\a.s01e01.mkv"))
        s.add(_title("zzz-sh", "/Volumes/share/TV Shows/Show/Season 01/b.s01e02.mkv"))
        s.add(_title("Stray", "/somewhere/else/c.s01e01.mkv"))
    with session_scope() as s:
        plan = plan_title_repair(s, "TV Shows")
    assert [r.folder for r in plan.repairs] == ["Show"]
    assert plan.skipped_paths == 1


def test_cli_is_dry_by_default_and_apply_backs_up_first(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _reset()
    import sys

    from homeTheater import main as cli

    _seed_killing()
    calls: list[str] = []
    import homeTheater.backup as backup

    monkeypatch.setattr(backup, "backup_database", lambda cfg: calls.append("backup"))

    monkeypatch.setattr(sys, "argv", ["home-theater", "repair-titles"])
    cli.main()
    assert "Dry run: 1 folder(s)" in capsys.readouterr().out
    assert calls == [] and len(_titles()) == 3

    monkeypatch.setattr(sys, "argv", ["home-theater", "repair-titles", "--apply"])
    cli.main()
    assert "Repaired 1 folder(s)" in capsys.readouterr().out
    assert calls == ["backup"] and len(_titles()) == 1
