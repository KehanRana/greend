"""Immutable runtime identity: live activation is explicit and only on empty databases."""
import hashlib
import json
from sqlalchemy import text
from greenroute.live import LiveManifest, VLLMAdapter
from greenroute.store import immediate, ready


def manifest_for(settings):
    if settings._bound_manifest is not None:
        return settings._bound_manifest
    if not settings.live_manifest:
        raise ValueError('Live deployment manifest is required')
    try:
        manifest = LiveManifest.model_validate_json(settings.live_manifest.read_text())
    except Exception:
        raise ValueError('Live deployment manifest is invalid') from None
    if manifest.baseline_endpoint != settings.baseline_endpoint:
        raise ValueError('Configured baseline must match the live manifest')
    for d in manifest.deployments:
        if not d.locality_verified or not d.model_equivalence_verified:
            raise ValueError('Every deployment requires reviewed locality and model equivalence')
        if ('REPLACE_' in d.locality_evidence or 'REPLACE_' in d.model_version
                or '.invalid' in d.base_url or 'REPLACE_' in d.grid_zone):
            raise ValueError('Example deployment placeholders cannot be activated')
        d.credential()
    return manifest


def digest(manifest):
    return hashlib.sha256(json.dumps(manifest.model_dump(), sort_keys=True).encode()).hexdigest()


def initialize_live(engine, settings):
    """Explicit admin action; never repurpose a database containing execution evidence."""
    ready(engine)
    if settings.mode != 'live':
        raise ValueError('Live settings required')
    manifest = manifest_for(settings)
    with immediate(engine) as conn:
        identity = conn.execute(text('SELECT * FROM runtime_identity WHERE id=1')).mappings().one()
        if identity['mode'] == 'live':
            if identity['manifest_digest'] != digest(manifest):
                raise ValueError('Manifest differs from the registered deployment set')
            return
        for table in ('jobs', 'attempts', 'decisions', 'grid_snapshots', 'emissions_estimates'):
            if conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar():
                raise ValueError('Live initialization requires an empty migrated database')
        conn.execute(text('DELETE FROM endpoints'))
        for rank, d in enumerate(manifest.deployments):
            conn.execute(text('INSERT INTO endpoints(id,region,model,model_version,latency_rank,healthy,locality,'
                'grid_zone,locality_evidence) VALUES (:id,:region,:model,:version,:rank,1,\'verified\',:zone,:evidence)'),
                dict(id=d.id, region=d.region, model=d.model_alias, version=d.model_version,
                     rank=rank, zone=d.grid_zone, evidence=d.locality_evidence))
        conn.execute(text("UPDATE runtime_identity SET mode='live',manifest_digest=:digest WHERE id=1"),
                     dict(digest=digest(manifest)))


def check_runtime(engine, settings):
    ready(engine)
    manifest = manifest_for(settings) if settings.mode == 'live' else None
    with engine.connect() as conn:
        identity = conn.execute(text('SELECT * FROM runtime_identity WHERE id=1')).mappings().one()
        if manifest:
            rows = conn.execute(text('SELECT id,region,model,model_version,grid_zone,locality,locality_evidence,'
                                     'latency_rank FROM endpoints ORDER BY latency_rank')).mappings().all()
            expected = [dict(id=d.id,region=d.region,model=d.model_alias,model_version=d.model_version,
                grid_zone=d.grid_zone,locality='verified',locality_evidence=d.locality_evidence,latency_rank=rank)
                for rank,d in enumerate(manifest.deployments)]
            if [dict(r) for r in rows] != expected:
                raise ValueError('Live endpoint registry differs from reviewed manifest')
    if identity['mode'] != settings.mode or (manifest and identity['manifest_digest'] != digest(manifest)):
        raise ValueError('Runtime mode or deployment manifest does not match database identity')
    settings._bound_manifest = manifest
    return manifest


def policy_options(settings):
    manifest = manifest_for(settings) if settings.mode == 'live' else None
    return dict(mode=settings.mode, model_version=manifest.deployments[0].model_version if manifest else 'fixture-v1')


async def execute_endpoint(client, settings, endpoint_id, region, payload, request_id):
    if settings.mode == 'demo':
        from greenroute.inference import execute
        return await execute(client, settings, region, payload, request_id)
    manifest = manifest_for(settings)
    deployment = next(d for d in manifest.deployments if d.id == endpoint_id)
    try:
        return await VLLMAdapter(client, settings.timeout_seconds).execute(deployment, payload, request_id)
    except ValueError:
        from greenroute.inference import UpstreamFailure
        raise UpstreamFailure(502, 'Regional configuration unavailable', 'upstream_configuration',
                              disable_endpoint=True) from None


if __name__ == '__main__':
    from greenroute.config import Settings
    from greenroute.store import make_engine
    settings = Settings.from_env()
    engine = make_engine(settings.database)
    try:
        initialize_live(engine, settings)
        print('Live database initialized; no network requests made')
    finally:
        engine.dispose()
