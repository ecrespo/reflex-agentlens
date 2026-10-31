"""Pinned semantic-convention versions and every key the mapping reads (plan task P3-01.1).

REQ-035, Constitution Art. 1, DD-009, Tech Design §5.3-§5.4.

The target model is the OpenTelemetry GenAI conventions from
``open-telemetry/semantic-conventions-genai`` at :data:`SEMCONV_GENAI_COMMIT` (the repository has
no release yet; re-pin to a tag when one is cut, Plan risk Q-T2), on top of core semantic
conventions :data:`SEMCONV_CORE`. Other dialects (OpenInference, LangSmith, PydanticAI, LangGraph
and the sibling projects' vocabulary of Tech Design §5.4) are read through the same table.

Keys are plain ``str`` constants grouped by dialect. :data:`ALL_KEYS` collects every attribute
key, so tests can check that the mapping table never names a key that is not declared here.
"""

# ruff: noqa: S105 - attribute names, not secrets

from __future__ import annotations

from typing import Final

SEMCONV_CORE: Final = "1.44.0"
SEMCONV_GENAI_REPO: Final = "open-telemetry/semantic-conventions-genai"
SEMCONV_GENAI_COMMIT: Final = "6f98aef121ae3d5f224c5084d9fb4433bc6f7f67"  # main, 2026-10-30

# ── resource ─────────────────────────────────────────────────────────────────────────────────
SERVICE_NAME: Final = "service.name"
SERVICE_NAMESPACE: Final = "service.namespace"

# ── OTel GenAI (spec) ────────────────────────────────────────────────────────────────────────
GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME: Final = "gen_ai.provider.name"
GEN_AI_SYSTEM: Final = "gen_ai.system"  # deprecated name of the provider
GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_PROMPT_TOKENS: Final = "gen_ai.usage.prompt_tokens"  # deprecated
GEN_AI_USAGE_COMPLETION_TOKENS: Final = "gen_ai.usage.completion_tokens"  # deprecated
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: Final = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS: Final = "gen_ai.usage.cache_write.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS: Final = "gen_ai.usage.cache_creation.input_tokens"
GEN_AI_USAGE_REASONING_OUTPUT_TOKENS: Final = "gen_ai.usage.reasoning.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS: Final = "gen_ai.usage.total_tokens"  # ignored for totals (DD-012)
GEN_AI_AGGREGATED_USAGE_PREFIX: Final = "gen_ai.aggregated_usage."  # ignored for totals
GEN_AI_USAGE_DETAILS_PREFIX: Final = "gen_ai.usage.details."  # kept raw only
GEN_AI_RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"
GEN_AI_REQUEST_TEMPERATURE: Final = "gen_ai.request.temperature"
GEN_AI_REQUEST_MAX_TOKENS: Final = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_TOP_P: Final = "gen_ai.request.top_p"
GEN_AI_REQUEST_TOP_K: Final = "gen_ai.request.top_k"
GEN_AI_REQUEST_FREQUENCY_PENALTY: Final = "gen_ai.request.frequency_penalty"
GEN_AI_REQUEST_PRESENCE_PENALTY: Final = "gen_ai.request.presence_penalty"
GEN_AI_REQUEST_SEED: Final = "gen_ai.request.seed"
GEN_AI_REQUEST_STREAM: Final = "gen_ai.request.stream"
GEN_AI_INPUT_MESSAGES: Final = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES: Final = "gen_ai.output.messages"
GEN_AI_SYSTEM_INSTRUCTIONS: Final = "gen_ai.system_instructions"
GEN_AI_TOOL_DEFINITIONS: Final = "gen_ai.tool.definitions"
GEN_AI_PROMPT: Final = "gen_ai.prompt"  # legacy
GEN_AI_COMPLETION: Final = "gen_ai.completion"  # legacy
GEN_AI_TOOL_NAME: Final = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID: Final = "gen_ai.tool.call.id"
GEN_AI_TOOL_TYPE: Final = "gen_ai.tool.type"
GEN_AI_TOOL_CALL_ARGUMENTS: Final = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT: Final = "gen_ai.tool.call.result"
GEN_AI_AGENT_NAME: Final = "gen_ai.agent.name"
GEN_AI_AGENT_ID: Final = "gen_ai.agent.id"
GEN_AI_WORKFLOW_NAME: Final = "gen_ai.workflow.name"
GEN_AI_CONVERSATION_ID: Final = "gen_ai.conversation.id"
ERROR_TYPE: Final = "error.type"
EXCEPTION_TYPE: Final = "exception.type"
EXCEPTION_MESSAGE: Final = "exception.message"

