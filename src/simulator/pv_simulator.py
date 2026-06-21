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
        'PV': {
            'public': True,
            'params': ['s_init', 't_init', 'scale_factor'],  # scale_factor: multiply P output (e.g. 10 = model 10× arrays)
            'attrs': ['S', 'T', 'V', 'I', 'P', 'P_load_mw', 'Q_load_mvar'],
        },
    },
}

class PV(mosaik_api.Simulator):
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
        self.vr_inputs = None               # input variables
        self.vr_outputs = None              # output variables
        self.step_size = 1                  # int simulation step size (must be 1 for each simulator)
        self.start_time = 0                 # FMPy parameter
        self.stop_time = 0                  # FMPy parameter
        self.stop_time_defined = False      # FMPy parameter
        self.sec_per_mt = 60                 # Number of seconds of internal time per mosaik time
        self.fmutimes = {}                  # Keeping track of each FMU's internal time
        self._phys_step = 0                 # Physical time step counter (incremented once per 60s step)
        self.S = None
        self.T = None
        self.eid = None
        self._scale = {}  # per-entity scale factor

    def init(self, sid, time_resolution=1.0, fmu_filename=None, instance_name=None, step_size=None, 
            start_time = 0, stop_time = 0, seconds_per_mosaik_timestep = 60):
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

        self.vr_inputs = [self.vrs['S'], self.vrs['T']]
        self.vr_outputs = [self.vrs['V'], self.vrs['I'], self.vrs['P']]

        print(self.vrs)
        print("Initialization of FMU PV successful")

        return self.meta
    
    def create(self, num, model, s_init=1000.0, t_init=298.15, scale_factor=1.0):
        counter = self.eid_counters.setdefault(model, count())
        entities = []

        for i in range(num):
            eid = '%s_%s' % (model, next(counter))  # entity ID
            self.eid = eid

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
                self._entities[eid].setupExperiment(startTime=self.start_time)
                self._entities[eid].enterInitializationMode()
                self._entities[eid].setReal([self.vrs['S']], [s_init])
                self._entities[eid].setReal([self.vrs['T']], [t_init])
                self._entities[eid].exitInitializationMode()

                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                self._scale[eid] = scale_factor
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
                fmu.setFloat64([self.vrs['S']], [s_init])
                fmu.setFloat64([self.vrs['T']], [t_init])
                fmu.exitInitializationMode()

                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                self._scale[eid] = scale_factor
                entities.append({'eid': eid, 'type': model, 'rel': []})

        return entities
    
    def step(self, time, inputs, max_advance):
        target_time = (self._phys_step + 1) * 60.0   # 60 s per physical time step
        self._phys_step += 1
        
        for eid in self._entities.keys():
            # Get input values for this entity (if any)
            if eid in inputs:
                entity_inputs = inputs[eid]

                if self.model_description.fmiVersion == '2.0':
                    # Set S (solar irradiance) input if provided
                    if 'S' in entity_inputs:
                        s_values = entity_inputs['S']
                        if s_values:
                            s_value = list(s_values.values())[0]  # Get the value
                            self._entities[eid].setReal([self.vrs['S']], [s_value])
                    
                    # Set T (temperature) input if provided
                    if 'T' in entity_inputs:
                        t_values = entity_inputs['T']
                        if t_values:
                            t_value = list(t_values.values())[0]  # Get the value
                            self._entities[eid].setReal([self.vrs['T']], [t_value])

                elif self.model_description.fmiVersion == '3.0':
                    # Set S (solar irradiance) input if provided
                    if 'S' in entity_inputs:
                        s_values = entity_inputs['S']
                        if s_values:
                            s_value = list(s_values.values())[0]  # Get the value
                            self._entities[eid].setFloat64([self.vrs['S']], [s_value])
                    
                    # Set T (temperature) input if provided
                    if 'T' in entity_inputs:
                        t_values = entity_inputs['T']
                        if t_values:
                            t_value = list(t_values.values())[0]  # Get the value
                            self._entities[eid].setFloat64([self.vrs['T']], [t_value])
            
            communication_point = self.fmutimes[eid] 
            communication_step_size = target_time - communication_point

            status = self._entities[eid].doStep(currentCommunicationPoint = communication_point,
                    communicationStepSize = communication_step_size)
            
            self.fmutimes[eid] += communication_step_size

        return time + self.step_size  # Return next Mosaik time step
    
    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid in self._entities:
                data[eid] = {}
                for attr in attrs:
                    if attr == 'P_load_mw':
                        # Power injection into grid: negate, scale, convert W → MW
                        if self.model_description.fmiVersion == '2.0':
                            p_w = self._entities[eid].getReal([self.vrs['P']])[0]
                        else:
                            p_w = self._entities[eid].getFloat64([self.vrs['P']])[0]
                        data[eid][attr] = -p_w * self._scale.get(eid, 1.0) / 1e6
                    elif attr == 'Q_load_mvar':
                        # Unity power factor inverter — no reactive injection
                        data[eid][attr] = 0.0
                    elif attr == 'P_W':
                        # Raw FMU output scaled by scale_factor
                        if self.model_description.fmiVersion == '2.0':
                            p_w = self._entities[eid].getReal([self.vrs['P']])[0]
                        else:
                            p_w = self._entities[eid].getFloat64([self.vrs['P']])[0]
                        data[eid][attr] = p_w * self._scale.get(eid, 1.0)
                    elif attr in self.vrs:
                        if self.model_description.fmiVersion == '2.0':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs[attr]])[0]
                        else:
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs[attr]])[0]
        return data
