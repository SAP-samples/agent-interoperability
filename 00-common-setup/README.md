# Common Setup - Onboarding a Third-Party Agent to SAP

Every cookbook in this repository shares the same one-time SAP-side onboarding. Complete it once, then follow your [platform-specific recipe](../README.md#cookbooks)

Integrating a third-party agent with SAP agents through the Agent Gateway involves two stages:

1. **Setup in SAP for Me** - register the external agent and wire it into the required formations.
2. **Authentication configuration** - choose how the external agent proves its identity at runtime.

## Stage 1 - Setup in SAP for Me

Before a third-party agent can invoke an SAP agent, create an **Agent Gateway consumer system** in SAP for Me to represent the external agent, then assign it to **three formations in this exact order**:

| Order | Formation | Purpose |
|---|---|---|
| 1 | **Integration with SAP Cloud Identity Services** | Creates a single-tenant OpenID Connect (OIDC) application representing the third-party agent's identity in SAP Cloud Identity Services. |
| 2 | **Integration with Agent Gateway** | Establishes the routing path from the agent's identity to the Agent Gateway. |
| 3 | **Integration with Agent Gateway Consumer** | Defines the explicit allow-list of SAP agents the third-party agent is permitted to invoke. |

> **Order matters.** Each formation produces configuration the next one depends on. Formation 1 must reach `READY` before Formation 2 is created, and Formation 2 before Formation 3. Creating them out of sequence leaves them in `CONFIG_PENDING` or `ERROR` for up to sixty minutes before they can be retried.

### Prerequisites

- The **cloud administrator** role in SAP for Me.
- The **administrator** role on the SAP Cloud Identity Services tenant.

### Information to prepare

- **List of SAP agents** the third-party agent needs to invoke (used to build the allow-list in Formation 3).
- **System name** for the third-party agent - identifies it across all formations.
- **Resource URL** of the third-party agent runtime.
- **JWKS endpoint URL** *(optional)* - only if the agent has its own Identity Provider and you use JWKS-based authentication (see Stage 2). Not required for client-credentials authentication.

### Create the consumer system

In SAP for Me, under **Portfolio & Products → My Product Packages → Customer Landscape → Systems**, create a new system of type **Agent Gateway Consumer** with:

| Field | Example value |
|---|---|
| System Type | `Agent Gateway Consumer` |
| Resource URL | `https://<your-agent-app-url>` |
| JWKS Endpoint URL | `https://<your-signing-application>/.well-known/jwks.json` *(leave blank for client-credentials auth)* |
| Name | a descriptive name for your agent |
| Application Identifier | e.g. `agent-fin-1` |

Then assign the system to the three formations in the order above.

## Stage 2 - Authentication configuration

After the SAP for Me setup, configure **one** of two authentication methods, depending on whether the third-party agent has its own Identity Provider (IdP):

### Option A - JWKS / IdP Trust *(recommended)*

Use this if the agent has its own IdP and a JWKS endpoint URL. Trust is established between the external IdP and SAP Cloud Identity Services via the JWKS URL. At runtime, SAP Cloud Identity Services validates incoming tokens against the registered JWKS URL and, on success, issues a token forwarded to the Agent Gateway. **No client secret is generated or stored.**

To configure, open the OIDC application created during onboarding in the SAP Cloud Identity Services Administration Console and, under **Trust → Trust Corporate Identity Providers**, add your external IdP.

### Option B - Client Credentials

Use this if the agent has **no** IdP or JWKS URL. SAP Cloud Identity Services generates a client ID and client secret for the third-party application; those credentials are used at runtime to obtain tokens.

To configure, open the same OIDC application, go to **Trust → Application APIs → Client Authentication → Secrets → Add**, and generate a client secret.

> **The client secret is shown only once.** Copy and store it securely before closing the dialog - it is required for every runtime token request, and a lost secret must be regenerated.

## Reference - SAP Help Portal

- [Creating an Agent Gateway Consumer System](https://help.sap.com/docs/CROSS_DOMAIN_AGENTS/4d8be6737cf24797bf753cbcc6e2288f/a0465991ba964736a781c275ddd852db.html)
- [Invoking an SAP Agent Through IdP Trust and JWKS-Based Integration](https://help.sap.com/docs/CROSS_DOMAIN_AGENTS/4d8be6737cf24797bf753cbcc6e2288f/e9da5158a18649168e040495b26c43bc.html)

---

*Next: pick your [platform-specific recipe](../README.md#cookbooks)*
