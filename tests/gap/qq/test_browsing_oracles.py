"""Independent source checks must measure browsing, not all activity."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "engine"))
from composition_eval_cases import oracle_c6_browsing, oracle_c20_domains


def database(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE activity_events (activity_type TEXT, title TEXT, url TEXT)")
    conn.executemany("INSERT INTO activity_events VALUES (?, ?, ?)", rows)
    return conn


def test_titles_exclude_non_visit_events():
    with database(
        [("push", "Popular code push", "https://code.example")] * 100
        + [("visit", title, "https://read.example") for title in
           ["Article Alpha"] * 3 + ["Article Beta"] * 2 + ["Article Gamma"]]
    ) as conn:
        assert oracle_c6_browsing(conn).needle_groups == [["Article Alpha"], ["Article Beta"], ["Article Gamma"]]


def test_domains_aggregate_long_tail_before_limiting_and_normalize_hosts():
    with database(
        [("push", "Code", "https://code.example")] * 1000
        + [("visit", "Page", f"https://longtail.example/{i}") for i in range(40)]
        + [("visit", "Page", "https://second.example/a")] * 20
        + [("visit", "Page", "HTTPS://WWW.SECOND.EXAMPLE:443/b")] * 15
        + [("visit", "Page", "https://third.example")] * 10
        + [("visit", "Page", "file:///local"), ("visit", "Page", "http://[invalid")]
    ) as conn:
        assert oracle_c20_domains(conn).needle_groups == [["longtail.example"], ["second.example"]]


def test_non_visit_only_corpus_is_ungradable():
    with database([("push", "A code contribution", "https://code.example")]) as conn:
        assert not oracle_c6_browsing(conn).ok
        assert not oracle_c20_domains(conn).ok
