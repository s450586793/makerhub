import pytest
import requests

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


def test_run_asset_tasks_applies_every_success_and_caps_workers():
    applied = []
    tasks = [
        {"url": str(index), "download": lambda: None, "apply": [lambda index=index: applied.append(index)]}
        for index in range(8)
    ]

    stats = run_asset_tasks(tasks, max_workers=4)

    assert stats == {"completed": 8, "failed": 0}
    assert sorted(applied) == list(range(8))
