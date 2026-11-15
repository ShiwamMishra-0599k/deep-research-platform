import { useEffect } from "react";
import { MapContainer, TileLayer, Marker, Popup, useMap } from "react-leaflet";
import L from "leaflet";
import type { LeafletMapData } from "@/types";

// Brand pin: flat teal drop with a light centre. Colours come from the design tokens.
const pinSvg = `
  <svg xmlns="http://www.w3.org/2000/svg" width="30" height="40" viewBox="0 0 36 46" aria-hidden="true">
    <path style="fill:rgb(var(--c-accent-strong));stroke:rgb(var(--c-surface));stroke-width:2.5"
      d="M18 2 C26 2 32 8 32 16 C32 26 18 44 18 44 C18 44 4 26 4 16 C4 8 10 2 18 2 Z"/>
    <circle cx="18" cy="16" r="5" style="fill:rgb(var(--c-surface))"/>
  </svg>`.trim();

const brandIcon = L.divIcon({
  className: "lumen-pin",
  html: pinSvg,
  iconSize: [30, 40],
  iconAnchor: [15, 38],
  popupAnchor: [0, -34],
});

function FitToMarkers({ markers }: { markers: { lat: number; lon: number }[] }) {
  const map = useMap();
  useEffect(() => {
    if (markers.length === 0) return;
    if (markers.length === 1) {
      map.setView([markers[0].lat, markers[0].lon], 15);
      return;
    }
    const bounds = L.latLngBounds(markers.map((m) => [m.lat, m.lon] as [number, number]));
    map.fitBounds(bounds, { padding: [30, 30], maxZoom: 16 });
  }, [map, markers]);
  return null;
}

export default function LeafletMapBlock({ data }: { data: LeafletMapData }) {
  const center = data.center ?? { lat: 0, lon: 0 };
  const markers = data.markers ?? [];
  const zoom = data.zoom ?? 13;

  return (
    <div
      role="region"
      aria-label={`Map showing ${markers.length} place${markers.length === 1 ? "" : "s"}`}
      className="card relative isolate overflow-hidden h-72 sm:h-96"
    >
      {markers.length > 0 && (
        <div className="tag absolute top-3 left-3 z-[400] shadow-sm">
          {markers.length} place{markers.length === 1 ? "" : "s"}
        </div>
      )}
      <MapContainer
        center={[center.lat, center.lon]}
        zoom={zoom}
        scrollWheelZoom={false}
        style={{ height: "100%", width: "100%" }}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <FitToMarkers markers={markers} />
        {markers.map((m, i) => (
          <Marker key={i} position={[m.lat, m.lon]} icon={brandIcon} title={m.label}>
            <Popup>
              <div className="font-medium">{m.label}</div>
              {m.popup && <div className="text-xs mt-1 text-muted">{m.popup}</div>}
            </Popup>
          </Marker>
        ))}
      </MapContainer>
    </div>
  );
}
