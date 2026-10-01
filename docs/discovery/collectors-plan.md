# Collectors: gathering system documentation read-only (plan)

Status: **proposal**. Nothing here is built. It follows from the systems register
(`TALOS_HOME/docs/discovery/workplace-systems.md`) and keeps every Talos promise: read-only, secrets only in the Keychain, models
propose and people decide.

## What a collector is

A small module per system, `src/talos/collectors/<system>.py`, that answers one question: *what is this
system's state right now?* It never changes the system.

```
launchd (talos.collect, e.g. nightly)  →  talos collect [--system unifi]  →  collector.fetch()   (GET only)
                                                                            ↓
                                                   normalise → compare with the last snapshot
                                                                            ↓
            object(kind=system).attrs.state   ← summary: health, versions, counts, observed_at, source
            note(kind='snapshot')             ← the full normalised snapshot (JSON + readable Markdown)
            event(kind='collector.change')    ← one row per difference: "firmware 9.1 → 9.2", "new VLAN 40"
```

- **Read-only by construction.** Each collector gets an HTTP client that only allows GET (and the few
  read-only POST query endpoints some APIs need, such as Azure Resource Graph or GraphQL, each listed
  explicitly). A guard test, like `tests/test_guards.py`, fails the build if a collector module contains a
  write verb or a write scope.
- **Least privilege at the source too.** Each system gets a dedicated read-only identity (below), so even a
  bug cannot write.
- **Secrets only in the Keychain**, in Talos's existing convention: service `talos`, account
  `collector:<system>`. The owner enters each one themselves:

  ```bash
  security add-generic-password -s talos -a collector:cloudflare -w
  ```

  `-w` as the last flag makes `security` prompt for the value, so it never lands in shell history, a file,
  or a chat. `talos status` shows presence only (`secrets.exists`), never the value.
- **Output is documentation, not telemetry.** A snapshot holds configuration and inventory (devices,
  versions, networks, policies, licences, accounts), not logs or metrics. Unknown stays unknown: a failed
  collection sets `health: unknown` with the error, never "healthy".
