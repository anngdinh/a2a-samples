import asyncio
import logging
import time
from typing import Any, Dict, Optional
from uuid import uuid4

import streamlit as st
import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory, create_text_message_object
from a2a.types import (
    AgentCard,
    Message,
    Task,
    TransportProtocol,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    EXTENDED_AGENT_CARD_PATH,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="A2A Agent Chat",
    page_icon="🤖",
    layout="wide"
)

st.title("🤖 A2A Protocol Agent Chat")
st.markdown("Chat with your agent using the A2A protocol")

# Get URL parameters
query_params = st.query_params
default_agent_url = query_params.get("agent_url", "http://localhost:10000")
default_streaming = query_params.get("streaming", "false").lower() == "true"

if "messages" not in st.session_state:
    st.session_state.messages = []

if "context_id" not in st.session_state:
    # Always initialize a context_id for the conversation
    st.session_state.context_id = str(uuid4())

if "task_id" not in st.session_state:
    st.session_state.task_id = None

if "agent_card" not in st.session_state:
    st.session_state.agent_card = None

if "client_initialized" not in st.session_state:
    st.session_state.client_initialized = False

with st.sidebar:
    st.header("⚙️ Configuration")

    base_url = st.text_input(
        "Agent Base URL",
        value=default_agent_url,
        help="The base URL of your A2A agent server"
    )

    use_auth = st.checkbox(
        "Use Authentication",
        help="Check this if your agent requires authentication"
    )

    auth_token = ""
    if use_auth:
        auth_token = st.text_input(
            "Authorization Token",
            type="password",
            placeholder="Bearer token...",
            help="Enter your authorization token"
        )

    streaming_mode = st.checkbox(
        "Enable Streaming",
        value=default_streaming,
        help="Stream responses from the agent"
    )

    col1, col2 = st.columns(2)
    with col1:
        connect_btn = st.button(
            "🔌 Connect", type="primary", use_container_width=True)
    with col2:
        clear_btn = st.button("🗑️ Clear Chat", use_container_width=True)

    if clear_btn:
        st.session_state.messages = []
        # New context for new conversation
        st.session_state.context_id = str(uuid4())
        st.session_state.task_id = None
        st.rerun()

    if st.session_state.agent_card:
        st.success("✅ Connected to Agent")
        with st.expander("Agent Card Details"):
            st.json(st.session_state.agent_card.model_dump(exclude_none=True))

        st.divider()
        st.caption("📝 Current Conversation")
        st.code(f"Context ID: {st.session_state.context_id}")
        if st.session_state.task_id:
            st.code(f"Task ID: {st.session_state.task_id}")
    else:
        st.info("👆 Click Connect to establish connection")


