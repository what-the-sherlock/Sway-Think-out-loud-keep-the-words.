# Platform Architecture Diagram Prompt Pack — Eraser

Source: pasted prompt pack, saved verbatim below for reuse.
Tool: Eraser → *Diagram as Code* → *Cloud Architecture* (or the matching category per diagram).

---

## Applicability to Sway (this repo)

Sway is a **local, single-user desktop app**: no server, no cloud, no accounts, no queues.
Several diagrams in the generic pack describe infrastructure that does not exist here. Use the
table before generating anything, and mark absent layers `N/A` rather than inventing them.

| # | Diagram | Applies? | Notes for Sway |
|---|---------|----------|----------------|
| 1 | Executive-Level Platform Overview | **Yes** | Layers collapse to: user → hotkey/overlay → engine → Groq API → SQLite. No gateway, no cloud. |
| 2 | User Interaction / End-to-End Flow | **Yes — most valuable** | Hotkey press → ring buffer → VAD trim → FLAC → Whisper → cleanup LLM → clipboard paste. |
| 3 | Detailed Internal Architecture | **Yes** | `src/sway/`: hotkey, audio, pipeline, groq_client, net_loop, inject, latency, dashboard. |
| 4 | Technology Stack | **Yes (trimmed)** | Python 3.11, pynput, sounddevice, webrtcvad, httpx/http2, SQLite, Tkinter overlay, local dashboard UI. No CDN/K8s/cloud rows. |
| 5 | Data Architecture | **Yes** | Ring buffer (transient) → encoded audio (transient) → SQLite (history, latency, dictionary, snippets). |
| 6 | Authentication & Authorization | **Partial** | No user auth. Only the Groq API key from `.env` / credential store. Draw as a secrets flow, not an IdP flow. |
| 7 | External Integrations | **Yes (small)** | One external dependency: Groq API (`whisper-large-v3-turbo`, `llama-3.1-8b-instant`). |
| 8 | Event-Driven / Async Architecture | **Partial** | No broker. Real async: audio callback thread, asyncio net-loop thread, streaming LLM response. |
| 9 | AI / ML Architecture | **Yes** | Whisper prompt biasing from the personal dictionary, cleanup system prompt, skip heuristic, streaming. |
| 10 | Deployment / Infrastructure | **No** | Replace with a packaging diagram: PyInstaller → local install → tray/autostart. |
| 11 | CI/CD & Developer Workflow | **No** | No pipeline in the repo. Omit unless one is added. |
| 12 | Observability | **Partial** | `latency/` stage tracker → SQLite + console table; local logs. No APM or alerting. |
| 13 | Security Architecture | **Partial** | Real controls: local-only binding, API key at rest, clipboard save/restore. No WAF/RBAC. |
| 14 | Primary User Journey (sequence) | **Yes** | Same path as #2, drawn as lifelines. |
| 15 | Failure & Recovery | **Yes** | Timeout, one retry with jitter, 429 header surfacing, overlay failure state, never hang mic-hot. |
| 16 | Complete C4 | **Yes (L1–L3, small)** | One container; components are the modules in `src/sway/`. |
| 17 | Architecture + Technology Mapping | **Yes** | Same as #1 with technology labels on each node. |
| 18 | One-Page Master | **Yes** | Drop the Edge/API, Cloud Infrastructure, and Authentication sections. |

**Rule for every prompt below:** do not let Eraser guess. Replace generic node names with the real
module (`src/sway/pipeline/…`, `src/sway/groq_client.py`) and mark anything unverified `Unknown / TBD`.

---

## Purpose

Use the prompts below in Eraser to create a complete set of architecture diagrams for the platform.

The goal is to understand the platform at multiple levels:

1. Overall platform architecture
2. User-to-platform interaction
3. End-to-end request/data flow
4. Internal service architecture
5. Technology stack
6. Data architecture
7. Authentication and authorization
8. External integrations
9. AI/ML components, if applicable
10. Deployment and infrastructure
11. Observability and monitoring
12. Security boundaries
13. Key user journeys
14. Failure and recovery flows
15. High-level system context

Each diagram should be understandable independently, while together they should provide a complete technical picture of how the platform works.

---

# Diagram 1 — Executive-Level Platform Overview

## Eraser Prompt

Create a high-level architecture diagram showing the complete platform from the user's perspective.

Show the following layers from left to right:

1. Users / User Devices
2. Client Applications / Frontend
3. API / Gateway Layer
4. Core Backend Services
5. Data Layer
6. External Services / Third-Party Integrations
7. Infrastructure / Cloud Environment

