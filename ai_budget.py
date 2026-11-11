"""Persistent, fail-closed AXHub AI reservations. Never calls a model API.

History import is an explicit operator step, not startup auto-seeding. A separate
advisory-lock key avoids deadlock when status is read inside an app transaction.
This ledger is app-wide, not an account-wide provider billing cap.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from persistence import PostgresConnection

POLICY = 'anthropic-demo-20261112'
MODEL = 'claude-haiku-4-5-20251001'
KNOWN_MINIMUM = 210240
LOCK_KEY = 718401217
SUPPLEMENTAL_ID = 'aurora-supplemental-onecall-20261110'
SUPPLEMENTAL_EXPIRES = '2026-11-12T00:00:00+00:00'
SOURCE_FILE_SHA256 = '253a3ef0a38baa058d5ce120347098b4a07b004e236719f1e6d8fd96ba2e3713'
OBSERVED_ROWS = [[1, '2026-11-09T14:23:32.049664Z', MODEL, 210240]]
FOLLOWUP_ID = 'aurora-diagnostic-onecall-20261111'
PREDECESSOR_CLAIM_ID = '3ae3c895-d359-4146-8f69-16469512b005'
PREDECESSOR_CLAIM_TIME = '2026-11-10T23:59:40.318916+00:00'
PREDECESSOR_FINGERPRINT = 'bb643dbbdffd8c5a56186a0a29d3547225e5042482fc3a8566a24d2bc63195bf'
PREDECESSOR_READBACK_SHA256 = '9665db94547c87c3768aa86f29cad9f22132e2f050a504d5b26c7b20449f6ad2'
REPAIR_ID = 'aurora-schema-repair-pool-20261111'
FOLLOWUP_CLAIM_ID = 'a72afaea-e656-4461-9cd5-0739fae69834'
FOLLOWUP_CLAIM_TIME = '2026-11-11T00:34:22.140277+00:00'
FOLLOWUP_FINGERPRINT = 'cc3bbf482f0ac3d244aecbe012ac1f62a8b29c8cb7aed311558f75a9ae7782c4'
CONSUMED_READBACK_SHA256 = '19300335072033e2996f9b55c2bc6ee711acc61b626e3c274f0cf59aa7501eb0'
REPAIR_MAX_ATTEMPTS = 5
REPAIR_MAX_RESERVED = 1051200
KNOWN_NEW_MAX_ATTEMPTS = 7
KNOWN_NEW_MAX_RESERVED = 1471680
CLAIM_HASH_FIELDS = ('pool_id', 'ordinal', 'claim_id', 'created_at', 'model',
                     'reserved_microusd', 'snapshot_sha256', 'previous_claim_sha256')



class BudgetError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != {'version', 'policy', 'model', 'prior_attempts', 'prior_reserved_microusd', 'source_rows_sha256'}:
        raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
    if value['version'] != 1 or value['policy'] != POLICY or value['model'] != MODEL:
        raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
    if (type(value['prior_attempts']) is not int or not 1 <= value['prior_attempts'] <= 1000000
            or type(value['prior_reserved_microusd']) is not int
            or not KNOWN_MINIMUM <= value['prior_reserved_microusd'] <= 10**15
            or not isinstance(value['source_rows_sha256'], str)
            or len(value['source_rows_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['source_rows_sha256'])):
        raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
    return value


def export_history(path):
    """Read an explicitly selected local ledger only; never read credentials."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {'ai_reservations', 'aurora_budget_imports', 'sqlite_sequence'}:
            raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
        rows = list(db.execute('SELECT id,created_at,model,reserved_microusd FROM ai_reservations ORDER BY id'))
        if not rows or any(r[2] != MODEL or type(r[3]) is not int or r[3] <= 0 for r in rows):
            raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
        return validate_snapshot({'version': 1, 'policy': POLICY, 'model': MODEL,
            'prior_attempts': len(rows), 'prior_reserved_microusd': sum(r[3] for r in rows),
            'source_rows_sha256': hashlib.sha256(canonical(rows).encode()).hexdigest()})


def supplemental_snapshot():
    """Fixed, reviewed scope. Neither observed reservations nor holds are invoices."""
    return {
        'version': 1, 'authorization_id': SUPPLEMENTAL_ID, 'policy': POLICY,
        'model': MODEL, 'expires_at': SUPPLEMENTAL_EXPIRES,
        'maximum_new_attempts': 1, 'maximum_new_reserved_microusd': 210240,
        'history_reconciled': False,
        'observed_history': {
            'source_file_sha256': SOURCE_FILE_SHA256,
            'source_rows_sha256': hashlib.sha256(canonical(OBSERVED_ROWS).encode()).hexdigest(),
            'rows': [list(row) for row in OBSERVED_ROWS],
            'attempts': 1, 'reserved_microusd': 210240,
        },
        'uncertainty_reserve': {
            'assumed_additional_attempts': 2, 'reserved_microusd': 420480,
            'is_observed_history': False, 'bounds_account_usage': False,
        },
        'basis': 'One separately bounded additional attempt under the approved USD 100 promotional-credit scope; recovered historical ledger is incomplete. No account-wide usage bound is asserted.',
    }


def validate_supplemental(value):
    # Canonical comparison also rejects bools in integer fields and extra fields.
    if canonical(value) != canonical(supplemental_snapshot()):
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    return value


def prepare_supplemental(path, authorization_reviewed=False):
    """Offline evidence check. Never reads credentials or modifies the source DB."""
    if authorization_reviewed is not True:
        raise BudgetError('AI_SUPPLEMENTAL_REVIEW_REQUIRED')
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    if hashlib.sha256(path.read_bytes()).hexdigest() != SOURCE_FILE_SHA256:
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    observed = export_history(path)  # Validates SQLite integrity, schema and rows.
    expected = supplemental_snapshot()
    if (observed['prior_attempts'] != 1 or observed['prior_reserved_microusd'] != 210240
            or observed['source_rows_sha256'] != expected['observed_history']['source_rows_sha256']):
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    # Recheck bytes after the read. A changing ledger is not accepted as evidence.
    if hashlib.sha256(path.read_bytes()).hexdigest() != SOURCE_FILE_SHA256:
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    return expected


