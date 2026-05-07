"""
CSV Reader simulator for providing time-series input data to other simulators.
"""
import mosaik_api
import pandas as pd
import os

META = {
    'type': 'time-based',
    'models': {
        'CSVReader': {
            'public': True,
            'params': ['csv_file', 'time_column'],
            'attrs': ['S', 'T'],  # Solar irradiance and Temperature
        },
    },
}

class CSVReader(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid = None
        self.data = None
        self.csv_file = None
        self.step_size = 1  # Data provided every minute
        self.time_column = 'time'
        self.attrs = []
        self.current_time = 0  # Track current simulation time
        
    def init(self, sid, time_resolution=1.0, step_size=1):
        self.step_size = step_size
        return self.meta
    
    def create(self, num, model, csv_file, time_column='time'):
        """
        Create CSV reader instance.
        
        Args:
            csv_file: Path to CSV file
            time_column: Name of the time column (default: 'time')
        """
        if num != 1:
            raise ValueError('Can only create one CSVReader instance')
        
        self.csv_file = csv_file
        self.time_column = time_column
        
        # Load CSV file
        if not os.path.exists(csv_file):
            raise FileNotFoundError(f"CSV file not found: {csv_file}")
        
        self.data = pd.read_csv(csv_file)
        
        # Get attribute names (all columns except time)
        self.attrs = [col for col in self.data.columns if col != time_column]
        
        self.eid = 'CSVReader_0'
        
        print(f"CSV Reader initialized with file: {csv_file}")
        print(f"Available attributes: {self.attrs}")
        print(f"Data points: {len(self.data)}")
        
        # Return entity with attrs included
        entity = {
            'eid': self.eid, 
            'type': model,
            'children': []
        }
        
        return [entity]
    
    def step(self, time, inputs, max_advance):
        # Store current time for use in get_data
        self.current_time = time
        return time + self.step_size
    
    def get_data(self, outputs):
        """
        Provide data for the requested outputs at the current time.
        Uses linear interpolation for values between data points.
        """
        data = {}
        time = self.current_time
        
        for eid, attrs in outputs.items():
            if eid != self.eid:
                continue
            
            data[eid] = {}
            
            # Find the row corresponding to current time (or interpolate)
            if self.time_column in self.data.columns:
                # Time-indexed data - find closest match
                # Convert time based on data format (assume seconds if max > 1440)
                time_values = self.data[self.time_column].values
                
                # Check if time in CSV is in seconds (max > 1440) or minutes
                if time_values[-1] > 1440:
                    # Time in CSV is in seconds, convert mosaik time (minutes) to seconds
                    search_time = time * 60
                else:
                    # Time in CSV is in minutes
                    search_time = time
                
                # Find the index for interpolation
                if search_time <= time_values[0]:
                    idx = 0
                elif search_time >= time_values[-1]:
                    idx = len(time_values) - 1
                else:
                    # Linear interpolation
                    idx = None
                    for i in range(len(time_values) - 1):
                        if time_values[i] <= search_time < time_values[i + 1]:
                            # Interpolate between i and i+1
                            t0, t1 = time_values[i], time_values[i + 1]
                            weight = (search_time - t0) / (t1 - t0) if t1 != t0 else 0
                            
                            for attr in attrs:
                                if attr in self.attrs:
                                    v0 = self.data.iloc[i][attr]
                                    v1 = self.data.iloc[i + 1][attr]
                                    data[eid][attr] = v0 + weight * (v1 - v0)
                            break
                
                # If exact match or edge case, use direct value
                if idx is not None:
                    for attr in attrs:
                        if attr in self.attrs:
                            data[eid][attr] = self.data.iloc[idx][attr]
            else:
                # No time column - use row index as time
                idx = min(time, len(self.data) - 1)
                for attr in attrs:
                    if attr in self.attrs:
                        data[eid][attr] = self.data.iloc[idx][attr]
        
        return data
