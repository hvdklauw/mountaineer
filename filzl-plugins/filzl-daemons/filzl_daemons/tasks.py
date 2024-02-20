import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from filzl_daemons.actions import ActionExecutionStub
from filzl_daemons.db import PostgresBackend
from filzl_daemons.logging import LOGGER
from filzl_daemons.models import DaemonAction, DaemonActionResult, QueableStatus
from filzl_daemons.registry import REGISTRY
from filzl_daemons.retry import RetryPolicy
from filzl_daemons.state import CurrentState

if TYPE_CHECKING:
    pass


class TaskManager:
    """
    In charge of delegating actions within the current instance-runner process. Maintains
    a group of async futures that will wait until work has been completed on action worker
    daemons, and ping the blocking asyncio runloop to make progress on the instance workflow.

    """

    def __init__(
        self,
        backend: PostgresBackend,
    ):
        self.backend = backend

        # In-memory waits that are part of the current event loop
        # Mapping of task ID to signal
        self.wait_signals: dict[int, asyncio.Future] = {}

    async def queue_work(
        self,
        *,
        task: ActionExecutionStub,
        state: CurrentState,
        instance_id: int,
        queue_name: str,
        retry: RetryPolicy,
    ):
        async with self.backend.session_maker() as session:
            # Determine if we've already queued
            existing_action, existing_result = await self.get_existing_action_execution(
                instance_id, state, session
            )

            if existing_action is None:
                existing_action = self.backend.local_models.DaemonAction(
                    workflow_name=queue_name,
                    instance_id=instance_id,
                    state=state.state,
                    registry_id=task.registry_id,
                    input_body=(
                        task.input_body.model_dump_json() if task.input_body else None
                    ),
                    retry_backoff_seconds=1,
                    retry_backoff_factor=1,
                    retry_jitter=1,
                )
                session.add(existing_action)
                await session.commit()

        if existing_action.id is None:
            raise ValueError("Action task ID is None")

        if existing_result is not None:
            LOGGER.debug(
                f"Found existing result, returning immediately: {existing_result}"
            )
            immediate_future: asyncio.Future[BaseModel | None] = asyncio.Future()
            self.resolve_future_from_result(
                immediate_future, task.registry_id, existing_result
            )
            return immediate_future

        # Queue work
        # We should be notified once it's completed
        # Return a signal that we can wait on
        self.wait_signals[existing_action.id] = asyncio.Future()
        return self.wait_signals[existing_action.id]

    async def get_existing_action_execution(
        self, instance_id: int, state: CurrentState, session: AsyncSession
    ) -> tuple[None, None] | tuple[DaemonAction, DaemonActionResult | None]:
        state_query = select(self.backend.local_models.DaemonAction).where(
            self.backend.local_models.DaemonAction.state == state.state,
            self.backend.local_models.DaemonAction.instance_id == instance_id,
        )
        results = await session.execute(state_query)
        existing_action = results.scalars().first()
        if not existing_action:
            return None, None

        # Now determine if this action has results
        result_query = select(self.backend.local_models.DaemonActionResult).where(
            self.backend.local_models.DaemonActionResult.action_id == existing_action.id
        )

        results = await session.execute(result_query)
        existing_result = results.scalars().first()

        # These should be the types anyway, but mypy complains about
        if TYPE_CHECKING:
            assert isinstance(existing_action, DaemonAction)
            assert existing_result is None or isinstance(
                existing_result, DaemonActionResult
            )

        return existing_action, existing_result

    async def delegate_done_actions(self):
        """
        We have waiting futures. Make sure this is running somewhere
        in the current runloop.

        """
        async for notification in self.backend.iter_ready_objects(
            model=self.backend.local_models.DaemonAction,
            queues=[],
            status=QueableStatus.DONE,
        ):
            # If we have no waiting futures, there's no use doing the additional roundtrips
            # to the database
            waiting_futures = self.wait_signals.get(notification.id)
            LOGGER.debug(f"Delegate done action: {notification.id}: {waiting_futures}")

            if waiting_futures is None:
                continue

            # Get the actual object
            async with self.backend.get_object_by_id(
                model=self.backend.local_models.DaemonAction,
                id=notification.id,
            ) as (obj, _):
                pass

            if not obj.final_result_id:
                # No result found, likely erroneous "done" setting
                LOGGER.warning(
                    f"Action {obj.id} is done, but has no final result. Skipping"
                )
                continue

            # Look for the most recent pointer
            async with self.backend.get_object_by_id(
                model=self.backend.local_models.DaemonActionResult,
                id=obj.final_result_id,
            ) as (result_obj, _):
                pass

            if result_obj.exception:
                LOGGER.debug(f"Got an exception: {result_obj.exception}")
                waiting_futures.set_exception(
                    Exception(
                        f"Action failed with error: {result_obj.exception} {result_obj.exception_stack}"
                    )
                )
            else:
                LOGGER.debug(f"Got a result: {result_obj.result_body}")
                self.resolve_future_from_result(
                    waiting_futures, obj.registry_id, result_obj
                )

    def resolve_future_from_result(
        self,
        future: asyncio.Future[BaseModel | None],
        registry_id: str,
        result: DaemonActionResult,
    ):
        action_model = REGISTRY.get_action_output(registry_id)

        if result.exception:
            # TODO: Include stack
            future.set_exception(Exception(result.exception))
        else:
            future_result: BaseModel | None
            if action_model and result.result_body:
                future_result = action_model.model_validate_json(result.result_body)
            elif action_model is None:
                future_result = None
            else:
                raise ValueError(
                    f"Disallowed response type: {action_model} {result.result_body}"
                )

            future.set_result(future_result)