def supplemental_import_sql(snapshot):
    """One atomic SELECT/INSERT statement for official AXHub dbexec, after review.

    Schema is created by normal app migration, never by this operator payload.
    No update/upsert or repair path exists. Both new records commit or neither
    does. A repeat can report already_imported only when all receipts/claims agree.
    The unique keys remain protective even if concurrent statement snapshots are
    older than the advisory-lock acquisition. A unique violation rolls back.
    """
    validate_supplemental(snapshot)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    sid, policy, model = map(quote, (SUPPLEMENTAL_ID, POLICY, MODEL))
    data = quote(canonical(snapshot))
    fingerprint = quote(hashlib.sha256(canonical(snapshot).encode()).hexdigest())
    expiry = quote(SUPPLEMENTAL_EXPIRES)
    return f"""-- Reviewed one-use supplemental authorization. No model call; no credentials.
-- History remains incomplete. Reservations and uncertainty holds are not billing.
WITH budget_lock AS MATERIALIZED (
    SELECT pg_advisory_xact_lock({LOCK_KEY})
), checks AS MATERIALIZED (
    SELECT
      NOT EXISTS (SELECT 1 FROM ai_cloud_budget_history)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_budget_reservations)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_followup_authorizations)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_followup_receipts)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_followup_claims)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_authorizations)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_receipts)
        AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_claims) AS no_complete_history,
      (SELECT COUNT(*) FROM ai_cloud_supplemental_authorizations) AS auth_count,
      (SELECT COUNT(*) FROM ai_cloud_supplemental_receipts) AS receipt_count,
      (SELECT COUNT(*) FROM ai_cloud_supplemental_claims) AS claim_count,
      EXISTS (
        SELECT 1 FROM ai_cloud_supplemental_authorizations a
        JOIN ai_cloud_supplemental_receipts r ON r.authorization_id=a.authorization_id
        WHERE a.authorization_id={sid} AND a.policy={policy}
          AND a.snapshot_sha256={fingerprint} AND a.snapshot_json={data}
          AND r.snapshot_sha256=a.snapshot_sha256 AND r.imported_at=a.imported_at
          AND a.imported_at::timestamptz < {expiry}::timestamptz
          AND a.imported_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
          AND ((a.consumed_claim_id IS NULL
                AND NOT EXISTS (SELECT 1 FROM ai_cloud_supplemental_claims))
            OR (a.consumed_claim_id IS NOT NULL
                AND (SELECT COUNT(*) FROM ai_cloud_supplemental_claims)=1
                AND EXISTS (SELECT 1 FROM ai_cloud_supplemental_claims c
                  WHERE c.authorization_id=a.authorization_id
                    AND c.claim_id=a.consumed_claim_id AND c.claim_id<>''
                    AND c.snapshot_sha256=a.snapshot_sha256
                    AND c.created_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
                    AND c.model={model} AND c.reserved_microusd=210240
                    AND c.created_at::timestamptz < {expiry}::timestamptz
                    AND c.created_at::timestamptz >= a.imported_at::timestamptz)))
      ) AS valid_existing
    FROM budget_lock
), decision AS MATERIALIZED (
    SELECT CASE
      WHEN no_complete_history AND auth_count=0 AND receipt_count=0 AND claim_count=0
        AND clock_timestamp() < {expiry}::timestamptz - INTERVAL '60 seconds' THEN 'import'
      WHEN no_complete_history AND auth_count=1 AND receipt_count=1 AND valid_existing
        THEN 'already_imported'
      ELSE 'blocked' END AS result
    FROM checks
), new_authorization AS (
    INSERT INTO ai_cloud_supplemental_authorizations
      (authorization_id,policy,snapshot_sha256,snapshot_json,consumed_claim_id,imported_at)
    SELECT {sid},{policy},{fingerprint},{data},NULL,clock_timestamp()::text
    FROM decision WHERE result='import'
    RETURNING authorization_id,snapshot_sha256,imported_at
), new_receipt AS (
    INSERT INTO ai_cloud_supplemental_receipts(authorization_id,snapshot_sha256,imported_at)
    SELECT authorization_id,snapshot_sha256,imported_at FROM new_authorization
    RETURNING authorization_id
)
SELECT CASE WHEN result='import' AND (SELECT COUNT(*) FROM new_receipt)=1
            THEN 'imported' ELSE result END AS supplemental_import_status,
       {sid} AS authorization_id, false AS history_reconciled, false AS model_called
FROM decision;
"""


def followup_snapshot():
    """Exactly one named successor, not a configurable reset/rollover facility."""
    return {
        'version': 1, 'authorization_id': FOLLOWUP_ID, 'policy': POLICY,
        'model': MODEL, 'expires_at': SUPPLEMENTAL_EXPIRES,
        'maximum_new_attempts': 1, 'maximum_new_reserved_microusd': 210240,
        'maximum_output_tokens': 2048,
        'cumulative_supplemental_maximum_attempts': 2,
        'cumulative_supplemental_maximum_reserved_microusd': 420480,
        'history_reconciled': False,
        'predecessor': {
            'authorization_id': SUPPLEMENTAL_ID, 'snapshot_sha256': PREDECESSOR_FINGERPRINT,
            'claim_id': PREDECESSOR_CLAIM_ID, 'created_at': PREDECESSOR_CLAIM_TIME,
            'reserved_microusd': 210240, 'readback_sha256': PREDECESSOR_READBACK_SHA256,
        },
        'observed_history': supplemental_snapshot()['observed_history'],
        'uncertainty_reserve': supplemental_snapshot()['uncertainty_reserve'],
        'basis': 'One separately scoped diagnostic follow-up within the approved USD 100 promotional-credit task. Preserve the failed consumed predecessor; no account-wide historical usage bound or recovered provider response is asserted.',
    }


def validate_followup(snapshot):
    if canonical(snapshot) != canonical(followup_snapshot()):
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    return snapshot


def prepare_followup(path, authorization_reviewed=False):
    """Offline read of the reviewed, non-secret official DB readback artifact."""
    if authorization_reviewed is not True:
        raise BudgetError('AI_SUPPLEMENTAL_REVIEW_REQUIRED')
    path = Path(path)
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != PREDECESSOR_READBACK_SHA256:
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    return followup_snapshot()


def followup_import_sql(snapshot):
    """Atomic explicit successor import. Never modifies the predecessor records."""
    validate_followup(snapshot)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    sid, prior, policy, model, claim = map(quote, (FOLLOWUP_ID, SUPPLEMENTAL_ID, POLICY, MODEL, PREDECESSOR_CLAIM_ID))
    content = quote(canonical(snapshot))
    fingerprint = quote(hashlib.sha256(canonical(snapshot).encode()).hexdigest())
    prior_content, prior_fingerprint = quote(canonical(supplemental_snapshot())), quote(PREDECESSOR_FINGERPRINT)
    expiry, prior_time = quote(SUPPLEMENTAL_EXPIRES), quote(PREDECESSOR_CLAIM_TIME)
    return f"""-- Explicit single diagnostic successor. No existing record is updated or reset.
-- Known supplemental total at most 2 attempts / USD 0.42048; not account usage.
WITH budget_lock AS MATERIALIZED (SELECT pg_advisory_xact_lock({LOCK_KEY})),
checks AS MATERIALIZED (
 SELECT
   NOT EXISTS (SELECT 1 FROM ai_cloud_budget_history)
     AND NOT EXISTS (SELECT 1 FROM ai_cloud_budget_reservations)
     AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_authorizations)
     AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_receipts)
     AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_claims)
     AND (SELECT COUNT(*) FROM ai_cloud_supplemental_authorizations)=1
     AND (SELECT COUNT(*) FROM ai_cloud_supplemental_receipts)=1
     AND (SELECT COUNT(*) FROM ai_cloud_supplemental_claims)=1
     AND EXISTS (
       SELECT 1 FROM ai_cloud_supplemental_authorizations a
       JOIN ai_cloud_supplemental_receipts r USING (authorization_id)
       JOIN ai_cloud_supplemental_claims c USING (authorization_id)
       WHERE a.authorization_id={prior} AND a.policy={policy}
         AND a.snapshot_sha256={prior_fingerprint} AND a.snapshot_json={prior_content}
         AND r.snapshot_sha256=a.snapshot_sha256 AND r.imported_at=a.imported_at
         AND c.snapshot_sha256=a.snapshot_sha256 AND c.claim_id={claim}
         AND a.consumed_claim_id=c.claim_id AND c.model={model} AND c.reserved_microusd=210240
         AND c.created_at::timestamptz={prior_time}::timestamptz
         AND a.imported_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
         AND c.created_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
         AND a.imported_at::timestamptz<=c.created_at::timestamptz
         AND c.created_at::timestamptz<{expiry}::timestamptz
     ) AS predecessor_valid,
   (SELECT COUNT(*) FROM ai_cloud_followup_authorizations) AS auth_count,
   (SELECT COUNT(*) FROM ai_cloud_followup_receipts) AS receipt_count,
   (SELECT COUNT(*) FROM ai_cloud_followup_claims) AS claim_count,
   EXISTS (
     SELECT 1 FROM ai_cloud_followup_authorizations a
     JOIN ai_cloud_followup_receipts r USING (authorization_id)
     WHERE a.authorization_id={sid} AND a.policy={policy}
       AND a.snapshot_sha256={fingerprint} AND a.snapshot_json={content}
       AND a.predecessor_claim_id={claim}
       AND r.snapshot_sha256=a.snapshot_sha256 AND r.imported_at=a.imported_at
       AND a.imported_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
       AND a.imported_at::timestamptz>={prior_time}::timestamptz
       AND a.imported_at::timestamptz<{expiry}::timestamptz
       AND ((a.consumed_claim_id IS NULL AND NOT EXISTS (SELECT 1 FROM ai_cloud_followup_claims))
         OR (a.consumed_claim_id IS NOT NULL AND (SELECT COUNT(*) FROM ai_cloud_followup_claims)=1
           AND EXISTS (SELECT 1 FROM ai_cloud_followup_claims c
             WHERE c.authorization_id=a.authorization_id AND c.claim_id=a.consumed_claim_id
               AND c.claim_id<>'' AND c.snapshot_sha256=a.snapshot_sha256
               AND c.model={model} AND c.reserved_microusd=210240
               AND c.created_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
               AND c.created_at::timestamptz>=a.imported_at::timestamptz
               AND c.created_at::timestamptz<{expiry}::timestamptz)))
   ) AS existing_valid
 FROM budget_lock
), decision AS MATERIALIZED (
 SELECT CASE
   WHEN predecessor_valid AND auth_count=0 AND receipt_count=0 AND claim_count=0
     AND clock_timestamp()>={prior_time}::timestamptz
     AND clock_timestamp()<{expiry}::timestamptz-INTERVAL '60 seconds' THEN 'import'
   WHEN predecessor_valid AND auth_count=1 AND receipt_count=1 AND existing_valid THEN 'already_imported'
   ELSE 'blocked' END AS result
 FROM checks
), new_authorization AS (
 INSERT INTO ai_cloud_followup_authorizations
   (authorization_id,policy,snapshot_sha256,snapshot_json,predecessor_claim_id,consumed_claim_id,imported_at)
 SELECT {sid},{policy},{fingerprint},{content},{claim},NULL,clock_timestamp()::text
 FROM decision WHERE result='import'
 RETURNING authorization_id,snapshot_sha256,imported_at
), new_receipt AS (
 INSERT INTO ai_cloud_followup_receipts(authorization_id,snapshot_sha256,imported_at)
 SELECT authorization_id,snapshot_sha256,imported_at FROM new_authorization
 RETURNING authorization_id
)
SELECT CASE WHEN result='import' AND (SELECT COUNT(*) FROM new_receipt)=1 THEN 'imported'
            ELSE result END AS followup_import_status,
       {sid} AS authorization_id,false AS history_reconciled,false AS model_called
FROM decision;
"""