async def fetch_agent_card(base_url: str, auth_token: str = "") -> Optional[AgentCard]:
    """Fetch the agent card from the server."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as httpx_client:
            resolver = A2ACardResolver(
                httpx_client=httpx_client,
                base_url=base_url,
            )

            logger.info(
                f"Fetching agent card from: {base_url}{AGENT_CARD_WELL_KNOWN_PATH}")

            public_card = None
            try:
                public_card = await resolver.get_agent_card()
            except Exception as e:
                logger.warning(
                    f"Failed to fetch from {AGENT_CARD_WELL_KNOWN_PATH}, trying /.well-known/agent.json")
                try:
                    public_card = await resolver.get_agent_card(
                        relative_card_path="/.well-known/agent.json"
                    )
                    logger.info(
                        "Successfully fetched agent card from /.well-known/agent.json")
                except Exception as fallback_e:
                    logger.error(
                        f"Failed to fetch from both paths: {fallback_e}")
                    raise

            if public_card and public_card.supports_authenticated_extended_card and auth_token:
                try:
                    auth_headers = {"Authorization": f"Bearer {auth_token}"}
                    extended_card = await resolver.get_agent_card(
                        relative_card_path=EXTENDED_AGENT_CARD_PATH,
                        http_kwargs={'headers': auth_headers},
                    )
                    logger.info("Using extended agent card")
                    return extended_card
                except Exception as e:
                    logger.warning(f"Failed to fetch extended card: {e}")
                    return public_card

            return public_card

    except Exception as e:
        logger.error(f"Failed to fetch agent card: {e}")
        return None


async def send_message_to_agent(
    message: str,
    base_url: str,
    agent_card: AgentCard,
    auth_token: str = "",
    context_id: Optional[str] = None,
    task_id: Optional[str] = None,
    streaming: bool = False
) -> Dict[str, Any]:
    """Send a message to the agent and get the response."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as httpx_client:
            config = ClientConfig(
                httpx_client=httpx_client,
                supported_transports=[TransportProtocol.jsonrpc]
            )
            factory = ClientFactory(config)
            client = factory.create(agent_card)

            # Create message object
            message_obj = create_text_message_object(
                content=message
            )
            message_obj.message_id = uuid4().hex
            
            if context_id:
                message_obj.context_id = context_id
            if task_id:
                message_obj.task_id = task_id

            if auth_token:
                httpx_client.headers["Authorization"] = f"Bearer {auth_token}"

            if streaming:
                response_parts = []
                collected_text = []
                final_response = None

                async for event in client.send_message(message_obj):
                    response_parts.append(event)
                    
                    # Handle different event types
                    if isinstance(event, Message):
                        # Direct message response
                        for part in event.parts:
                            if hasattr(part.root, 'text'):
                                collected_text.append(part.root.text)
                        final_response = event.model_dump(mode='json', exclude_none=True)
                    elif isinstance(event, tuple) and len(event) >= 1:
                        # Task update event
                        task = event[0]
                        if task.status and task.status.message:
                            for part in task.status.message.parts:
                                if hasattr(part.root, 'text'):
                                    collected_text.append(part.root.text)
                        
                        # Check for artifacts
                        if task.artifacts:
                            for artifact in task.artifacts:
                                for part in artifact.parts:
                                    if hasattr(part.root, 'text'):
                                        collected_text.append(part.root.text)
                        
                        final_response = task.model_dump(mode='json', exclude_none=True)

                if final_response:
                    # Add collected text to the final response
                    if collected_text:
                        # Ensure we have a proper structure for the text
                        if 'result' not in final_response:
                            final_response['result'] = {}
                        if 'artifacts' not in final_response['result']:
                            final_response['result']['artifacts'] = []

                        # Add collected text as an artifact
                        final_response['result']['artifacts'].append({
                            'parts': [{'text': '\n'.join(collected_text)}]
                        })

                    return final_response
                return {"error": "No response received"}
            else:
                # Non-streaming: collect all responses
                responses = []
                async for event in client.send_message(message_obj):
                    responses.append(event)
                
                if responses:
                    # Return the last response
                    last_event = responses[-1]
                    if isinstance(last_event, Message):
                        return last_event.model_dump(mode='json', exclude_none=True)
                    elif isinstance(last_event, tuple) and len(last_event) >= 1:
                        task = last_event[0]
                        return {'result': task.model_dump(mode='json', exclude_none=True)}
                
                return {"error": "No response received"}

    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return {"error": str(e)}


if connect_btn:
    with st.spinner("Connecting to agent..."):
        agent_card = asyncio.run(fetch_agent_card(base_url, auth_token))

        if agent_card:
            st.session_state.agent_card = agent_card
            st.session_state.client_initialized = True
            st.success("Successfully connected to agent!")
            st.rerun()
        else:
            st.error(
                "Failed to connect to agent. Please check the URL and try again.")


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            if "duration" in message:
                st.caption(f"⏱️ Response time: {message['duration']:.2f}s")
            if "response_data" in message:
                with st.expander("📊 Full Response"):
                    st.json(message["response_data"])


