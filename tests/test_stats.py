from datetime import timedelta

from odds_scanner import cli, stats
from odds_scanner.config import config_from_dict
from odds_scanner.storage import CsvArbLog, SqliteArbLog
from tests.conftest import NOW
from tests.test_opportunities import arbs


def logged(hours_before, **kw):
    start = NOW + timedelta(hours=hours_before)
    (a,) = arbs(now=NOW, start=start, **kw)
    return a


def cfg(tmp_path):
    return config_from_dict({"storage": {"backend": "both", "csv_path": str(tmp_path / "a.csv"), "sqlite_path": str(tmp_path / "a.db")}})


def test_buckets_and_pairs_from_sqlite(tmp_path):
    c = cfg(tmp_path)
    log = SqliteArbLog(c.storage.sqlite_path)
    log.append([logged(0.5), logged(2), logged(2.5), logged(20), logged(100), logged(2, ba="doxxbet")])
    log.close()
    rows, source = stats.load_rows(c)
    assert len(rows) == 6 and source.endswith("a.db")
    text = stats.summarise(rows)
    assert "under 1 h" in text and "1-3 h" in text and "12-24 h" in text and "over 3 days" in text
    assert "Nike + Tipos" in text and "Doxxbet + Tipos" in text
    assert "Only 6 episodes" in text  # small samples are flagged


def test_csv_fallback_and_empty(tmp_path):
    c = cfg(tmp_path)
    assert stats.load_rows(c) == ([], "") and "No arbitrages logged yet" in stats.summarise([])
    CsvArbLog(c.storage.csv_path).append([logged(2), logged(30)])
    rows, source = stats.load_rows(c)
    assert len(rows) == 2 and source.endswith("a.csv") and rows[0].books


def test_cli_command(tmp_path, capsys):
    conf = tmp_path / "c.yaml"
    conf.write_text(f"storage: {{backend: sqlite, sqlite_path: {tmp_path}/none.db, csv_path: {tmp_path}/none.csv}}\n")
    assert cli.main(["-c", str(conf), "stats"]) == 0
    assert "No arbitrages logged yet" in capsys.readouterr().out