- **Timeline.** The `event` rows give each system object a history ("what changed on the firewall in
  March?") next to its mail.
- **The vault stays read-only.** State lands in the Talos DB. Whether a summary is later written back to the
  system's Obsidian State note is a separate decision (see `docs/obsidian-mapping.md`, phases 2–3).
- **Argus watches the job**: a collector run checks in like the sync does, so a silent failure is noticed.

## Order of work

Start where access half exists, then go by value per hour of setup.

| Step | Systems | Why first | The owner's effort |
|---|---|---|---|
| 1 | Microsoft 365, Entra ID, Intune, Defender, Purview, Copilot reports | The Entra app Talos already signs in with (device code, token cache in the Keychain) just needs more read scopes | 15 min in the Entra portal |
| 2 | Azure, then Cost Management | Cost exports may already exist; one service principal covers inventory | 15 min |
| 3 | Cloudflare, GitHub Enterprise, Snipe-IT, Pingdom | One read-only token each, well-documented APIs | 5 min each |
| 4 | UniFi, Proxmox | Local APIs with view-only roles | 10 min each |
| 5 | AWS | Needs IAM Identity Center set up in the new organisation first | 30 min |
| 6 | Check Point | Depends on how the gateways are managed (open question) | depends |
| 7 | Mail-derived state: a NAS, an alarm company, the accounting, expense and HR systems | No API worth using; parse the machine mail already in the archive (`events.py`) | none |

## Per system: the access to create, and the owner's steps

### Microsoft 365, Entra ID, Intune, Defender (the existing Entra app) (step 1)

Talos's app today asks for `User.Read Mail.Read Chat.Read ChannelMessage.Read.All Calendars.Read`
(`src/talos/graphauth.py`). Add a **separate collector scope set**, requested in its own token so the mail
sync's token does not widen:

| Scope (delegated) | Gives |
|---|---|
| Organization.Read.All | tenant, licences (subscribedSkus), domains |
| Directory.Read.All | users, groups, devices, directory roles |
| Policy.Read.All | Conditional Access, authentication methods policies |
| RoleManagement.Read.Directory | role assignments, PIM eligibility |
| AuditLog.Read.All, Reports.Read.All | sign-in and usage reports (incl. Copilot usage) |
| IdentityRiskyUser.Read.All | risky users (the Identity Protection digest, but live) |
| DeviceManagementManagedDevices.Read.All | Intune devices and compliance |
| DeviceManagementConfiguration.Read.All | Intune configuration and compliance policies |
| DeviceManagementApps.Read.All | Intune apps |
| DeviceManagementServiceConfig.Read.All | Autopilot, enrolment, Apple (ABM/ADE) connectors |
| ServiceHealth.Read.All, ServiceMessage.Read.All | service health and message center (replaces those mails) |
| SecurityAlert.Read.All, SecurityIncident.Read.All | Defender alerts and incidents |
| Application.Read.All | app registrations and enterprise apps (including this one) |

The owner's steps:
1. Entra admin center → App registrations → the Talos app → API permissions → Add → Microsoft Graph →
   Delegated → tick the scopes above → **Grant admin consent for <your organisation>**.
2. Run the first `talos collect --system m365` in a terminal; it prints a device code once for the
   collector token and caches it in the Keychain like the mail token.
3. Later, for a second organisation: the same in that tenant (a second app registration there, or make the app
   multi-tenant and consent there).

Delegated scopes act as *the owner*, limited by the scopes: nothing here can write. If the owner later wants
collection without their sign-in, the alternative is an app-only registration with the same `.Read.All` application
permissions and a certificate whose private key sits in the Keychain.

DLP policies are not in Graph; Exchange Online PowerShell with the **View-Only Organization Management**
role group covers them, as a later, optional step.

### Azure (step 2)

A service principal `talos-collector` with **Reader** and **Cost Management Reader** at the tenant root
management group (or per subscription). Certificate credential, private key in the Keychain
(`collector:azure`). Collects subscriptions, resource inventory (Resource Graph), Advisor recommendations,
Defender for Cloud secure score, and monthly cost. Steps: Entra → App registrations → New → Certificates
& secrets → upload a certificate created locally; Management groups → Tenant Root → Access control →
add the two roles. Optionally reuse existing cost exports instead of calling the Cost API.

### Cloudflare (step 3)

My Profile → API Tokens → Create Token → Custom. Permissions, all **Read**: Account Settings, Zone, DNS,
Zone Settings, Firewall Services, Workers Scripts, Page Rules. Account resources: the company's account
only. Keychain: `collector:cloudflare`.

### GitHub Enterprise (step 3)

A GitHub App installed on the company's organisations with **Administration: read,
Members: read, Metadata: read, Organization plan: read**; plus a classic token with only `read:enterprise`
and `read:audit_log` for enterprise billing and audit (fine-grained tokens do not cover enterprise billing).
Keychain: `collector:github-app` (private key), `collector:github-enterprise`.

### Snipe-IT (snipe-it.io) (step 3)

Create a user `talos-api` in a permission group with only **View** on assets, licences, accessories,
consumables, components and users; log in as it → Manage API Keys → create. Keychain: `collector:snipeit`.

### Pingdom (step 3)

Pingdom → Settings → Pingdom API → Add API token → **Read Access**. Keychain: `collector:pingdom`.

### UniFi (step 4)

On the console: create a local admin `talos` with the **View Only** role for UniFi Network; as that user,
Settings → Control Plane → Integrations → create an API key. Keychain: `collector:unifi`. Collects devices,
firmware, networks and VLANs, WLANs, port profiles and client counts. (Alternative for a multi-site
overview: a Site Manager API key from unifi.ui.com, read-only.)

### Proxmox VE (step 4)

Datacenter → Permissions → Users → add `talos@pve`; API Tokens → add `talos@pve!collector` with
**Privilege Separation** on; Permissions → add token permission on `/` with role **PVEAuditor**. Keychain:
`collector:proxmox`.

### AWS (step 5)

Enable IAM Identity Center in the management account; create a permission set `TalosReadOnly` from the
AWS managed policies **ViewOnlyAccess** and **SecurityAudit** (ReadOnlyAccess if data-plane reads are wanted
too); assign it to the owner's user in every member account. Talos then uses `aws sso login` short-lived
credentials, so no long-lived key exists anywhere.

### Check Point (step 6)

If the gateways are centrally managed (Smart-1 Cloud or a management server): an administrator `talos`
with the **Read Only All** permission profile and an API key; the collector calls only `show-*` commands.
If they are locally managed Quantum Spark appliances: SNMPv3 read-only for health and version, and a
read-only admin for a scheduled configuration export. Decide after answering the question in the register.

### Others, briefly

| System | Read-only access |
|---|---|
| Grafana | service account, Viewer role, token |
| MongoDB Atlas | organisation API key, Organization Read Only |
| ClickHouse Cloud | API key with a read-only role |
| TeamViewer | Web API script token with read rights only |
| Cursor, Claude, OpenAI | each vendor's admin API key for members and usage (read endpoints only) |
| Domains | registrar read-only API keys where offered; else public RDAP |
| Certificates | Certificate Transparency (public, no credentials) |
| Tailscale | API access token scoped to devices:read |
| NAS, UPS, printers | SNMPv3 read-only user (`net-snmp` is already installed) |
| Domain controllers | an unprivileged domain account for LDAP reads; DNS/DHCP config via an export scheduled on the DC |

## YouTube: what it would add, and how to get it

**What it adds.** An archive typically holds only a few YouTube notification mails, so the owner's real
YouTube interests are invisible to Talos. Their watch history, likes, playlists and subscriptions are years of
stated interest: they would confirm or sharpen the personal candidates (hobbies such as music or photography)
and surface topics that never reach the mailbox. Talos would cluster channels and titles
into themes with counts and first/last dates, and propose areas and topics the same way as
`TALOS_HOME/docs/discovery/new-areas-topics.md`: as a ranked draft for the owner to accept or reject.

**Recommendation: Google Takeout, not crawling the owner's browser.**

| | A Google Takeout file the owner downloads | Crawling the owner's logged-in browser |
|---|---|---|
| Completeness | All watch and search history since the account began, likes, playlists, subscriptions | Only what the page loads; history pages are infinite scroll and often truncated |
| Access | The owner decides what to export and hands over a file path; Talos never touches the account | An agent drives their signed-in session: account access, which these rules forbid |
| Safety | Offline, read-only, auditable, repeatable | Bot detection, CAPTCHAs, rate limits, possible account flags; a mis-click can change the account |
| Stability | A documented format (JSON/CSV) | Breaks whenever YouTube changes its pages |
| Privacy | The file stays on the Mac, in the vault like any other original | Cookies and session tokens in play |

**The owner's steps** (about 5 minutes, then a wait for Google's email):

1. Open **takeout.google.com** signed in as the account used for YouTube.
2. Click **Deselect all**.
3. Scroll to **YouTube and YouTube Music** and tick it.
4. Click **Multiple formats** → set **history** to **JSON** (the default is HTML) → OK.
5. Click **All YouTube data included** → keep **history**, **playlists** (includes *Liked videos*),
   **subscriptions**; untick **videos** (the owner's own uploads, large) and anything else not to be shared → OK.
6. Optional, for search terms: also tick **My Activity**, open its options, and limit it to **YouTube**.
7. **Next step** → Destination **Send download link via email**, Frequency **Export once**, File type
   **.zip**, Size **2 GB** → **Create export**.
8. When the email arrives, download the zip to, for example, `TALOS_HOME/imports/takeout-youtube.zip`
   and give that path. Talos stores it in the vault like any original and parses
   `watch-history.json`, `search-history.json`, `playlists/*.csv` and `subscriptions/subscriptions.csv`.
