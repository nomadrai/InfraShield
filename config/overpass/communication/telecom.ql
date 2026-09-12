// Telecom: communication masts/towers and manhole-level network infrastructure.
node["man_made"="mast"]["communication:*"];
node["man_made"="tower"]["communication:*"];
way["man_made"="mast"];
way["man_made"="tower"];
node["man_made"="mast"]["tower:type"="communication"];
node["telecom"~"^(exchange|data_center)$"];
way["building"="telecom_exchange"];
