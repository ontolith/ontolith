"""Identity and authorization layer.

This module contains:
- Principal model (humans, AI agents, services)
- AuthProvider port (authentication abstraction)
- Capability management
- Delegation support

`TokenAuthProvider`/`hash_token` (`identity/token_auth.py`, the concrete
`AuthProvider` implementation, ADR-0014 — the one every `create_rest_app`/
`create_graphql_app`/`create_mcp_server` docstring example actually uses)
are exported below, but the import order matters: `token_auth.py` imports
`StorageBackend` from `store.base`, and `store.base` imports `Principal`
from this package — reaching back into `ontolith.identity` while it's
still initializing. That reverse import only succeeds if `Principal` has
already been bound in this module's namespace by the time it's reached,
which requires the `principal` import below to run *before* the
`token_auth` one — reproduced directly: swapping the two order raises
`ImportError: cannot import name 'Principal' from partially initialized
module`. This isn't a silent footgun: the required order (`token_auth`
alphabetically last) is exactly what `ruff`'s own isort rule (`I001`,
already CI-blocking) enforces, so any reordering that broke this would
also fail `ruff check` before merge. Round-1 review of this ADR-0019
Update found and corrected an earlier, inaccurate version of this
docstring claiming the import was circular *unconditionally* — verified
false; it depends on order, and the enforced order happens to be the
working one.
"""

from ontolith.identity.admin_event import AdminAction, AdminEvent
from ontolith.identity.credential import PrincipalCredential
from ontolith.identity.erasure import ErasureMode, ErasureRecord
from ontolith.identity.ports import AuthProvider
from ontolith.identity.principal import Principal, min_capability
from ontolith.identity.token_auth import TokenAuthProvider, hash_token

__all__ = [
    "AdminAction",
    "AdminEvent",
    "AuthProvider",
    "ErasureMode",
    "ErasureRecord",
    "Principal",
    "PrincipalCredential",
    "TokenAuthProvider",
    "hash_token",
    "min_capability",
]
