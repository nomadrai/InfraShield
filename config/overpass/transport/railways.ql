// Railways: tracks, stations, and halt points.
way["railway"~"^(rail|light_rail|subway|tram|monorail|narrow_gauge|funicular)$"];
node["railway"~"^(station|halt|tram_stop)$"];
way["public_transport"="stop_area"];