def repair_snapshot():
    """One finite five-attempt repair pool bound to both spent predecessors."""
    return {
        'version': 1, 'pool_id': REPAIR_ID, 'policy': POLICY, 'model': MODEL,
        'expires_at': SUPPLEMENTAL_EXPIRES, 'maximum_output_tokens': 2048,
        'maximum_pool_attempts': REPAIR_MAX_ATTEMPTS,
        'maximum_pool_reserved_microusd': REPAIR_MAX_RESERVED,
        'per_attempt_reserved_microusd': 210240,
        'known_new_maximum_attempts': KNOWN_NEW_MAX_ATTEMPTS,
        'known_new_maximum_reserved_microusd': KNOWN_NEW_MAX_RESERVED,
        'history_reconciled': False, 'automatic_retries': 0,
        'predecessors': [
            {'authorization_id': SUPPLEMENTAL_ID, 'claim_id': PREDECESSOR_CLAIM_ID,
             'created_at': PREDECESSOR_CLAIM_TIME, 'snapshot_sha256': PREDECESSOR_FINGERPRINT,
             'reserved_microusd': 210240},
            {'authorization_id': FOLLOWUP_ID, 'claim_id': FOLLOWUP_CLAIM_ID,
             'created_at': FOLLOWUP_CLAIM_TIME, 'snapshot_sha256': FOLLOWUP_FINGERPRINT,
             'reserved_microusd': 210240},
        ],
        'consumed_readback_sha256': CONSUMED_READBACK_SHA256,
        'observed_history': supplemental_snapshot()['observed_history'],
        'uncertainty_reserve': supplemental_snapshot()['uncertainty_reserve'],
        'basis': 'A finite repair pool within the approved USD 100 task, after preserving both failed consumed attempts. Each additional request is manually initiated and permanently reserved. No automatic retries, historical reconciliation or account-wide usage bound is asserted.',
    }


def validate_repair(snapshot):
    if canonical(snapshot) != canonical(repair_snapshot()):
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    return snapshot


def prepare_repair(path, authorization_reviewed=False):
    if authorization_reviewed is not True:
        raise BudgetError('AI_SUPPLEMENTAL_REVIEW_REQUIRED')
    path = Path(path)
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != CONSUMED_READBACK_SHA256:
        raise BudgetError('AI_SUPPLEMENTAL_CONFLICT')
    return repair_snapshot()


def claim_digest(claim):
    # Fixed fields have delimiter-free UUID/model/hash/time/integer encodings.
    return hashlib.sha256('|'.join(str(claim[key]) for key in CLAIM_HASH_FIELDS).encode()).hexdigest()


