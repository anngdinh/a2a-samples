import json
import os

from collections.abc import AsyncIterable
from typing import Any, Literal

import httpx

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel


memory = MemorySaver()


@tool
def get_exchange_rate(
    currency_from: str = 'USD',
    currency_to: str = 'EUR',
    currency_date: str = 'latest',
):
    """Use this to get current exchange rate.

    Args:
        currency_from: The currency to convert from (e.g., "USD").
        currency_to: The currency to convert to (e.g., "EUR").
        currency_date: The date for the exchange rate or "latest". Defaults to
            "latest".

    Returns:
        A dictionary containing the exchange rate data, or an error message if
        the request fails.
    """
    # # sleep for 2 seconds to simulate a real API call
    # import time
    # time.sleep(2)
    try:
        response = httpx.get(
            f'https://api.frankfurter.dev/v1/{currency_date}',
            params={'from': currency_from, 'to': currency_to},
            follow_redirects=True,
        )
        response.raise_for_status()

        data = response.json()
        if 'rates' not in data:
            return {'error': 'Invalid API response format.'}
        return data
    except httpx.HTTPError as e:
        return {'error': f'API request failed: {e}'}
    except ValueError:
        return {'error': 'Invalid JSON response from API.'}


class ResponseFormat(BaseModel):
    """Respond to the user in this format."""

    status: Literal['input_required', 'completed', 'error'] = 'input_required'
    message: str


class CurrencyAgent:
    """CurrencyAgent - a specialized assistant for currency convesions."""

    SYSTEM_INSTRUCTION = (
        'You are a specialized assistant for currency conversions. '
        "Your sole purpose is to use the 'get_exchange_rate' tool to answer questions about currency exchange rates. "
        'If the user asks about anything other than currency conversion or exchange rates, '
        'politely state that you cannot help with that topic and can only assist with currency-related queries. '
        'Do not attempt to answer unrelated questions or use tools for other purposes.'
    )

    FORMAT_INSTRUCTION = (
        'Set response status to input_required if the user needs to provide more information to complete the request.'
        'Set response status to error if there is an error while processing the request.'
        'Set response status to completed if the request is complete.'
    )

    def __init__(self):
        model_source = os.getenv('model_source', 'google')
        if model_source == 'google':
            self.model = ChatGoogleGenerativeAI(model='gemini-2.0-flash')
        else:
            self.model = ChatOpenAI(
                model=os.getenv('TOOL_LLM_NAME'),
                openai_api_key=os.getenv('API_KEY', 'EMPTY'),
                openai_api_base=os.getenv('TOOL_LLM_URL'),
                temperature=0,
            )
        self.tools = [get_exchange_rate]

        self.graph = create_react_agent(
            self.model,
            tools=self.tools,
            checkpointer=memory,
            prompt=self.SYSTEM_INSTRUCTION,
            response_format=(self.FORMAT_INSTRUCTION, ResponseFormat),
            interrupt_before=['tools'],
        )

    _APPROVE = {'yes', 'y', 'approve', 'approved', 'ok'}

    @staticmethod
    def _format_tool_calls(tool_calls: list[dict]) -> str:
        blocks = []
        for tc in tool_calls:
            args_json = json.dumps(tc['args'], indent=2)
            blocks.append(
                f'**Tool:** `{tc["name"]}`  \n'
                f'**ID:** `{tc["id"]}`  \n'
                f'**Args:**\n```json\n{args_json}\n```'
            )
        return '\n\n'.join(blocks)

    async def astream(self, query, context_id) -> AsyncIterable[dict[str, Any]]:
        config = {'configurable': {'thread_id': context_id}}

        # ── Detect resume from tool-approval interrupt ─────────────────────
        saved = self.graph.get_state(config)
        pending_tools = bool(saved.values) and 'tools' in (saved.next or [])

        if pending_tools:
            if query.strip().lower() in self._APPROVE:
                inputs = None  # resume → tools will execute
            else:
                # Rejection: inject ToolMessages and resume past the tools node
                last_ai = saved.values['messages'][-1]
                rejection = [
                    ToolMessage(
                        content='Tool execution rejected by user.',
                        tool_call_id=tc['id'],
                    )
                    for tc in last_ai.tool_calls
                ]
                await self.graph.aupdate_state(
                    config, {'messages': rejection}, as_node='tools'
                )
                inputs = None
        else:
            inputs = {'messages': [('user', query)]}

        # ── Stream graph ───────────────────────────────────────────────────
        async for item in self.graph.astream(inputs, config, stream_mode='values'):
            message = item['messages'][-1]
            if isinstance(message, AIMessage) and message.tool_calls:
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Preparing tool call…',
                }
            elif isinstance(message, ToolMessage):
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Processing exchange rates…',
                }

        # ── Check for tool-approval interrupt ──────────────────────────────
        final = self.graph.get_state(config)
        if 'tools' in (final.next or []):
            last_ai = final.values['messages'][-1]
            tool_calls = [
                {'id': tc['id'], 'name': tc['name'], 'args': tc['args']}
                for tc in last_ai.tool_calls
            ]
            content = (
                'Approve tool execution? Reply **yes** to proceed or **no** to cancel.\n\n'
                + self._format_tool_calls(tool_calls)
            )
            yield {
                'is_task_complete': False,
                'require_user_input': True,
                'content': content,
            }
        else:
            yield self.get_agent_response(config)

    def get_agent_response(self, config):
        current_state = self.graph.get_state(config)
        structured_response = current_state.values.get('structured_response')
        if structured_response and isinstance(
            structured_response, ResponseFormat
        ):
            if structured_response.status == 'input_required':
                return {
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.message,
                }
            if structured_response.status == 'error':
                return {
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.message,
                }
            if structured_response.status == 'completed':
                return {
                    'is_task_complete': True,
                    'require_user_input': False,
                    'content': structured_response.message,
                }

        return {
            'is_task_complete': False,
            'require_user_input': True,
            'content': (
                'We are unable to process your request at the moment. '
                'Please try again.'
            ),
        }

    SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']
