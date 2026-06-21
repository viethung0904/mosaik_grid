import mosaik_api
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave
from fmpy.fmi3 import FMU3Slave
import collections
from itertools import count

META = {
    "api_version": "3.0",
    "type": "hybrid",
    'models': {
        'Battery': {
            'public': True,
            'params': ['p_charge_mw'],          # charging power setpoint [MW] (positive = charge)
            'attrs': ['P_load_mw', 'SOC', 'V_volt', 'I_amp'],
            # P_load_mw : grid injection convention (positive=charging, negative=discharging)
            # V_volt    : terminal voltage [V]
            # I_amp     : terminal current [A]
        },
    },
}


class Battery(mosaik_api.Simulator):
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
        self.step_size = 1                  # int simulation step size (must be 1 for each simulator)
        self.start_time = 0                 # FMPy parameter
        self.stop_time = 0                  # FMPy parameter
        self.sec_per_mt = 60                # Number of seconds of internal time per mosaik time
        self.fmutimes = {}                  # Keeping track of each FMU's internal time
        self._phys_step = 0                 # Physical time step counter (incremented once per 60s step)

    def init(self, sid, time_resolution=1.0, fmu_filename=None, instance_name=None,
             step_size=None, start_time=0, stop_time=0, seconds_per_mosaik_timestep=1):
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
        print("Initialization of FMU Battery successful")

        return self.meta

    def create(self, num, model, p_charge_mw=0.0):
        counter = self.eid_counters.setdefault(model, count())
        entities = []

        for i in range(num):
            eid = '%s_%s' % (model, next(counter))  # entity ID

            # extract the FMU
            self.unzipdir = extract(self.model_name)

            # Use unique instance name for each entity
            unique_instance_name = f"{self.instance_name}_{eid}"

            if self.model_description.fmiVersion == '2.0':
                fmu = FMU2Slave(
                    guid=self.model_description.guid,
                    unzipDirectory=self.unzipdir,
                    modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                    instanceName=unique_instance_name)

                self._entities[eid] = fmu

                fmu.instantiate()
                fmu.setupExperiment(startTime=self.start_time)
                fmu.enterInitializationMode()
                # Set initial charging power [W] and zero load
                fmu.setReal([self.vrs['P_charge']], [p_charge_mw * 1e6])
                fmu.setReal([self.vrs['P_load']],   [0.0])
                fmu.exitInitializationMode()

                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                self.data[eid] = {'P_load_mw': 0.0, 'SOC': 100.0, 'V_volt': 0.0, 'I_amp': 0.0}
                entities.append({'eid': eid, 'type': model, 'rel': []})

            elif self.model_description.fmiVersion == '3.0':
                fmu = FMU3Slave(
                    guid=self.model_description.guid,
                    unzipDirectory=self.unzipdir,
                    modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                    instanceName=unique_instance_name)

                self._entities[eid] = fmu

                fmu.instantiate()
                fmu.enterInitializationMode(startTime=self.start_time)
                # Set initial charging power [W] and zero load
                fmu.setFloat64([self.vrs['P_charge']], [p_charge_mw * 1e6])
                fmu.setFloat64([self.vrs['P_load']],   [0.0])
                fmu.exitInitializationMode()

                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                self.data[eid] = {'P_load_mw': 0.0, 'SOC': 100.0, 'V_volt': 0.0, 'I_amp': 0.0}
                entities.append({'eid': eid, 'type': model, 'rel': []})

        return entities

    def step(self, time, inputs, max_advance):
        target_time = (self._phys_step + 1) * 60.0   # 60 s per physical time step
        self._phys_step += 1

        for eid in self._entities.keys():
            communication_point = self.fmutimes[eid]
            communication_step_size = target_time - communication_point

            self._entities[eid].doStep(
                currentCommunicationPoint=communication_point,
                communicationStepSize=communication_step_size)

            self.fmutimes[eid] += communication_step_size

        return time + self.step_size  # Return next Mosaik time step

    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid not in self._entities:
                continue
            data[eid] = {}
            for attr in attrs:
                if attr == 'P_load_mw':
                    # Read raw power [W] from FMU, negate for grid injection convention:
                    #   discharge (positive FMU P) → negative P_load_mw (supply to grid)
                    #   charge    (negative FMU P) → positive P_load_mw (draw from grid)
                    if self.model_description.fmiVersion == '2.0':
                        p_w = self._entities[eid].getReal([self.vrs['P']])[0]
                    else:
                        p_w = self._entities[eid].getFloat64([self.vrs['P']])[0]
                    data[eid]['P_load_mw'] = -p_w / 1e6

                elif attr == 'SOC':
                    if self.model_description.fmiVersion == '2.0':
                        data[eid]['SOC'] = self._entities[eid].getReal([self.vrs['SOC']])[0]
                    else:
                        data[eid]['SOC'] = self._entities[eid].getFloat64([self.vrs['SOC']])[0]

                elif attr == 'V_volt':
                    if self.model_description.fmiVersion == '2.0':
                        data[eid]['V_volt'] = self._entities[eid].getReal([self.vrs['V']])[0]
                    else:
                        data[eid]['V_volt'] = self._entities[eid].getFloat64([self.vrs['V']])[0]

                elif attr == 'I_amp':
                    if self.model_description.fmiVersion == '2.0':
                        data[eid]['I_amp'] = self._entities[eid].getReal([self.vrs['I']])[0]
                    else:
                        data[eid]['I_amp'] = self._entities[eid].getFloat64([self.vrs['I']])[0]

        return data
