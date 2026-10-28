# GreenRoute dashboard

React/TypeScript/Vite console for the local FastAPI service. See the root README for gateway/mock/worker setup.

From the repository root:

```sh
npm ci --prefix frontend
npm run build --prefix frontend
```

Start/restart the gateway after building and open its `/dashboard/` URL. For development use `npm run dev --prefix frontend`; `/api` proxies to `http://127.0.0.1:8000`, or `GREENROUTE_API_ORIGIN` when configured in the environment. Vite's printed URL includes the `/dashboard/` base.

```sh
npm run test --prefix frontend
npm run build --prefix frontend
```

The client key is entered at runtime and held in memory only. An independent operator key authorizes scenario changes; neither key is compiled into assets. Data, counters, queue states and estimates come from authenticated APIs. Polling uses one non-overlapping loop per resource, aborts on disconnect/unmount and preserves last-good data on refresh failure.

Map geometry is a generated local SVG path from world-atlas/Natural Earth. Regenerate with `node frontend/scripts/map.mjs` from the repository root; do not hand-edit `src/map.json`. Map and font licenses ship in `public/`. No external font/map connection is needed.

The overview intentionally has no invented time-series history. It shows current region values, a recent stored routing comparison, paired estimate totals and an owner queue. `DESIGN.md` records the direction, data inventory, responsive frame and provenance.
