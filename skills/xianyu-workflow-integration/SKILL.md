---
name: xianyu-workflow-integration
description: Use when integrating an external asynchronous business system into XianyuAutoAgent, especially for start-task, poll-status, QR-login, image delivery, or AI-rendered fulfillment results.
---

# Xianyu Workflow Integration

Use this skill when we need to connect `XianyuAutoAgent` to an external async workflow system such as B 站答题、发卡、登录授权、审核处理 or other post-order automation.

## Core Rule

Do not wire Agent directly to internal business endpoints unless they already speak Agent's contract.

Prefer a thin adapter layer that converts:
- `order.status.changed` webhook from Agent
- into your internal `start` API
- and converts your internal `poll` API
- back into Agent's task polling contract

## Read First

Before proposing or implementing an integration, read:
- `docs/integration/xianyu-workflow-contract.md`
- `docs/actions.md`
- `docs/integration/webhook-example.md`

If code alignment is needed, inspect:
- `core/handlers/order_route_handler.py`
- `core/async_task_poller.py`
- `core/action_executor.py`
- `main.py`

## Expected Shape

The standard workflow is:

1. Agent receives `order.status.changed`
2. Agent sends event to adapter endpoint configured in `ORDER_ITEM_WEBHOOK_ROUTES`
3. Adapter calls internal `start` API
4. Adapter returns Agent actions such as:
   - `send_image`
   - `send_text`
   - `track_async_task`
5. Agent polls adapter `status_url`
6. Adapter calls internal `poll` API
7. Adapter returns:
   - top-level `status`
   - optional `message`
   - optional `result`
   - optional `presentation.mode=ai`
8. Agent notifies buyer and stops polling on final status

## Required Constraints

- Do not return raw `{"data": ...}` payloads to Agent. Flatten them.
- Do not use `localhost` in Docker-network URLs. Use service names.
- Keep business truth in the external system. Agent may render wording, but should not infer scores or pass/fail state.
- Use `send_image` only for URLs that return real image bytes.
- Use `presentation.mode=ai` when Agent should rewrite facts into buyer-facing language.

## Start Endpoint Checklist

- Verify `X-Agent-Signature`
- Ignore non-triggering order statuses by returning `{"actions":[]}`
- Call the internal start API
- Convert QR image URL into `send_image`
- Convert fallback login URL into `send_text`
- Create and persist a stable `task_id`
- Return `track_async_task` with adapter polling URL

## Poll Endpoint Checklist

- Resolve `task_id` to internal workflow state
- Call the internal poll API
- Flatten response to top-level `status`, `message`, `result`
- Map status into one of:
  - `await_login`
  - `running`
  - `passed`
  - `failed`
  - `timeout`
  - `cancelled`
- If natural buyer wording is desired, return `presentation.mode=ai`

## Good Defaults

- `poll_interval_seconds`: `5`
- Use `send_image` plus a backup `send_text`
- Use `presentation.mode=ai` for final outcome messages
- Provide `fallback_text` whenever using AI rendering

## When Not to Use This Skill

- Pure AI chat replies with no external workflow
- Synchronous webhook callbacks that already return final `send_text`
- Integrations that do not involve Xianyu order/chat events
