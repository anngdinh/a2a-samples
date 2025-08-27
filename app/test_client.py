import logging

from typing import Any
from uuid import uuid4

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


async def main() -> None:
    # Configure logging to show INFO level messages
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)  # Get a logger instance

    # --8<-- [start:A2ACardResolver]

    base_url = 'http://localhost:10001'

    async with httpx.AsyncClient() as httpx_client:
        # Initialize A2ACardResolver
        resolver = A2ACardResolver(
            httpx_client=httpx_client,
            base_url=base_url,
            # agent_card_path uses default, extended_agent_card_path also uses default
        )
        # --8<-- [end:A2ACardResolver]

        # Fetch Public Agent Card and Initialize Client
        final_agent_card_to_use: AgentCard | None = None

        try:
            logger.info(
                f'Attempting to fetch public agent card from: {base_url}{AGENT_CARD_WELL_KNOWN_PATH}'
            )
            _public_card = (
                await resolver.get_agent_card()
            )  # Fetches from default public path
            logger.info('Successfully fetched public agent card:')
            logger.info(
                _public_card.model_dump_json(indent=2, exclude_none=True)
            )
            final_agent_card_to_use = _public_card
            logger.info(
                '\nUsing PUBLIC agent card for client initialization (default).'
            )

            if _public_card.supports_authenticated_extended_card:
                try:
                    logger.info(
                        '\nPublic card supports authenticated extended card. '
                        'Attempting to fetch from: '
                        f'{base_url}{EXTENDED_AGENT_CARD_PATH}'
                    )
                    auth_headers_dict = {
                        'Authorization': 'Bearer dummy-token-for-extended-card'
                    }
                    _extended_card = await resolver.get_agent_card(
                        relative_card_path=EXTENDED_AGENT_CARD_PATH,
                        http_kwargs={'headers': auth_headers_dict},
                    )
                    logger.info(
                        'Successfully fetched authenticated extended agent card:'
                    )
                    logger.info(
                        _extended_card.model_dump_json(
                            indent=2, exclude_none=True
                        )
                    )
                    final_agent_card_to_use = (
                        _extended_card  # Update to use the extended card
                    )
                    logger.info(
                        '\nUsing AUTHENTICATED EXTENDED agent card for client '
                        'initialization.'
                    )
                except Exception as e_extended:
                    logger.warning(
                        f'Failed to fetch extended agent card: {e_extended}. '
                        'Will proceed with public card.',
                        exc_info=True,
                    )
            elif (
                _public_card
            ):  # supports_authenticated_extended_card is False or None
                logger.info(
                    '\nPublic card does not indicate support for an extended card. Using public card.'
                )

        except Exception as e:
            logger.error(
                f'Critical error fetching public agent card: {e}', exc_info=True
            )
            raise RuntimeError(
                'Failed to fetch the public agent card. Cannot continue.'
            ) from e

        # Create client using new ClientFactory API
        config = ClientConfig(
            httpx_client=httpx_client,
            supported_transports=[TransportProtocol.jsonrpc]
        )
        factory = ClientFactory(config)
        client = factory.create(final_agent_card_to_use)
        logger.info('Client initialized using ClientFactory.')

        # --8<-- [start:send_message]
        message_obj = create_text_message_object(
            content='how much is 10 USD in INR?'
        )
        message_obj.message_id = uuid4().hex

        responses = []
        async for event in client.send_message(message_obj):
            responses.append(event)
        
        # Get the final result
        if responses:
            final_event = responses[-1]
            if isinstance(final_event, tuple):
                task = final_event[0]
                print(task.model_dump(mode='json', exclude_none=True))
            else:
                print(final_event.model_dump(mode='json', exclude_none=True))
        # --8<-- [end:send_message]

        # --8<-- [start:Multiturn]
        # First message: ambiguous question
        first_message = create_text_message_object(
            content='What is the exchange rate?'  # Ambiguous - missing currencies
        )
        first_message.message_id = uuid4().hex

        first_responses = []
        async for event in client.send_message(first_message):
            first_responses.append(event)
        
        if first_responses:
            first_task = first_responses[-1][0] if isinstance(first_responses[-1], tuple) else None
            if first_task:
                print(first_task.model_dump(mode='json', exclude_none=True))
                
                task_id = first_task.id
                context_id = first_task.context_id

                # Second message: provide clarification
                second_message = create_text_message_object(
                    content='USD to EUR'
                )
                second_message.message_id = uuid4().hex
                second_message.task_id = task_id
                second_message.context_id = context_id

                second_responses = []
                async for event in client.send_message(second_message):
                    second_responses.append(event)
                
                if second_responses:
                    second_task = second_responses[-1][0] if isinstance(second_responses[-1], tuple) else None
                    if second_task:
                        print(second_task.model_dump(mode='json', exclude_none=True))
        # --8<-- [end:Multiturn]

        # --8<-- [start:send_message_streaming]
        streaming_message = create_text_message_object(
            content='How much is 5 EUR in JPY?'
        )
        streaming_message.message_id = uuid4().hex

        async for event in client.send_message(streaming_message):
            if isinstance(event, tuple):
                task = event[0]
                print(task.model_dump(mode='json', exclude_none=True))
        # --8<-- [end:send_message_streaming]


if __name__ == '__main__':
    import asyncio

    asyncio.run(main())
