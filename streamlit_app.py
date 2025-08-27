import asyncio
import logging
from typing import Any, Dict, Optional
from uuid import uuid4

import streamlit as st
import httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendStreamingMessageRequest,
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
        value="http://localhost:10000",
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
            
            public_card = None
            try:
                public_card = await resolver.get_agent_card()
            except Exception as e:
                logger.warning(f"Failed to fetch from {AGENT_CARD_WELL_KNOWN_PATH}, trying /.well-known/agent.json")
                try:
                    public_card = await resolver.get_agent_card(
                        relative_card_path="/.well-known/agent.json"
                    )
                    logger.info("Successfully fetched agent card from /.well-known/agent.json")
                except Exception as fallback_e:
                    logger.error(f"Failed to fetch from both paths: {fallback_e}")
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
            client = A2AClient(
                httpx_client=httpx_client,
                agent_card=agent_card
            )

            send_message_payload: Dict[str, Any] = {
                'message': {
                    'role': 'user',
                    'parts': [{'kind': 'text', 'text': message}],
                    'message_id': uuid4().hex,
                },
            }

            if context_id:
                send_message_payload['message']['context_id'] = context_id
            if task_id:
                send_message_payload['message']['task_id'] = task_id

            if auth_token:
                httpx_client.headers["Authorization"] = f"Bearer {auth_token}"

            if streaming:
                request = SendStreamingMessageRequest(
                    id=str(uuid4()),
                    params=MessageSendParams(**send_message_payload)
                )

                response_parts = []
                collected_text = []
                final_response = None

                async for chunk in client.send_message_streaming(request):
                    response_parts.append(chunk)
                    chunk_data = chunk.model_dump(
                        mode='json', exclude_none=True)

                    # Extract text from streaming chunks
                    if 'result' in chunk_data:
                        result = chunk_data['result']

                        # Check for text in status message (working state)
                        status = result.get('status', {})
                        if 'message' in status:
                            status_message = status['message']
                            parts = status_message.get('parts', [])
                            for part in parts:
                                if 'text' in part:
                                    collected_text.append(part['text'])

                        # Keep the last chunk as final response for metadata
                        final_response = chunk_data

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
                request = SendMessageRequest(
                    id=str(uuid4()),
                    params=MessageSendParams(**send_message_payload)
                )

                response = await client.send_message(request)
                return response.model_dump(mode='json', exclude_none=True)

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
                        client = A2AClient(
                            httpx_client=httpx_client,
                            agent_card=st.session_state.agent_card
                        )

                        send_message_payload = {
                            'message': {
                                'role': 'user',
                                'parts': [{'kind': 'text', 'text': prompt}],
                                'message_id': uuid4().hex,
                            },
                        }

                        if st.session_state.context_id:
                            send_message_payload['message']['context_id'] = st.session_state.context_id
                        if st.session_state.task_id:
                            send_message_payload['message']['task_id'] = st.session_state.task_id

                        if auth_token:
                            httpx_client.headers["Authorization"] = f"Bearer {auth_token}"

                        request = SendStreamingMessageRequest(
                            id=str(uuid4()),
                            params=MessageSendParams(**send_message_payload)
                        )

                        final_result = None
                        async for chunk in client.send_message_streaming(request):
                            chunk_data = chunk.model_dump(
                                mode='json', exclude_none=True)

                            if 'result' in chunk_data:
                                result = chunk_data['result']
                                final_result = result

                                # Update context and task IDs (handle both camelCase and snake_case)
                                if 'contextId' in result:
                                    st.session_state.context_id = result['contextId']
                                elif 'context_id' in result:
                                    st.session_state.context_id = result['context_id']

                                if 'taskId' in result:
                                    st.session_state.task_id = result['taskId']
                                elif 'id' in result:
                                    st.session_state.task_id = result['id']

                                # Check the kind of update
                                kind = result.get('kind', '')

                                # Handle artifact-update events (contains the final answer)
                                if kind == 'artifact-update':
                                    artifact = result.get('artifact', {})
                                    parts = artifact.get('parts', [])
                                    for part in parts:
                                        if part.get('kind') == 'text' or 'text' in part:
                                            text = part.get('text', '')
                                            if text:
                                                # Append final answer to existing messages
                                                collected_text_ref.append(text)
                                                # Display all messages with each on a new line
                                                message_placeholder.markdown(
                                                    '\n\n'.join(collected_text_ref))

                                # Handle status-update events
                                elif kind == 'status-update':
                                    status = result.get('status', {})
                                    state = status.get('state', '')

                                    if state == 'working' and 'message' in status:
                                        # Extract text from working state messages
                                        status_message = status['message']
                                        parts = status_message.get('parts', [])
                                        for part in parts:
                                            if part.get('kind') == 'text' or 'text' in part:
                                                text = part.get('text', '')
                                                if text:
                                                    collected_text_ref.append(
                                                        text)
                                                    # Update display with all collected text
                                                    message_placeholder.markdown(
                                                        '\n\n'.join(collected_text_ref))

                                    elif state == 'input-required' and 'message' in status:
                                        # Handle input required state
                                        status_message = status['message']
                                        parts = status_message.get('parts', [])
                                        for part in parts:
                                            if part.get('kind') == 'text' or 'text' in part:
                                                text = part.get('text', '')
                                                if text and text not in collected_text_ref:
                                                    collected_text_ref.append(
                                                        text)
                                                    # Update display
                                                    message_placeholder.markdown(
                                                        '\n\n'.join(collected_text_ref))

                                    elif state == 'completed':
                                        # Task is completed, final answer should have been in artifact-update
                                        # If no artifact was sent, keep the working messages
                                        pass

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

    if "assistant_message" in locals():
        st.session_state.messages.append(
            {"role": "assistant", "content": assistant_message})

if not st.session_state.client_initialized:
    st.info(
        "👈 Please configure and connect to your agent in the sidebar to start chatting.")
