import logging
import os
import sys

import click
import httpx
import uvicorn
from starlette.middleware.cors import CORSMiddleware

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)
from dotenv import load_dotenv

from app.agent import CurrencyAgent
from app.agent_executor import CurrencyAgentExecutor


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""


@click.command()
@click.option('--host', 'host', default='localhost')
@click.option('--port', 'port', default=10000)
def main(host, port):
    """Starts the Currency Agent server."""
    try:
        if os.getenv('model_source', 'google') == 'google':
            if not os.getenv('GOOGLE_API_KEY'):
                raise MissingAPIKeyError(
                    'GOOGLE_API_KEY environment variable not set.'
                )
        else:
            if not os.getenv('TOOL_LLM_URL'):
                raise MissingAPIKeyError(
                    'TOOL_LLM_URL environment variable not set.'
                )
            if not os.getenv('TOOL_LLM_NAME'):
                raise MissingAPIKeyError(
                    'TOOL_LLM_NAME environment not variable not set.'
                )

        capabilities = AgentCapabilities(
            streaming=True, push_notifications=True)
        skill = AgentSkill(
            id='convert_currency',
            name='Currency Exchange Rates Tool',
            description='Helps with exchange values between various currencies',
            tags=['currency conversion', 'currency exchange'],
            examples=['What is exchange rate between USD and GBP?'],
        )
        
        # Get agent URL from environment or construct from host:port
        agent_url = os.getenv('AGENT_URL')
        if not agent_url:
            agent_url = f'http://{host}:{port}/'
            logger.info(f'AGENT_URL not set, using default: {agent_url}')
        else:
            logger.info(f'Using AGENT_URL from environment: {agent_url}')
        
        agent_card = AgentCard(
            name='Currency Agent',
            description='Helps with exchange rates for currencies',
            url=agent_url,
            version='1.0.0',
            default_input_modes=CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            default_output_modes=CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[skill],
        )

        # --8<-- [start:DefaultRequestHandler]
        httpx_client = httpx.AsyncClient()
        push_config_store = InMemoryPushNotificationConfigStore()
        push_sender = BasePushNotificationSender(httpx_client=httpx_client,
                                                 config_store=push_config_store)
        request_handler = DefaultRequestHandler(
            agent_executor=CurrencyAgentExecutor(),
            task_store=InMemoryTaskStore(),
            push_config_store=push_config_store,
            push_sender=push_sender
        )
        server = A2AStarletteApplication(
            agent_card=agent_card, http_handler=request_handler
        )

        # Build the application and add CORS middleware
        app = server.build()

        # Configure CORS - allow environment variable to set allowed origins
        cors_origins = os.getenv('CORS_ORIGINS', '*').split(',')
        if cors_origins == ['*']:
            logger.warning(
                'CORS is configured to allow all origins. Consider restricting in production.')

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

        uvicorn.run(app, host=host, port=port)
        # --8<-- [end:DefaultRequestHandler]

    except MissingAPIKeyError as e:
        logger.error(f'Error: {e}')
        sys.exit(1)
    except Exception as e:
        logger.error(f'An error occurred during server startup: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