if prompt := st.chat_input("Type your message here...", disabled=not st.session_state.client_initialized):
    start_time = time.time()
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if streaming_mode:
            # Streaming mode - display responses in real-time
            message_placeholder = st.empty()
            final_answer = ""

            async def stream_response():
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as httpx_client:
                        config = ClientConfig(
                            httpx_client=httpx_client,
                            supported_transports=[TransportProtocol.jsonrpc]
                        )
                        factory = ClientFactory(config)
                        client = factory.create(st.session_state.agent_card)

                        # Create message object
                        message_obj = create_text_message_object(
                            content=prompt
                        )
                        message_obj.message_id = uuid4().hex
                        
                        if st.session_state.context_id:
                            message_obj.context_id = st.session_state.context_id
                        if st.session_state.task_id:
                            message_obj.task_id = st.session_state.task_id

                        if auth_token:
                            httpx_client.headers["Authorization"] = f"Bearer {auth_token}"

                        streaming_text = ""
                        final_text = ""

                        async for event in client.send_message(message_obj):
                            # Handle different event types
                            if isinstance(event, Message):
                                # Direct message response
                                for part in event.parts:
                                    if hasattr(part.root, 'text'):
                                        final_text = part.root.text
                                        message_placeholder.markdown(final_text)
                                
                                # Update context/task IDs if present
                                if hasattr(event, 'context_id'):
                                    st.session_state.context_id = event.context_id
                                if hasattr(event, 'task_id'):
                                    st.session_state.task_id = event.task_id
                                    
                            elif isinstance(event, tuple) and len(event) >= 1:
                                # Task update event
                                task = event[0]
                                
                                # Update IDs
                                if hasattr(task, 'context_id'):
                                    st.session_state.context_id = task.context_id
                                if hasattr(task, 'id'):
                                    st.session_state.task_id = task.id
                                
                                # Check for text in status message
                                if task.status and task.status.message:
                                    for part in task.status.message.parts:
                                        if hasattr(part.root, 'text'):
                                            text = part.root.text
                                            if not final_text:  # Stream if no final answer
                                                streaming_text += text
                                                message_placeholder.markdown(streaming_text)
                                
                                # Check for artifacts (final answer)
                                if task.artifacts:
                                    for artifact in task.artifacts:
                                        for part in artifact.parts:
                                            if hasattr(part.root, 'text'):
                                                final_text = part.root.text
                                                message_placeholder.markdown(final_text)

                        return final_text or streaming_text

                except Exception as e:
                    if 'terminal state' in str(e) or 'completed' in str(e):
                        st.session_state.task_id = None
                        return await stream_response()
                    else:
                        st.error(f"Error: {e}")
                        return ""

            # Run streaming
            final_answer = asyncio.run(stream_response())
            assistant_message = final_answer or "Response received (no text content)"
            response_data = None  # Streaming mode doesn't have full response data

        else:
            # Non-streaming mode - original behavior
            with st.spinner("Thinking..."):
                response_data = asyncio.run(
                    send_message_to_agent(
                        message=prompt,
                        base_url=base_url,
                        agent_card=st.session_state.agent_card,
                        auth_token=auth_token,
                        context_id=st.session_state.context_id,
                        task_id=st.session_state.task_id,
                        streaming=streaming_mode
                    )
                )

                if "error" in response_data:
                    # Check if it's a completed task error
                    error_msg = str(response_data.get('error', ''))
                    if 'terminal state' in error_msg or 'completed' in error_msg:
                        # Only reset task_id, keep context_id for conversation continuity
                        st.session_state.task_id = None
                        # Retry the message with same context but no task
                        response_data = asyncio.run(
                            send_message_to_agent(
                                message=prompt,
                                base_url=base_url,
                                agent_card=st.session_state.agent_card,
                                auth_token=auth_token,
                                context_id=st.session_state.context_id,
                                task_id=None,
                                streaming=streaming_mode
                            )
                        )
                    else:
                        st.error(f"Error: {response_data['error']}")
                        assistant_message = f"Sorry, I encountered an error: {response_data['error']}"

                if "error" not in response_data:
                    try:
                        result = response_data.get('result', {})

                        if 'context_id' in result:
                            st.session_state.context_id = result['context_id']
                        if 'id' in result:
                            st.session_state.task_id = result['id']

                        assistant_message = None

                        # Check status for input-required state with message
                        status = result.get('status', {})
                        state = status.get('state', '')

                        if state == 'input-required' and 'message' in status:
                            # Extract message from status when input is required
                            status_message = status['message']
                            parts = status_message.get('parts', [])
                            for part in parts:
                                if 'text' in part:
                                    assistant_message = part['text']
                                    st.markdown(assistant_message)
                                    break

                        # If no message from status, check artifacts
                        if not assistant_message:
                            artifacts = result.get('artifacts', [])
                            if artifacts:
                                for artifact in artifacts:
                                    parts = artifact.get('parts', [])
                                    for part in parts:
                                        if 'text' in part:
                                            assistant_message = part['text']
                                            st.markdown(assistant_message)
                                            break
                                    if assistant_message:
                                        break

                        # If still no message, check messages array
                        if not assistant_message:
                            messages = result.get('messages', [])
                            if messages:
                                last_message = messages[-1]
                                parts = last_message.get('parts', [])
                                for part in parts:
                                    if 'text' in part:
                                        assistant_message = part['text']
                                        st.markdown(assistant_message)
                                        break

                        # Fallback if no message found
                        if not assistant_message:
                            assistant_message = "Response received (no text content)"
                            st.info(assistant_message)

                        with st.expander("📊 Full Response"):
                            st.json(response_data)

                    except Exception as e:
                        st.error(f"Error parsing response: {e}")
                        assistant_message = "Error parsing response"
                else:
                    # Error was already handled above
                    if "assistant_message" not in locals():
                        assistant_message = "Error occurred"

    # Calculate and display duration
    duration = time.time() - start_time
    st.caption(f"⏱️ Response time: {duration:.2f}s")

    if "assistant_message" in locals():
        message_data = {
            "role": "assistant",
            "content": assistant_message,
            "duration": duration
        }
        if "response_data" in locals() and response_data:
            message_data["response_data"] = response_data
        st.session_state.messages.append(message_data)

if not st.session_state.client_initialized:
    st.info(
        "👈 Please configure and connect to your agent in the sidebar to start chatting.")
