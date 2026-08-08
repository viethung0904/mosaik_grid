import mosaik_api
import math
import collections
from itertools import count
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave
from fmpy.fmi3 import FMU3Slave

META = {
    "api_version": "3.0",
    "type": "hybrid",
    'models': {
        'TransformerBranch': {
            'public': True,
            'params': ['r_hv_ohm', 'x_hv_ohm', 'rated_u1_kv', 'rated_u2_kv'],
            'attrs': [
                'V_hv_mag_kv', 'V_hv_ang_deg',
                'V_lv_mag_kv', 'V_lv_ang_deg',
                'I_hv_in_re', 'I_hv_in_im',
                'I_lv_in_re', 'I_lv_in_im',
                'I_hv_mag_kA',
                'P_loss_mw', 'Q_loss_mvar',
            ],
        },
    },
}

_INPUT_ATTRS  = ['V_hv_mag_kv', 'V_hv_ang_deg', 'V_lv_mag_kv', 'V_lv_ang_deg']
_OUTPUT_ATTRS = ['I_hv_in_re', 'I_hv_in_im', 'I_lv_in_re', 'I_lv_in_im',
                 'I_hv_mag_kA', 'P_loss_mw', 'Q_loss_mvar']


class TransformerBranch(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid_counters  = {}
        self.data          = collections.defaultdict(dict)
        self._entities     = {}
        self.unzipdir      = None
        self.model_description = None
        self.model_name    = None
        self.instance_name = None
        self.vrs           = {}
        self.step_size     = 1
        self.start_time    = 0
        self.sec_per_mt    = 60
        self.fmutimes      = {}

    # ── init ─────────────────────────────────────────────────────────────────
    def init(self, sid, time_resolution=1.0, fmu_filename=None,
             instance_name=None, step_size=None,
             start_time=0, stop_time=0):
        self.start_time    = start_time
        self.step_size     = step_size
        self.model_name    = fmu_filename
        self.instance_name = instance_name

        self.model_description = read_model_description(fmu_filename)
        for var in self.model_description.modelVariables:
            self.vrs[var.name] = var.valueReference

        print(f'Initialization of FMU TransformerBranch successful (FMI {self.model_description.fmiVersion})')
        return self.meta

    # ── create ────────────────────────────────────────────────────────────────
    def create(self, num, model,
               r_hv_ohm=0.0, x_hv_ohm=1.0,
               rated_u1_kv=69.0, rated_u2_kv=13.8):
        counter  = self.eid_counters.setdefault(model, count())
        entities = []

        for _ in range(num):
            eid = f'{model}_{next(counter)}'
            unique_name = f'{self.instance_name}_{eid}'
            self.unzipdir = extract(self.model_name)

            if self.model_description.fmiVersion == '2.0':
                fmu = FMU2Slave(
                    guid=self.model_description.guid,
                    unzipDirectory=self.unzipdir,
                    modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                    instanceName=unique_name,
                )
                fmu.instantiate()
                fmu.setupExperiment(startTime=self.start_time)
                fmu.enterInitializationMode()
                fmu.setReal([self.vrs['r_hv_ohm']],    [r_hv_ohm])
                fmu.setReal([self.vrs['x_hv_ohm']],    [x_hv_ohm])
                fmu.setReal([self.vrs['rated_u1_kv']], [rated_u1_kv])
                fmu.setReal([self.vrs['rated_u2_kv']], [rated_u2_kv])
                fmu.exitInitializationMode()

            elif self.model_description.fmiVersion == '3.0':
                fmu = FMU3Slave(
                    guid=self.model_description.guid,
                    unzipDirectory=self.unzipdir,
                    modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                    instanceName=unique_name,
                )
                fmu.instantiate()
                fmu.enterInitializationMode(startTime=self.start_time)
                fmu.setFloat64([self.vrs['r_hv_ohm']],    [r_hv_ohm])
                fmu.setFloat64([self.vrs['x_hv_ohm']],    [x_hv_ohm])
                fmu.setFloat64([self.vrs['rated_u1_kv']], [rated_u1_kv])
                fmu.setFloat64([self.vrs['rated_u2_kv']], [rated_u2_kv])
                fmu.exitInitializationMode()

            self._entities[eid] = fmu
            self.fmutimes[eid]  = self.start_time * self.sec_per_mt
            entities.append({'eid': eid, 'type': model, 'rel': []})

        return entities

    # ── step ─────────────────────────────────────────────────────────────────
    def step(self, time, inputs, max_advance):
        target_time = (time + 1 + self.start_time) * self.sec_per_mt

        for eid, fmu in self._entities.items():
            entity_inputs = inputs.get(eid, {})
            if self.model_description.fmiVersion == '2.0':
                for attr in _INPUT_ATTRS:
                    if attr in entity_inputs and entity_inputs[attr]:
                        fmu.setReal([self.vrs[attr]], [sum(entity_inputs[attr].values())])
            elif self.model_description.fmiVersion == '3.0':
                for attr in _INPUT_ATTRS:
                    if attr in entity_inputs and entity_inputs[attr]:
                        fmu.setFloat64([self.vrs[attr]], [sum(entity_inputs[attr].values())])

            cp   = self.fmutimes[eid]
            step = target_time - cp
            fmu.doStep(currentCommunicationPoint=cp, communicationStepSize=step)
            self.fmutimes[eid] += step

        return time + 1

    # ── get_data ──────────────────────────────────────────────────────────────
    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid not in self._entities:
                continue
            fmu = self._entities[eid]
            data[eid] = {}
            is_fmi3 = self.model_description.fmiVersion == '3.0'
            get = fmu.getFloat64 if is_fmi3 else fmu.getReal
            for attr in attrs:
                if attr == 'I_hv_mag_kA':
                    re = get([self.vrs['I_hv_in_re']])[0]
                    im = get([self.vrs['I_hv_in_im']])[0]
                    data[eid][attr] = math.hypot(re, im)
                elif attr in _OUTPUT_ATTRS:
                    data[eid][attr] = get([self.vrs[attr]])[0]
                elif attr in _INPUT_ATTRS:
                    data[eid][attr] = get([self.vrs[attr]])[0]
        return data

    def finalize(self):
        for fmu in self._entities.values():
            try:
                fmu.terminate()
                fmu.freeInstance()
            except Exception:
                pass
