import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from app.api.routes import plan as routes


class CityGuideStreamTests(unittest.IsolatedAsyncioTestCase):
    def process(self):
        proc = MagicMock()
        proc.stdout.readline.side_effect = [
            b'{"type":"guide","text":"city introduction"}\n',
            b'{"type":"final","data":{}}\n', b'']
        proc.stderr.readline.return_value = b''
        return proc

    async def test_guide_uses_search_agent_and_only_destination(self):
        proc = self.process()
        response = routes.city_guide_stream(routes.PlanRequest(destination='杭州', basic={'budget': 1000}))
        with patch.object(routes.subprocess, 'Popen', return_value=proc) as launch, \
                patch.object(routes, '_stop_process_group', new=AsyncMock()):
            events = [event async for event in response.body_iterator]
        self.assertEqual(launch.call_args.args[0], [str(routes.SEARCH_PYTHON), str(routes.SEARCH_PY), '--guide-stream'])
        self.assertEqual(json.loads(proc.stdin.write.call_args.args[0]), {'destination': '杭州'})
        self.assertIn('city introduction', events[0])
        self.assertIn('"final"', events[1])
        self.assertEqual(events[-1], 'data: [DONE]\n\n')

    async def test_cancellation_cleans_up_search_process(self):
        proc = self.process()
        response = routes.city_guide_stream(routes.PlanRequest(destination='杭州'))
        with patch.object(routes.subprocess, 'Popen', return_value=proc), \
                patch.object(routes, '_stop_process_group', new=AsyncMock()) as cleanup:
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
        cleanup.assert_any_await(proc)

    def test_empty_destination_rejected(self):
        with self.assertRaises(HTTPException) as error:
            routes.city_guide_stream(routes.PlanRequest(destination='  '))
        self.assertEqual(error.exception.status_code, 422)
