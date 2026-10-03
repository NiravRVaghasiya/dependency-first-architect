# Plan: Zero-downtime cloud migration of the hospital patient scheduling system

> **Stack assumptions (please confirm).** I don't know your stack yet, so I've planned for the most common hospital setup: a Windows/.NET-style scheduling app on a **SQL Server** database. It exchanges **HL7 v2** messages with the EHR (Electronic Health Record) through an interface engine such as Mirth, Rhapsody or Cloverleaf. The default cloud is **Azure**, which is common where a hospital already runs Microsoft 365 or Entra ID. Section 2 says what changes if any of this is wrong. The biggest one: if this is a vendor product (Epic Cadence, Oracle Health, etc.), the vendor's supported hosting options set the rules.

---

## 1. Classification
- **What:** Moving a live patient scheduling system (app, database, links to other systems, scheduled jobs) from the hospital's own servers to the cloud with no outage users can see.
- **Type:** Infrastructure and software combined. Not an AI system.
- **Dominant constraint:** **Correctness**, closely followed by **compliance**. Every appointment must survive exactly once, at the right time. Protected health information (PHI) must stay inside the HIPAA boundary.
- **Worst failure:** Appointments that are lost, duplicated or shifted in time, found only after patients miss care. Close runners-up: two systems both accepting bookings ("split-brain"), and duplicate reminder texts or HL7 messages sent from a shadow copy.

## 2. Key decisions (resolved up front)