Show the major interactions between these layers using directional arrows.

Include different types of users if applicable, such as:

* End User
* Admin
* Internal Operator
* External System

For the frontend, show the major applications or interfaces.

For the backend, show the major logical services/modules without going into implementation-level detail.

For the data layer, show the major databases, caches, object storage, queues, and search systems if they exist.

For external integrations, show third-party APIs, payment providers, communication providers, identity providers, analytics services, AI providers, or other external dependencies.

Use clear grouping and boundaries to distinguish:

* Client side
* Platform boundary
* Internal services
* Data systems
* External systems

Keep this diagram at a Level 1 / C4 Container-style abstraction. Do not show individual classes, functions, database columns, or low-level implementation details.

Add a legend explaining:

* Synchronous communication
* Asynchronous communication
* External dependency
* Data store
* User interaction

The final diagram should communicate how the entire platform fits together within 30–60 seconds of viewing.

---

# Diagram 2 — User Interaction and End-to-End Flow

## Eraser Prompt

Create a detailed sequence/data-flow architecture diagram showing how a user interacts with the platform and how the platform produces an output.

Start with:

User → Client / Frontend → API / Gateway → Backend Services → Data / External Services → Backend → API → Frontend → User

Show the complete lifecycle of a typical user request.

Include:

1. User action
2. Frontend request
3. Authentication/session validation
4. API request
5. Request routing
6. Business logic processing
7. Database reads/writes
8. Cache access where applicable
9. External API calls where applicable
10. Background jobs or asynchronous processing where applicable
11. Response generation
12. Frontend state update
13. Final output shown to the user

Clearly distinguish synchronous requests from asynchronous processing.

Show what happens when the request:

* succeeds
* fails validation
* encounters an internal error
* depends on an unavailable external service

Use numbered steps on the main successful path.

The purpose is to make it possible for a new engineer to trace a request from the moment the user clicks something until the final result appears on screen.

---

# Diagram 3 — Detailed Internal Platform Architecture

## Eraser Prompt

Create a detailed internal architecture diagram focused exclusively on the platform's backend and internal components.

Do not focus on the user interface.

Organize the diagram into these logical layers:

1. API / Edge Layer
2. Authentication / Authorization
3. Application Services
4. Domain / Business Logic
5. Background Workers
6. Messaging / Event Layer
7. Data Access Layer
8. Databases and Storage
9. External Integrations

Within the application layer, show each major backend service/module used by the platform.

For every service, show:

* Primary responsibility
* Main inputs
* Main outputs
* Dependencies
* Data stores accessed

Show communication between services.

Use different arrow styles for:

* REST/HTTP
* GraphQL
* gRPC
* Message queue
* Event/pub-sub
* Database access

Only use technologies that are actually part of the platform. If the exact implementation is unknown, label the component as "Implementation TBD" rather than inventing a technology.

Group tightly coupled components together and clearly show service boundaries.

The final diagram should allow an engineer to understand the internal architecture without reading source code.

---

# Diagram 4 — Technology Stack Diagram

## Eraser Prompt

Create a technology stack architecture diagram for the platform.

Organize the diagram vertically into layers:

1. User / Client
2. Frontend
3. API / Gateway
4. Backend / Application
5. AI / ML Layer, if applicable
6. Messaging / Async Processing
7. Database
8. Cache
9. Search / Indexing
10. Object / File Storage
11. Third-Party Integrations
12. Infrastructure
13. CI/CD
14. Monitoring / Observability

For every component, display:

* Technology name
* Technology category
* Primary purpose
* Which platform component uses it

Examples of categories include:

Frontend:

* Framework
* Language
* UI library
* State management

Backend:

* Language
* Framework
* API technology
* Runtime

Data:

* Relational database
* NoSQL database
* Cache
* Object storage
* Search engine

Infrastructure:

* Cloud provider
* Containers
* Kubernetes/serverless/VMs
* Load balancer
* CDN
* DNS

DevOps:

* Source control
* CI/CD
* Container registry
* Infrastructure as Code

Observability:

* Logging
* Metrics
* Tracing
* Error tracking

Do not invent technologies. Use only the technologies actually used by the platform.

Show relationships between technologies and the platform components that use them.

---

# Diagram 5 — Data Architecture

## Eraser Prompt

Create a detailed data architecture diagram showing how data moves through the platform.

Show:

User Input
→ Frontend
→ API
→ Application Services
→ Data Access Layer
→ Databases / Storage / Cache / Search
→ Processing
→ Output

