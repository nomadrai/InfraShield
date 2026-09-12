// Waterways and water bodies: flood-relevant hydrography.
way["waterway"~"^(river|canal|stream|drain|ditch)$"];
way["natural"="water"];
way["waterway"="riverbank"];
relation["natural"="water"];
relation["waterway"="river"];
