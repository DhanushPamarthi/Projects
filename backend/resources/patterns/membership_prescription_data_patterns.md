# Membership and Prescription Data Patterns

Patterns based on the latest paired sample supplied in chat. Each row has outer database columns and a nested `data` object. Confirm required fields and allowed values against the target database schema before loading.

## Shared row columns

| Column | Pattern | Rule |
|---|---|---|
| `id` | Unique integer row ID | Use a new ID or let the database assign it. Never copy the example ID. |
| `load_id` | Batch ID string | Use the same value for membership and prescription rows in one load. |
| `created_time` | ISO 8601 timestamp in UTC | Use one consistent batch timestamp, e.g. `YYYY-MM-DDTHH:mm:ss+00:00`. |
| `data` | JSON object | Populate the entity-specific fields below. |

## Membership `data` fields

| JSON path | Pattern | Example / relationship rule |
|---|---|---|
| `data.member_id` | Unique synthetic member ID | `PBM7-MEMBER-{YYYYMMDD}-{sequence}`. Use the same value in the linked prescription. |
| `data.subscriber_id` | Subscriber ID | Same as `member_id` for a subscriber; use the subscriber's ID for a dependent. |
| `data.first_name` | Fictional first name | Use synthetic names only; vary by generated record. |
| `data.last_name` | Fictional last name | Use synthetic names only; vary by generated record. |
| `data.speciality_id` | Synthetic or valid fixture ID | `PBM7-SPECIALTY-{sequence}` or a valid ID from the test environment. |
| `data.status` | Supported membership status | `ACTIVE` for an active-member case; use a schema-supported inactive status for exclusion cases. |
| `data.source_event_id` | Event ID | Generate a unique event ID per scenario. Match the prescription event ID when both rows belong to the same event. |
| `data.source_correlation_id` | Correlation ID | Generate one per scenario and share it across the linked membership and prescription. |
| `data.phones` | Array of phone objects | Use a synthetic test number. For a reachable SMS case, include one primary `MOBILE` phone. Use `[]` or a disabled preference for no-contact cases, if allowed. |
| `data.phones[].phone_type` | Supported phone type | `MOBILE` for the example pattern. |
| `data.phones[].phone_number` | Safe synthetic phone | Use a reserved test number format accepted by the target system; never use a real person's number. |
| `data.phones[].is_primary` | Boolean | `true` for the primary phone; only one phone should be primary. |
| `data.communication_preferences` | Array of preference objects | Include an SMS preference for SMS scenarios; use disabled/omitted preference for opt-out scenarios. |
| `data.communication_preferences[].channel` | Supported channel | `SMS` for the supplied example. |
| `data.communication_preferences[].value` | Contact value | Must match the phone number used for SMS. |
| `data.communication_preferences[].enabled` | Boolean | `true` when SMS is enabled; `false` for an opt-out case if supported. |
| `data.communication_preferences[].preference_type` | Supported preference type | `PRIMARY` for the supplied example. |

## Prescription `data` fields

| JSON path | Pattern | Example / relationship rule |
|---|---|---|
| `data.member_id` | Membership foreign key | Must equal the paired membership's `data.member_id`. |
| `data.prescription_id` | Unique synthetic prescription ID | `PBM7-RX-{YYYYMMDD}-{sequence}`. |
| `data.rx_number` | Prescription number | Unique per prescription; can match `prescription_id` if accepted by the target schema. |
| `data.drug_name` | Synthetic medication name | Use a fictional test medication, or a valid test fixture when drug lookup is enforced. |
| `data.strength` | Medication strength | Use a schema-valid value such as `25 MG`; keep consistent with the chosen medication. |
| `data.manufacturer` | Synthetic manufacturer | Use a fictional/test value such as `TestIQ Synthetic Pharmacy`. |
| `data.therapy_code` | Supported therapy code | Use `PBM-REFILL` for refill scenarios when accepted by the target system. |
| `data.status` | Supported prescription status | `ACTIVE` for an active prescription; use a supported inactive status for negative cases. |
| `data.eligible_ind` | Boolean eligibility flag | `true` for eligible cases, `false` for ineligible cases. Must agree with the test case. |
| `data.next_refill_date` | ISO date (`YYYY-MM-DD`) | Set on/before the campaign date for due cases; after it for not-yet-due cases. |
| `data.refills_allowed` | Nonnegative integer | Total refills allowed by the scenario. |
| `data.refills_remaining` | Nonnegative integer | Usually between `0` and `refills_allowed`; use `0` for no-refills-left scenarios. |
| `data.autofill_enrolled_ind` | Boolean enrollment flag | `false` for not enrolled; `true` for enrolled scenarios. |
| `data.source_event_id` | Event ID | Match the membership event ID when both rows are part of the same event. |
| `data.source_correlation_id` | Correlation ID | Match the membership correlation ID for the same scenario. |

## Scenario patterns

| Scenario | Membership values | Prescription values |
|---|---|---|
| Refill due and contactable | `status: ACTIVE`; primary mobile; SMS preference enabled | `status: ACTIVE`; `eligible_ind: true`; refill date on/before campaign date; remaining refills greater than `0` |
| Refill not yet due | Active member with SMS enabled | Next refill date after campaign date |
| SMS opt-out / no contact | Active member; SMS preference disabled or absent | Keep prescription eligible and due if testing contact suppression specifically |
| Ineligible member or prescription | Use the status required by the test case | `eligible_ind: false` or supported inactive status |
| No refills remaining | Active member | `refills_remaining: 0` |
| Autofill enrolled | Active member | `autofill_enrolled_ind: true` |

## Consistency checks

- The two records for one scenario share `load_id`, `member_id`, `source_event_id`, and `source_correlation_id` when the ingestion contract expects them to represent one linked event.
- IDs must be unique across generated scenarios. Keep `member_id`, `prescription_id`, and `rx_number` distinct where the target system requires distinct identifiers.
- Keep phone and SMS preference values identical.
- Keep dates, eligibility, membership status, and refill counts aligned with the test case's expected outcome.
- The test-data generator reads these rules when it creates draft Membership and Prescription payloads. Outer Bronze columns such as `id`, `load_id`, and `created_time` are assigned during ingestion, not invented by the generator.
