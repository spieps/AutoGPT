import asyncio
import logging
from typing import Any, Optional

from pydantic import JsonValue

from backend.data.block import (
    Block,
    BlockCategory,
    BlockInput,
    BlockOutput,
    BlockSchema,
    BlockType,
    get_block,
)
from backend.data.execution import ExecutionStatus
from backend.data.model import SchemaField
from backend.util import json, retry

_logger = logging.getLogger(__name__)


class AgentExecutorBlock(Block):
    class Input(BlockSchema):
        user_id: str = SchemaField(description="User ID")
        graph_id: str = SchemaField(description="Graph ID")
        graph_version: int = SchemaField(description="Graph Version")

        inputs: BlockInput = SchemaField(description="Input data for the graph")
        input_schema: dict = SchemaField(description="Input schema for the graph")
        output_schema: dict = SchemaField(description="Output schema for the graph")

        nodes_input_masks: Optional[dict[str, dict[str, JsonValue]]] = SchemaField(
            default=None, hidden=True
        )

        @classmethod
        def get_input_schema(cls, data: BlockInput) -> dict[str, Any]:
            return data.get("input_schema", {})

        @classmethod
        def get_input_defaults(cls, data: BlockInput) -> BlockInput:
            return data.get("inputs", {})

        @classmethod
        def get_missing_input(cls, data: BlockInput) -> set[str]:
            required_fields = cls.get_input_schema(data).get("required", [])
            return set(required_fields) - set(data)

        @classmethod
        def get_mismatch_error(cls, data: BlockInput) -> str | None:
            return json.validate_with_jsonschema(cls.get_input_schema(data), data)

    class Output(BlockSchema):
        pass

    def __init__(self):
        super().__init__(
            id="e189baac-8c20-45a1-94a7-55177ea42565",
            description="Executes an existing agent inside your agent",
            input_schema=AgentExecutorBlock.Input,
            output_schema=AgentExecutorBlock.Output,
            block_type=BlockType.AGENT,
            categories={BlockCategory.AGENT},
        )

    async def run(self, input_data: Input, **kwargs) -> BlockOutput:

        from backend.executor import utils as execution_utils

        graph_exec = await execution_utils.add_graph_execution(
            graph_id=input_data.graph_id,
            graph_version=input_data.graph_version,
            user_id=input_data.user_id,
            inputs=input_data.inputs,
            nodes_input_masks=input_data.nodes_input_masks,
            use_db_query=False,
        )

        logger = execution_utils.LogMetadata(
            logger=_logger,
            user_id=input_data.user_id,
            graph_eid=graph_exec.id,
            graph_id=input_data.graph_id,
            node_eid="*",
            node_id="*",
            block_name=self.name,
        )

        try:
            async for name, data in self._run(
                graph_id=input_data.graph_id,
                graph_version=input_data.graph_version,
                graph_exec_id=graph_exec.id,
                user_id=input_data.user_id,
                logger=logger,
            ):
                yield name, data
        except asyncio.CancelledError:
            await self._stop(
                graph_exec_id=graph_exec.id,
                user_id=input_data.user_id,
                logger=logger,
            )
            logger.warning(
                f"Execution of graph {input_data.graph_id}v{input_data.graph_version} was cancelled."
            )
        except Exception as e:
            await self._stop(
                graph_exec_id=graph_exec.id,
                user_id=input_data.user_id,
                logger=logger,
            )
            logger.error(
                f"Execution of graph {input_data.graph_id}v{input_data.graph_version} failed: {e}, execution is stopped."
            )
            raise

    async def _run(
        self,
        graph_id: str,
        graph_version: int,
        graph_exec_id: str,
        user_id: str,
        logger,
    ) -> BlockOutput:

        from backend.data.execution import ExecutionEventType
        from backend.executor import utils as execution_utils

        event_bus = execution_utils.get_async_execution_event_bus()

        log_id = f"Graph #{graph_id}-V{graph_version}, exec-id: {graph_exec_id}"
        logger.info(f"Starting execution of {log_id}")

        async for event in event_bus.listen(
            user_id=user_id,
            graph_id=graph_id,
            graph_exec_id=graph_exec_id,
        ):
            if event.status not in [
                ExecutionStatus.COMPLETED,
                ExecutionStatus.TERMINATED,
                ExecutionStatus.FAILED,
            ]:
                logger.debug(
                    f"Execution {log_id} received event {event.event_type} with status {event.status}"
                )
                continue

            if event.event_type == ExecutionEventType.GRAPH_EXEC_UPDATE:
                # If the graph execution is COMPLETED, TERMINATED, or FAILED,
                # we can stop listening for further events.
                break

            logger.debug(
                f"Execution {log_id} produced input {event.input_data} output {event.output_data}"
            )

            if not event.block_id:
                logger.warning(f"{log_id} received event without block_id {event}")
                continue

            block = get_block(event.block_id)
            if not block or block.block_type != BlockType.OUTPUT:
                continue

            output_name = event.input_data.get("name")
            if not output_name:
                logger.warning(f"{log_id} produced an output with no name {event}")
                continue

            for output_data in event.output_data.get("output", []):
                logger.debug(
                    f"Execution {log_id} produced {output_name}: {output_data}"
                )
                yield output_name, output_data

    @retry.func_retry
    async def _stop(
        self,
        graph_exec_id: str,
        user_id: str,
        logger,
    ) -> None:
        from backend.executor import utils as execution_utils

        log_id = f"Graph exec-id: {graph_exec_id}"
        logger.info(f"Stopping execution of {log_id}")

        try:
            await execution_utils.stop_graph_execution(
                graph_exec_id=graph_exec_id,
                user_id=user_id,
                use_db_query=False,
            )
            logger.info(f"Execution {log_id} stopped successfully.")
        except Exception as e:
            logger.error(f"Failed to stop execution {log_id}: {e}")
