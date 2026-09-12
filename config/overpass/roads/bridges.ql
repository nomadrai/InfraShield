// Bridges: any way tagged as a bridge (mostly carried by roads and railways).
// Bridge segments are single points of failure; flooded approaches can cut
// whole corridors even when the span itself is undamaged.
way["bridge"~"^(yes|aqueduct|cantilever|culvert|movable|trestle|viaduct)$"];
