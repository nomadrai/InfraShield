// Hospitals and healthcare facilities.
node["amenity"~"^(hospital|clinic|doctors|dentist)$"];
way["amenity"~"^(hospital|clinic)$"];
relation["amenity"~"^(hospital|clinic)$"];

// Fire stations and police stations -- first responders.
node["amenity"~"^(fire_station|police)$"];
way["amenity"~"^(fire_station|police)$"];