Identify all major data stores.

For each data store, show:

* Technology
* Type of storage
* Purpose
* Main entities/data stored
* Services that read from it
* Services that write to it

Show major data entities and their relationships where appropriate.

Include:

* Primary database
* Read replicas, if applicable
* Cache
* Object/file storage
* Search/indexing
* Message/event storage
* Analytics/data warehouse
* Temporary storage

Distinguish between:

* Persistent data
* Cached data
* Temporary data
* Derived/indexed data
* Events

Show data ownership boundaries.

Where possible, indicate:

* Read operations
* Write operations
* Event-driven updates
* Data synchronization

Do not expose individual database columns unless they are essential to understanding the architecture.

---

# Diagram 6 — Authentication and Authorization Architecture

## Eraser Prompt

Create a security architecture diagram showing how users authenticate and how authorization is enforced throughout the platform.

Show:

User
→ Client
→ Identity Provider / Authentication Service
→ Token / Session
→ API Gateway
→ Authorization Layer
→ Backend Services
→ Data Resources

Include the actual authentication mechanism used by the platform.

Show:

* Login
* Signup
* Token/session creation
* Token validation
* Token refresh
* Logout
* Role/permission validation
* Service-to-service authentication

Show the different user roles and their access boundaries if applicable.

Clearly distinguish:

* Authentication
* Authorization
* Identity management
* Session management

Show where security checks occur.

Include external identity providers if used.

Do not invent authentication technologies. Use the actual platform implementation.

---

# Diagram 7 — External Integrations Architecture

## Eraser Prompt

Create an integration architecture diagram showing all external systems that communicate with the platform.

Put the platform in the center.

Around it, show external dependencies grouped into categories:

1. Identity / Authentication
2. Payments
3. Communication
4. Email
5. SMS
6. AI / LLM Providers
7. Analytics
8. Storage
9. Search
10. Business/Partner APIs
11. Webhooks
12. Other third-party services

For every integration show:

* External system name
* Purpose
* Protocol/API type
* Direction of communication
* Data exchanged
* Whether communication is synchronous or asynchronous
* Authentication mechanism

Use arrows to show:

Platform → External System

and

External System → Platform

For webhook-based integrations, clearly show the webhook flow back into the platform.

Highlight critical external dependencies.

---

# Diagram 8 — Event-Driven / Asynchronous Architecture

## Eraser Prompt

Create a detailed event-driven architecture diagram for the platform.

Show all components involved in asynchronous processing.

Use this general flow:

User / API
→ Application Service
→ Event / Message Producer
→ Queue / Event Bus
→ Consumer / Worker
→ Processing
→ Database / External Service
→ Result / Event

Identify:

* Producers
* Queues
* Topics
* Event buses
* Workers
* Consumers
* Retry mechanisms
* Dead-letter queues
* Scheduled jobs

For each major event, show:

* Event name
* Producer
* Consumer
* Purpose
* Payload category

Clearly distinguish synchronous API requests from asynchronous processing.

Show retry and failure paths.

The diagram should explain why asynchronous processing is used and how background work eventually produces the desired result.

---

# Diagram 9 — AI / ML Architecture

## Eraser Prompt

Create a detailed architecture diagram showing the AI/ML components of the platform.

Only include this diagram if the platform uses AI/ML.

Show:

User
→ Frontend
→ API
→ AI Orchestration Layer
→ Prompt / Processing Logic
→ Model Provider / Model
→ Tools / External APIs / Retrieval
→ Response Processing
→ Backend
→ User

If applicable, include:

* LLM
* Embedding model
* Vector database
* RAG pipeline
* Prompt templates
* Agent/orchestration layer
* Tool calling
* Function calling
* Context management
* Conversation history
* Guardrails
* Content filtering
* Evaluation
* Model fallback
* Streaming responses

Show where data enters the AI pipeline and where it is persisted.

Distinguish:

* User-generated input
* Retrieved context
* System instructions
* Model output
* Post-processed output

Show synchronous and asynchronous AI processing separately where applicable.

---

# Diagram 10 — Deployment / Infrastructure Architecture

## Eraser Prompt

Create a production deployment architecture diagram showing how the platform is deployed and accessed in the cloud.

Organize the architecture into:

Internet
→ DNS
→ CDN / WAF
→ Load Balancer / API Gateway
→ Application Infrastructure
→ Data Infrastructure

Show the actual cloud provider and infrastructure technologies used by the platform.

Include where applicable:

