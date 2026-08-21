"""What connects a batch to the graph and the graph to the outbound queue.

The worker owns neither side of this: the pipeline hands it a batch, and what
comes back is a set of bubbles that have to be queued in order and then sent.
Keeping the seam here means the worker does not have to know what a GraphState
is, and the graph does not have to know what an ARQ job is.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fittrack.graph.checkpoint import thread_config
from fittrack.graph.state import initial_state
from fittrack.services.batch import Batch
from fittrack.services.outbound import Bubble, OutboundQueue

log = logging.getLogger(__name__)


class Graph(Protocol):
    async def ainvoke(
        self, state: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


class Scheduler(Protocol):
    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any: ...


class GraphRunner:
    """Runs one batch through the graph and queues whatever it produced."""

    def __init__(
        self,
        graph: Graph,
        outbound: OutboundQueue,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._graph = graph
        self._outbound = outbound
        self._scheduler = scheduler

    async def handle(self, batch: Batch, bsuid: str) -> None:
        state = await self._graph.ainvoke(
            dict(
                initial_state(
                    tenant_id=batch.tenant_id,
                    bsuid=bsuid,
                    batch_id=batch.id,
                    input_text=batch.combined_text,
                    message_ids=batch.message_ids,
                )
            ),
            config=thread_config(bsuid),
        )

        for message in state.get("errors", []):
            log.warning("batch %s: %s", batch.id, message)

        bubbles = [
            Bubble(kind=str(item.get("kind", "text")), payload=dict(item.get("payload", {})))
            for item in state.get("outbound", [])
        ]
        if not bubbles:
            # Silence is a legitimate outcome (ack_mode "silent"), but it is
            # also what a broken graph looks like, so it gets a line.
            log.info("batch %s produced nothing to send", batch.id)
            return

        await self._outbound.enqueue(batch.tenant_id, bubbles)

        if self._scheduler is None:
            log.warning("batch %s queued bubbles but nothing will send them", batch.id)
            return
        await self._scheduler.enqueue_job("deliver_outbound", batch.tenant_id, bsuid)
