"""Test the server."""
from contextlib import asynccontextmanager, AsyncExitStack
from datetime import datetime
from functools import partial, wraps
from json.decoder import JSONDecodeError
from typing import Callable

from asgiar import ASGIAR
from fastapi import FastAPI, HTTPException
import httpx
from jsonschema.exceptions import ValidationError
import pytest
from reasoner_validator import validate
from starlette.requests import Request

from app.server import APP


def with_context(context, *args_, **kwargs_):
    """Turn context manager into decorator."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with context(*args_, **kwargs_):
                await func(*args, **kwargs)
        return wrapper
    return decorator


@asynccontextmanager
async def function_overlay(url: str, fcn: Callable):
    """Apply an ASGIAR overlay that runs `fcn` for all routes."""
    async with AsyncExitStack() as stack:
        app = FastAPI()

        # pylint: disable=unused-variable disable=unused-argument
        @app.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "DELETE"],
        )
        async def all_paths(path: str, request: Request):
            try:
                return fcn(await request.json())
            except JSONDecodeError:
                return fcn()

        await stack.enter_async_context(
            ASGIAR(app, url=url)
        )
        yield


with_function_overlay = partial(with_context, function_overlay)


def validate_trapi(request_json: dict):
    """Return request verbatim if it is a valid TRAPI query."""
    try:
        validate(request_json, "Query", "1.1.0")
    except ValidationError as err:
        raise HTTPException(422, str(err))
    return request_json


def with_subservice_overlay(
    nodenorm=validate_trapi,
    meta_kg=lambda: {"nodes": {}},
    lookup=validate_trapi,
    ranker=validate_trapi,
):
    """Overlay nodenormalization, automat, and aragorn-ranker."""
    def wrap(fcn):
        return with_function_overlay(
            "https://nodenormalization-sri.renci.org/*",
            nodenorm,
        )(
            with_function_overlay(
                "https://automat.renci.org/robokopkg/1.2/meta_knowledge_graph",
                meta_kg,
            )(
                with_function_overlay(
                    "https://automat.renci.org/robokopkg/1.2/query",
                    lookup,
                )(
                    with_function_overlay(
                        "https://aragorn-ranker.renci.org/*",
                        ranker,
                    )(fcn)
                )
            )
        )
    return wrap


@pytest.fixture
async def client():
    """Create and teardown async httpx client."""
    transport = httpx.ASGITransport(
        app=APP,
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
@with_subservice_overlay(
    lookup=lambda request: request | {
        "automat": request.get("automat", []) + [datetime.now().isoformat()]
    },
    ranker=lambda request: request | {
        "ranker": request.get("ranker", []) + [datetime.now().isoformat()]
    },
)
async def test_server(client):
    """Test the server."""
    payload = {"message": {}}
    response = await client.post("/query", json=payload)
    response.raise_for_status()
    output = response.json()
    assert len(output.get("automat")) == 1
    assert len(output.get("ranker")) == 3
    assert all(
        output.get("automat")[0] < ranker_ts
        for ranker_ts in output.get("ranker")
    )


@pytest.mark.asyncio
async def test_endpoint(client):
    """Test getting OpenAPI."""
    response = await client.get(
        "/openapi.json",
    )
    response.raise_for_status()
    # and again, to make sure the caching works
    response = await client.get(
        "/openapi.json",
    )
    response.raise_for_status()


@pytest.mark.asyncio
@with_subservice_overlay(
    meta_kg=lambda: {"nodes": {"biolink:Disease": {"id_prefixes": ["MONDO"]}}},
)
async def test_request(client):
    """Test the request generated internally."""
    payload = {
        "message": {
            "knowledge_graph": {
                "nodes": {
                    "MONDO:xxx": {"categories": ["biolink:Disease"]}
                },
                "edges": {},
            }
        }
    }
    response = await client.post("/query", json=payload)
    assert response.status_code == 200
