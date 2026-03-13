"""
AI agent that demonstrates silent failure detection.

This agent uses Claude to answer questions about a user's order history.
It has a tool (lookup_order_history) that can silently fail by returning
empty results, simulating an upstream data source issue.

Instrumented with:
- agent.empty_context_rate metric: tracks when the tool returns no data
- Structured logging for Vega correlation
- LaunchDarkly observability SDK integration (when configured)
"""

import json
import logging
import os
import random
import time
from contextlib import contextmanager

import anthropic
from dotenv import load_dotenv

load_dotenv()

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agent")

# --- LaunchDarkly Observability SDK ---

LD_SDK_KEY = os.environ.get("LD_SDK_KEY")
ld_observe = None
ld_client = None
tracer = None

if LD_SDK_KEY:
    try:
        import ldclient
        from ldclient.config import Config
        from ldobserve import ObservabilityConfig, ObservabilityPlugin, observe
        from opentelemetry import trace

        plugin = ObservabilityPlugin(
            ObservabilityConfig(
                service_name="silent-failure-demo",
                service_version=os.environ.get("SERVICE_VERSION", "dev"),
            )
        )

        config = Config(
            sdk_key=LD_SDK_KEY,
            plugins=[plugin],
        )
        # Use LDClient directly with start_wait to block until initialized
        ld_client = ldclient.LDClient(config=config, start_wait=10)
        if not ld_client.is_initialized():
            logger.warning("LaunchDarkly client failed to initialize — check your LD_SDK_KEY")
        ld_observe = observe
        tracer = trace.get_tracer("silent-failure-demo")
        logger.info("LaunchDarkly observability SDK initialized")
    except ImportError:
        logger.warning(
            "LD_SDK_KEY is set but required packages are not installed. "
            "Install with: pip install launchdarkly-server-sdk launchdarkly-observability opentelemetry-api"
        )
else:
    logger.info("LD_SDK_KEY not set — using local metrics store (demo mode)")


@contextmanager
def traced_span(name: str, attributes: dict | None = None):
    """Create a trace span if the LaunchDarkly SDK is configured, otherwise no-op."""
    if tracer:
        with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
            yield span
    else:
        yield None

# --- Metrics ---

metrics_store: dict[str, list[float]] = {}


def metric(name: str, value: float, attributes: dict | None = None) -> None:
    """Record a metric value.

    When LD_SDK_KEY is configured, sends the metric to LaunchDarkly via the
    observability SDK. Otherwise, records locally for the demo summary.
    """
    metrics_store.setdefault(name, []).append(value)
    logger.info("metric: %s = %s", name, value)

    if ld_observe:
        ld_observe.record_metric(name, value, attributes=attributes or {})


def get_metric_summary(name: str) -> dict:
    values = metrics_store.get(name, [])
    if not values:
        return {"count": 0, "avg": 0.0}
    return {
        "count": len(values),
        "avg": sum(values) / len(values),
    }


# --- Tool: Order History Lookup ---

SAMPLE_ORDERS = {
    "user_123": [
        {"id": "ORD-001", "item": "Wireless Headphones", "total": 79.99, "status": "delivered"},
        {"id": "ORD-002", "item": "USB-C Hub", "total": 34.99, "status": "shipped"},
    ],
    "user_456": [
        {"id": "ORD-003", "item": "Mechanical Keyboard", "total": 129.00, "status": "delivered"},
    ],
}

# Set this to control the failure rate (0.0 = never fail, 1.0 = always fail)
EMPTY_RESULT_RATE = float(os.environ.get("EMPTY_RESULT_RATE", "0.4"))


def lookup_order_history(user_id: str) -> dict:
    """Simulate an upstream data source that can silently return empty results."""
    if random.random() < EMPTY_RESULT_RATE:
        # Simulate upstream returning nothing
        return {}
    return {"orders": SAMPLE_ORDERS.get(user_id, [])}


# --- Agent ---

