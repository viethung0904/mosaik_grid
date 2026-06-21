"""
CSV reader simulator for mosaik 3.0.

Reads a CSV file with columns: time, S, T
(time in seconds at fixed intervals, one row per mosaik timestep).
Outputs S (solar irradiance, W/m²) and T (temperature, K) at every step.
"""
import csv
import collections
import mosaik_api

META = {
    "api_version": "3.0",
    "type": "time-based",
    "models": {
        "WeatherData": {
            "public": True,
            "params": ["csv_file"],
            "attrs": ["S", "T"],
        },
    },
}


class CSVReader(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self._entities = {}   # eid -> list of {S, T} dicts (one per row)
        self._cache = {}      # eid -> {S, T} for the current step
        self.step_size = 1
        self._phys_step = 0   # Physical time step counter (incremented once per 60s step)

    def init(self, sid, time_resolution=1.0, step_size=1):
        self.step_size = step_size
        return self.meta

    def create(self, num, model, csv_file):
        entities = []
        for i in range(num):
            eid = f"{model}_{i}"
            rows = []
            with open(csv_file, newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rows.append({"S": float(row["S"]), "T": float(row["T"])})
            self._entities[eid] = rows
            # Pre-load step 0
            self._cache[eid] = rows[0] if rows else {"S": 0.0, "T": 298.15}
            entities.append({"eid": eid, "type": model, "rel": []})
        return entities

    def step(self, time, inputs, max_advance):
        for eid, rows in self._entities.items():
            idx = min(self._phys_step, len(rows) - 1)
            self._cache[eid] = rows[idx]
        self._phys_step += 1
        return time + self.step_size

    def get_data(self, outputs):
        result = {}
        for eid, attrs in outputs.items():
            if eid in self._cache:
                result[eid] = {attr: self._cache[eid][attr] for attr in attrs
                               if attr in self._cache[eid]}
        return result
