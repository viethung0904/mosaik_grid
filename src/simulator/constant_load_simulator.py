"""
Constant power load simulator — no FMU needed.

Each entity outputs fixed P_load_mw and Q_load_mvar values every step.
These are connected to Substation FMUs as load demand inputs.
Mosaik automatically sums multiple sources feeding the same attr.
"""
import mosaik_api
from itertools import count

META = {
    "api_version": "3.0",
    "type": "time-based",
    'models': {
        'ConstantLoad': {
            'public': True,
            'params': ['p_mw', 'q_mvar'],
            'attrs': ['P_load_mw', 'Q_load_mvar'],
        },
    },
}


class ConstantLoad(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid_counters = {}
        self.entities = {}   # {eid: {'P_load_mw': float, 'Q_load_mvar': float}}
        self.step_size = 1

    def init(self, sid, time_resolution=1.0, step_size=1):
        self.step_size = step_size
        return self.meta

    def create(self, num, model, p_mw=0.0, q_mvar=0.0):
        counter = self.eid_counters.setdefault(model, count())
        entities = []
        for _ in range(num):
            eid = f'{model}_{next(counter)}'
            self.entities[eid] = {'P_load_mw': p_mw, 'Q_load_mvar': q_mvar}
            entities.append({'eid': eid, 'type': model})
        self.eid_counters[model] = counter
        return entities

    def step(self, time, inputs, max_advance):
        # Constant load — nothing to update
        return time + self.step_size

    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            data[eid] = {attr: self.entities[eid][attr] for attr in attrs}
        return data