# operation names (gen_ai.operation.name)
OP_CHAT: Final = "chat"
OP_TEXT_COMPLETION: Final = "text_completion"
OP_GENERATE_CONTENT: Final = "generate_content"
OP_EMBEDDINGS: Final = "embeddings"
OP_EXECUTE_TOOL: Final = "execute_tool"
OP_INVOKE_AGENT: Final = "invoke_agent"
OP_CREATE_AGENT: Final = "create_agent"
OP_INVOKE_WORKFLOW: Final = "invoke_workflow"
OP_RETRIEVAL: Final = "retrieval"
OP_PLAN: Final = "plan"
LLM_OPERATIONS: Final = frozenset({OP_CHAT, OP_TEXT_COMPLETION, OP_GENERATE_CONTENT, OP_EMBEDDINGS})
AGENT_OPERATIONS: Final = frozenset({OP_INVOKE_AGENT, OP_CREATE_AGENT, OP_INVOKE_WORKFLOW})

# events
EVENT_SYSTEM_MESSAGE: Final = "gen_ai.system.message"
EVENT_USER_MESSAGE: Final = "gen_ai.user.message"
EVENT_ASSISTANT_MESSAGE: Final = "gen_ai.assistant.message"
EVENT_TOOL_MESSAGE: Final = "gen_ai.tool.message"
EVENT_CHOICE: Final = "gen_ai.choice"
EVENT_INFERENCE_DETAILS: Final = "gen_ai.client.inference.operation.details"
EVENT_EXCEPTION: Final = "exception"
MESSAGE_EVENTS: Final = frozenset(
    {
        EVENT_SYSTEM_MESSAGE,
        EVENT_USER_MESSAGE,
        EVENT_ASSISTANT_MESSAGE,
        EVENT_TOOL_MESSAGE,
        EVENT_CHOICE,
        EVENT_INFERENCE_DETAILS,
    }
)

# metrics (Tech Design §5.3 "Metrics persisted", Amendment A2)
METRIC_TOKEN_USAGE: Final = "gen_ai.client.token.usage"  # legacy histogram
METRIC_INFERENCE_USAGE_PREFIX: Final = "gen_ai.client.inference.usage."
METRIC_OPERATION_DURATION: Final = "gen_ai.client.operation.duration"
METRIC_INFERENCE_DURATION: Final = "gen_ai.client.inference.duration"
METRIC_INFERENCE_TTFC: Final = "gen_ai.client.inference.time_to_first_chunk"
METRIC_OPERATION_TTFC: Final = "gen_ai.client.operation.time_to_first_chunk"  # legacy
METRIC_SERVER_PREFIX: Final = "gen_ai.server."
METRIC_INVOKE_AGENT_PREFIX: Final = "gen_ai.invoke_agent."
METRIC_EXECUTE_TOOL_DURATION: Final = "gen_ai.execute_tool.duration"
GEN_AI_METRIC_PREFIX: Final = "gen_ai."

# ── OpenInference ────────────────────────────────────────────────────────────────────────────
OI_SPAN_KIND: Final = "openinference.span.kind"
OI_LLM_PROVIDER: Final = "llm.provider"
OI_LLM_SYSTEM: Final = "llm.system"
OI_LLM_MODEL_NAME: Final = "llm.model_name"
OI_TOKEN_PROMPT: Final = "llm.token_count.prompt"
OI_TOKEN_COMPLETION: Final = "llm.token_count.completion"
OI_TOKEN_CACHE_READ: Final = "llm.token_count.prompt_details.cache_read"
OI_TOKEN_CACHE_WRITE: Final = "llm.token_count.prompt_details.cache_write"
OI_INVOCATION_PARAMETERS: Final = "llm.invocation_parameters"
OI_INPUT_MESSAGES: Final = "llm.input_messages"  # flattened: llm.input_messages.<i>.message.*
OI_OUTPUT_MESSAGES: Final = "llm.output_messages"
OI_TOOL_NAME: Final = "tool.name"
OI_TOOL_CALL_ID: Final = "tool_call.id"
OI_INPUT_VALUE: Final = "input.value"
OI_OUTPUT_VALUE: Final = "output.value"
OI_SESSION_ID: Final = "session.id"
OI_AGENT_NAME: Final = "agent.name"
OI_GRAPH_NODE_ID: Final = "graph.node.id"
OI_GRAPH_NODE_NAME: Final = "graph.node.name"
OI_GRAPH_NODE_PARENT_ID: Final = "graph.node.parent_id"
OI_METADATA: Final = "metadata"  # JSON string; LangGraph puts langgraph_node/_step here

