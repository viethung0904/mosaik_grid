import math
import mosaik_api
from itertools import count
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave
from fmpy.fmi3 import FMU3Slave
import collections

META = {
    "api_version": "3.0",
    "type": "hybrid",
    'models': {
        'Substation': {
            'public': True,
            'params': ['Y_self_re', 'Y_self_im', 'B_shunt', 'omega_relax', 'is_slack', 'V_slack_kv', 'V_slack_ang_deg', 'is_sync_machine', 'V_reg_kv'],
            'attrs': ['I_in_re', 'I_in_im', 'P_load_mw', 'Q_load_mvar', 'V_mag_kv', 'V_ang_deg'],
        },
    },
}


class Substation(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid_counters = {}
        self.data = collections.defaultdict(dict)
        self._entities = {}
        self.unzipdir = None                # directory of FMU
        self.model_description = None       # model description of FMU
        self.model_name = None              # model name of FMU
        self.instance_name = None           # instance name of FMU
        self.vrs = {}                       # dict for FMU value references
        self.step_size = 1                  # int simulation step size
        self.start_time = 0                 # FMPy parameter
        self.stop_time = 0                  # FMPy parameter
        self.sec_per_mt = 60                 # Number of seconds of internal time per mosaik time
        self.fmutimes = {}                  # Keeping track of each FMU's internal time

    def init(self, sid, time_resolution=1.0, fmu_filename=None, instance_name=None, step_size=None,
             start_time=0, stop_time=0, seconds_per_mosaik_timestep=1):
        self.start_time = start_time
        self.stop_time = stop_time
        self.step_size = step_size
        self.model_name = fmu_filename
        self.instance_name = instance_name

        self.model_description = read_model_description(fmu_filename)
        assert self.model_description is not None

        for variable in self.model_description.modelVariables:
            self.vrs[variable.name] = variable.valueReference
        assert self.vrs is not None

        print(self.vrs)
        print("Initialization of FMU Substation successful")

        return self.meta

    def create(self, num, model, Y_self_re=0.0, Y_self_im=0.0, B_shunt=0.0,
               omega_relax=0.5, is_slack=False, V_slack_kv=20.0, V_slack_ang_deg=0.0,
               is_sync_machine=0.0, V_reg_kv=0.0):
        counter = self.eid_counters.setdefault(model, count())
        entities = []

        for i in range(num):
            eid = '%s_%s' % (model, next(counter))  # entity ID

            # extract the FMU
            self.unzipdir = extract(self.model_name)

            # Use unique instance name for each entity
            unique_instance_name = f"{self.instance_name}_{eid}"

            if self.model_description.fmiVersion == '2.0':
                fmu = FMU2Slave(guid=self.model_description.guid,
                                unzipDirectory=self.unzipdir,
                                modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                                instanceName=unique_instance_name)

                self._entities[eid] = fmu

                fmu.instantiate()
                fmu.setupExperiment(startTime=self.start_time)
                fmu.enterInitializationMode()

                # Set parameters during initialization
                fmu.setReal([self.vrs['Y_self_re']], [Y_self_re])
                fmu.setReal([self.vrs['Y_self_im']], [Y_self_im])
                fmu.setReal([self.vrs['B_shunt']], [B_shunt])
                fmu.setReal([self.vrs['omega_relax']], [omega_relax])
                fmu.setReal([self.vrs['is_slack']], [float(is_slack)])
                fmu.setReal([self.vrs['V_slack_kv']], [V_slack_kv])
                fmu.setReal([self.vrs['V_slack_ang_deg']], [V_slack_ang_deg])
                fmu.setReal([self.vrs['is_sync_machine']], [float(is_sync_machine)])
                fmu.setReal([self.vrs['V_reg_kv']], [float(V_reg_kv)])

                fmu.exitInitializationMode()

                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                entities.append({'eid': eid, 'type': model, 'rel': []})

            elif self.model_description.fmiVersion == '3.0':
                fmu = FMU3Slave(guid=self.model_description.guid,
                                unzipDirectory=self.unzipdir,
                                modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                                instanceName=unique_instance_name)

                self._entities[eid] = fmu

                fmu.instantiate()
                fmu.enterInitializationMode(startTime=self.start_time)

                # Set parameters during initialization
                fmu.setFloat64([self.vrs['Y_self_re']], [Y_self_re])
                fmu.setFloat64([self.vrs['Y_self_im']], [Y_self_im])
                fmu.setFloat64([self.vrs['B_shunt']], [B_shunt])
                fmu.setFloat64([self.vrs['omega_relax']], [omega_relax])
                fmu.setFloat64([self.vrs['is_slack']], [float(is_slack)])
                fmu.setFloat64([self.vrs['V_slack_kv']], [V_slack_kv])
                fmu.setFloat64([self.vrs['V_slack_ang_deg']], [V_slack_ang_deg])
                fmu.setFloat64([self.vrs['is_sync_machine']], [float(is_sync_machine)])
                fmu.setFloat64([self.vrs['V_reg_kv']], [float(V_reg_kv)])

                fmu.exitInitializationMode()

                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                entities.append({'eid': eid, 'type': model, 'rel': []})

        return entities

    def step(self, time, inputs, max_advance):
        target_time = (time + 1 + self.start_time) * self.sec_per_mt

        for eid in self._entities.keys():
            if eid in inputs:
                entity_inputs = inputs[eid]

                if self.model_description.fmiVersion == '2.0':
                    # Convert rectangular I_in to polar for the FMU
                    i_re = sum(entity_inputs['I_in_re'].values()) if 'I_in_re' in entity_inputs and entity_inputs['I_in_re'] else 0.0
                    i_im = sum(entity_inputs['I_in_im'].values()) if 'I_in_im' in entity_inputs and entity_inputs['I_in_im'] else 0.0
                    i_mag = math.hypot(i_re, i_im)
                    i_ang = math.degrees(math.atan2(i_im, i_re))
                    self._entities[eid].setReal([self.vrs['I_in_mag']], [i_mag])
                    self._entities[eid].setReal([self.vrs['I_in_ang']], [i_ang])

                    # Set P_load_mw input
                    if 'P_load_mw' in entity_inputs:
                        values = entity_inputs['P_load_mw']
                        if values:
                            self._entities[eid].setReal([self.vrs['P_load_mw']], [sum(values.values())])

                    # Set Q_load_mvar input
                    if 'Q_load_mvar' in entity_inputs:
                        values = entity_inputs['Q_load_mvar']
                        if values:
                            self._entities[eid].setReal([self.vrs['Q_load_mvar']], [sum(values.values())])

                elif self.model_description.fmiVersion == '3.0':
                    # Convert rectangular I_in to polar for the FMU
                    i_re = sum(entity_inputs['I_in_re'].values()) if 'I_in_re' in entity_inputs and entity_inputs['I_in_re'] else 0.0
                    i_im = sum(entity_inputs['I_in_im'].values()) if 'I_in_im' in entity_inputs and entity_inputs['I_in_im'] else 0.0
                    i_mag = math.hypot(i_re, i_im)
                    i_ang = math.degrees(math.atan2(i_im, i_re))
                    self._entities[eid].setFloat64([self.vrs['I_in_mag']], [i_mag])
                    self._entities[eid].setFloat64([self.vrs['I_in_ang']], [i_ang])

                    # Set P_load_mw input
                    if 'P_load_mw' in entity_inputs:
                        values = entity_inputs['P_load_mw']
                        if values:
                            self._entities[eid].setFloat64([self.vrs['P_load_mw']], [sum(values.values())])

                    # Set Q_load_mvar input
                    if 'Q_load_mvar' in entity_inputs:
                        values = entity_inputs['Q_load_mvar']
                        if values:
                            self._entities[eid].setFloat64([self.vrs['Q_load_mvar']], [sum(values.values())])

            communication_point = self.fmutimes[eid]
            communication_step_size = target_time - communication_point

            self._entities[eid].doStep(currentCommunicationPoint=communication_point,
                                       communicationStepSize=communication_step_size)

            self.fmutimes[eid] += communication_step_size

        return time + 1  # Return next Mosaik time step

    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid in self._entities:
                data[eid] = {}
                for attr in attrs:
                    if attr not in self.vrs:
                        continue
                    if self.model_description.fmiVersion == '2.0':
                        data[eid][attr] = self._entities[eid].getReal([self.vrs[attr]])[0]
                    elif self.model_description.fmiVersion == '3.0':
                        data[eid][attr] = self._entities[eid].getFloat64([self.vrs[attr]])[0]
        return data
