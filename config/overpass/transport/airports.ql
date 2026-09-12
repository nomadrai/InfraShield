// Airport infrastructure inside the AOI (Mumbai International Airport edge).
way["aeroway"~"^(runway|taxiway|apron|helipad)$"];
node["aeroway"~"^(aerodrome|helipad)$"];
way["aeroway"="aerodrome"];
