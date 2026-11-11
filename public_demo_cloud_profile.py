"""Fail-closed placeholders. Fill only after official NEW app/DB verification.
No secrets belong here. Never populate from production or guessed identifiers.
"""
APP_ID='00000000-0000-4000-8000-000000000001'
ORIGIN='https://example.invalid'
DATABASE_NAME='example_demo'
DATABASE_USER='example_demo'
OWNER_UUID='00000000-0000-4000-8000-000000000002'
TENANT='lounge'
EXPIRES_AT='2026-11-16T14:59:59+00:00'  # explicit timezone-aware end time; NOT a billing stop
PUBLIC_RELEASE=False  # enable only after owner QA and real HTTPS checks

GATEWAY_VERIFIED=False  # official header overwrite + no direct backend ingress confirmed
HTTPS_BROWSER_VERIFIED=False  # rendered browser UI and independent HTTPS cookie-jar boundaries verified; submit UI test blocked

ANONYMOUS_TEST_ENABLED=False  # short verification only, not general release
ANONYMOUS_TEST_UNTIL=''  # absolute UTC deadline, at most 15 minutes from first setup

DATABASE_TRANSPORT='axhub_runtime_standard'  # platform injected DSN unchanged; internal DB TLS not promised

ANONYMOUS_TEST_ROUND=''

PUBLIC_AI_ENABLED=False
PUBLIC_AI_PRICING_VERIFIED=False  # Official Haiku standard prices rechecked 2026-11-11
