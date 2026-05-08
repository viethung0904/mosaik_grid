"""
A simple data collector that prints all data and saves to a csv file when the simulation finishes.

"""
import collections
import json
import os
import csv
import signal
import sys
import atexit
import time as _wall_time
import mosaik_api
import pandas as pd
import matplotlib.pyplot as plt

META = {
    'type': 'hybrid',
    'models': {
        'Monitor': {
            'public': True,
            'any_inputs': True,
            'params': [],
            'attrs': [],
        },
    },
}


class Collector(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid = None
        self.data = collections.defaultdict(lambda:
                                            collections.defaultdict(dict))
        self.step_size = 1
        self.output_dir = '.'
        self.total_steps = None
        self._last_save_wall = 0.0   # wall-clock time of last output.json write
        # Register cleanup handlers
        atexit.register(self.emergency_save)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        """Handle termination signals"""
        print(f"\n!!! Received signal {signum}, saving data before exit !!!")
        sys.stdout.flush()
        self.emergency_save()
        sys.exit(0)
    
    def emergency_save(self):
        """Emergency save if process is being terminated."""
        if getattr(self, '_already_saved', False):
            return
        if not self.data:
            return
        try:
            output_path = os.path.join(self.output_dir, 'output.json')
            new_dict = {}
            for sim, sim_data in self.data.items():
                for attr, values in sim_data.items():
                    new_dict[attr] = {}
                    for t, value in values.items():
                        new_dict[attr][str(t)] = value[0] if isinstance(value, list) and len(value) > 0 else value
            with open(output_path, 'w') as f:
                json.dump(new_dict, f, indent=2)
            print("Emergency save: output.json written")
            sys.stdout.flush()
        except Exception as e:
            print(f"Emergency save failed: {e}")
            sys.stdout.flush()

    def init(self, sid, time_resolution, output_dir='.', total_steps=None):
        self.output_dir  = output_dir
        self.total_steps = total_steps
        # Read start_time written by scenario.py so we can preserve it
        self._start_time = None
        try:
            _sp = os.path.join(output_dir, 'sim_status.json')
            with open(_sp) as _f:
                self._start_time = json.load(_f).get('start_time')
        except Exception:
            pass
        return self.meta

    def create(self, num, model):
        if num > 1 or self.eid is not None:
            raise RuntimeError('Can only create one instance of Monitor.')

        self.eid = 'Monitor'
        return [{'eid': self.eid, 'type': model}]

    def step(self, time, inputs, max_advance):
        data = inputs.get(self.eid, {})
        for attr, values in data.items():
            for src, value in values.items():
                self.data[src][attr][time] = value

        # Save data at most 5×/second so the live dashboard can poll
        self._save_data_now(current_step=time)

        return time + self.step_size
    
    def _save_data_now(self, current_step=None):
        """Flatten and atomically save output.json; always save sim_status.json."""
        output_path = os.path.join(self.output_dir, 'output.json')
        status_path = os.path.join(self.output_dir, 'sim_status.json')

        # Rate-limit: write output data at most 5×/second (status always written).
        # For small sims (≤500 steps) write every step so the live dashboard sees updates.
        now = _wall_time.time()
        min_interval = 0.0 if (self.total_steps and self.total_steps <= 500) else 0.2
        should_write_data = (now - self._last_save_wall) >= min_interval

        if should_write_data:
            try:
                flat = {}
                for sim_data in self.data.values():
                    for attr, values in sim_data.items():
                        flat[attr] = {
                            str(t): (v[0] if isinstance(v, list) and v else v)
                            for t, v in values.items()
                        }
                # Atomic write: write to .tmp then rename so HTTP server never
                # reads a partially-written file
                tmp = output_path + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(flat, f)
                os.replace(tmp, output_path)
                self._last_save_wall = now
            except Exception as e:
                print(f'[Collector] output.json save failed: {e}')

        if current_step is not None:
            try:
                with open(status_path, 'w') as f:
                    json.dump({'running': True, 'step': current_step,
                               'total': self.total_steps,
                               'start_time': self._start_time}, f)
            except Exception as e:
                print(f'[Collector] sim_status.json save failed: {e}')

    def finalize(self):
        # Force a final data write ignoring the rate-limit
        self._last_save_wall = 0.0
        self._save_data_now()   # current_step=None → no status update here

        total = self.total_steps or '?'
        print(f'\n=== Collector: simulation complete — {total} steps ===')
        for sim, sim_data in sorted(self.data.items()):
            for attr, values in sorted(sim_data.items()):
                print(f'  {attr}: {len(values)} steps')
        sys.stdout.flush()
        self._already_saved = True

        # Mark simulation as complete in the live dashboard status file
        status_path = os.path.join(self.output_dir, 'sim_status.json')
        try:
            with open(status_path, 'w') as f:
                json.dump({'running': False,
                           'step':    self.total_steps,
                           'total':   self.total_steps,
                           'start_time': self._start_time}, f)
        except Exception as e:
            print(f'[Collector] Could not write final sim_status.json: {e}')

        _wall_time.sleep(0.3)   # let the HTTP server serve the final files


if __name__ == '__main__':
    mosaik_api.start_simulation(Collector())
