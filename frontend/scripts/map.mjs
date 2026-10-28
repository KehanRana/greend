import fs from 'node:fs';
import { geoNaturalEarth1, geoPath } from 'd3-geo';
import { feature } from 'topojson-client';
const topology=JSON.parse(fs.readFileSync(new URL('../node_modules/world-atlas/countries-110m.json',import.meta.url),'utf8'));
const countries=feature(topology,topology.objects.countries);
const land={type:'FeatureCollection',features:countries.features.filter(f=>f.id!=='010')};
const projection=geoNaturalEarth1().fitExtent([[8,8],[752,292]],land);
const points={
  'us-east-1':projection([-77.49,39.04]),
  'eu-central-1':projection([8.68,50.11]),
  'ap-southeast-2':projection([151.21,-33.87])
};
fs.writeFileSync(new URL('../src/map.json',import.meta.url),JSON.stringify({land:geoPath(projection)(land),points,
  attribution:'Natural Earth / world-atlas. Public-domain geographic data; no grid boundaries implied.',
  source:'https://www.naturalearthdata.com/about/terms-of-use/'})+'\n');
