import asyncio
import concurrent.futures
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from crashdump_mcp_server import server


pytestmark = pytest.mark.usefixtures("restore_upload_runtime_state")


@pytest.fixture(autouse=True)
def upload_test_env(configure_upload_runtime):
    configure_upload_runtime(max_upload_mb=1)
    server.configure_public_base_url(explicit_base_url="http://crashdump.local:8000")


def test_create_upload_session_returns_upload_target():
    payload = server.create_upload_session("crash.dmp")

    assert payload["session_id"]
    assert "upload_path" not in payload
    assert payload["upload_url"] == f"http://crashdump.local:8000{server.build_upload_path(payload['session_id'])}"
    assert payload["next_steps"] == [
        "PUT upload_url with raw dump bytes",
        f"call open_windbg_dump(session_id={payload['session_id']})",
    ]
    assert "Upload the raw dump bytes to upload_url" in payload["upload_instructions"]

    metadata = server.session_registry.upload_sessions[payload["session_id"]]
    assert metadata.original_file_name == "crash.dmp"
    assert metadata.status == server.UploadSessionStatus.PENDING


def test_create_upload_session_rejects_unusable_public_base_url():
    server.configure_public_base_url(explicit_base_url="http://0.0.0.0:8000")

    with pytest.raises(server.UploadWorkflowError) as exc_info:
        server.create_upload_session("crash.dmp")

    assert exc_info.value.code == server.UPLOAD_ERROR_URL_UNAVAILABLE
    assert "public base URL" in exc_info.value.message
    assert not server.session_registry.upload_sessions


@pytest.mark.parametrize("base_url", ["", "http://127.0.0.1:8000", "http://localhost:8000"])
def test_create_upload_session_requires_explicit_client_reachable_public_base_url(base_url):
    server.configure_public_base_url(explicit_base_url=base_url)

    with pytest.raises(server.UploadWorkflowError) as exc_info:
        server.create_upload_session("crash.dmp")

    assert exc_info.value.code == server.UPLOAD_ERROR_URL_UNAVAILABLE
    assert not server.session_registry.upload_sessions


def test_put_upload_dump_succeeds_and_marks_session_uploaded():
    payload = server.create_upload_session("uploaded.dmp")
    app = server.create_http_app()

    with TestClient(app) as client:
        response = client.put(server.build_upload_path(payload["session_id"]), content=b"MDMPxxxx")
        assert response.status_code == 201
        assert response.json()["status"] == "uploaded"

        metadata = server.session_registry.upload_sessions[payload["session_id"]]
        assert metadata.status == server.UploadSessionStatus.UPLOADED
        assert Path(metadata.temp_file_path).read_bytes() == b"MDMPxxxx"


def test_put_upload_dump_rejects_invalid_signature_and_rolls_back():
    payload = server.create_upload_session("bad.dmp")
    metadata = server.session_registry.upload_sessions[payload["session_id"]]
    app = server.create_http_app()

    with TestClient(app) as client:
        response = client.put(server.build_upload_path(payload["session_id"]), content=b"NOTDUMP")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == server.UPLOAD_ERROR_INVALID_FORMAT
    assert "Upload the raw bytes" in response.json()["error"]["remediation"]
    assert payload["session_id"] not in server.session_registry.upload_sessions
    assert not Path(metadata.temp_file_path).exists()


def test_put_upload_dump_rolls_back_on_cancellation(monkeypatch):
    payload = server.create_upload_session("cancelled.dmp")
    metadata = server.session_registry.upload_sessions[payload["session_id"]]
    app = server.create_http_app()

    async def mock_stream_upload_to_file(*_args, **_kwargs):
        Path(metadata.temp_file_path).write_bytes(b"partial")
        raise asyncio.CancelledError()

    monkeypatch.setattr(server, "_stream_upload_to_file", mock_stream_upload_to_file)

    with TestClient(app) as client:
        with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
            client.put(server.build_upload_path(payload["session_id"]), content=b"MDMP")

    assert payload["session_id"] not in server.session_registry.upload_sessions
    assert not Path(metadata.temp_file_path).exists()
