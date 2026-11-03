"""A mock payment processor REST API, served from the Phase 1 settlement data.

This is the *upstream* that does not exist in real life: `reconciliation-agent-plan.md` §4.4
Session 2.2 builds `mcp-processor` as a client, and a client is only worth writing if the thing
it talks to has the failure modes a real processor forces on you. So this app has three:

1. opaque cursor pagination (the cursor is signed, so offsets cannot be guessed or forged);
2. bearer-token auth (missing, malformed and wrong token are distinct 401s);
3. a token-bucket rate limiter that answers 429 with an honest `Retry-After`.

The records themselves are served as the generator wrote them, byte for byte (see `store`).
"""

from crossfoot.mock_processor.app import create_app
from crossfoot.mock_processor.settings import ApiSettings

__all__ = ["ApiSettings", "create_app"]
