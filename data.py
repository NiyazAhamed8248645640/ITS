import pandas as pd
import numpy as np

np.random.seed(42)

rows = 5000

data = {
    "timestamp": pd.date_range(start="2024-01-01", periods=rows, freq="5min"),
    "road_id": np.random.choice(["R101", "R102", "R103", "R104", "R105"], rows),
    "vehicle_count": np.random.randint(20, 200, rows),
    "avg_speed": np.random.randint(10, 80, rows),
    "traffic_density": np.random.randint(10, 100, rows),
    "signal_wait_time": np.random.randint(10, 150, rows),
    "lane_count": np.random.randint(2, 5, rows),
    "weather": np.random.randint(0, 3, rows),
    "hour": np.random.randint(0, 24, rows),
    "day_type": np.random.randint(0, 2, rows)
}

df = pd.DataFrame(data)

# Congestion logic (label creation)
conditions = [
    (df["avg_speed"] < 25) & (df["traffic_density"] > 70),
    (df["avg_speed"] < 45) & (df["traffic_density"] > 40)
]

choices = [2, 1]
df["congestion_level"] = np.select(conditions, choices, default=0)

df.to_csv("urban_traffic_dataset.csv", index=False)

print("✅ Dataset created successfully!")