# ── LangSmith ────────────────────────────────────────────────────────────────────────────────
LS_SPAN_KIND: Final = "langsmith.span.kind"
LS_MODEL_NAME: Final = "langsmith.metadata.ls_model_name"
LS_THREAD_ID: Final = "langsmith.metadata.thread_id"
LS_SESSION_ID: Final = "langsmith.metadata.session_id"
LS_CONVERSATION_ID: Final = "langsmith.metadata.conversation_id"

# ── PydanticAI ───────────────────────────────────────────────────────────────────────────────
PAI_AGENT_NAME: Final = "agent_name"
PAI_MODEL_REQUEST_PARAMETERS: Final = "model_request_parameters"
PAI_ALL_MESSAGES: Final = "pydantic_ai.all_messages"
PAI_FINAL_RESULT: Final = "final_result"
PAI_TOOL_DEFERRAL_NAME: Final = "pydantic_ai.tool.deferral.name"

# ── sibling projects (Tech Design §5.4, REQ-251) ─────────────────────────────────────────────
AGENTKIT_APPROVAL_PREFIX: Final = "reflex_agentkit.approval."
MCPHOST_APPROVAL_PREFIX: Final = "reflex_mcphost.approval."
APPROVAL_PREFIXES: Final = (AGENTKIT_APPROVAL_PREFIX, MCPHOST_APPROVAL_PREFIX)
APPROVAL_ID_SUFFIX: Final = "id"
APPROVAL_DECISION_SUFFIX: Final = "decision"
APPROVAL_APPROVER_SUFFIX: Final = "approver"
APPROVAL_TOOL_CALL_ID_SUFFIX: Final = "tool_call.id"
AGENTKIT_AGUI_RUN_ID: Final = "reflex_agentkit.agui.run_id"
AGENTKIT_AGUI_THREAD_ID: Final = "reflex_agentkit.agui.thread_id"
AGENTKIT_AGUI_EVENT_COUNT: Final = "reflex_agentkit.agui.event_count"
MCPHOST_SERVER_NAME: Final = "reflex_mcphost.server.name"
MCPHOST_SERVER_TRANSPORT: Final = "reflex_mcphost.server.transport"

# ── content-bearing keys (REQ-040 drop; Tech Design §5.3) ────────────────────────────────────
CONTENT_KEYS: Final = frozenset(
    {
        GEN_AI_INPUT_MESSAGES,
        GEN_AI_OUTPUT_MESSAGES,
        GEN_AI_SYSTEM_INSTRUCTIONS,
        GEN_AI_TOOL_DEFINITIONS,
        GEN_AI_TOOL_CALL_ARGUMENTS,
        GEN_AI_TOOL_CALL_RESULT,
        GEN_AI_PROMPT,
        GEN_AI_COMPLETION,
        OI_INPUT_VALUE,
        OI_OUTPUT_VALUE,
        PAI_ALL_MESSAGES,
        PAI_FINAL_RESULT,
    }
)
CONTENT_KEY_PREFIXES: Final = (
    OI_INPUT_MESSAGES + ".",
    OI_OUTPUT_MESSAGES + ".",
    GEN_AI_PROMPT + ".",  # legacy indexed form gen_ai.prompt.<i>.content
    GEN_AI_COMPLETION + ".",
    "llm.prompts.",  # OpenInference completions-API prompts
    "llm.prompt_template.",
    "llm.tools.",  # tool definitions (JSON schemas, descriptions)
    "retrieval.documents.",
    "reranker.input_documents.",
    "reranker.output_documents.",
    "embedding.embeddings.",
)


def _collect() -> frozenset[str]:
    """Every attribute key above (names, not prefixes, events, operations or metrics)."""
    skip = ("OP_", "EVENT_", "METRIC_", "SEMCONV_", "APPROVAL_", "LLM_", "AGENT_", "MESSAGE_")
    return frozenset(
        value
        for name, value in globals().items()
        if name.isupper()
        and isinstance(value, str)
        and not name.startswith(skip)
        and not name.endswith(("_PREFIX", "_REPO"))
    )


ALL_KEYS: Final = _collect()
"""Attribute keys the mapping may read (see the module docstring)."""
