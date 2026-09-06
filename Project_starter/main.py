"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser

from pydantic import BaseModel, Field, ValidationError


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# TODO: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()  # Replace this line


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-qp7wk76ult.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   # TODO: Replace with your Gateway URL
KB_ID       = "JOAYIABGDW"          # TODO: Replace with your Knowledge Base ID
REGION      = "us-east-1"        # TODO: Replace with your AWS region
MEMORY_ID   = "CustomerSupportMemory-3BPCXX4Wc5"        # TODO: Replace with your Memory ID


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)  # Replace this line

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)  # Replace this line

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)  # Replace this line

SYSTEM_PROMPT = """You are CSAI_Agent, a helpful customer support assistant for an e-commerce platform.

You can help customers with:
- Order tracking and status inquiries
- Processing returns and refunds
- Product information, specifications, and recommendations
- Loyalty program details, tier benefits, and discount calculations
- Return policies and warranty information

Guidelines:
- Rely on the tools available to you through the Gateway — do not assume capabilities that aren't exposed as tools.
- For product or policy questions, search the knowledge base first.
- For order tracking or refund requests, use the appropriate gateway tools.
- For discount calculations, use the loyalty discount calculator.
- If a request needs information across multiple tools, call them in sequence and combine the results.
- If no available tool can fulfil a request, say so clearly instead of guessing.
- Ask clarifying questions when the user's request is ambiguous or missing required details.
- If you remember context about the customer from previous interactions, use it to personalize your responses.
- Present results in a clear, concise, customer-friendly format."""


# ===========================================================================
# PYDANTIC MODELS
# ===========================================================================

class DiscountBreakdown(BaseModel):
    """Validated loyalty discount calculation result."""
    original_total: float = Field(ge=0, description="Original order total in USD")
    tier: str = Field(description="Customer loyalty tier")
    tier_discount_rate: str = Field(description="Tier discount percentage, e.g. '10%'")
    tier_discount: float = Field(ge=0, description="Dollar amount saved from tier")
    points_redeemed: int = Field(ge=0, description="Loyalty points used")
    points_discount: float = Field(ge=0, description="Dollar amount saved from points")
    total_discount: float = Field(ge=0, description="Combined savings")
    final_total: float = Field(ge=0, description="Amount the customer pays")
    remaining_points: int = Field(ge=0, description="Points left after redemption")


