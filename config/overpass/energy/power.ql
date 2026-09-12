// Power grid: lines, substations, and towers.
way["power"~"^(line|minor_line|cable)$"];
node["power"~"^(substation|sub_station|plant|generator)$"];
way["power"="plant"];
way["power"~"^(substation|sub_station)$"];
node["power"="tower"];
node["man_made"="mast"]["tower:type"="communication"];
