from src.strava_fetch import fetch_shoes
from src.database import upsert_shoes

df = fetch_shoes()
print(df)
upsert_shoes(df)
print("Done")
