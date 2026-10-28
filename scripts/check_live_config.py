"""Offline manifest/credential-presence check. Never makes network requests."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import ValidationError
from greenroute.live import LiveManifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    try:
        manifest = LiveManifest.model_validate_json(args.manifest.read_text())
    except (OSError, ValidationError, ValueError):
        print(json.dumps({'status': 'invalid_manifest', 'network_requests': 0}))
        return 1
    credentials_present = all(os.environ.get(d.api_key_env, '').strip() for d in manifest.deployments)
    grid_present = bool(os.environ.get('ELECTRICITYMAPS_API_KEY', '').strip())
    print(json.dumps({'status': 'offline_config_valid', 'network_requests': 0,
        'deployment_count': len(manifest.deployments),
        'inference_credentials_present': credentials_present, 'grid_credential_present': grid_present,
        'locality_verified': False, 'live_runtime_enabled': False,
        'remaining_gates': ['entitlement_and_rate_limits', 'zone_and_locality_review',
                            'model_equivalence', 'budget', 'isolated_live_runtime', 'real_provider_validation']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
