# Agent Interoperability
<!-- Please include descriptive title -->

<!--- Register repository https://api.reuse.software/register, then add REUSE badge:
[![REUSE status](https://api.reuse.software/badge/github.com/SAP-samples/REPO-NAME)](https://api.reuse.software/info/github.com/SAP-samples/REPO-NAME)
-->

## Description
<!-- Please include SEO-friendly description -->


[![REUSE status](https://api.reuse.software/badge/github.com/SAP-samples/agent-interoperability)](https://api.reuse.software/info/github.com/SAP-samples/agent-interoperability)

Hands-on recipes for connecting AI agents from third-party platforms — Microsoft, Google, and Amazon — to agents running on the **SAP Business AI Platform** through the **SAP Agent Gateway**, with real end-to-end user identity.

## Description

Enterprises run SAP alongside agents built on Microsoft, Google, and AWS. For those agents to collaborate on a business process, they need more than a shared protocol — they need identity federation, trust, and a controlled path into the SAP landscape.

These cookbooks are the **how-to** companion to the SAP Architecture Center reference architectures for agent interoperability. The Architecture Center explains the **what and why** of each integration pattern; the cookbooks here give you the **concrete, step-by-step setup** — including runnable code samples — to reproduce it.

- **Agent-to-Agent (A2A)** is the protocol agents use to delegate tasks to one another.
- **SAP Agent Gateway** is the single, secured inbound hub through which external agents reach Joule Agents, Assistants, and Custom Agents on the SAP Business AI Platform.
- **SAP Cloud Identity Services (IAS)** brokers identity so every delegated task runs under the signed-in business user, respecting the SAP authorizations already in place.

> Reference architectures on the SAP Architecture Center:
> [Integrating Joule Agents into Your Ecosystem](https://architecture.learning.sap.com/docs/ref-arch/ca1d2a3e/5) ·
> [A2A and MCP for Interoperability](https://architecture.learning.sap.com/docs/ref-arch/ca1d2a3e/1)

## Cookbooks

| Platform | Integration pattern | Cookbook |
|---|---|---|
| **Microsoft Copilot Studio** | Per-user token exchange via a Power Platform custom connector (Entra or direct IAS OAuth) | [→](./microsoft-azure-integration/) |
| **Google Gemini Enterprise** | Three-legged OAuth with a proxy agent on Google Agent Runtime | [→](./gcp-integration/) |
| **Amazon (AWS AgentCore)** | On-behalf-of token exchange via AWS AgentCore Identity | [→](./aws-integration/) |

## Before you start

All three cookbooks share the same one-time SAP-side onboarding — creating an Agent Gateway consumer system in SAP for Me and configuring authentication. Do this once and reuse it across platforms:

**→ [Common setup](./common-setup/)**

## Requirements

- An **SAP BTP** subaccount with **SAP Business AI Platform**, **SAP Cloud Identity Services**, and access to **SAP for Me** (cloud administrator role).
- An account on the third-party platform you want to connect (Microsoft Copilot Studio, Google Gemini Enterprise, or AWS).
- Platform-specific prerequisites are listed at the top of each cookbook.

## Download and Installation

Clone or browse this repository and open the cookbook for the platform you want to connect. Each platform is a self-contained folder with the same layout, so you can go straight to the one you need:

```
<platform>/
├── README.md     the recipe — read this first
├── images/       architecture diagrams
└── code/         runnable code samples, with their own README
```

Start with the shared, platform-independent [common setup](./common-setup/), then follow the recipe in your platform folder. The code samples under each `code/` folder include their own README with setup and run instructions.

## Known Issues

No known issues.

## How to obtain support

[Create an issue](https://github.com/SAP-samples/agent-interoperability/issues) in this repository if you find a bug or have a question about the content.

For additional support, [ask a question in SAP Community](https://answers.sap.com/questions/ask.html).

## Contributing

If you wish to contribute code, offer fixes or improvements, please send a pull request. Due to legal reasons, contributors will be asked to accept a DCO when they create the first pull request to this project. This happens in an automated fashion during the submission process. SAP uses [the standard DCO text of the Linux Foundation](https://developercertificate.org/). See [CONTRIBUTING](CONTRIBUTING.md) for details.

## Code of Conduct

Please read the [SAP Open Source Code of Conduct](https://github.com/SAP-samples/.github/blob/main/CODE_OF_CONDUCT.md).

## License

Copyright 2026 SAP SE or an SAP affiliate company and agent-interoperability contributors. Please see our [LICENSE](LICENSE) for copyright and license information. Detailed information including third-party components and their licensing/copyright information is available [via the REUSE tool](https://api.reuse.software/info/github.com/SAP-samples/agent-interoperability).