def repair_import_sql(snapshot):
    """Atomic explicit pool import, using built-in PostgreSQL SHA256 for integrity.

    All old records remain read-only. Reimport validates every existing ordinal,
    chain hash and counter and can never refill or reset the pool.
    """
    validate_repair(snapshot)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    pool, policy, model, prior_claim = map(quote, (REPAIR_ID, POLICY, MODEL, FOLLOWUP_CLAIM_ID))
    content = quote(canonical(snapshot))
    fingerprint = quote(hashlib.sha256(canonical(snapshot).encode()).hexdigest())
    expiry, prior_time = quote(SUPPLEMENTAL_EXPIRES), quote(FOLLOWUP_CLAIM_TIME)
    def spent(prefix, expected, claim_id, claim_time, predecessor=None):
        aid = quote(expected.get('authorization_id'))
        text = quote(canonical(expected)); sha = quote(hashlib.sha256(canonical(expected).encode()).hexdigest())
        cid, ctime = quote(claim_id), quote(claim_time)
        link = (' AND a.predecessor_claim_id=' + quote(predecessor)
                + ' AND a.imported_at::timestamptz>=' + quote(PREDECESSOR_CLAIM_TIME) + '::timestamptz') if predecessor else ''
        return f"""(SELECT COUNT(*) FROM {prefix}_authorizations)=1
 AND (SELECT COUNT(*) FROM {prefix}_receipts)=1
 AND (SELECT COUNT(*) FROM {prefix}_claims)=1
 AND EXISTS (SELECT 1 FROM {prefix}_authorizations a
 JOIN {prefix}_receipts r USING (authorization_id)
 JOIN {prefix}_claims c USING (authorization_id)
 WHERE a.authorization_id={aid} AND a.policy={policy}
 AND a.snapshot_sha256={sha} AND a.snapshot_json={text}{link}
 AND r.snapshot_sha256=a.snapshot_sha256 AND r.imported_at=a.imported_at
 AND c.claim_id={cid} AND a.consumed_claim_id=c.claim_id
 AND c.snapshot_sha256=a.snapshot_sha256 AND c.model={model} AND c.reserved_microusd=210240
 AND c.created_at::timestamptz={ctime}::timestamptz
 AND a.imported_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
 AND c.created_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
 AND a.imported_at::timestamptz<=c.created_at::timestamptz
 AND c.created_at::timestamptz<{expiry}::timestamptz)"""
    old_valid = spent('ai_cloud_supplemental', supplemental_snapshot(), PREDECESSOR_CLAIM_ID, PREDECESSOR_CLAIM_TIME)
    second_valid = spent('ai_cloud_followup', followup_snapshot(), FOLLOWUP_CLAIM_ID, FOLLOWUP_CLAIM_TIME, PREDECESSOR_CLAIM_ID)
    return f"""-- One fixed repair pool: max 5 additional attempts / USD 1.0512 reserved.
-- Previous two consumed attempts are read-only; known-new aggregate max 7 / USD 1.47168.
WITH budget_lock AS MATERIALIZED (SELECT pg_advisory_xact_lock({LOCK_KEY})),
checks AS MATERIALIZED (
 SELECT NOT EXISTS (SELECT 1 FROM ai_cloud_budget_history)
   AND NOT EXISTS (SELECT 1 FROM ai_cloud_budget_reservations)
   AND {old_valid} AND {second_valid} AS predecessors_valid,
 (SELECT COUNT(*) FROM ai_cloud_repair_authorizations) AS auth_count,
 (SELECT COUNT(*) FROM ai_cloud_repair_receipts) AS receipt_count,
 (SELECT COUNT(*) FROM ai_cloud_repair_claims) AS claim_count,
 EXISTS (SELECT 1 FROM ai_cloud_repair_authorizations a
   JOIN ai_cloud_repair_receipts r USING (pool_id)
   WHERE a.pool_id={pool} AND a.policy={policy}
   AND a.snapshot_sha256={fingerprint} AND a.snapshot_json={content}
   AND a.predecessor_claim_id={prior_claim}
   AND r.snapshot_sha256=a.snapshot_sha256 AND r.imported_at=a.imported_at
   AND a.imported_at ~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
   AND a.imported_at::timestamptz>={prior_time}::timestamptz
   AND a.imported_at::timestamptz<{expiry}::timestamptz
   AND a.consumed_count BETWEEN 0 AND 5
   AND a.consumed_count=(SELECT COUNT(*) FROM ai_cloud_repair_claims)
   AND a.consumed_microusd=a.consumed_count*210240
   AND a.consumed_microusd=(SELECT COALESCE(SUM(reserved_microusd),0) FROM ai_cloud_repair_claims)
   AND ((a.consumed_count=0 AND a.last_claim_id IS NULL AND a.last_claim_sha256 IS NULL)
     OR (a.consumed_count>0
       AND (SELECT MIN(ordinal) FROM ai_cloud_repair_claims)=1
       AND (SELECT MAX(ordinal) FROM ai_cloud_repair_claims)=a.consumed_count
       AND EXISTS (SELECT 1 FROM ai_cloud_repair_claims c
         WHERE c.pool_id=a.pool_id AND c.ordinal=a.consumed_count
         AND c.claim_id=a.last_claim_id AND c.claim_sha256=a.last_claim_sha256)))
   AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_claims c
     WHERE c.pool_id<>a.pool_id OR c.snapshot_sha256<>a.snapshot_sha256
       OR c.ordinal NOT BETWEEN 1 AND 5 OR c.model<>{model} OR c.reserved_microusd<>210240
       OR c.claim_id !~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
       OR c.created_at !~ '(Z|[+-][0-9]{{2}}(:[0-9]{{2}})?)$'
       OR c.created_at::timestamptz<a.imported_at::timestamptz
       OR c.created_at::timestamptz>={expiry}::timestamptz
       OR c.claim_sha256<>encode(sha256(convert_to(concat_ws('|',c.pool_id,c.ordinal::text,c.claim_id,c.created_at,c.model,c.reserved_microusd::text,c.snapshot_sha256,c.previous_claim_sha256),'UTF8')),'hex')
       OR (c.ordinal=1 AND c.previous_claim_sha256<>a.snapshot_sha256)
       OR (c.ordinal>1 AND NOT EXISTS (SELECT 1 FROM ai_cloud_repair_claims previous
         WHERE previous.pool_id=c.pool_id AND previous.ordinal=c.ordinal-1
         AND c.previous_claim_sha256=previous.claim_sha256
         AND c.created_at::timestamptz>=previous.created_at::timestamptz)))
 ) AS existing_valid
 FROM budget_lock
), decision AS MATERIALIZED (
 SELECT CASE WHEN predecessors_valid AND auth_count=0 AND receipt_count=0 AND claim_count=0
   AND clock_timestamp()>={prior_time}::timestamptz
   AND clock_timestamp()<{expiry}::timestamptz-INTERVAL '60 seconds' THEN 'import'
 WHEN predecessors_valid AND auth_count=1 AND receipt_count=1 AND existing_valid THEN 'already_imported'
 ELSE 'blocked' END AS result FROM checks
), new_authorization AS (
 INSERT INTO ai_cloud_repair_authorizations
 (pool_id,policy,snapshot_sha256,snapshot_json,predecessor_claim_id,consumed_count,consumed_microusd,last_claim_id,last_claim_sha256,imported_at)
 SELECT {pool},{policy},{fingerprint},{content},{prior_claim},0,0,NULL,NULL,clock_timestamp()::text
 FROM decision WHERE result='import' RETURNING pool_id,snapshot_sha256,imported_at
), new_receipt AS (
 INSERT INTO ai_cloud_repair_receipts(pool_id,snapshot_sha256,imported_at)
 SELECT pool_id,snapshot_sha256,imported_at FROM new_authorization RETURNING pool_id
)
SELECT CASE WHEN result='import' AND (SELECT COUNT(*) FROM new_receipt)=1 THEN 'imported' ELSE result END AS repair_import_status,
 {pool} AS pool_id,false AS history_reconciled,false AS model_called FROM decision;
"""