* DNS
* CDN
* WAF
* Load balancer
* API gateway
* Containers
* Kubernetes
* Serverless functions
* Virtual machines
* Application services
* Worker services
* Databases
* Cache
* Object storage
* Message queues
* Secrets management
* Monitoring

Show network boundaries such as:

* Public network
* Private network
* Application subnet
* Database subnet

Clearly indicate which components are internet-facing and which are private.

---

# Diagram 11 — CI/CD and Developer Workflow

## Eraser Prompt

Create a CI/CD architecture diagram showing how code moves from developer machines to production.

Show:

Developer
→ Git Repository
→ Pull Request
→ Code Review
→ CI Pipeline
→ Tests
→ Build
→ Security Checks
→ Artifact / Container Registry
→ Deployment
→ Staging
→ Production

Include actual tools used for:

* Source control
* CI/CD
* Testing
* Linting
* Security scanning
* Containerization
* Artifact management
* Infrastructure deployment

Show separate environments where applicable:

* Local
* Development
* Test
* Staging
* Production

Show deployment triggers and approval gates.

Also show rollback mechanisms if they exist.

---

# Diagram 12 — Observability Architecture

## Eraser Prompt

Create an observability architecture diagram showing how the platform is monitored in production.

Show application services generating:

* Logs
* Metrics
* Traces
* Errors
* Audit events

Route these signals into the actual observability tools used by the platform.

Show:

Application
→ Logging
→ Metrics
→ Distributed Tracing
→ Error Tracking
→ Dashboards
→ Alerts
→ On-call / Operations

For each observability component show:

* Tool
* Data collected
* Purpose

Include correlation between:

* Request ID
* Logs
* Traces
* Errors
* User actions

Show alerting flows for critical failures.

---

# Diagram 13 — Security Architecture

## Eraser Prompt

Create a security architecture diagram showing the major security controls across the entire platform.

Organize security controls into:

1. User / Identity Security
2. Network Security
3. Application Security
4. API Security
5. Data Security
6. Infrastructure Security
7. Secrets Management
8. Monitoring / Audit

Show:

User
→ Authentication
→ API Security
→ Authorization
→ Application Services
→ Data Access

Highlight:

* TLS
* Encryption at rest
* Encryption in transit
* Secrets
* API keys
* Access tokens
* Role-based access
* Network boundaries
* Firewalls
* WAF
* Rate limiting
* Input validation
* Audit logging

Only include controls actually implemented by the platform. Do not assume a control exists if it has not been verified.

Use trust boundaries to distinguish:

* User-controlled environment
* Public internet
* Platform-controlled infrastructure
* Sensitive data systems
* External third parties

---

# Diagram 14 — Key User Journey: Happy Path

## Eraser Prompt

Create a detailed sequence diagram for the platform's primary user journey.

Use the most important end-to-end workflow as the example.

Show participants as vertical lifelines:

User
Frontend
API
Authentication
Backend Service(s)
Database
External Services
Background Worker
Output

Number every major interaction.

Show:

1. User initiates action
2. Frontend validates/submits input
3. Authentication/session validation
4. API receives request
5. Backend processes request
6. Data is retrieved
7. External systems are called where necessary
8. Background processing occurs where necessary
9. Result is persisted
10. Response is returned
11. Frontend updates
12. User receives final output

Clearly identify synchronous versus asynchronous interactions.

The goal is to show exactly what happens during the platform's most important user workflow.

---

# Diagram 15 — Failure and Recovery Architecture

## Eraser Prompt

Create a failure-handling architecture diagram showing what happens when different parts of the platform fail.

Start with a normal user request and show failure branches for:

* Authentication failure
* Invalid request
* Rate limit exceeded
* Backend service failure
* Database failure
* Cache failure
* External API failure
* Timeout
* Queue failure
* Worker failure
* AI/model failure, if applicable

For each failure show:

Failure
→ Detection
→ Error handling
→ Retry / fallback
→ Logging
→ Alerting
→ User response

Clearly distinguish:

* Retryable errors
* Non-retryable errors
* User errors
* System errors
* External dependency failures

Show mechanisms such as:

* Retry
* Exponential backoff
* Circuit breaker
* Dead-letter queue
* Fallback
* Graceful degradation
* Rollback

Only show mechanisms actually implemented by the platform.

---

# Diagram 16 — Complete C4 Architecture

## Eraser Prompt

Create a complete C4-style architecture model of the platform.

Produce a hierarchy containing:

### Level 1 — System Context

Show:

* Users
* Platform
* External systems
* Major interactions

