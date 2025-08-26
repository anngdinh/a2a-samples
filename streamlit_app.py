import asyncio
import logging
from typing import Any, Dict, Optional
from uuid import uuid4

import streamlit as st
import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory, create_text_message_object
from a2a.types import (
    AgentCard,
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
        value="http://localhost:10001",
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
        value=False,
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
        st.code(f"Context ID: {st.session_state.context_id[:8]}...")
        if st.session_state.task_id:
            st.code(f"Task ID: {st.session_state.task_id[:8]}...")
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
            public_card = await resolver.get_agent_card()

            if public_card.supports_authenticated_extended_card and auth_token:
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
            if auth_token:
                httpx_client.headers["Authorization"] = f"Bearer {auth_token}"
                
            config = ClientConfig(
                httpx_client=httpx_client,
                supported_transports=[
                    TransportProtocol.jsonrpc,
                    TransportProtocol.http_json,
                ],
            )
            client_factory = ClientFactory(config)
            client = client_factory.create(agent_card)

            # Create message object using the new API
            message_obj = create_text_message_object(content=message)
            
            # Add context_id and task_id if provided
            if context_id:
                message_obj.context_id = context_id
            if task_id:
                message_obj.task_id = task_id

            # Send message and collect responses
            responses = []
            collected_text = []
            final_task = None
            
            async for event in client.send_message(message_obj):
                responses.append(event)
                
                # Handle different event types
                if hasattr(event, '__iter__') and not isinstance(event, str):
                    # It's a tuple (task, event_type)
                    task, event_type = event
                    final_task = task
                    
                    # Extract text from artifacts if available
                    if hasattr(task, 'artifacts') and task.artifacts:
                        for artifact in task.artifacts:
                            if hasattr(artifact, 'parts') and artifact.parts:
                                for part in artifact.parts:
                                    if hasattr(part, 'root') and hasattr(part.root, 'text'):
                                        collected_text.append(part.root.text)
                else:
                    # Handle other response types (like Message objects)
                    final_task = event
                    if hasattr(event, 'parts'):
                        for part in event.parts:
                            if hasattr(part, 'text'):
                                collected_text.append(part.text)

            # Format response similar to old API
            if final_task:
                result = {
                    'result': {
                        'id': getattr(final_task, 'id', None),
                        'context_id': getattr(final_task, 'context_id', context_id),
                        'status': {
                            'state': getattr(getattr(final_task, 'status', None), 'state', 'completed')
                        }
                    }
                }
                
                # Add artifacts with collected text
                if collected_text:
                    result['result']['artifacts'] = [{
                        'parts': [{'text': '\n'.join(collected_text)}]
                    }]
                
                return result
            
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


if prompt := st.chat_input("Type your message here...", disabled=not st.session_state.client_initialized):
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if streaming_mode:
            # Streaming mode - display responses in real-time
            message_placeholder = st.empty()
            collected_text = []

            async def stream_response(collected_text_ref):
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as httpx_client:
                        if auth_token:
                            httpx_client.headers["Authorization"] = f"Bearer {auth_token}"
                            
                        config = ClientConfig(
                            httpx_client=httpx_client,
                            supported_transports=[
                                TransportProtocol.jsonrpc,
                                TransportProtocol.http_json,
                            ],
                        )
                        client_factory = ClientFactory(config)
                        client = client_factory.create(st.session_state.agent_card)

                        # Create message object using the new API
                        message_obj = create_text_message_object(content=prompt)
                        
                        # Add context_id and task_id if provided
                        if st.session_state.context_id:
                            message_obj.context_id = st.session_state.context_id
                        if st.session_state.task_id:
                            message_obj.task_id = st.session_state.task_id

                        final_result = None
                        current_content = ""
                        
                        async for event in client.send_message(message_obj):
                            # Handle tuple events (task, event_object)
                            if isinstance(event, tuple) and len(event) == 2:
                                task, event_obj = event
                                final_result = task
                                
                                # Update session state with task info
                                if hasattr(task, 'context_id') and task.context_id:
                                    st.session_state.context_id = task.context_id
                                if hasattr(task, 'id') and task.id:
                                    st.session_state.task_id = task.id
                                
                                # Check event kind to handle different types
                                if hasattr(event_obj, 'kind'):
                                    
                                    # Handle status updates (working progress)
                                    if event_obj.kind == 'status-update' and hasattr(event_obj, 'status'):
                                        status = event_obj.status
                                        if (hasattr(status, 'state') and status.state.value == 'working' and 
                                            hasattr(status, 'message') and status.message):
                                            # Extract status message text
                                            message = status.message
                                            if hasattr(message, 'parts') and message.parts:
                                                for part in message.parts:
                                                    if hasattr(part, 'root') and hasattr(part.root, 'text'):
                                                        status_text = part.root.text
                                                        # Show status as italic
                                                        collected_text_ref.append(f"*{status_text}*")
                                                        message_placeholder.markdown('\n\n'.join(collected_text_ref))
                                    
                                    # Handle artifact updates (final/intermediate results)
                                    elif event_obj.kind == 'artifact-update' and hasattr(event_obj, 'artifact'):
                                        artifact = event_obj.artifact
                                        if hasattr(artifact, 'parts') and artifact.parts:
                                            for part in artifact.parts:
                                                if hasattr(part, 'root') and hasattr(part.root, 'text'):
                                                    result_text = part.root.text
                                                    # Add final result
                                                    collected_text_ref.append(result_text)
                                                    message_placeholder.markdown('\n\n'.join(collected_text_ref))
                                
                            else:
                                # Handle Message objects directly
                                final_result = event
                                if hasattr(event, 'parts') and event.parts:
                                    for part in event.parts:
                                        if hasattr(part, 'root') and hasattr(part.root, 'text'):
                                            collected_text_ref.clear()
                                            collected_text_ref.append(part.root.text)
                                            message_placeholder.markdown(part.root.text)
                                        elif hasattr(part, 'text'):
                                            collected_text_ref.clear()
                                            collected_text_ref.append(part.text)
                                            message_placeholder.markdown(part.text)

                        return final_result

                except Exception as e:
                    if 'terminal state' in str(e) or 'completed' in str(e):
                        # Reset task_id and retry
                        st.session_state.task_id = None
                        collected_text_ref.clear()
                        return await stream_response(collected_text_ref)
                    else:
                        st.error(f"Error: {e}")
                        return None

            # Run streaming
            final_result = asyncio.run(stream_response(collected_text))

            # Save the final message
            assistant_message = '\n\n'.join(
                collected_text) if collected_text else "Response received (no text content)"

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
                            st.write(f"Debug - Found input-required state")
                            st.write(f"Debug - Status: {status}")
                            status_message = status['message']
                            parts = status_message.get('parts', [])
                            st.write(f"Debug - Parts: {parts}")
                            for part in parts:
                                st.write(f"Debug - Part: {part}")
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

    if "assistant_message" in locals():
        st.session_state.messages.append(
            {"role": "assistant", "content": assistant_message})

if not st.session_state.client_initialized:
    st.info(
        "👈 Please configure and connect to your agent in the sidebar to start chatting.")