class CloudBudgetLedger:
    def __init__(self, database_url, connector=None):
        if not database_url.startswith(('postgresql://', 'postgres://')):
            raise ValueError('Cloud budget requires PostgreSQL; SQLite fallback is forbidden')
        self.database_url = database_url
        self.connector = connector

    def connect(self, url=None):
        db = PostgresConnection(url or self.database_url, connector=self.connector, lock_key=LOCK_KEY)
        try:
            db.raw.execute("SET LOCAL lock_timeout = '3s'")
            db.raw.execute("SET LOCAL statement_timeout = '5s'")
        except Exception:
            db.close()
            raise
        return db

    def initialize(self, migration_url=None):
        with self.connect(migration_url) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS ai_cloud_budget_history (
                policy TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL,
                prior_attempts INTEGER NOT NULL CHECK(prior_attempts > 0),
                prior_reserved_microusd BIGINT NOT NULL CHECK(prior_reserved_microusd >= 210240),
                imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_budget_reservations (
                id TEXT PRIMARY KEY, policy TEXT NOT NULL REFERENCES ai_cloud_budget_history(policy),
                created_at TEXT NOT NULL, model TEXT NOT NULL,
                attempts INTEGER NOT NULL CHECK(attempts > 0),
                reserved_microusd BIGINT NOT NULL CHECK(reserved_microusd > 0));
            CREATE TABLE IF NOT EXISTS ai_cloud_supplemental_authorizations (
                authorization_id TEXT PRIMARY KEY,
                policy TEXT UNIQUE NOT NULL, snapshot_sha256 TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, consumed_claim_id TEXT UNIQUE,
                imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_supplemental_receipts (
                authorization_id TEXT PRIMARY KEY REFERENCES ai_cloud_supplemental_authorizations(authorization_id),
                snapshot_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_supplemental_claims (
                authorization_id TEXT PRIMARY KEY REFERENCES ai_cloud_supplemental_authorizations(authorization_id),
                claim_id TEXT UNIQUE NOT NULL, snapshot_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL CHECK(model = 'claude-haiku-4-5-20251001'),
                reserved_microusd BIGINT NOT NULL CHECK(reserved_microusd = 210240));
            CREATE TABLE IF NOT EXISTS ai_cloud_followup_authorizations (
                authorization_id TEXT PRIMARY KEY, policy TEXT UNIQUE NOT NULL,
                snapshot_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                predecessor_claim_id TEXT NOT NULL REFERENCES ai_cloud_supplemental_claims(claim_id),
                consumed_claim_id TEXT UNIQUE, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_followup_receipts (
                authorization_id TEXT PRIMARY KEY REFERENCES ai_cloud_followup_authorizations(authorization_id),
                snapshot_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_followup_claims (
                authorization_id TEXT PRIMARY KEY REFERENCES ai_cloud_followup_authorizations(authorization_id),
                claim_id TEXT UNIQUE NOT NULL, snapshot_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL CHECK(model = 'claude-haiku-4-5-20251001'),
                reserved_microusd BIGINT NOT NULL CHECK(reserved_microusd = 210240));
            CREATE TABLE IF NOT EXISTS ai_cloud_repair_authorizations (
                pool_id TEXT PRIMARY KEY, policy TEXT UNIQUE NOT NULL,
                snapshot_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                predecessor_claim_id TEXT NOT NULL REFERENCES ai_cloud_followup_claims(claim_id),
                consumed_count INTEGER NOT NULL CHECK(consumed_count BETWEEN 0 AND 5),
                consumed_microusd BIGINT NOT NULL CHECK(consumed_microusd BETWEEN 0 AND 1051200),
                last_claim_id TEXT UNIQUE, last_claim_sha256 TEXT, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_repair_receipts (
                pool_id TEXT PRIMARY KEY REFERENCES ai_cloud_repair_authorizations(pool_id),
                snapshot_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_cloud_repair_claims (
                pool_id TEXT NOT NULL REFERENCES ai_cloud_repair_authorizations(pool_id),
                ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 5),
                claim_id TEXT UNIQUE NOT NULL, snapshot_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL, model TEXT NOT NULL CHECK(model = 'claude-haiku-4-5-20251001'),
                reserved_microusd BIGINT NOT NULL CHECK(reserved_microusd = 210240),
                previous_claim_sha256 TEXT NOT NULL, claim_sha256 TEXT NOT NULL,
                PRIMARY KEY(pool_id,ordinal));
            ''')

    @staticmethod
    def mode(db):
        complete = (db.execute('SELECT 1 FROM ai_cloud_budget_history LIMIT 1').fetchone()
                    or db.execute('SELECT 1 FROM ai_cloud_budget_reservations LIMIT 1').fetchone())
        supplemental = any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
            for table in ('ai_cloud_supplemental_authorizations', 'ai_cloud_supplemental_receipts', 'ai_cloud_supplemental_claims'))
        followup = CloudBudgetLedger.followup_present(db)
        repair = CloudBudgetLedger.repair_present(db)
        if complete and (supplemental or followup or repair):
            raise BudgetError('AI_BUDGET_MODE_CONFLICT')
        return 'repair' if repair else 'followup' if followup else 'supplemental' if supplemental else 'complete'

    @staticmethod
    def repair_present(db):
        return any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
            for table in ('ai_cloud_repair_authorizations', 'ai_cloud_repair_receipts', 'ai_cloud_repair_claims'))

    @staticmethod
    def consumed_predecessors(db):
        authorization, claims = CloudBudgetLedger.followup_state(db)
        if (len(claims) != 1 or claims[0]['claim_id'] != FOLLOWUP_CLAIM_ID
                or claims[0]['snapshot_sha256'] != FOLLOWUP_FINGERPRINT
                or datetime.fromisoformat(claims[0]['created_at']) != datetime.fromisoformat(FOLLOWUP_CLAIM_TIME)):
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        return authorization, claims

    @staticmethod
    def repair_state(db):
        CloudBudgetLedger.consumed_predecessors(db)
        authorizations = db.execute('SELECT * FROM ai_cloud_repair_authorizations').fetchall()
        receipts = db.execute('SELECT * FROM ai_cloud_repair_receipts').fetchall()
        claims = db.execute('SELECT * FROM ai_cloud_repair_claims ORDER BY ordinal').fetchall()
        if len(authorizations) != 1 or len(receipts) != 1 or len(claims) > REPAIR_MAX_ATTEMPTS:
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        authorization, receipt = authorizations[0], receipts[0]
        content = canonical(repair_snapshot())
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        if (authorization['pool_id'] != REPAIR_ID or authorization['policy'] != POLICY
                or authorization['snapshot_json'] != content or authorization['snapshot_sha256'] != fingerprint
                or authorization['predecessor_claim_id'] != FOLLOWUP_CLAIM_ID
                or receipt['pool_id'] != REPAIR_ID or receipt['snapshot_sha256'] != fingerprint
                or receipt['imported_at'] != authorization['imported_at']):
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        try:
            imported = datetime.fromisoformat(authorization['imported_at'])
            if (imported.tzinfo is None or imported < datetime.fromisoformat(FOLLOWUP_CLAIM_TIME)
                    or imported >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES)):
                raise ValueError()
            count, used = authorization['consumed_count'], authorization['consumed_microusd']
            if (type(count) is not int or count != len(claims) or not 0 <= count <= REPAIR_MAX_ATTEMPTS
                    or type(used) is not int or used != count * 210240
                    or used != sum(c['reserved_microusd'] for c in claims)
                    or used > REPAIR_MAX_RESERVED or 2 + count > KNOWN_NEW_MAX_ATTEMPTS
                    or 420480 + used > KNOWN_NEW_MAX_RESERVED):
                raise ValueError()
            previous_hash, previous_time = fingerprint, imported
            for ordinal, claim in enumerate(claims, 1):
                created = datetime.fromisoformat(claim['created_at'])
                if (claim['pool_id'] != REPAIR_ID or type(claim['ordinal']) is not int or claim['ordinal'] != ordinal
                        or str(uuid.UUID(claim['claim_id'])) != claim['claim_id']
                        or claim['snapshot_sha256'] != fingerprint or claim['model'] != MODEL
                        or claim['reserved_microusd'] != 210240 or created.tzinfo is None
                        or created < previous_time or created >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES)
                        or claim['previous_claim_sha256'] != previous_hash
                        or claim['claim_sha256'] != claim_digest(claim)):
                    raise ValueError()
                previous_hash, previous_time = claim['claim_sha256'], created
            if claims:
                if (authorization['last_claim_id'] != claims[-1]['claim_id']
                        or authorization['last_claim_sha256'] != claims[-1]['claim_sha256']):
                    raise ValueError()
            elif authorization['last_claim_id'] is not None or authorization['last_claim_sha256'] is not None:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BudgetError('AI_BUDGET_UNAVAILABLE') from None
        return authorization, claims

    def import_repair(self, snapshot, authorization_reviewed=False):
        if authorization_reviewed is not True:
            raise BudgetError('AI_SUPPLEMENTAL_REVIEW_REQUIRED')
        validate_repair(snapshot)
        with self.connect() as db:
            mode = self.mode(db)
            self.consumed_predecessors(db)
            if mode == 'repair':
                self.repair_state(db)
                return 'already_imported'
            self.check_supplemental_window()
            if datetime.now(timezone.utc) < datetime.fromisoformat(FOLLOWUP_CLAIM_TIME):
                raise BudgetError('AI_BUDGET_UNAVAILABLE')
            content = canonical(snapshot)
            fingerprint = hashlib.sha256(content.encode()).hexdigest()
            timestamp = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO ai_cloud_repair_authorizations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (REPAIR_ID, POLICY, fingerprint, content, FOLLOWUP_CLAIM_ID, 0, 0, None, None, timestamp))
            db.execute('INSERT INTO ai_cloud_repair_receipts VALUES (?,?,?)', (REPAIR_ID, fingerprint, timestamp))
        return 'imported'

    def reserve_repair(self, db):
        authorization, claims = self.repair_state(db)
        self.check_supplemental_window()
        if len(claims) >= REPAIR_MAX_ATTEMPTS:
            raise BudgetError('AI_USAGE_LIMIT')
        claim = {'pool_id': REPAIR_ID, 'ordinal': len(claims) + 1,
                 'claim_id': str(uuid.uuid4()), 'snapshot_sha256': authorization['snapshot_sha256'],
                 'created_at': datetime.now(timezone.utc).isoformat(), 'model': MODEL,
                 'reserved_microusd': 210240,
                 'previous_claim_sha256': claims[-1]['claim_sha256'] if claims else authorization['snapshot_sha256']}
        claim['claim_sha256'] = claim_digest(claim)
        db.execute('INSERT INTO ai_cloud_repair_claims VALUES (?,?,?,?,?,?,?,?,?)',
            tuple(claim[key] for key in ('pool_id','ordinal','claim_id','snapshot_sha256','created_at',
                                        'model','reserved_microusd','previous_claim_sha256','claim_sha256')))
        changed = db.execute('UPDATE ai_cloud_repair_authorizations SET consumed_count=?,consumed_microusd=?,last_claim_id=?,last_claim_sha256=? '
            'WHERE pool_id=? AND consumed_count=? AND consumed_microusd=? AND last_claim_id IS NOT DISTINCT FROM ? '
            'AND last_claim_sha256 IS NOT DISTINCT FROM ?',
            (len(claims)+1,(len(claims)+1)*210240,claim['claim_id'],claim['claim_sha256'],REPAIR_ID,
             authorization['consumed_count'],authorization['consumed_microusd'],
             authorization['last_claim_id'],authorization['last_claim_sha256']))
        if changed.rowcount != 1:
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        self.repair_state(db)
        return 210240

    @staticmethod
    def followup_present(db):
        return any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
            for table in ('ai_cloud_followup_authorizations', 'ai_cloud_followup_receipts', 'ai_cloud_followup_claims'))

    @staticmethod
    def predecessor_state(db):
        authorization, claims = CloudBudgetLedger.supplemental_state(db)
        if (len(claims) != 1 or claims[0]['claim_id'] != PREDECESSOR_CLAIM_ID
                or claims[0]['snapshot_sha256'] != PREDECESSOR_FINGERPRINT
                or datetime.fromisoformat(claims[0]['created_at']) != datetime.fromisoformat(PREDECESSOR_CLAIM_TIME)):
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        return authorization, claims

    @staticmethod
    def followup_state(db):
        CloudBudgetLedger.predecessor_state(db)
        authorizations = db.execute('SELECT * FROM ai_cloud_followup_authorizations').fetchall()
        receipts = db.execute('SELECT * FROM ai_cloud_followup_receipts').fetchall()
        claims = db.execute('SELECT * FROM ai_cloud_followup_claims').fetchall()
        if len(authorizations) != 1 or len(receipts) != 1 or len(claims) > 1:
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        authorization, receipt = authorizations[0], receipts[0]
        content = canonical(followup_snapshot())
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        if (authorization['authorization_id'] != FOLLOWUP_ID or authorization['policy'] != POLICY
                or authorization['snapshot_json'] != content or authorization['snapshot_sha256'] != fingerprint
                or authorization['predecessor_claim_id'] != PREDECESSOR_CLAIM_ID
                or receipt['authorization_id'] != FOLLOWUP_ID or receipt['snapshot_sha256'] != fingerprint
                or receipt['imported_at'] != authorization['imported_at']):
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        try:
            imported = datetime.fromisoformat(authorization['imported_at'])
            if (imported.tzinfo is None or imported >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES)
                    or imported < datetime.fromisoformat(PREDECESSOR_CLAIM_TIME)):
                raise ValueError()
            if claims:
                claim = claims[0]
                created = datetime.fromisoformat(claim['created_at'])
                if (claim['authorization_id'] != FOLLOWUP_ID
                        or not isinstance(claim['claim_id'], str) or not claim['claim_id']
                        or claim['claim_id'] != authorization['consumed_claim_id']
                        or claim['snapshot_sha256'] != fingerprint or claim['model'] != MODEL
                        or claim['reserved_microusd'] != 210240
                        or created.tzinfo is None or created < imported
                        or created >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES)):
                    raise ValueError()
            elif authorization['consumed_claim_id'] is not None:
                raise ValueError()
            if 1 + len(claims) > 2 or 210240 + sum(c['reserved_microusd'] for c in claims) > 420480:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise BudgetError('AI_BUDGET_UNAVAILABLE') from None
        return authorization, claims

    def import_followup(self, snapshot, authorization_reviewed=False):
        if authorization_reviewed is not True:
            raise BudgetError('AI_SUPPLEMENTAL_REVIEW_REQUIRED')
        validate_followup(snapshot)
        with self.connect() as db:
            mode = self.mode(db)
            self.predecessor_state(db)
            if mode in ('followup', 'repair'):
                self.repair_state(db) if mode == 'repair' else self.followup_state(db)
                return 'already_imported'
            self.check_supplemental_window()
            if datetime.now(timezone.utc) < datetime.fromisoformat(PREDECESSOR_CLAIM_TIME):
                raise BudgetError('AI_BUDGET_UNAVAILABLE')
            content = canonical(snapshot)
            fingerprint = hashlib.sha256(content.encode()).hexdigest()
            timestamp = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO ai_cloud_followup_authorizations VALUES (?,?,?,?,?,?,?)',
                (FOLLOWUP_ID, POLICY, fingerprint, content, PREDECESSOR_CLAIM_ID, None, timestamp))
            db.execute('INSERT INTO ai_cloud_followup_receipts VALUES (?,?,?)',
                (FOLLOWUP_ID, fingerprint, timestamp))
        return 'imported'

    def reserve_followup(self, db, budget_microusd):
        authorization, claims = self.followup_state(db)
        self.check_supplemental_window()
        # This fixed bound covers known supplemental attempts, not old unknown use.
        if claims or budget_microusd < 420480:
            raise BudgetError('AI_USAGE_LIMIT')
        claim_id = str(uuid.uuid4())
        db.execute('INSERT INTO ai_cloud_followup_claims VALUES (?,?,?,?,?,?)',
            (FOLLOWUP_ID, claim_id, authorization['snapshot_sha256'],
             datetime.now(timezone.utc).isoformat(), MODEL, 210240))
        changed = db.execute('UPDATE ai_cloud_followup_authorizations SET consumed_claim_id=? '
            'WHERE authorization_id=? AND consumed_claim_id IS NULL', (claim_id, FOLLOWUP_ID))
        if changed.rowcount != 1:
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        self.followup_state(db)
        return 210240

    @staticmethod
    def supplemental_state(db):
        """A missing/corrupt single record never repairs or resets allowance."""
        authorizations = db.execute('SELECT * FROM ai_cloud_supplemental_authorizations').fetchall()
        receipts = db.execute('SELECT * FROM ai_cloud_supplemental_receipts').fetchall()
        claims = db.execute('SELECT * FROM ai_cloud_supplemental_claims').fetchall()
        if len(authorizations) != 1 or len(receipts) != 1 or len(claims) > 1:
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        authorization, receipt = authorizations[0], receipts[0]
        expected = supplemental_snapshot()
        content = canonical(expected)
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        if (authorization['authorization_id'] != SUPPLEMENTAL_ID or authorization['policy'] != POLICY
                or authorization['snapshot_json'] != content or authorization['snapshot_sha256'] != fingerprint
                or receipt['authorization_id'] != SUPPLEMENTAL_ID or receipt['snapshot_sha256'] != fingerprint
                or receipt['imported_at'] != authorization['imported_at']):
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        try:
            imported = datetime.fromisoformat(authorization['imported_at'])
            if imported.tzinfo is None or imported >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES):
                raise ValueError()
            if claims:
                claim = claims[0]
                created = datetime.fromisoformat(claim['created_at'])
                if (claim['authorization_id'] != SUPPLEMENTAL_ID
                        or not isinstance(claim['claim_id'], str) or not claim['claim_id']
                        or claim['claim_id'] != authorization['consumed_claim_id']
                        or claim['snapshot_sha256'] != fingerprint or claim['model'] != MODEL
                        or claim['reserved_microusd'] != 210240
                        or created.tzinfo is None or created < imported
                        or created >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES)):
                    raise ValueError()
            elif authorization['consumed_claim_id'] is not None:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise BudgetError('AI_BUDGET_UNAVAILABLE') from None
        return authorization, claims

    @staticmethod
    def check_supplemental_window():
        if datetime.now(timezone.utc) >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES) - timedelta(seconds=60):
            raise BudgetError('AI_CREDIT_EXPIRED')

    def import_supplemental(self, snapshot, authorization_reviewed=False):
        if authorization_reviewed is not True:
            raise BudgetError('AI_SUPPLEMENTAL_REVIEW_REQUIRED')
        validate_supplemental(snapshot)
        with self.connect() as db:
            if (db.execute('SELECT 1 FROM ai_cloud_budget_history LIMIT 1').fetchone()
                    or db.execute('SELECT 1 FROM ai_cloud_budget_reservations LIMIT 1').fetchone()):
                raise BudgetError('AI_BUDGET_MODE_CONFLICT')
            mode = self.mode(db)
            if mode in ('supplemental', 'followup', 'repair'):
                self.repair_state(db) if mode == 'repair' else self.followup_state(db) if mode == 'followup' else self.supplemental_state(db)
                return 'already_imported'
            self.check_supplemental_window()
            content = canonical(snapshot)
            fingerprint = hashlib.sha256(content.encode()).hexdigest()
            timestamp = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO ai_cloud_supplemental_authorizations VALUES (?,?,?,?,?,?)',
                (SUPPLEMENTAL_ID, POLICY, fingerprint, content, None, timestamp))
            db.execute('INSERT INTO ai_cloud_supplemental_receipts VALUES (?,?,?)',
                (SUPPLEMENTAL_ID, fingerprint, timestamp))
        return 'imported'

    def reserve_supplemental(self, db, reserved, budget_microusd):
        authorization, claims = self.supplemental_state(db)
        self.check_supplemental_window()
        if claims:
            raise BudgetError('AI_USAGE_LIMIT')
        if budget_microusd < 210240:
            raise BudgetError('AI_USAGE_LIMIT')
        claim_id = str(uuid.uuid4())
        # Fixed full cap even when the caller asks for fewer output tokens.
        db.execute('INSERT INTO ai_cloud_supplemental_claims VALUES (?,?,?,?,?,?)',
            (SUPPLEMENTAL_ID, claim_id, authorization['snapshot_sha256'],
             datetime.now(timezone.utc).isoformat(), MODEL, 210240))
        changed = db.execute('UPDATE ai_cloud_supplemental_authorizations SET consumed_claim_id=? '
            'WHERE authorization_id=? AND consumed_claim_id IS NULL', (claim_id, SUPPLEMENTAL_ID))
        if changed.rowcount != 1:
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        self.supplemental_state(db)
        return 210240

    @staticmethod
    def totals(db):
        receipt = db.execute('SELECT * FROM ai_cloud_budget_history WHERE policy=?', (POLICY,)).fetchone()
        if not receipt:
            raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
        baseline = db.execute('SELECT * FROM ai_cloud_budget_reservations WHERE id=?', ('history:' + receipt['snapshot_sha256'],)).fetchone()
        if (not baseline or baseline['policy'] != POLICY or baseline['model'] != MODEL
                or baseline['attempts'] != receipt['prior_attempts']
                or baseline['reserved_microusd'] != receipt['prior_reserved_microusd']):
            raise BudgetError('AI_BUDGET_UNAVAILABLE')
        row = db.execute('SELECT COALESCE(SUM(attempts),0) AS attempts, COALESCE(SUM(reserved_microusd),0) AS reserved FROM ai_cloud_budget_reservations WHERE policy=?', (POLICY,)).fetchone()
        return int(row['attempts']), int(row['reserved'])

    def import_history(self, snapshot, history_reviewed=False):
        if history_reviewed is not True:
            raise BudgetError('AI_BUDGET_HISTORY_REQUIRED')
        validate_snapshot(snapshot)
        fingerprint = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
        with self.connect() as db:
            if self.mode(db) != 'complete':
                raise BudgetError('AI_BUDGET_MODE_CONFLICT')
            old = db.execute('SELECT * FROM ai_cloud_budget_history WHERE policy=?', (POLICY,)).fetchone()
            if old:
                if old['snapshot_sha256'] != fingerprint:
                    raise BudgetError('AI_BUDGET_HISTORY_CONFLICT')
                self.totals(db)  # Never silently repair missing history.
                return 'already_imported'
            if db.execute('SELECT 1 FROM ai_cloud_budget_reservations LIMIT 1').fetchone():
                raise BudgetError('AI_BUDGET_HISTORY_CONFLICT')
            timestamp = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO ai_cloud_budget_history VALUES (?,?,?,?,?)',
                (POLICY, fingerprint, snapshot['prior_attempts'], snapshot['prior_reserved_microusd'], timestamp))
            db.execute('INSERT INTO ai_cloud_budget_reservations VALUES (?,?,?,?,?,?)',
                ('history:' + fingerprint, POLICY, timestamp, MODEL, snapshot['prior_attempts'], snapshot['prior_reserved_microusd']))
            return 'imported'

    def reserve(self, reserved, max_requests, budget_microusd):
        # Hard ceilings are duplicated intentionally at the persistence boundary.
        if (type(reserved) is not int or not 1 <= reserved <= 210240
                or type(max_requests) is not int or not 1 <= max_requests <= 3
                or type(budget_microusd) is not int or not 10000 <= budget_microusd <= 1000000):
            raise BudgetError('AI_CONFIGURATION_INVALID')
        try:
            with self.connect() as db:
                mode = self.mode(db)
                if mode == 'repair':
                    return self.reserve_repair(db)
                if mode == 'followup':
                    return self.reserve_followup(db, budget_microusd)
                if mode == 'supplemental':
                    # Returning exits the context: commit completes before the
                    # adapter can issue count_tokens or messages. Commit errors
                    # surface as unavailable; no provider request is then sent.
                    return self.reserve_supplemental(db, reserved, budget_microusd)
                attempts, used = self.totals(db)
                if attempts >= max_requests or used + reserved > budget_microusd:
                    raise BudgetError('AI_USAGE_LIMIT')
                db.execute('INSERT INTO ai_cloud_budget_reservations VALUES (?,?,?,?,?,?)',
                    (str(uuid.uuid4()), POLICY, datetime.now(timezone.utc).isoformat(), MODEL, 1, reserved))
        except BudgetError:
            raise
        except Exception:
            raise BudgetError('AI_BUDGET_UNAVAILABLE') from None
        return reserved

    def status(self):
        try:
            with self.connect() as db:
                mode = self.mode(db)
                if mode == 'repair':
                    _, claims = self.repair_state(db)
                    expired = datetime.now(timezone.utc) >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES) - timedelta(seconds=60)
                    used = len(claims)*210240
                    return {
                        'mode': 'repair_pool', 'ledger_readable': True, 'history_reconciled': False,
                        'total_attempts_reserved': None, 'total_cost_reserved_usd': None,
                        'observed_prior_attempts': 1, 'observed_prior_reserved_usd': 0.21024,
                        'uncertainty_reserve_usd': 0.42048, 'uncertainty_is_observed_history': False,
                        'account_usage_bound_verified': False, 'repair_pool_id': REPAIR_ID,
                        'repair_max_attempts': REPAIR_MAX_ATTEMPTS, 'repair_attempts_reserved': len(claims),
                        'repair_remaining_attempts': 0 if expired else REPAIR_MAX_ATTEMPTS-len(claims),
                        'repair_cost_reserved_usd': used/1000000, 'repair_max_reserved_usd': REPAIR_MAX_RESERVED/1000000,
                        'known_new_attempts_reserved': 2+len(claims), 'known_new_max_attempts': KNOWN_NEW_MAX_ATTEMPTS,
                        'known_new_reserved_usd': (420480+used)/1000000,
                        'known_new_max_reserved_usd': KNOWN_NEW_MAX_RESERVED/1000000,
                        'predecessor_attempts_reserved': 2, 'predecessor_cost_reserved_usd': 0.42048,
                        'repair_expired': expired, 'legacy_limits_apply': False,
                    }
                if mode in ('supplemental', 'followup'):
                    _, claims = self.followup_state(db) if mode == 'followup' else self.supplemental_state(db)
                    expired = datetime.now(timezone.utc) >= datetime.fromisoformat(SUPPLEMENTAL_EXPIRES) - timedelta(seconds=60)
                    count = len(claims)
                    return {
                        'mode': 'supplemental_one_attempt', 'ledger_readable': True,
                        'history_reconciled': False, 'total_attempts_reserved': None,
                        'total_cost_reserved_usd': None,
                        'observed_prior_attempts': 1, 'observed_prior_reserved_usd': 0.21024,
                        'uncertainty_reserve_usd': 0.42048,
                        'uncertainty_is_observed_history': False,
                        'account_usage_bound_verified': False,
                        'supplemental_authorization_id': FOLLOWUP_ID if mode == 'followup' else SUPPLEMENTAL_ID,
                        'supplemental_sequence': 2 if mode == 'followup' else 1,
                        'predecessor_attempts_reserved': 1 if mode == 'followup' else 0,
                        'predecessor_cost_reserved_usd': 0.21024 if mode == 'followup' else 0,
                        'cumulative_new_attempts_reserved': count + (1 if mode == 'followup' else 0),
                        'cumulative_new_cost_reserved_usd': (count + (1 if mode == 'followup' else 0)) * 0.21024,
                        'cumulative_new_max_attempts': 2 if mode == 'followup' else 1,
                        'cumulative_new_max_reserved_usd': 0.42048 if mode == 'followup' else 0.21024,
                        'supplemental_max_attempts': 1,
                        'supplemental_attempts_reserved': count,
                        'supplemental_cost_reserved_usd': count * 0.21024,
                        'supplemental_remaining_attempts': 0 if expired else 1 - count,
                        'supplemental_expired': expired,
                    }
                attempts, reserved = self.totals(db)
            return {'total_attempts_reserved': attempts, 'total_cost_reserved_usd': reserved / 1000000,
                    'ledger_readable': True, 'history_reconciled': True}
        except BudgetError as error:
            return {'total_attempts_reserved': None, 'total_cost_reserved_usd': None,
                    'ledger_readable': error.code == 'AI_BUDGET_HISTORY_REQUIRED',
                    'history_reconciled': False, 'ledger_error': error.code}
        except Exception:
            return {'total_attempts_reserved': None, 'total_cost_reserved_usd': None,
                    'ledger_readable': False, 'history_reconciled': False, 'ledger_error': 'AI_BUDGET_UNAVAILABLE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export-history', help='Offline read of one selected SQLite budget ledger')
    export.add_argument('--ledger', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    restore = sub.add_parser('import-history', help='Explicit write to authorized cloud budget DB; never invokes AI')
    restore.add_argument('--snapshot', type=Path, required=True)
    restore.add_argument('--history-reviewed', action='store_true')
    prepare = sub.add_parser('prepare-supplemental', help='Offline preparation only; never connects to a database')
    prepare.add_argument('--ledger', type=Path, required=True)
    prepare.add_argument('--snapshot-output', type=Path, required=True)
    prepare.add_argument('--sql-output', type=Path, required=True)
    prepare.add_argument('--supplemental-reviewed', action='store_true')
    followup = sub.add_parser('prepare-followup', help='Offline reviewed successor preparation; no DB connection')
    followup.add_argument('--predecessor-readback', type=Path, required=True)
    followup.add_argument('--snapshot-output', type=Path, required=True)
    followup.add_argument('--sql-output', type=Path, required=True)
    followup.add_argument('--followup-reviewed', action='store_true')
    repair = sub.add_parser('prepare-repair', help='Offline finite repair-pool preparation; no database access')
    repair.add_argument('--consumed-readback', type=Path, required=True)
    repair.add_argument('--snapshot-output', type=Path, required=True)
    repair.add_argument('--sql-output', type=Path, required=True)
    repair.add_argument('--repair-reviewed', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'export-history':
            result = export_history(args.ledger)
            with args.output.open('x') as stream:
                stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'status': 'exported', 'attempts': result['prior_attempts'], 'model_called': False}))
        elif args.command == 'prepare-supplemental':
            snapshot = prepare_supplemental(args.ledger, args.supplemental_reviewed)
            sql = supplemental_import_sql(snapshot)
            with args.snapshot_output.open('x') as stream:
                stream.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
            with args.sql_output.open('x') as stream:
                stream.write(sql)
            print(json.dumps({'status': 'prepared_for_review', 'authorization_id': SUPPLEMENTAL_ID,
                'history_reconciled': False, 'database_connected': False, 'model_called': False}))
        elif args.command == 'prepare-followup':
            snapshot = prepare_followup(args.predecessor_readback, args.followup_reviewed)
            sql = followup_import_sql(snapshot)
            with args.snapshot_output.open('x') as stream:
                stream.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
            with args.sql_output.open('x') as stream:
                stream.write(sql)
            print(json.dumps({'status': 'prepared_for_review', 'authorization_id': FOLLOWUP_ID,
                'history_reconciled': False, 'database_connected': False, 'model_called': False}))
        elif args.command == 'prepare-repair':
            snapshot = prepare_repair(args.consumed_readback, args.repair_reviewed)
            sql = repair_import_sql(snapshot)
            with args.snapshot_output.open('x') as stream:
                stream.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
            with args.sql_output.open('x') as stream:
                stream.write(sql)
            print(json.dumps({'status': 'prepared_for_review', 'pool_id': REPAIR_ID,
                'history_reconciled': False, 'database_connected': False, 'model_called': False}))
        else:
            # No secret/DB URI in argv, stdout or error output.
            snapshot = json.loads(args.snapshot.read_text())
            ledger = CloudBudgetLedger(os.environ.get('DIRECT_DATABASE_URL') or os.environ.get('DATABASE_URL', ''))
            print(json.dumps({'status': ledger.import_history(snapshot, args.history_reviewed), 'model_called': False}))
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'code': error.code if isinstance(error, BudgetError) else 'AI_BUDGET_UNAVAILABLE', 'model_called': False}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
