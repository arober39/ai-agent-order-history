# ai-agent-order-history

A demo AI agent that answers customer questions about order history, with built-in silent failure detection and observability instrumentation.

## Overview

This project simulates a customer support agent powered by Claude that looks up order history. The key twist: the order lookup tool **silently fails 40% of the time** (configurable), returning empty results instead of raising errors. The project demonstrates how to detect and instrument these silent failures using metrics, structured logging, and distributed tracing.

## Tech Stack

- **Python 3** with the [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) (Claude Opus 4.6)
- **OpenTelemetry** for distributed tracing (optional)
- **LaunchDarkly** observability SDK for metric reporting (optional)

## Setup

```bash
# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

Create a `.env` file with:

```
ANTHROPIC_API_KEY=your-api-key
```

### Optional environment variables

| Variable | Description | Default |
|---|---|---|
| `EMPTY_RESULT_RATE` | Probability the tool returns empty results (0.0–1.0) | `0.4` |
| `LD_SDK_KEY` | LaunchDarkly SDK key for observability | — |
| `SERVICE_VERSION` | Version tag for tracing metadata | `dev` |

## Usage

```bash
python3 agent.py
```

The agent processes 5 sample queries about order history, prints responses, and reports an alert if the empty context rate exceeds 15%.

## How It Works

1. **Agent loop** — Takes a user query, sends it to Claude with a tool definition for order lookup, and iterates until Claude produces a final response.
2. **Order lookup tool** — Returns mock order data for known users (`user_123`, `user_456`). Silently returns an empty result at a configurable rate to simulate upstream failures.
3. **Instrumentation** — Each tool call is wrapped with:
   - A metric (`agent.empty_context_rate`) recording whether the result was empty
   - Structured log lines with tool name, user ID, and empty status
   - Optional OpenTelemetry spans with result metadata attributes

## Sample Data

| User | Orders |
|---|---|
| `user_123` | Wireless Headphones ($79.99), USB-C Hub ($34.99) |
| `user_456` | Mechanical Keyboard ($129.00) |
