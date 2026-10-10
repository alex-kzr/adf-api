import unittest
import httpx
from adf_api.main import app

class DiagnosticContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_alive_but_not_ready(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            self.assertEqual((await client.get("/healthz")).status_code, 200)
            result = await client.get("/readyz")
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.json()["code"], "SCAFFOLD_NOT_IMPLEMENTED")
            self.assertEqual((await client.post("/api/runs", json={})).status_code, 404)
