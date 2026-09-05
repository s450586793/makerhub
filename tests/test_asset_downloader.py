import pytest
import requests

from app.services import asset_downloader
from app.services.asset_downloader import download_file, run_asset_tasks


def test_download_file_removes_partial_file_on_stream_failure(tmp_path):
    destination = tmp_path / "asset.bin"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"partial"
            raise requests.RequestException("stream interrupted")

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(requests.RequestException):
        download_file(Session(), "https://cdn.example.test/asset.bin", destination)

    assert not destination.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_run_asset_tasks_applies_successes_caps_workers_and_reports_failures(monkeypatch):
    applied = []
    errors = []
    progress = []
    worker_counts = []

    class FakeFuture:
        def __init__(self, fn):
            self.fn = fn

        def result(self):
            return self.fn()

    class FakeExecutor:
        def __init__(self, max_workers):
            worker_counts.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn):
            return FakeFuture(fn)

    def fail_download():
        raise requests.RequestException("download failed")

    tasks = [
        {"url": "0", "download": lambda: None, "apply": [lambda: applied.append(0)]},
        {"url": "1", "download": fail_download, "apply": [lambda: applied.append(1)]},
        {"url": "2", "download": lambda: None, "apply": [lambda: applied.append(2)]},
    ]

    monkeypatch.setattr(asset_downloader, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(asset_downloader, "as_completed", lambda futures: list(futures))

    stats = run_asset_tasks(
        tasks,
        max_workers=8,
        on_progress=lambda completed, total: progress.append((completed, total)),
        on_error=lambda task, exc: errors.append((task["url"], exc)),
    )

    assert worker_counts == [len(tasks)]
    assert stats == {"completed": 2, "failed": 1}
    assert applied == [0, 2]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert len(errors) == 1
    assert errors[0][0] == "1"
    assert isinstance(errors[0][1], requests.RequestException)