TOOLS = [
    {
        "name": "lookup_order_history",
        "description": "Look up a user's order history by their user ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "The user's ID, e.g. 'user_123'",
                }
            },
            "required": ["user_id"],
        },
    }
]


def execute_tool(name: str, tool_input: dict, context: dict) -> str:
    """Execute a tool call and instrument the result."""
    with traced_span("tool_call", {"tool.name": name, "user.id": context.get("user_id", "unknown")}) as span:
        if name == "lookup_order_history":
            result = lookup_order_history(tool_input["user_id"])
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

        is_empty = not result or len(result) == 0

        if span:
            span.set_attribute("tool.result_empty", is_empty)
            span.set_attribute("tool.result_count", len(result.get("orders", [])) if result else 0)

        # Track whether the agent had real data to work with
        metric(
            "agent.empty_context_rate",
            1 if is_empty else 0,
            attributes={"tool": name, "user_id": context.get("user_id", "unknown")},
        )

        # Log enough context for Vega to correlate later
        logger.info(
            "agent.tool_result | tool=%s user_id=%s is_empty=%s",
            name,
            context.get("user_id", "unknown"),
            is_empty,
        )

        return json.dumps(result) if result else json.dumps({"orders": []})


def run_agent(user_query: str, user_id: str = "user_123") -> str:
    """Run the agent with a user query, returning the final response."""
    with traced_span("agent_request", {"user.id": user_id, "agent.query": user_query}) as span:
        client = anthropic.Anthropic()
        context = {"user_id": user_id}

        messages = [{"role": "user", "content": user_query}]

        print(f"\n{'='*60}")
        print(f"User: {user_query}")
        print(f"{'='*60}")

        llm_calls = 0
        while True:
            with traced_span("llm_call", {"llm.model": "claude-opus-4-6", "llm.call_number": llm_calls}):
                response = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=1024,
                    system=(
                        "You are a helpful customer support agent. Use the lookup_order_history "
                        "tool to answer questions about a user's orders. The current user's ID "
                        f"is '{user_id}'."
                    ),
                    tools=TOOLS,
                    messages=messages,
                )
            llm_calls += 1

            if response.stop_reason == "end_turn":
                break

            # Process tool use blocks
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tool in tool_use_blocks:
                result = execute_tool(tool.name, tool.input, context)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        # Extract final text
        final_text = next(
            (b.text for b in response.content if b.type == "text"), ""
        )

        if span:
            span.set_attribute("agent.llm_calls", llm_calls)
            span.set_attribute("agent.response_length", len(final_text))

        print(f"\nAgent: {final_text}")
        return final_text


def main():
    print("=" * 60)
    print("Silent Failure Detection Demo")
    print(f"Empty result rate: {EMPTY_RESULT_RATE:.0%}")
    print("=" * 60)

    queries = [
        "What's the status of my most recent order?",
        "How many orders do I have?",
        "Can you tell me about my order history?",
        "What was the total of my last order?",
        "Do I have any orders that are currently shipped?",
    ]

    user_ids = ["user_123", "user_456", "user_123", "user_123", "user_456"]

    for query, uid in zip(queries, user_ids):
        run_agent(query, user_id=uid)
        time.sleep(1)  # small delay between requests

    # Print metric summary
    summary = get_metric_summary("agent.empty_context_rate")
    print(f"\n{'='*60}")
    print("Metric Summary: agent.empty_context_rate")
    print(f"  Total tool calls: {summary['count']}")
    print(f"  Empty context rate: {summary['avg']:.0%}")
    print(f"{'='*60}")

    if summary["avg"] > 0.15:
        print(
            "\n⚠ ALERT: agent.empty_context_rate exceeded 15% threshold!\n"
            "In production, Vega would investigate this spike and\n"
            "deliver a root cause summary to your Slack channel."
        )
    else:
        print("\n✓ Empty context rate is within normal range.")


if __name__ == "__main__":
    try:
        main()
    finally:
        if ld_client:
            ld_client.close()
