# Commercial License

Copyright (C) 2026 Sparkle SpA. All rights reserved.

## Dual-licensing model

The STLS AI core packages in this repository are **dual-licensed**:

1. **GNU Affero General Public License v3.0 (AGPLv3)** — see [`LICENSE`](LICENSE).
   Free and open source. You may use, study, modify, and redistribute the
   code under its terms. The AGPLv3's defining obligation is in its
   §13: if you run a modified version of the software to provide a
   service to users over a network (including as a SaaS backend, hosted
   API, or embedded agent infrastructure), you must make the complete
   corresponding source of your modified version available to those
   users. Any work that statically or dynamically links against
   AGPLv3-licensed code, or is otherwise a "modified version" under
   the license, inherits that same obligation.

2. **Commercial License** (this document). An alternative to the AGPLv3
   for organizations that need to:
   - embed STLS packages in a **closed-source or proprietary product**
     without triggering the AGPLv3's source-disclosure obligations;
   - distribute STLS as part of a commercial SaaS offering without
     releasing their own modifications or integration code;
   - obtain **warranty, indemnification, or support SLA terms** beyond
     the "AS IS" disclaimer in AGPLv3 §15–17;
   - get a license compatible with their own closed-source legal/compliance
     requirements, where AGPL-licensed dependencies are not permitted.

You need exactly one of the two. If the AGPLv3 already fits how you use
the software, you do not need to contact us or pay anything — the open
source license is a complete, standalone grant.

## Scope

This dual-licensing model applies to the following packages in this
repository, each of which carries its own copy of `LICENSE` and
`LICENSE-COMMERCIAL.md`:

| Package | Path |
|---|---|
| stls-core (Python) | [`python/stls-core`](python/stls-core) |
| stls-identity (Python) | [`python/stls-identity`](python/stls-identity) |
| stls-a2a (Python) | [`python/stls-a2a`](python/stls-a2a) |
| stls-mcp (Python) | [`python/stls-mcp`](python/stls-mcp) |
| stls-core (Node.js) | [`node/stls-core`](node/stls-core) |
| stls-mcp (Node.js) | [`node/stls-mcp`](node/stls-mcp) |

Code under [`samples/`](samples) (example clients, servers, and deployment
scaffolding demonstrating integration with the packages above and with
third-party frameworks) is **not** covered by this dual-licensing grant.
It carries no license from this project beyond what is needed to read
and adapt it for your own use of the packages listed above; ask before
redistributing it.

## This is a notice, not a contract

This document describes the licensing model; it is not itself a signed
commercial license agreement. Actual commercial terms (scope, pricing,
support, indemnification, duration) are set out in a separate agreement
negotiated directly with Sparkle SpA.

## Getting a commercial license

Contact: **ettore.pulieri@tisparkle.com**

Please include which package(s) you intend to use, your intended
deployment model (embedded, SaaS, on-prem, redistributed), and expected
scale, so we can propose the right terms.
