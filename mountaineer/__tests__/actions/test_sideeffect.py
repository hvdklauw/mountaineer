from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from unittest.mock import patch

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from pydantic.main import BaseModel
from starlette.datastructures import Headers

from mountaineer.__tests__.common import calculate_primes
from mountaineer.actions.fields import FunctionActionType, get_function_metadata
from mountaineer.actions.sideeffect import sideeffect
from mountaineer.annotation_helpers import MountaineerUnsetValue
from mountaineer.app import AppController
from mountaineer.controller import ControllerBase
from mountaineer.logging import LOGGER
from mountaineer.render import RenderBase


class ExampleRenderModel(RenderBase):
    value_a: str
    value_b: str


def test_markup_sideeffect():
    """
    Check that the @sideeffect decorator extracts the expected
    data from our model definition.
    """

    class ExamplePassthroughModel(BaseModel):
        first_name: str

    class TestController(ControllerBase):
        # We declare as complicated a payload as @sideeffect supports so we can
        # see the full amount of metadata properties that are set
        @sideeffect(
            response_model=ExamplePassthroughModel,
            reload=tuple([ExampleRenderModel.value_a]),
        )
        def sideeffect_and_return_data(self):
            return dict(
                first_name="John",
            )

    metadata = get_function_metadata(TestController.sideeffect_and_return_data)
    assert metadata.action_type == FunctionActionType.SIDEEFFECT
    assert metadata.get_passthrough_model() == ExamplePassthroughModel
    assert metadata.function_name == "sideeffect_and_return_data"
    assert metadata.reload_states == tuple([ExampleRenderModel.value_a])
    assert isinstance(metadata.render_model, MountaineerUnsetValue)
    assert isinstance(metadata.url, MountaineerUnsetValue)
    assert isinstance(metadata.return_model, MountaineerUnsetValue)
    assert isinstance(metadata.render_router, MountaineerUnsetValue)


class ControllerCommon(ControllerBase):
    url: str = "/test/{query_id}/"

    def __init__(self):
        super().__init__()
        self.counter = 0
        self.render_counts = 0

    @sideeffect
    def call_sideeffect(self, payload: dict):
        self.counter += 1

    @sideeffect
    async def call_sideeffect_async(self, payload: dict):
        self.counter += 1


async def call_sideeffect_common(controller: ControllerCommon):
    app = AppController(view_root=Path())
    app.register(controller)

    @asynccontextmanager
    async def mock_get_render_parameters(*args, **kwargs):
        yield {
            "query_id": 1,
        }

    # After our wrapper is called, our function is now async
    # Avoid the dependency resolution logic since that's tested separately
    with patch(
        "mountaineer.actions.sideeffect.get_render_parameters"
    ) as patched_get_render_params:
        patched_get_render_params.side_effect = mock_get_render_parameters

        # Even if the "request" is not required by our sideeffects, it's required
        # by the function injected by the sideeffect decorator.
        return_value_sync = await controller.call_sideeffect(
            {},
            request=Request({"type": "http"}),
        )

        return_value_async = await controller.call_sideeffect_async(
            {},
            request=Request({"type": "http"}),
        )

        # The response payload should be the same both both sync and async endpoints
        expected_response = {
            "sideeffect": ExampleRenderModel(
                value_a="Hello",
                value_b="World",
            ),
            "passthrough": None,
        }

        assert return_value_sync == expected_response
        assert return_value_async == expected_response

        assert controller.counter == 2
        assert controller.render_counts == 2


@pytest.mark.asyncio
async def test_can_call_sideeffect():
    """
    Ensure that we can call the sideeffect, which will in turn
    call the render function to get fresh data.
    """

    class ExampleController(ControllerCommon):
        def render(
            self,
            query_id: int,
        ) -> ExampleRenderModel:
            self.render_counts += 1
            return ExampleRenderModel(
                value_a="Hello",
                value_b="World",
            )

    await call_sideeffect_common(ExampleController())


