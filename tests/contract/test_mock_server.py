import asyncio
import threading

import pytest
from iris_memory_sdk.client import AsyncIrisMemoryClient, IrisMemoryApiError

from tools.mock_server import create_server


def test_python_async_sdk_negotiates_with_mock_server() -> None:
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        client = AsyncIrisMemoryClient(base_url)
        capabilities = asyncio.run(client.capabilities())
        negotiated = asyncio.run(client.negotiate())
        assert capabilities.api_version == "v1"
        assert negotiated == capabilities
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_python_async_sdk_maps_stable_error_envelope() -> None:
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AsyncIrisMemoryClient(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(IrisMemoryApiError) as captured:
            asyncio.run(client.negotiate(("v999",)))
        assert captured.value.envelope.code == "unsupported_version"
        assert captured.value.envelope.retryable is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
