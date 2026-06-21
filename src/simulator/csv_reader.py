"""
CSV Reader simulator for providing time-series input data to other simulators.
"""
import bisect
import mosaik_api
import pandas as pd
import os

META = {
    'api_version': '3.0',
    'type': 'time-based',
    'models': {
        'CSVReader': {
            'public': True,
            'params': ['csv_file', 'time_column'],
            'attrs': [],  # Populated dynamically from CSV column headers in init()
        },
    },
}

class CSVReader(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid = None
        self.data = None
        self.csv_file = None
        self.step_size = 1
        self.time_column = 'time'
        self.attrs = []
        self.current_time = 0
        self.seconds_per_step = 60  # Seconds of wall-clock time per mosaik timestep
        
    def init(self, sid, time_resolution=1.0, step_size=1, csv_file=None,
             time_column='time', seconds_per_step=60):
        self.step_size = step_size
        self.time_column = time_column
        self.seconds_per_step = seconds_per_step
        if csv_file is not None:
            if not os.path.exists(csv_file):
                raise FileNotFoundError(f'CSV file not found: {csv_file}')
            self.csv_file = csv_file
            self.data = pd.read_csv(csv_file)
            self.attrs = [col for col in self.data.columns if col != time_column]
            self.meta['models']['CSVReader']['attrs'] = list(self.attrs)
            print(f'CSV Reader: discovered attrs {self.attrs} from {csv_file}')
        return self.meta
    
    def create(self, num, model, csv_file=None, time_column=None):
        """
        Create CSV reader instance.
        csv_file / time_column can be passed here as a fallback if not already
        supplied via world.start() (which forwards them to init()).
        """
        if num != 1:
            raise ValueError('Can only create one CSVReader instance')

        # Load CSV here only if not already loaded during init()
        if csv_file is not None:
            _time_col = time_column or self.time_column
            self.time_column = _time_col
            self.csv_file = csv_file
            if not os.path.exists(csv_file):
                raise FileNotFoundError(f'CSV file not found: {csv_file}')
            self.data = pd.read_csv(csv_file)
            self.attrs = [col for col in self.data.columns if col != _time_col]
            self.meta['models']['CSVReader']['attrs'] = list(self.attrs)

        if self.data is None:
            raise RuntimeError(
                'No CSV file loaded — pass csv_file to world.start() or create()')

        self.eid = 'CSVReader_0'
        print(f'CSV Reader initialized with file: {self.csv_file}')
        print(f'Available attributes: {self.attrs}')
        print(f'Data points: {len(self.data)}')

        return [{'eid': self.eid, 'type': model, 'children': []}]
    
    def step(self, time, inputs, max_advance):
        # Store current time for use in get_data
        self.current_time = time
        return time + self.step_size
    
    def get_data(self, outputs):
        """
        Provide data for the requested outputs at the current time.
        Uses linear interpolation for values between data points.
        search_time = mosaik_time * seconds_per_step; CSV time column is in seconds.
        """
        data = {}
        time = self.current_time

        for eid, attrs in outputs.items():
            if eid != self.eid:
                continue

            data[eid] = {}
            search_time = time * self.seconds_per_step

            if self.time_column in self.data.columns:
                time_values = self.data[self.time_column].values

                if search_time <= time_values[0]:
                    row = self.data.iloc[0]
                    for attr in attrs:
                        if attr in self.attrs:
                            data[eid][attr] = float(row[attr])
                elif search_time >= time_values[-1]:
                    row = self.data.iloc[-1]
                    for attr in attrs:
                        if attr in self.attrs:
                            data[eid][attr] = float(row[attr])
                else:
                    # bisect gives O(log n) bracket lookup — no silent data-loss
                    i = bisect.bisect_right(list(time_values), search_time) - 1
                    i = max(0, min(i, len(time_values) - 2))
                    t0, t1 = float(time_values[i]), float(time_values[i + 1])
                    weight = (search_time - t0) / (t1 - t0) if t1 != t0 else 0.0
                    for attr in attrs:
                        if attr in self.attrs:
                            v0 = float(self.data.iloc[i][attr])
                            v1 = float(self.data.iloc[i + 1][attr])
                            data[eid][attr] = v0 + weight * (v1 - v0)
            else:
                # No time column — use mosaik timestep as row index
                idx = min(int(time), len(self.data) - 1)
                row = self.data.iloc[idx]
                for attr in attrs:
                    if attr in self.attrs:
                        data[eid][attr] = float(row[attr])

        return data