### Level 2 — Containers

Break the platform into:

* Frontend
* API
* Backend services
* Workers
* Databases
* Queues
* Storage
* External integrations

### Level 3 — Components

For the most important backend services, show:

* Controllers/API handlers
* Application services
* Domain logic
* Data repositories
* External clients
* Event publishers/consumers

Use separate boundaries for each major service.

Clearly show dependencies and communication protocols.

Do not include implementation details that are not necessary for understanding architecture.

The result should provide a clean architectural map from the entire platform down to its major internal components.

---

# Diagram 17 — Platform Architecture With Technology Mapping

## Eraser Prompt

Create a comprehensive architecture diagram that combines system architecture with technology mapping.

For every major architectural component, display its actual technology.

Example structure:

User
↓
Frontend
[Actual Frontend Technology]
↓
API Gateway
[Actual Technology]
↓
Backend Service
[Actual Language + Framework]
↓
Database
[Actual Database Technology]

Include all major:

* Applications
* Services
* Databases
* Queues
* Caches
* Storage
* External APIs
* Cloud services
* Infrastructure components

Use labels in this format:

Component Name
Technology: <technology>
Purpose: <purpose>

Show communication protocols on arrows.

Show which components are:

* Client-side
* Internal
* External
* Stateful
* Stateless

Do not invent missing technologies. Mark unknown components as "TBD".

---

# Diagram 18 — One-Page Master Architecture

## Eraser Prompt

Create a single master architecture diagram that provides the most complete possible overview of the platform while remaining readable.

Use these major sections:

1. Users
2. Frontend
3. Edge/API
4. Authentication
5. Core Backend
6. Async Processing
7. AI/ML
8. Data Layer
9. External Integrations
10. Cloud Infrastructure
11. Observability

Show the major components in each section.

Use directional arrows to represent data and control flow.

Use different visual styles for:

* User interaction
* API calls
* Internal service communication
* Database access
* Events/messages
* External APIs
* Monitoring

Label important technologies directly on components.

Avoid showing low-level implementation details.

Prioritize readability and architectural relationships.

The final diagram should answer these questions at a glance:

* Who uses the platform?
* What does the frontend communicate with?
* Where does the request enter the platform?
* What backend services process it?
* Where is data stored?
* Which external systems are involved?
* Which processes are asynchronous?
* Where does AI/ML fit?
* How is the platform deployed?
* How is it monitored?
* What are the major trust/security boundaries?

---

# Recommended Eraser Diagram Sequence

Create the diagrams in this order:

1. **Master Architecture**
2. **System Context**
3. **User Interaction / End-to-End Flow**
4. **Internal Backend Architecture**
5. **Technology Stack**
6. **Data Architecture**
7. **Authentication & Authorization**
8. **External Integrations**
9. **Event-Driven Architecture**
10. **AI/ML Architecture**
11. **Deployment Architecture**
12. **CI/CD**
13. **Observability**
14. **Security**
15. **Primary User Journey**
16. **Failure & Recovery**
17. **Complete C4 Architecture**

This order goes from **business-level understanding → system architecture → internal implementation → infrastructure → operational concerns**.

---

# Important Instructions Before Using These Prompts

Before generating the final diagrams, replace generic descriptions with the platform's actual components.

For example:

`Backend Service`

should become:

`User Service — Node.js / NestJS`

and:

`Database`

should become:

`PostgreSQL — Primary Application Database`

Similarly, replace:

`External AI Provider`

with the actual provider and model if applicable.

Do not allow the diagramming tool to guess technologies or architecture decisions that have not been verified.

If some architecture information is unknown, explicitly mark it:

`Unknown / TBD`

rather than creating a potentially incorrect architecture.

---

# Suggested Documentation Structure

Once the diagrams are created, organize the architecture documentation as:

## 01 — Platform Overview

* Master Architecture
* System Context

## 02 — Application Architecture

* User Interaction
* Internal Backend Architecture
* C4 Architecture

## 03 — Data & Processing

* Data Architecture
* Event-Driven Architecture
* AI/ML Architecture

## 04 — Security

* Authentication & Authorization
* Security Architecture

## 05 — Infrastructure

* Deployment Architecture
* External Integrations

## 06 — Engineering Operations

* CI/CD
* Observability
* Failure & Recovery

## 07 — User Flows

* Primary User Journey
* Additional Important User Journeys

This structure should make the architecture documentation useful for onboarding engineers, debugging issues, explaining the platform to stakeholders, and making future architectural decisions.
