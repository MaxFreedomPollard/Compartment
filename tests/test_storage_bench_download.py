"""The benchmark dataset download, cut short.

The LongMemEval files run to gigabytes. A download interrupted part way
used to leave a truncated file where the dataset belongs, and every run
after trusted it because it existed.
"""
import io
import urllib.request

import pytest

from compartment import longmemeval as L


class _Response(io.BytesIO):
    """A response body that drops after `cut` bytes, as a connection does."""

    def __init__(self, body: bytes, cut: int | None = None):
        super().__init__(body)
        self._cut = cut

    def read(self, n=-1):
        if self._cut is not None and self.tell() >= self._cut:
            raise ConnectionResetError(54, "Connection reset by peer")
        if self._cut is not None and n > 0:
            n = min(n, self._cut - self.tell())
        return super().read(n)


@pytest.fixture()
def bench_home(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "data_dir", lambda: tmp_path / "longmemeval")
    return tmp_path / "longmemeval"


def test_a_download_cut_short_leaves_nothing_that_looks_like_the_dataset(
        bench_home, monkeypatch):
    body = b'[{"question_id": "q1", "haystack_sessions": []}]' * 1000
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url: _Response(body, cut=len(body) // 3))
    with pytest.raises(ConnectionResetError):
        L.download("s")
    assert not L.dataset_path("s").exists()
    assert list(bench_home.iterdir()) == [], "and no partial file beside it"
    with pytest.raises(L.CryptoError, match="download-longmemeval"):
        L.run("s")                       # says to fetch it, not "bad JSON"


def test_a_whole_download_lands_where_run_looks(bench_home, monkeypatch):
    body = b'[{"question_id": "q1", "haystack_sessions": []}]'
    monkeypatch.setattr(urllib.request, "urlopen", lambda url: _Response(body))
    dest = L.download("s")
    assert dest == L.dataset_path("s") and dest.read_bytes() == body
    assert [p.name for p in bench_home.iterdir()] == [dest.name]