class KBSearchResult(BaseModel):
    """Validated knowledge base search response."""
    query: str = Field(description="Original search query")
    num_results: int = Field(ge=0, description="Number of chunks returned")
    content: str = Field(description="Combined retrieval text")


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict[str, str]:
    """Return a dict mapping strategy type → namespace template string."""
    # TODO: Implement this function

    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        # TODO: Store actor_id, session_id, memory_id, memory_client as attributes
        # TODO: Call get_namespaces() and store the result as self.namespaces

        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)
        logger.info("Namespaces loaded: %s", self.namespaces)

    # Implement conversation summarization
    def _summarize_if_needed(self, agent):
        """Summarize older turns if conversation exceeds token budget."""
        messages = agent.messages
        MAX_TURNS = 20  # threshold before summarizing

        if len(messages) <= MAX_TURNS:
            return

        # Keep the last 10 turns, summarize everything before that
        old_messages = messages[:-10]
        recent_messages = messages[-10:]

        # Build a summary of older turns
        summary_parts = []
        for msg in old_messages:
            role = msg["role"]
            content = msg.get("content", [])
            if isinstance(content, list) and content:
                text = content[0].get("text", "")
                if text and "toolResult" not in content[0]:
                    summary_parts.append(f"{role}: {text[:200]}")

        summary = "Previous conversation summary:\n" + "\n".join(summary_parts[-5:])

        # Replace message history: summary as first message + recent turns
        agent.messages.clear()
        agent.messages.append({
            "role": "user",
            "content": [{"text": summary}]
        })
        agent.messages.extend(recent_messages)
        logger.info("Summarized %d old turns into 1 summary message", len(old_messages))

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        # TODO: Implement memory retrieval
        # Steps:
        #   1. Get the last message from event.agent.messages
        #   2. Check it is a user message and not a tool result
        #   3. Extract the user query text
        #   4. For each namespace in self.namespaces, call retrieve_memories()
        #   5. Collect non-empty memory texts with strategy type tags
        #   6. If any found, prepend them to the user message
        
        # Summarize if conversation is getting long
        self._summarize_if_needed(event.agent)

        actor_id = self.actor_id

        messages = event.agent.messages
        if (
            not messages
            or messages[-1]["role"] != "user"
            or "toolResult" in messages[-1]["content"][0]
        ):
            return

        user_query = messages[-1]["content"][0]["text"]

        try:
            all_context = []
            for strategy_type, namespace in self.namespaces.items():
                resolved_namespace = namespace.format(actorId=actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=resolved_namespace,
                    query=user_query,
                    top_k=5,
                )
                for memory in memories:
                    if isinstance(memory, dict):
                        text = memory.get("content", {}).get("text", "").strip()
                        if text:
                            all_context.append(f"[{strategy_type}] {text}")

            if all_context:
                context_block = "\n".join(all_context)
                original_text = messages[-1]["content"][0]["text"]
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{context_block}\n\n{original_text}"
                )
                logger.info("Retrieved %d memory items for actor %s", len(all_context), actor_id)

        except Exception as exc:
            logger.error("Failed to retrieve customer context: %s", exc)

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        # TODO: Implement memory saving
        # Steps:
        #   1. Get messages from event.agent.messages
        #   2. Walk backwards to find the last user query (plain text)
        #      and the last assistant response
        #   3. Call memory_client.create_event() with both messages

        actor_id = self.actor_id
        session_id = self.session_id

        try:
            messages = event.agent.messages
            user_text = agent_text = None

            for msg in reversed(messages):
                if msg["role"] == "assistant" and not agent_text:
                    content = msg["content"]
                    if isinstance(content, list):
                        agent_text = content[0].get("text", "")
                    else:
                        agent_text = str(content)
                elif (
                    msg["role"] == "user"
                    and not user_text
                    and "toolResult" not in msg["content"][0]
                ):
                    user_text = msg["content"][0]["text"]
                    break

            if user_text and agent_text:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    messages=[
                        (user_text, "USER"),
                        (agent_text, "ASSISTANT"),
                    ],
                )
                logger.info("Saved interaction to memory for actor %s", actor_id)

        except Exception as exc:
            logger.error("Failed to save interaction: %s", exc)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        # TODO: Register retrieve_customer_context on MessageAddedEvent
        # TODO: Register save_support_interaction on AfterInvocationEvent
        
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    resp = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
    )
    results = resp.get("retrievalResults", [])
    if not results:
        return f"No information found for: {query}"

    chunks = [r["content"]["text"] for r in results]
    combined = "\n---\n".join(chunks)

    # Validate output
    try:
        validated = KBSearchResult(
            query=query,
            num_results=len(chunks),
            content=combined,
        )
        return validated.model_dump_json(indent=2)
    except ValidationError as e:
        logger.warning("KB result validation failed: %s", e)
        return combined


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # TODO: Build the code string (use an f-string to inject the arguments)
    code = f"""
    import json

    loyalty_points = {loyalty_points}
    tier = "{tier}"
    order_total = {order_total}
    product_category = "{product_category}"

    # Tier discount rates
    tier_discounts = {{"Bronze": 0.02, "Silver": 0.05, "Gold": 0.10, "Platinum": 0.15}}
    tier_discount_rate = tier_discounts.get(tier, 0.0)

    # Category multipliers
    category_multipliers = {{"standard": 1.0, "electronics": 0.5, "premium": 0.75}}
    multiplier = category_multipliers.get(product_category, 1.0)

    # Points redemption: 100 points = $1
    points_to_redeem = min(loyalty_points, int(order_total * 100 * multiplier))
    points_discount = points_to_redeem / 100

    # Tier discount
    tier_discount = round(order_total * tier_discount_rate, 2)

    # Final total
    total_discount = round(points_discount + tier_discount, 2)
    final_total = round(max(order_total - total_discount, 0), 2)
    remaining_points = loyalty_points - points_to_redeem

    result = {{
        "original_total": order_total,
        "tier": tier,
        "tier_discount_rate": f"{{int(tier_discount_rate * 100)}}%",
        "tier_discount": tier_discount,
        "points_redeemed": points_to_redeem,
        "points_discount": points_discount,
        "total_discount": total_discount,
        "final_total": final_total,
        "remaining_points": remaining_points,
    }}
    print(json.dumps(result))
    """

    try:
        # TODO: Execute the code using code_session and return the result
        with code_session(REGION) as code_client:
            response = code_client.invoke("executeCode", {
                "code": code,
                "language": "python",
                "clearContext": True,
            })

            for event in response["stream"]:
                raw = event["result"]
                # Validate the sandbox output
                try:
                    validated = DiscountBreakdown.model_validate_json(
                        raw if isinstance(raw, str) else json.dumps(raw)
                    )
                    return validated.model_dump_json(indent=2)
                except ValidationError as e:
                    logger.warning("Discount output validation failed: %s", e)
                    return json.dumps(raw)

    except Exception as exc:
        # TODO: Implement fallback calculation using tier discount only
        logger.error("Code Interpreter failed: %s", exc)
        # Fallback: do the calculation locally
        tier_discounts = {"Bronze": 0.02, "Silver": 0.05, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_discounts.get(tier, 0.0)
        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)
        return json.dumps({
            "original_total": order_total,
            "tier": tier,
            "tier_discount": tier_discount,
            "final_total": final_total,
            "note": "Simplified calculation — Code Interpreter unavailable",
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    # TODO: Implement the agent invocation
    # Step 1: Extract payload fields
    user_input = payload.get("prompt", "Hello!")
    actor_id = payload.get("customer_id", "unknown")
    session_id = payload.get("session_id", str(uuid.uuid4()))

    logger.info("User [%s] session [%s]: %s", actor_id, session_id, user_input[:80])

    try:
        # Step 2: Instantiate MemoryHook
        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        # Step 3: Instantiate AgentCoreBrowser
        agent_core_browser = AgentCoreBrowser(region=REGION)

        # Step 4: Build initial tools list
        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        # Step 5: Connect to Gateway and extend tools
        client = MCPClient(lambda: streamable_http_client(url=GATEWAY_URL))
        with client:
            gateway_tools = client.list_tools_sync()
            logger.info("Discovered %d gateway tools", len(gateway_tools))
            tools.extend(gateway_tools)

            # Step 6: Create and invoke Agent
            agent = Agent(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                tools=tools,
                hooks=[memory_hook],
            )

            response = agent(user_input)

        # Step 7: Return first content block text
        return response.message["content"][0]["text"]

    except Exception as exc:
        # Step 8: Handle exceptions
        logger.error("Agent invocation failed: %s", exc)
        return f"I'm sorry, something went wrong processing your request. Error: {exc}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
