# Plan: Moving the Patient Scheduling System to the Cloud Without Downtime

I don't know your specific system, so I've made some assumptions. They're listed at the end along with the questions that would most change this plan.

## A note on "no downtime"

You can keep the system fully available to users the whole time. The one hard moment is when you switch which database accepts new bookings. Two databases taking bookings at once can cause double-booked slots, which is a patient-safety risk. Before you commit to a target, it helps to agree on what "no downtime" means:

- **Reads never stop.** Users can always see schedules.
- **Writes pause for under 60 seconds** at cutover, during the quietest hour. The app queues or retries them, so users see at most a short "saving…" delay.
- **Hospital downtime procedures are on standby** but shouldn't be needed.

True zero write-pause needs two-way replication with conflict handling. I don't recommend it for scheduling, because conflicts here mean two patients in one slot.

---

## Phase 0: Governance and compliance (weeks 1–4)

- **Business Associate Agreement (BAA)** signed with the cloud provider. Use only services covered by it.
- **Security and privacy review:** HIPAA risk assessment, encryption at rest and in transit, key management, audit logging, and where data is stored.
- **Stakeholders:** clinic managers, the access and scheduling center, IT, the interface team, the compliance officer, and the change advisory board.
- **Success criteria:** response time no worse than today, zero lost or duplicate appointments, and a tested rollback.

## Phase 1: Discovery (weeks 2–6)

Integrations are where hospital migrations usually fail, so map all of them:

| Category | Examples to inventory |
|---|---|
| Inbound feeds | ADT (admissions/discharges/transfers) from the EHR, provider and resource master files |
| Outbound feeds | Scheduling messages (HL7 SIU) to the EHR, billing/eligibility, radiology/lab systems |
| Interface engine | Rhapsody, Cloverleaf, Mirth, etc.: all endpoints, IP/firewall rules, hard-coded hostnames |
| Patient-facing | Portal, online booking, SMS/IVR reminders, kiosks |
| Internal | Reports, data warehouse ETL jobs, nightly batch jobs, printers and label stations |
| Identity | Active Directory/LDAP, SSO, service accounts |

Also record:
- Peak load: the 7–9am clinic rush, Monday mornings, flu season.
- Database size, how fast data changes, and latency sensitivity.
- Licensing limits. Some vendors restrict cloud hosting or require their approval.

## Phase 2: Target design (weeks 4–8)

- **Approach:** Rehost or lightly replatform first (e.g., move the database to a managed service). Leave redesigning the application for after the move; don't change the code and the location at the same time.
- **Network:** a dedicated private link (AWS Direct Connect or Azure ExpressRoute) between the hospital and the cloud, with a site-to-site VPN as backup. Test latency from every clinic site.
- **High availability:** spread across multiple availability zones, with automatic database failover.
- **Security:** private subnets, no public database endpoints, a web application firewall in front of the patient portal, and logs sent to your security monitoring (SIEM).
- **Disaster recovery:** set target recovery times and data-loss limits. Consider keeping on-prem as a warm standby for the first 30–90 days.

## Phase 3: Build and continuous sync (weeks 8–14)

1. Build the cloud environment with infrastructure-as-code (Terraform, Bicep, etc.).
2. **Load an initial database copy**, then turn on **continuous change replication** from on-prem to the cloud. Options include AWS DMS, Azure DMS, native log shipping or replicas, or Debezium.
3. Deploy the app tier in the cloud, pointed at the replica in **read-only mode**.
4. **Reconcile data every day:** appointment counts per day, provider, and location, plus checksums on key tables. Investigate any difference before going further.
5. Set up the interface engine so it can send to either environment, controlled by a configuration switch.

## Phase 4: Testing (weeks 12–18)

- **Functional tests:** book, reschedule, cancel, waitlist, recurring appointments, overbooking rules.
- **Integration tests:** send HL7 messages from the EHR test system end to end in both directions.
- **Load tests:** at least 1.5× historical peak.
- **Failover tests:** take down an availability zone, the database primary, and the private network link.
- **Rehearse the full cutover and rollback at least twice** in a staging environment, and time each step.
- **User acceptance testing** with real schedulers and clinic staff.

## Phase 5: Gradual traffic shift (weeks 18–20)

1. Lower DNS cache times (TTL) to about 60 seconds, a week ahead.
2. Send **read-only traffic** (reports, schedule viewing) to the cloud first.
3. Optionally pilot one low-risk clinic or department as a test group, if the application can route users that way.

## Phase 6: Cutover runbook (target: 1–2am on a low-volume weekday)

| Time | Step |
|---|---|
| T-24h | Go/no-go meeting; confirm reconciliation is clean; downtime procedures briefed |
| T-30m | Freeze batch jobs; command center open (IT, interfaces, vendor, clinical lead) |
| T-0 | Put on-prem database in **read-only**; app queues writes |
| T+15s | Confirm replication lag = 0; run final checksum |
| T+30s | Promote cloud database to primary; switch app, load balancer and DNS |
| T+45s | Interface engine routes to the cloud; drain queued writes |
| T+1m | Smoke tests: book, cancel, check the HL7 message arrives in the EHR |
| T+5m | **Start reverse replication cloud → on-prem**, which keeps rollback possible |
| T+2h | Go/no-go checkpoint for rollback |

**Rollback triggers:** errors above threshold, HL7 messages backing up, data mismatch, or response time worse than baseline. Because reverse replication is running, rollback is the same switch in the opposite direction.

## Phase 7: Hypercare and decommission (weeks 20–32)

- **Hypercare (2–4 weeks):** IT staff on site at scheduling centers, dashboards for errors, response time, and HL7 queue depth.
- **Daily reconciliation** continues for 2 weeks.
- **Keep on-prem warm for 30–90 days**, then archive it according to your record-retention policy. Wipe disks securely, with certificates of destruction.
- **Post-migration review**, then plan improvements now that you're in the cloud.

---

## Main risks

| Risk | How to reduce it |
|---|---|
| A hidden integration breaks (an old IP address or hostname nobody knew about) | Capture network traffic during discovery; monitor the interface engine |
| Double-booking during cutover | Writes go to one database only; brief write freeze |
| Latency at remote clinics | Private network link, latency tests per site |
| Vendor refuses to support cloud hosting | Get vendor approval in Phase 0 |
| Cutover coincides with a surge in patients | Choose the window with clinical leaders; keep a no-go option |
| Compliance gap | Signed BAA, risk assessment, logging in place before any real patient data moves |

**Rough timeline:** about 5–8 months, depending on how many integrations you have and how quickly the vendor engages.

---

## Assumptions and questions

The plan assumes the system is a standalone app (vendor-built or in-house) with its own database, connected to the EHR through HL7. These answers would change it the most:

1. **Which system is it?** If scheduling is a module of Epic, Oracle Health/Cerner, or Meditech, the vendor's own hosting program usually decides the path.
2. **What database and platform?** For example SQL Server or Oracle, on Windows or Linux. This decides which replication tools you can use.
3. **Which cloud provider**, and is there an existing BAA?
4. **How many integrations and clinic sites**, and are any sites rural or on slow connections?
5. **Is it your code or the vendor's?** That determines whether you can add write-queuing for the cutover.

If you can share architecture diagrams, an interface list, or vendor documents, I can turn this into a detailed runbook for your environment.