| Decision | Default (chosen now) | Revisit trigger |
|---|---|---|
| **What "zero downtime" means** (agree this with clinical leadership first) | Reading schedules never stops. Booking pauses **once, for ≤60 s**, at the database switchover, covered by automatic retry or a "saving…" banner. **No data loss.** No scheduled downtime window. | If clinical leadership needs literally zero booking pause, don't add two-way writes. Run an announced 5–10 minute read-only period with standard downtime procedures instead. If the app can't retry cleanly (found in Phase 3), the same applies. |
| Consistency vs availability | **Consistency for bookings.** One database accepts bookings at any moment; the copy replicates in one direction. No setup where both sites accept bookings during the migration. | Never flips for booking slots, because double-booking is a patient-safety issue. Read-only views may show data up to 5 s old from a replica. |
| Move as-is vs re-platform vs rebuild | **Move first, improve later.** Same app build. The database moves to managed **Azure SQL Managed Instance (SQL MI)**. Little else changes. | If the compatibility check (Phase 1) finds features SQL MI can't run, such as server-level tricks, FILESTREAM or unsupported cross-server links, use **SQL Server on Azure virtual machines** with a cross-site availability group instead. |
| Database copy / fallback mechanism | **SQL MI link** (built on SQL Server's distributed availability groups): continuous near-real-time copy, then a planned switch. | Switching *back* through MI link needs on-prem **SQL Server 2022**. If you run an older version, either upgrade on-prem first (adds a phase) or use the virtual-machine option, which allows a switch back. If the database is Oracle, use Data Guard or GoldenGate. |
| Monolith vs services | **Keep the monolith.** A migration is not the time to redesign. | Only after 90 days of stable running in the cloud. Then consider breaking pieces off gradually (see Deferred). |
| Sync vs async | Bookings commit **synchronously** to the one primary database. Copying from on-prem to cloud is **async**, and switches to **sync** just before the cutover. HL7 traffic stays **async**, queued in the interface engine. | If the network delay between hospital and cloud is >10 ms at peak, synchronous mode slows bookings too much. Keep the sync window to minutes, or choose a closer cloud region. |
| Build vs buy | **Buy** the copying tool (MI link), the managed database, the web firewall (WAF) and monitoring. **Build** only three things: the data-checking tool, the scripted cutover steps, and the switch that blocks outgoing messages. | Vendor-supported product: follow the vendor's certified cloud setup, or their hosted option, and this plan becomes your acceptance checklist. |
| How the cutover is split | **One switch for the whole database**. Read traffic and the app servers move **clinic by clinic** first. | If each facility already has its own database, migrate one site at a time. |
| Keep the interface engine on-prem or move it | **Keep it on-prem** during the migration. Point the scheduling links at network names that can be switched, not fixed addresses. | Move it to the cloud only after cutover, as a separate project, or earlier if the on-prem engine is end-of-life within 12 months. |
| Cloud provider / PHI boundary | Azure, one US region, **Business Associate Agreement (BAA) signed**. Only private network endpoints. Hospital-held encryption keys in Key Vault. **No PHI in logs or traces.** | If you already have an AWS enterprise agreement and BAA, use AWS equivalents (RDS Custom or SQL on EC2, DMS, Direct Connect). |
| AI gates (prompt+RAG vs fine-tune, hosted vs self-host) | **N/A.** There is no AI part. | N/A |

## 3. First end-to-end slice (Phase 0): a read-only shadow path
The goal is one real request through every cloud layer, on real production data. The hospital's on-prem system stays the source of truth throughout.

- **The request:** "List Dr. X's appointments at Clinic Y for tomorrow." An automated test probe sends it from inside the hospital network every 5 minutes. One IT analyst can also run it from a workstation.
- **The response:** The same appointment list on-prem returns. Every run checks that both lists match exactly.
- **Layers it crosses:** Hospital network → internal DNS name (`sched-cloud.hosp.internal`) → **ExpressRoute** (private line to Azure, with a VPN backup) → hub firewall → **Application Gateway with WAF** (private access only) → **one app server** running the current build in read-only mode → **SQL MI readable copy** of the scheduling database, fed by MI link from the live on-prem database.
- **How it's deployed:** Terraform or Bicep (infrastructure-as-code) from git, through a pipeline. No manual changes in the Azure portal. The app is the same version that runs in production on-prem, from the same artifact.
- **Logging and monitoring from day zero:** Application Insights traces each request with an ID. Probe results go to Log Analytics. Security logs go to Microsoft Sentinel. A dashboard shows **copy delay (seconds)**, **probe match rate** and **latency at each layer**. Alerts fire if copy delay is >30 s or a probe result doesn't match.
- **Why it's safe:** Nothing clinical depends on the cloud yet. If the skeleton breaks, patients aren't affected.

**Exit check:** For 7 days the probe returns results identical to on-prem. The trace and copy-delay metric show on the dashboard. A deliberately broken probe sets off the alert.

## 4. Phases (each depends on the one before; within a phase, the task that would cause the most rework is done first)

**Phase 0: First end-to-end slice** (above). Its tasks, in order:
1. Sign the BAA and do the HIPAA risk analysis for the new setup.
2. Map every dependency of the current system: 30 days of network-traffic capture, including a month-end, plus Azure Migrate dependency analysis. List every connection: HL7 feeds, patient portal, SMS/phone reminders, billing, kiosks, printers, fax, reports, fixed IP addresses in config.
3. Build the Azure landing zone: networks, private endpoints, no public IPs.
4. ExpressRoute plus VPN backup.
5. Sign-in: extend Active Directory to Azure (Entra ID hybrid).
6. Set up MI link for the main scheduling database.
7. Deploy one app server.
8. Build the probe and dashboard.

- *Unlocks:* Every later phase. *Depends on:* Nothing.

**Phase 1: Prove the data**
- *Unlocks:* Trusting the cloud copy of the data. *Depends on:* Phase 0's working MI link and pipeline.
- *Tasks (most rework-prone first):*
  1. **Compatibility checks:** time zone, daylight-saving and sort-order settings. Cloud servers default to UTC; if the app stores server-local time, every appointment shifts by hours. Also run Data Migration Assistant for feature gaps.
  2. Link **all** scheduling databases, including reporting and audit databases.
  3. Nightly data check: row counts plus checksums per table, and a field-by-field comparison of next-30-days appointments.
  4. Test point-in-time restore in the cloud.
  5. Test the reverse copy (cloud back to on-prem) in a test environment.
- *Exit check:* 30 days running through a month-end. Copy delay p99 <5 s. Zero unexplained data mismatches. A restore to a set time is proven. Time-zone checks pass across a daylight-saving test date.

**Phase 2: Side effects and integrations**
- *Unlocks:* Running cloud app servers without them sending duplicate messages. *Depends on:* Phase 1's proven data.
- *Tasks:*
  1. **Network-name layer** for every endpoint (DNS aliases for the database listener, app URL and HL7 endpoints). Everything else will point at these names, so this comes first.
  2. **Outgoing-message block:** a config switch that stops cloud servers sending HL7 appointment messages, SMS/phone reminders, email and fax, on by default until cutover.
  3. **Scheduled-job ownership:** a lock so each job (reminder runs, no-show sweeps, nightly reports) runs in exactly one place.
  4. Interface engine to cloud connections over secure channels, tested by replaying captured incoming patient-admission messages.
  5. Repoint printers, label printers and kiosks through the network names.
- *Exit check:* The cloud servers sent nothing in 14 days of shadow running, proven by outbound message counts. Every connection on the Phase 0 list is tested or marked "unchanged by migration".

**Phase 3: Full app copy in the cloud and moving read traffic**
- *Unlocks:* Real users reading from the cloud. *Depends on:* Phases 1 and 2.
- *Tasks:*
  1. **Sign-in parity:** single sign-on/Kerberos, role mapping, and PHI access audit logs matching what you have today.
  2. Run app servers across multiple availability zones behind the gateway.
  3. **Retry test:** stop the database for 60 s during a test and confirm the app recovers without data errors. This decides the "zero downtime" gate.
  4. Load test at 2× the busiest real hour (for example Monday 08:00).
  5. Move schedule-*viewing* traffic to the cloud clinic by clinic: pilot clinic → 25% → 100%. Bookings still go to the on-prem database.
  6. Reports and print layouts.
- *Exit check:* All read traffic served from the cloud for 14 days. p95 latency ≤ on-prem baseline +50 ms. Retry test passes. Security penetration test findings closed.

**Phase 4: Rehearsals and switch-back test**
- *Unlocks:* Authorisation to go live. *Depends on:* Phase 3.
- *Tasks:*
  1. Write the cutover steps as a script (database switch, network-name changes, outgoing-message block lifted, job ownership moved).
  2. **Full dress rehearsal** on a production-sized copy in a separate environment rebuilt from code.
  3. **Switch-back rehearsal** to on-prem.
  4. Drill the clinical downtime procedures: printed schedules, read-only fallback.
  5. Define go/no-go criteria and rollback triggers, approved by the change board and the clinical safety lead.
- *Exit check:* Two clean rehearsals in a row, each with booking pause ≤60 s and zero data mismatches. A switch-back completed in ≤15 minutes.

**Phase 5: Database cutover** (production)
- *Unlocks:* The cloud as the primary system. *Depends on:* Phase 4's sign-off.
- *Tasks:*
  1. Go/no-go check: copy delay 0, no open major incidents, quiet time (for example Sunday 02:00, outside flu surge and month-end).
  2. Switch the copy to synchronous mode.
  3. Pause scheduled jobs.
  4. **Planned database switch** (bookings pause here).
  5. Start the reverse copy to on-prem.
  6. Switch network names.
  7. Lift the outgoing-message block; turn off on-prem outgoing messages.
  8. Move job ownership.
  9. Book and then cancel a test appointment on a test patient, end to end through HL7 to the EHR.
- *Exit check:* Booking pause ≤60 s as measured. Data check clean at +1 h and +24 h. Zero duplicate outgoing messages. HL7 acknowledgement rates normal.

**Phase 6: Hypercare, disaster recovery and decommissioning**
- *Depends on:* Phase 5 plus 30 stable days.
- *Tasks:*
  1. 2–4 weeks of hypercare, keeping the on-prem database as a live reverse copy for switch-back.
  2. Set up disaster recovery: a copy in a second Azure region, plus a DR test. This must be proven **before** on-prem is retired, so DR is never weaker than today.
  3. Archive data under the records-retention policy.
  4. Retire on-prem, with NIST 800-88 disk wiping.
  5. Cost baseline.
- *Exit check:* Cloud DR test passes the agreed recovery targets. On-prem retired. HIPAA risk analysis updated.

## 5. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | BAA signed, private endpoints only, hospital-held encryption keys, Defender for Cloud, logs kept free of PHI | Probe, copy-delay metric, request tracing, Sentinel | All infrastructure from code via pipeline; same app version as on-prem | ExpressRoute plus VPN backup; on-prem stays primary, so a cloud failure has no clinical effect |
| 1 | Database encryption, encrypted copy channel, least-privilege copy account, no PHI in dev/test without de-identification | Data-mismatch metric, copy-delay alerts, data-check reports | Schema and copy settings stored as code; data-check scripts version-controlled | Database spread across zones, point-in-time restore tested, re-copy procedure written |
| 2 | Secure HL7 channels, firewall allow-list per connection, secrets in Key Vault | Outgoing and incoming message counts and acknowledgement rates, on-prem vs cloud | Interface engine channel settings exported to git | Interface engine queues messages if a link fails; outgoing block on by default |
| 3 | Penetration test, WAF rules, multi-factor login for admins, PHI access audit logs matching today's | Per-request rate, error and latency metrics; per-clinic user monitoring; latency compared against on-prem | Fixed, versioned app images; config in App Configuration | Servers across zones, retries with backoff, load test at 2× peak, a server deliberately shut down |
| 4 | Emergency "break-glass" accounts tested, access review, change-board approval | Timing of each rehearsal step recorded and compared with targets | Cutover script versioned; rehearsal environment rebuilt from code each time | Switch-back rehearsed, downtime procedures drilled, rollback triggers written |
| 5 | Change freeze, time-limited admin access, recorded admin sessions | War-room dashboard: copy delay, pause length, message counts, error rate | Exact tagged script version run; every step logged | Reverse copy running, switch-back available, downtime procedures on standby |
| 6 | Copy credentials revoked, NIST 800-88 disk wiping, HIPAA risk analysis updated | Uptime-target alerts, cost dashboards, DR test reports | DR region rebuilt from the same code | Second-region DR copy tested; on-prem kept as switch-back until DR is proven |

## 6. AI layer
**N/A.** There are no AI or agent parts in this system, so the AI ordering doesn't apply.

## 7. Deferred
- **Splitting the app into smaller services** (gradual replacement). Bring forward if, after 90 days stable, one module (for example reminders) is blocking release speed.
- **Bookings accepted in more than one region at once.** Bring forward only if the hospital's resilience target can't be met by single-writer DR. This needs a booking design that avoids double-booking.
- **Moving the interface engine to the cloud, and FHIR APIs.** Bring forward if the on-prem engine is end-of-life within 12 months, or the EHR vendor requires FHIR for scheduling.
- **Patient self-scheduling and portal improvements.** Bring forward once Phase 6 is done. Features never ride along with the migration.
- **Cost optimisation and fine-tuned autoscaling** (reserved capacity, smaller servers). Bring forward once 30 days of cloud usage data exists.
- **Analytics or data lake on scheduling data.** Bring forward when a governed de-identification pipeline exists.
