import sys
from pathlib import Path
import unittest

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def discover_tools() -> list[str]:
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    parameters = StdioServerParameters(command=sys.executable, args=[str(server_path)])
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.name for tool in result.tools]


class McpIntegrationTests(unittest.TestCase):
    def test_server_exposes_screening_and_trial_detail_tools(self):
        self.assertEqual(
            anyio.run(discover_tools),
            [
                "get_cde_trial_local",
                "search_cde_trials",
                "get_cde_trial_questionnaire_source",
                "verify_cde_trial_live",
            ],
        )


if __name__ == "__main__":
    unittest.main()
