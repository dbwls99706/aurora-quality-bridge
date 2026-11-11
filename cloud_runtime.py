"""Private AXHub gateway policy. Never deploy this port outside that gateway."""
import os
import re
import uuid
from urllib.parse import urlsplit


class CloudPolicy:
    def __init__(self):
        mode = os.environ.get('AURORA_DEPLOYMENT', 'local')
        if mode not in ('local', 'axhub'):
            raise ValueError('Unknown AURORA_DEPLOYMENT')
        self.enabled = mode == 'axhub'
        self.origin = self.authority = self.tenant = None
        if not self.enabled:
            return
        self.origin = os.environ.get('AURORA_PUBLIC_ORIGIN', '')
        parsed = urlsplit(self.origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or self.origin != self.origin.lower()
                or not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?', parsed.netloc)):
            raise ValueError('AURORA_PUBLIC_ORIGIN must be one exact HTTPS origin without a trailing slash')
        self.authority = parsed.netloc
        self.tenant = os.environ.get('AURORA_AXHUB_TENANT', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', self.tenant):
            raise ValueError('AURORA_AXHUB_TENANT is required')
        if not os.environ.get('DATABASE_URL', '').startswith(('postgres://', 'postgresql://')):
            raise ValueError('AXHub requires PostgreSQL DATABASE_URL; SQLite fallback is forbidden')
        network = os.environ.get('AURORA_AI_ALLOW_NETWORK', '0')
        if network not in ('0', '1'):
            raise ValueError('AURORA_AI_ALLOW_NETWORK must be 0 or 1')
        if network == '1':
            # A proposed, explicit opt-in profile. Default manifest remains offline.
            # Deployment and egress changes still require separate operator approval.
            from ai_budget import POLICY, MODEL
            if (os.environ.get('AURORA_AI_POLICY') != POLICY
                    or os.environ.get('AURORA_AI_PROVIDER') != 'anthropic'
                    or os.environ.get('AURORA_AI_MODEL') != MODEL
                    or os.environ.get('AURORA_AI_ENDPOINT', 'https://api.anthropic.com/v1/messages') != 'https://api.anthropic.com/v1/messages'):
                raise ValueError('Only the reviewed, expiring Anthropic demo profile may enable AI')

    def authenticate(self, headers):
        def single(name):
            values = headers.get_all(name, [])
            return values[0] if len(values) == 1 else ''
        identity = single('X-AxHub-User-ID')
        try:
            uuid.UUID(identity)
        except (ValueError, AttributeError):
            return False
        return (single('X-AxHub-Tenant-Slug') == self.tenant
                and single('X-AxHub-Surface') in ('tenant', 'admin')
                and single('X-AxHub-App-Role') in ('owner', 'platform_admin', 'tenant_admin'))
