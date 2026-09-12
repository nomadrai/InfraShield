(
  way["landuse"~"^(residential|industrial|commercial|retail)$"]({{bbox}});
  way["natural"="water"]({{bbox}});
  way["waterway"~"^(river|stream|canal|drain)$"]({{bbox}});
);