@pytest.mark.asyncio
async def test_can_call_sideeffect_async_render():
    """
    Render functions can also work asynchronously.
    """

    class ExampleController(ControllerCommon):
        async def render(
            self,
            query_id: int,
        ) -> ExampleRenderModel:
            self.render_counts += 1
            return ExampleRenderModel(
                value_a="Hello",
                value_b="World",
            )

    await call_sideeffect_common(ExampleController())


@pytest.mark.asyncio
async def test_get_render_parameters():
    """
    Given a controller, reproduce the logic of FastAPI to sniff the render()
    function for dependencies and resolve them.

    """
    from mountaineer.actions.sideeffect import get_render_parameters

    found_cookie = None

    def grab_cookie_dependency(request: Request):
        nonlocal found_cookie
        found_cookie = request.cookies.get("test-cookie")
        return found_cookie

    class TestController(ControllerBase):
        url: str = "/test/{query_id}/"

        def render(
            self,
            query_id: int,
            cookie_dependency: str = Depends(grab_cookie_dependency),
        ) -> ExampleRenderModel:
            return ExampleRenderModel(
                value_a="Hello",
                value_b="World",
            )

    # We need to load this test controller to an actual application runtime
    # or else we don't have the render() metadata added
    app = AppController(view_root=Path())
    controller = TestController()
    app.register(controller)

    fake_request = Request(
        {
            "type": "http",
            "headers": Headers(
                {
                    "cookie": "test-cookie=cookie-value",
                    # Its important the referer aligns with the controller url, since that is expected
                    # to be the original view page that is calling this sub-function
                    "referer": "http://example.com/test/5/",
                }
            ).raw,
            "http_version": "1.1",
            "scheme": "",
            "client": "",
            "server": "",
            # The URL and method should both be different, to test whether we are able
            # to map the request to the correct endpoint
            "method": "POST",
            "url": "http://localhost/related_action_endpoint",
        }
    )

    async with get_render_parameters(controller, fake_request) as resolved_dependencies:
        assert resolved_dependencies == {
            "cookie_dependency": "cookie-value",
            "query_id": 5,
        }


@pytest.mark.parametrize(
    "use_experimental,min_time,max_time",
    [
        (False, 1, None),
        (True, None, 0.10),
    ],
)
def test_limit_codepath_experimental(
    use_experimental: bool,
    min_time: float | None,
    max_time: float | None,
):
    class ExampleController(ControllerBase):
        url: str = "/test/{query_id}/"

        def render(
            self,
            query_id: int,
        ) -> ExampleRenderModel:
            a = calculate_primes(10000)
            b = calculate_primes(1000000)
            return ExampleRenderModel(
                value_a=f"Hello {a}",
                value_b=f"World {b}",
            )

        @sideeffect(
            reload=(ExampleRenderModel.value_a,),
            experimental_render_reload=use_experimental,
        )
        def call_sideeffect(self, payload: dict):
            pass

    # We need to load this test controller to an actual application runtime
    # or else we don't have the render() metadata added
    app = AppController(view_root=Path())
    controller = ExampleController()
    app.register(controller)

    sideeffect_url = get_function_metadata(ExampleController.call_sideeffect).url
    assert isinstance(sideeffect_url, str)

    client = TestClient(app.app)
    start = time()
    response = client.post(
        sideeffect_url,
        json={},
        headers={
            # From the original view page that is calling this sub-function
            "referer": "http://example.com/test/5/",
        },
    )
    elapsed = time() - start
    assert response.status_code == 200
    assert response.json() == {
        "sideeffect": {
            "value_a": "Hello 1229",
        }
    }

    LOGGER.info(f"Use Experimental: {use_experimental}\nElapsed: {elapsed}")

    if min_time is not None:
        assert elapsed >= min_time
    if max_time is not None:
        assert elapsed <= max_time
